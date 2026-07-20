# Request Performance Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the shared authentication, realtime, upload-progress, and network-discovery bottlenecks that slow every BLREC page, while adding request-level evidence for later endpoint-specific work.

**Architecture:** Keep the existing FastAPI, Angular, SQLite, and single browser SSE connection. Add low-overhead request metrics, rate-limit authentication activity writes, make realtime sampling topic- and subscriber-aware, replace the full upload-job realtime projection with an active-job projection, and cache host interface discovery outside the event loop. This is the first of four plans derived from `docs/superpowers/specs/2026-07-20-end-to-end-request-performance-design.md`.

**Tech Stack:** Python 3.9, FastAPI/Starlette ASGI, SQLite, asyncio, Angular 15, RxJS, Jasmine/Karma, pytest.

## Global Constraints

- Do not use git worktrees; all work stays in the current repository.
- Do not increase Bilibili polling, upload, comment, or danmaku request frequency.
- Preserve one SSE connection per browser tab and preserve queue-overflow/reconnect resync behavior.
- Never log query values, cookies, tokens, credentials, form bodies, or local media paths.
- Do not remove CSRF, session revocation, password-reset revocation, rate limiting, or remote-outcome safety.
- Write a failing regression test before each behavior change.
- Each task must be independently reviewable and committed before the next dependent task begins.

---

### Task 1: Request performance context and ASGI middleware

**Files:**
- Create: `src/blrec/web/request_metrics.py`
- Create: `src/blrec/web/middlewares/request_performance.py`
- Modify: `src/blrec/bili_upload/database.py`
- Modify: `src/blrec/web/main.py`
- Test: `tests/web/test_request_performance_middleware.py`
- Test: `tests/bili_upload/test_database.py`

**Interfaces:**
- Produces: `request_metrics_scope() -> ContextManager[RequestMetrics]`.
- Produces: `record_database_call(elapsed_seconds: float) -> None`.
- Produces: `RequestPerformanceMiddleware(app, slow_request_seconds=0.25)`.
- Consumes: existing `audit()` redaction and `BiliUploadDatabase._run()` boundary.

- [x] **Step 1: Write failing context and middleware tests**

```python
def test_request_metrics_accumulates_database_calls() -> None:
    with request_metrics_scope() as metrics:
        record_database_call(0.012)
        record_database_call(0.003)
    assert metrics.database_calls == 2
    assert metrics.database_ms == pytest.approx(15.0)

@pytest.mark.asyncio
async def test_middleware_audits_normalized_route_without_secrets(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(
        'blrec.web.middlewares.request_performance.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    app = FastAPI()
    app.add_middleware(RequestPerformanceMiddleware, slow_request_seconds=0)

    @app.get('/items/{item_id}')
    async def item(item_id: int) -> dict:
        record_database_call(0.004)
        return {'id': item_id}

    response = TestClient(app).get('/items/7?token=must-not-appear')
    assert response.status_code == 200
    assert events[-1][1]['route'] == '/items/{item_id}'
    assert events[-1][1]['database_calls'] == 1
    assert 'token' not in str(events[-1])
```

- [x] **Step 2: Run the focused tests and verify the missing modules fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/web/test_request_performance_middleware.py -q`

Expected: collection failure because `request_metrics` and `RequestPerformanceMiddleware` do not exist.

- [x] **Step 3: Implement the request-local accumulator**

```python
@dataclass
class RequestMetrics:
    database_calls: int = 0
    database_ms: float = 0.0

_current: ContextVar[Optional[RequestMetrics]] = ContextVar(
    'blrec_request_metrics', default=None
)

@contextmanager
def request_metrics_scope() -> Iterator[RequestMetrics]:
    metrics = RequestMetrics()
    token = _current.set(metrics)
    try:
        yield metrics
    finally:
        _current.reset(token)

def record_database_call(elapsed_seconds: float) -> None:
    metrics = _current.get()
    if metrics is None:
        return
    metrics.database_calls += 1
    metrics.database_ms += max(0.0, elapsed_seconds * 1000.0)
```

- [x] **Step 4: Implement a pure ASGI middleware**

The middleware must wrap `send`, collect the status, content type and body byte count, and log only after the final body frame. Skip long-duration completion logging for `text/event-stream` and media responses; those receive separate metrics in a later plan. Resolve the route with `scope.get('route').path` after downstream routing and use the fixed `<unmatched>` label when no route exists; never log the raw request path.

```python
with request_metrics_scope() as metrics:
    started = time.perf_counter()
    await self.app(scope, receive, measured_send)
elapsed_ms = (time.perf_counter() - started) * 1000.0
audit(
    'http_request_performance',
    level='WARNING' if elapsed_ms >= self._slow_ms else 'DEBUG',
    method=scope.get('method', ''),
    route=normalized_route,
    status=status_code,
    elapsed_ms=round(elapsed_ms, 3),
    response_bytes=response_bytes,
    database_calls=metrics.database_calls,
    database_ms=round(metrics.database_ms, 3),
)
```

- [x] **Step 5: Instrument the database executor wait boundary**

```python
async def _run(self, operation: Callable[..., _T], *args: Any) -> _T:
    started = time.perf_counter()
    try:
        return await loop.run_in_executor(self._executor, partial(operation, *args))
    finally:
        record_database_call(time.perf_counter() - started)
```

This measures queue wait plus execution, which is the latency observed by the API request.

- [x] **Step 6: Register the middleware and run tests**

Register `RequestPerformanceMiddleware` in `src/blrec/web/main.py` inside the security and compression wrappers so it observes routed API work without logging request values.

Run: `PYTHONPATH=src .venv/bin/pytest tests/web/test_request_performance_middleware.py tests/bili_upload/test_database.py -q`

Expected: all tests pass.

- [x] **Step 7: Commit**

```bash
git add src/blrec/web/request_metrics.py src/blrec/web/middlewares/request_performance.py src/blrec/bili_upload/database.py src/blrec/web/main.py tests/web/test_request_performance_middleware.py tests/bili_upload/test_database.py
git commit -m "perf: add request performance accounting"
```

### Task 2: Rate-limit authentication activity writes

**Files:**
- Modify: `src/blrec/web/auth_store.py`
- Test: `tests/web/test_auth_store.py`
- Test: `tests/web/test_browser_extension_routes.py`

**Interfaces:**
- Produces: the existing `AdminAuthStore` constructor with a new keyword-only `activity_write_interval_seconds: int = 60` argument.
- Preserves: `authenticate_session(token) -> Optional[SessionCredentials]` and `authenticate_extension(token) -> Optional[ExtensionIdentity]`.

- [x] **Step 1: Write tests that count persisted activity writes**

```python
def test_session_activity_is_persisted_at_most_once_per_interval(tmp_path) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    credentials = auth.initialize('owner', 'correct horse battery staple')
    before = auth._connection.total_changes
    assert auth.authenticate_session(credentials.session_token) is not None
    first = auth._connection.total_changes
    assert auth.authenticate_session(credentials.session_token) is not None
    assert auth._connection.total_changes == first
    clock.value += 60
    assert auth.authenticate_session(credentials.session_token) is not None
    assert auth._connection.total_changes == first + 1
    assert first >= before
```

Add the equivalent test for extension tokens and assert that repeated use inside 60 seconds creates neither an UPDATE nor an `extension_token_used` audit row.

- [x] **Step 2: Run tests and verify repeated calls currently write**

Run: `PYTHONPATH=src .venv/bin/pytest tests/web/test_auth_store.py -q`

Expected: the new write-count tests fail.

- [x] **Step 3: Add the validated interval setting**

```python
if activity_write_interval_seconds <= 0:
    raise ValueError('activity write interval must be positive')
self._activity_write_interval_seconds = activity_write_interval_seconds
```

- [x] **Step 4: Make session authentication read-only inside the interval**

Select `last_seen_at` together with `expires_at`. Update when either the session is inside the refresh window or `now - last_seen_at >= interval`. Expiry, CSRF hash verification, logout and password-reset revocation remain unchanged.

```python
should_refresh_expiry = expires_at - now <= self._session_refresh_window_seconds
should_touch_activity = now - last_seen_at >= self._activity_write_interval_seconds
if should_refresh_expiry or should_touch_activity:
    # execute the fenced UPDATE and verify rowcount
```

- [x] **Step 5: Apply the same rule to extension token usage**

Inside the interval, return the persisted `last_used_at`; after the interval, update it and add one audit row. Revoked tokens still fail immediately on every request.

- [x] **Step 6: Run authentication and route tests**

Run: `PYTHONPATH=src .venv/bin/pytest tests/web/test_auth_store.py tests/web/test_auth_routes.py tests/web/test_browser_extension_routes.py -q`

Expected: all tests pass, including expiry and revocation tests.

- [x] **Step 7: Commit**

```bash
git add src/blrec/web/auth_store.py tests/web/test_auth_store.py tests/web/test_browser_extension_routes.py
git commit -m "perf: throttle authentication activity writes"
```

### Task 3: Make the shared realtime channel topic-aware

**Files:**
- Modify: `src/blrec/web/realtime.py`
- Modify: `src/blrec/web/routers/realtime.py`
- Test: `tests/web/test_realtime_routes.py`

**Interfaces:**
- Produces: `RealtimeBroker.subscribe(topics: Optional[Collection[str]] = None)`.
- Produces: `RealtimeBroker.has_subscribers(event_type: str) -> bool`.
- Preserves: queue overflow replaces pending events with `resync`.

- [x] **Step 1: Write failing subscriber-interest tests**

```python
@pytest.mark.asyncio
async def test_sampler_does_not_compute_unsubscribed_topics() -> None:
    uploads = AsyncMock(return_value=[])
    highlights = AsyncMock(return_value=[])
    broker = RealtimeBroker()
    broker.subscribe({'tasks'})
    sampler = RealtimeSampler(
        broker,
        task_provider=lambda: [],
        network_provider=lambda: {},
        upload_provider=uploads,
        highlight_provider=highlights,
    )
    await sampler.sample_once()
    uploads.assert_not_awaited()
    highlights.assert_not_awaited()

@pytest.mark.asyncio
async def test_topic_subscriber_receives_only_requested_events() -> None:
    subscription = broker.subscribe({'network'})
    await broker.publish('tasks', {})
    await broker.publish('network', {'interfaces': []})
    assert (await subscription.get()).type == 'network'
```

- [x] **Step 2: Run tests and verify current broker broadcasts everything**

Run: `PYTHONPATH=src .venv/bin/pytest tests/web/test_realtime_routes.py -q`

Expected: the new topic tests fail.

- [x] **Step 3: Store immutable topic sets per subscription**

Allow only `tasks`, `network`, `upload_progress`, and `highlight_progress`; `None` means all topics for compatibility. `resync` and heartbeat delivery are control events and are never filtered out.

- [x] **Step 4: Skip provider work without interested subscribers**

```python
if self._broker.has_subscribers('tasks'):
    await self._publish_changed('tasks', {'tasks': self._task_provider()})
if self._broker.has_subscribers('upload_progress'):
    uploads = await self._upload_provider()
    await self._publish_changed('upload_progress', {'jobs': uploads})
```

- [x] **Step 5: Parse and validate the SSE `topics` query**

`GET /api/v1/realtime?topics=tasks,network` subscribes only to those topics. Missing `topics` retains all-topic compatibility. Unknown or empty explicit topics return HTTP 422 before opening the stream.

- [x] **Step 6: Run tests**

Run: `PYTHONPATH=src .venv/bin/pytest tests/web/test_realtime_routes.py -q`

Expected: all realtime tests pass.

- [x] **Step 7: Commit**

```bash
git add src/blrec/web/realtime.py src/blrec/web/routers/realtime.py tests/web/test_realtime_routes.py
git commit -m "perf: sample only subscribed realtime topics"
```

### Task 4: Replace full upload and highlight realtime scans

**Files:**
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `src/blrec/bili_upload/highlight_worker.py`
- Test: `tests/bili_upload/test_journal.py`
- Test: `tests/bili_upload/test_highlight_worker.py`

**Interfaces:**
- Preserves: `RecordingJournalBridge.realtime_upload_progress() -> List[Dict[str, object]]`.
- Changes selection: active jobs, jobs with active post-submit branches, and jobs updated in the last 300 seconds only.
- Changes highlight selection: queued/processing or recently updated clips only.

- [x] **Step 1: Write a regression fixture matching the NAS state**

Seed 43 old `approved` jobs with terminal comment, danmaku, collection and repair states plus at least 1,000 confirmed chunks. Add one current uploading job. Assert realtime progress returns only the uploading job and executes at most two `fetchall` calls.

```python
progress = await journal.realtime_upload_progress()
assert [item['jobId'] for item in progress] == [active_job_id]
assert database.fetchall_calls <= 2
```

- [x] **Step 2: Run the focused tests and verify historical approved jobs appear**

Run: `PYTHONPATH=src .venv/bin/pytest tests/bili_upload/test_journal.py -k realtime -q`

Expected: the historical-job exclusion/query-budget test fails.

- [x] **Step 3: Query the lightweight active job projection**

Use one job query with this semantic predicate:

```sql
job.state IN ('waiting_artifacts','ready','uploading','submitting','waiting_review')
OR job.repair_state IN ('queued','checking','reuploading','editing','waiting_review')
OR job.comment_branch_state IN ('pending','running')
OR job.danmaku_branch_state IN ('pending','importing','publishing')
OR job.collection_branch_state IN ('pending','running')
OR job.updated_at >= ?
```

Use one aggregate query for only the selected job IDs to calculate confirmed/total bytes, current part, confirmed part count and discovered part count. Do not read danmaku items, unknown outcomes, account records, policy JSON or submission verification for SSE.

- [x] **Step 4: Bound highlight realtime rows**

```sql
SELECT id,room_id,name,state,attempt,error_message,updated_at
FROM highlight_clips
WHERE state IN ('queued','processing') OR updated_at>=?
ORDER BY updated_at DESC,id DESC
```

Use the existing worker clock and a 300-second cutoff.

- [x] **Step 5: Run journal and highlight worker tests**

Run: `PYTHONPATH=src .venv/bin/pytest tests/bili_upload/test_journal.py tests/bili_upload/test_highlight_worker.py -q`

Expected: all tests pass.

- [x] **Step 6: Re-run the deterministic NAS-shaped harness**

Run a temporary SQLite fixture with 43 historical approved jobs and 32,000 chunks. Expected: zero historical jobs returned, no chunk aggregate when there is no active/recent job, and no more than two SQL statements with one active job.

- [x] **Step 7: Commit**

```bash
git add src/blrec/bili_upload/journal.py src/blrec/bili_upload/highlight_worker.py tests/bili_upload/test_journal.py tests/bili_upload/test_highlight_worker.py
git commit -m "perf: bound realtime upload and highlight scans"
```

### Task 5: Cache network interface discovery outside realtime sampling

**Files:**
- Modify: `src/blrec/networking/manager.py`
- Modify: `src/blrec/web/routers/network.py`
- Test: `tests/networking/test_network_routing.py`
- Test: `tests/web/test_network_routes.py`

**Interfaces:**
- Produces: `await NetworkRouteManager.refresh_interfaces(force: bool = False)`.
- Changes: `interfaces()` returns the cached configured snapshot and never starts a subprocess.
- Default cache TTL: 10 seconds.

- [x] **Step 1: Write cache and async-refresh tests**

```python
@pytest.mark.asyncio
async def test_interfaces_uses_cache_until_async_refresh() -> None:
    provider = Mock(return_value=_interfaces())
    manager = NetworkRouteManager(
        lambda: NetworkSettings(),
        interface_provider=provider,
        interface_cache_ttl_seconds=10,
        clock=clock,
    )
    manager.interfaces()
    manager.interfaces()
    assert provider.call_count == 1
    clock.value += 11
    await manager.refresh_interfaces()
    assert provider.call_count == 2
```

Also assert `network.snapshot()` does not invoke the provider after construction.

- [x] **Step 2: Run focused tests and verify repeated discovery**

Run: `PYTHONPATH=src .venv/bin/pytest tests/networking/test_network_routing.py tests/web/test_network_routes.py -q`

Expected: the new provider-count tests fail.

- [x] **Step 3: Add a locked cache and async refresh**

Prime once during manager construction, retain the existing `RLock`, and execute later discovery with `run_in_executor`. Apply configured enabled/limit values only when returning the cached snapshot.

- [x] **Step 4: Refresh only at explicit route boundaries**

`GET /network/interfaces` performs a non-forced TTL refresh before returning. Probe performs a refresh before selecting interfaces. PATCH reuses the cache and forces one refresh only after persistence if needed. `network.snapshot()` and realtime sampling use cached data only.

- [x] **Step 5: Run tests**

Run: `PYTHONPATH=src .venv/bin/pytest tests/networking/test_network_routing.py tests/networking/test_network_platform.py tests/web/test_network_routes.py -q`

Expected: all tests pass and realtime snapshot invokes zero subprocesses.

- [x] **Step 6: Commit**

```bash
git add src/blrec/networking/manager.py src/blrec/web/routers/network.py tests/networking/test_network_routing.py tests/web/test_network_routes.py
git commit -m "perf: cache host network interface discovery"
```

### Task 6: Subscribe the Angular client to route-specific realtime topics

**Files:**
- Modify: `webapp/src/app/core/services/realtime.service.ts`
- Modify: `webapp/src/app/core/services/realtime.service.spec.ts`
- Modify: `webapp/src/app/tasks/tasks.component.spec.ts`
- Modify: `webapp/src/app/network/network.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`

**Interfaces:**
- Preserves: `RealtimeService.events$: Observable<RealtimeEvent>` and one shared EventSource.
- Produces: `realtimeTopicsForUrl(url: string): readonly RealtimeEventType[]`.
- Behavior: suppress only the first server bootstrap `resync`; later reconnect/overflow `resync` remains visible.

- [x] **Step 1: Write URL mapping and bootstrap-resync tests**

```typescript
expect(realtimeTopicsForUrl('/tasks')).toEqual(['tasks']);
expect(realtimeTopicsForUrl('/network')).toEqual(['network']);
expect(realtimeTopicsForUrl('/recordings')).toEqual(['upload_progress']);
expect(realtimeTopicsForUrl('/upload-tasks')).toEqual(['upload_progress']);
expect(realtimeTopicsForUrl('/clips')).toEqual([
  'upload_progress',
  'highlight_progress',
]);
```

Emit two `resync` events and assert the first is ignored while the second reaches subscribers. Assert two Angular subscribers still create exactly one EventSource.

- [x] **Step 2: Run the service test and verify the current URL lacks topics**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/core/services/realtime.service.spec.ts'`

Expected: the topic URL and initial-resync tests fail.

- [x] **Step 3: Build the topic URL at EventSource creation**

Inject `Router`, normalize query/hash away, map the active route to sorted unique topics, and call:

```typescript
const params = encodeURIComponent(topics.join(','));
const source = this.eventSourceFactory(
  this.url.makeApiUrl(`/api/v1/realtime?topics=${params}`),
);
```

Keep listener callbacks inside Angular only for received subscribed events. Ignore the first well-formed `resync` on that EventSource instance.

- [x] **Step 4: Assert page initialization no longer reloads**

Update the task, network and recording-session component tests to emit the bootstrap resync followed by a real resync. The initial load count stays one; the later event causes one reload.

- [x] **Step 5: Run Angular tests and checks**

Run:

```bash
cd webapp
npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/core/services/realtime.service.spec.ts' --include='src/app/tasks/tasks.component.spec.ts' --include='src/app/network/network.component.spec.ts' --include='src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts'
npx ng lint
npx tsc --noEmit -p tsconfig.app.json
```

Expected: all commands pass.

- [x] **Step 6: Commit**

```bash
git add webapp/src/app/core/services/realtime.service.ts webapp/src/app/core/services/realtime.service.spec.ts webapp/src/app/tasks/tasks.component.spec.ts webapp/src/app/network/network.component.spec.ts webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts
git commit -m "perf: subscribe to route-specific realtime updates"
```

### Task 7: Foundation integration verification and endpoint ledger

**Files:**
- Create: `docs/performance/request-audit.md`
- Modify: `docs/superpowers/plans/2026-07-20-request-performance-foundation.md`

**Interfaces:**
- Consumes: request metrics, auth throttling, topic-aware realtime, lightweight progress, and interface cache from Tasks 1–6.
- Produces: checked audit rows for all 103 inbound routes and the known outbound operation groups.

- [x] **Step 1: Create the request audit ledger**

Group all routes by router and record method/path, IO classes (`R`, `W`, `F`, `P`, `X`, `S`), budget, evidence, finding and disposition. Do not include credentials, query values, local paths or account identifiers.

- [x] **Step 2: Run backend verification**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest tests/web tests/networking tests/bili_upload/test_database.py tests/bili_upload/test_journal.py tests/bili_upload/test_highlight_worker.py -q
black --check src tests
isort --check-only src tests
flake8 src tests
mypy src/blrec
```

Expected: all tests and checks pass.

- [x] **Step 3: Run frontend verification and production build**

Run:

```bash
cd webapp
npm test -- --watch=false --browsers=ChromeHeadless
npx ng lint
npm run build
```

Expected: tests, lint, and build pass; only previously accepted bundle/style warnings may remain.

- [x] **Step 4: Re-run the baseline harness**

Expected with the NAS-shaped fixture:

- no realtime subscriber: zero upload/highlight DB queries and zero interface subprocess calls;
- room-only subscriber: no upload/highlight/network provider calls;
- 43 old approved jobs with terminal branches: zero chunk aggregation;
- repeated authenticated GETs inside 60 seconds: zero repeated activity writes.

- [x] **Step 5: Commit**

```bash
git add docs/performance/request-audit.md docs/superpowers/plans/2026-07-20-request-performance-foundation.md
git commit -m "docs: record request performance audit"
```

- [x] **Step 6: Prepare the next independent plan**

Create `docs/superpowers/plans/2026-07-20-hot-read-path-performance.md` from the remaining approved design scope: lightweight recording/upload summaries, room-policy N+1, retention, highlight counts/timeline, list indexes, Angular row rendering and lazy route bundles. Do not begin that plan until this foundation passes its regression suite.

## Completion record (2026-07-20)

- Tasks 1-6 were implemented and independently committed from `d41cefc` through
  `57361f7`, including their review fixes. Task 7 produced the 103-row inbound
  ledger, the major outbound-operation ledger, and the independent hot-read plan.
- Registered-route comparison: 103 exact matches in registration order (101 HTTP,
  2 WebSocket); IDs I-001 through I-103 are contiguous.
- Backend regression: the brief's test selection passed with 245 tests and 7
  warnings. The local virtual-environment `pytest` launcher had a stale
  interpreter shebang, so the same environment ran it as
  `.venv/bin/python -m pytest`.
- Backend quality: Black left 333 files unchanged; isort and Flake8 exited 0; mypy
  reported no issues in 248 source files. The shell had no bare quality-tool
  commands on `PATH`, so each module ran through `.venv/bin/python -m`.
- Frontend regression: full Karma completed with `371 SUCCESS`.
- Frontend lint: `npx ng lint` reproduced exactly five starting-SHA errors and zero
  warnings. They are the empty lifecycle hooks in page-not-found and three task
  detail components, plus the native-event output name in info-panel. These
  unrelated baseline errors were not changed.
- Production build: succeeded with a temporary output path and did not modify the
  tracked packaged webapp. Existing selector, component-style, initial-bundle, and
  CommonJS optimization warnings remain.
- Baseline harness: no subscribers called task/network/upload/highlight providers
  `(0,0,0,0)`; a tasks-only subscriber called only tasks `(1,0,0,0)`; 43 historical
  terminal jobs skipped chunk aggregation; cached interface reads did not rediscover
  interfaces; repeated session and extension authentication inside 60 seconds made
  zero activity writes. The four focused persisted-state/cache tests passed.
- Follow-up work is documented in
  `docs/superpowers/plans/2026-07-20-hot-read-path-performance.md`; no follow-up
  implementation is part of this foundation task.
