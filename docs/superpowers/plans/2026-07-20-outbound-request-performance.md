# Outbound Request Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate unsafe repeats of non-idempotent Bilibili writes, then reduce duplicate outbound reads and unbounded delivery work without increasing any Bilibili request cadence, changing upload-route affinity, or widening risk-control exposure.

**Architecture:** Preserve the existing FastAPI, asyncio, SQLite, Angular, network-route manager, and account-gate boundaries. First make remote `unknown_outcome` terminal for automatic UPOS completion and danmaku posting. Then pass or briefly coalesce immutable read results at the narrowest owning boundary, keep account-scoped writes serialized, and replace detached notification/webhook work with lifecycle-owned bounded dispatchers. Update metadata may use a stale cache; cookie validation must only reuse a cookie-less transport and must never cache credentials.

**Tech Stack:** Python 3.8+, asyncio, aiohttp, requests, FastAPI, SQLite, pytest, Angular 15, RxJS, Jasmine/Karma.

**Source Evidence:** `docs/superpowers/specs/2026-07-20-end-to-end-request-performance-design.md` defines the target architecture and non-goals; `docs/performance/request-audit.md` is the current 105-route/18-group ledger (`I-104` is recording detail and `I-105` is marker counts); `docs/performance/outbound-request-audit.md` supplies the code-verified O-01–O-15 findings and corrects the stale QR/Review descriptions. New inbound work must start at `I-106`; this plan does not reuse an existing ID. When implementation discovers drift, update the failing test and this plan before changing the stated request budget.

## Global Constraints

- Do not use git worktrees. Work in the current repository and inspect `git status --short` before every commit.
- Do not run pressure, cadence, or failure-injection tests against Bilibili, PyPI, webhook targets, notification providers, or the NAS. Every request-count test uses fakes or a loopback test server.
- Do not increase Bilibili request frequency, parallel write lines, retry attempts, or fallback frequency. Savings come only from response handoff, single-flight, bounded concurrency, removal of duplicate probes, and safer backoff.
- Keep `LiveStatusCoordinator` at the configured 30–60 second interval (default 30), batch size at most 29, sequential batches, one stale confirmation, breaker/canary behavior, and 600-second fallback cooldown.
- Keep stream-availability and fMP4 debounce sleeps at one second. Keep HLS init-section two-read agreement, segment size/CRC checks, transfer retry windows, and route rotation.
- Keep QR polling at one second with a 180-second TTL. `GET /qr-sessions/{id}` stays a local memory/SQLite read and must never become a polling trigger.
- Keep Review at 900 seconds per account. Keep Comments at one item per broad-loop turn, the five-second action delay, the 600-second WBI-key cache, text reconciliation, and unknown-pin manual pause.
- Keep danmaku posting at `max(25, configured_interval)` with one sending line per account, current fairness/breakers, and daily/rate-limit pauses. Only `DefinitelyNotSent` may return to automatic retry.
- Keep UPOS chunk concurrency at default 2 and hard maximum 3, chunk attempts at most 3, preupload admission at 1→5/minute, cooldown at most 15 minutes, session pooling, file-identity checks, and the upload route selected for that upload. Never rotate a chunk or completion onto another interface to recover an error.
- Keep submission/edit `DefinitelyNotSent` versus `RemoteOutcomeUnknown` classification and every existing manual-repair fence. A non-idempotent unknown result has zero blind repeat requests.
- Keep Categories' 24-hour credential-version cache, per-account lock, and stale fallback. Keep Network probe explicitly user-triggered, one request per interface, cached, and bounded by eight seconds.
- Keep cover trust policy: HTTPS-only trusted origins, no redirect, 2 MiB maximum. Keep the persistent `(asset_id, account_id)` custom-cover URL cache.
- Preserve account credential-version rechecks while a write gate is held. UI admission may time out; background workers retain the ability to wait. Do not weaken or bypass the gate.
- Use `asyncio.wait_for` for Python 3.8+ absolute deadlines. Every deadline owner accepts an internal timeout parameter with its production default; tests inject `0.01` seconds instead of sleeping through production budgets. Do not add `asyncio.timeout`, `TaskGroup`, or another Python 3.11-only API.
- Never place cookies, tokens, account identifiers, concrete webhook URLs, local media paths, or response bodies in logs, metrics, cache keys exposed to logs, or test failure messages.
- Write the failing test first. A request-budget test must assert call count, maximum in-flight work, deadline, and remote-outcome state where applicable—not merely response content.
- Each task is one independently reviewable commit. Stage only the files listed by that task; do not absorb unrelated worktree changes.

### Accepted frontend lint baseline

Full `npx ng lint` is compared with starting SHA `57361f7`: it may exit 1 only for the five pre-existing file/rule pairs below and must add no warning or error. Every changed frontend file must also pass targeted ESLint, focused Karma, TypeScript compilation, and the production build.

- `webapp/src/app/page-not-found/page-not-found.component.ts`: `@angular-eslint/no-empty-lifecycle-method`.
- `webapp/src/app/tasks/task-detail/task-postprocessing-detail/task-postprocessing-detail.component.ts`: `@angular-eslint/no-empty-lifecycle-method`.
- `webapp/src/app/tasks/task-detail/task-room-info-detail/task-room-info-detail.component.ts`: `@angular-eslint/no-empty-lifecycle-method`.
- `webapp/src/app/tasks/task-detail/task-user-info-detail/task-user-info-detail.component.ts`: `@angular-eslint/no-empty-lifecycle-method`.
- `webapp/src/app/tasks/info-panel/info-panel.component.ts`: `@angular-eslint/no-output-native`.

---

## File Map

- `src/blrec/bili/live.py`, `src/blrec/task/task.py`, `src/blrec/task/task_manager.py`, `src/blrec/application.py`: composite room/anchor refresh, initialization revision handoff, and bounded room-disjoint task work.
- `src/blrec/bili/live_monitor.py`, `src/blrec/core/stream_recorder.py`, `src/blrec/core/stream_recorder_impl.py`, `src/blrec/core/operators/stream_url_resolver.py`: play-resolution handoff and removal of the pre-transfer validation GET.
- `src/blrec/bili_upload/upos.py`, `protocol.py`, `errors.py`, `upload.py`: UPOS completion safety, timeout taxonomy, `Retry-After`, jitter, and persisted defer behavior.
- `src/blrec/bili_upload/danmaku_publish.py`: durable unknown/in-flight danmaku state and branch pause.
- `src/blrec/bili_upload/archive_reads.py`, `review.py`, `upload.py`, `runtime.py`: shared read-only archive pages and bounded reconciliation.
- `src/blrec/bili_upload/collections.py`, `categories.py`, `collection_publish.py`, `accounts.py`, `runtime.py`: catalog single-flight and account-scoped collection-write admission.
- `src/blrec/bili_upload/covers.py`, `src/blrec/core/cover_downloader.py`: live-cover coalescing, broadcast-scoped bytes, and downloader lifecycle.
- `src/blrec/notification/dispatcher.py`, `providers.py`, `notifiers.py`, `operational.py`: bounded notification delivery and pooled providers.
- `src/blrec/webhook/webhook_emitter.py`: independently bounded, per-URL ordered webhook delivery.
- `src/blrec/update/helpers.py`, `src/blrec/update/api.py`, `src/blrec/bili/helpers.py`, update/validation routers, and `src/blrec/application.py`: lifecycle-owned low-frequency clients with deliberately different cache policies.
- Focused tests remain next to the existing test suites under `tests/bili/`, `tests/bili_upload/`, `tests/core/`, `tests/notification/`, `tests/webhook/`, `tests/update/`, and `tests/web/`.

## Audit Coverage and Task Routing

O-03 and O-04 are one task because the initialization revision is produced by the composite refresh and consumed by the durable task-control operation. O-14 and O-15 are separate commits because update metadata is cached while cookie validation must remain uncached and preserve its existing HTTP error mapping. Notification and Webhook stay separate because their overload policies differ and must be independently reversible.

| Audit item | Task | Result |
| --- | --- | --- |
| O-01 | Task 1 | UPOS completion unknown is durable and manually paused. |
| O-02 | Task 2 | Danmaku in-flight/unknown is durable and never auto-posted. |
| O-03/O-04 | Task 3 | One composite room response plus revision handoff and concurrency 2. |
| O-05 | Task 4 | Play resolution is handed off; validation GET is removed. |
| O-06 | Task 5 | Explicit transport budgets, bounded `Retry-After`, and chunk jitter. |
| O-07 | Task 6 | Archive page single-flight and bounded review/reconciliation cycles. |
| O-08 | Task 7 | Collection list TTL/single-flight and no post-create double list. |
| O-09 | Task 8 | Collection writes share the account gate; UI admission is bounded. |
| O-10 | Task 9 | Broadcast cover bytes and transient live-cover work are reused. |
| O-11 | Task 10 | One nonterminal QR session and poller per manager subject. |
| O-12 | Task 11 | Notification work is bounded, pooled, and drained. |
| O-13 | Task 12 | Webhook work is bounded, ordered, pooled, and drained. |
| O-14 | Task 13 | Update metadata client, single-flight, and stale cache. |
| O-15 | Task 14 | Uncached pooled cookie validation with unchanged HTTP semantics. |

All 18 audited outbound groups have an explicit disposition:

| # | Outbound group | Disposition |
| --- | --- | --- |
| 1 | Room status | **Keep unchanged**; Task 15 reruns the 58-room/batch request guards. |
| 2 | Room detail | Task 3. |
| 3 | Play info | Task 4. |
| 4 | Recording transfer | Task 4; only the pre-validation GET is removed. |
| 5 | Danmaku WebSocket | **Keep handshake/auth/fallback/heartbeat/reconnect cadence unchanged**; Task 15 checks it. |
| 6 | UPOS | Tasks 1 and 5. |
| 7 | Submission | Tasks 5 and 6; unknown reconciliation remains mandatory. |
| 8 | Review | Task 6; existing per-account grouping and 900-second cadence stay. |
| 9 | Comments | **Keep unchanged**; Task 15 reruns unknown/pin/cadence guards. |
| 10 | Danmaku posting | Task 2. |
| 11 | Collections | Tasks 7 and 8. |
| 12 | Categories | Task 7 coalesces one forced generation; **keep the 24-hour credential-scoped cache/stale behavior unchanged**. |
| 13 | Covers | Task 9. |
| 14 | QR/account | Tasks 8 and 10; status remains local and renewal unknown remains fenced. |
| 15 | Notifications | Task 11. |
| 16 | Webhook | Task 12. |
| 17 | Network probe | **Keep explicit-only one-request-per-interface behavior**; Task 15 reruns the eight-second guard. |
| 18 | Update check | Task 13. |

The 20 inbound triggers with an `Outbound` disposition are all covered without inventing a second background-operation model:

| Trigger IDs | Count | Plan boundary |
| --- | ---: | --- |
| I-011 | 1 | Tasks 3–4: stable per-room results; only outbound-bearing actions use concurrency 2. |
| I-018/I-019 | 2 | Task 3: one composite request per room, ten-second logical deadline. |
| I-022/I-023 | 2 | Tasks 3–4; existing Write/media accepted-operation boundary and Danmaku WS cadence remain. |
| I-026/I-027 | 2 | Task 4: reuse the monitor/debounce resolution; no extra stream GET. |
| I-030 | 1 | Task 3 initialization revision; Task 4 play reuse; WS behavior unchanged. |
| I-036 | 1 | **Keep** existing Danmaku WS connection budgets; Task 15 verifies no faster reconnect. |
| I-042 | 1 | Task 14 uncached, pooled cookie validation. |
| I-045 | 1 | Task 13 update cache/single-flight/stale result. |
| I-056/I-057 | 2 | Task 10 create single-flight; status remains local. |
| I-059 | 1 | Task 8 bounded UI admission and 60-second renewal deadline. |
| I-076/I-078/I-099 | 3 | Task 7 coalesces concurrent refreshes while keeping Categories' existing cache semantics. |
| I-083 | 1 | Task 7 collection catalog cache. |
| I-084 | 1 | Tasks 7–9 collection read/write and cover reuse. |
| I-102 | 1 | Task 3 add→start handoff, Task 4 play handoff, Categories behavior unchanged. |

---

### Task 1: Fence unknown UPOS completion outcomes (O-01, P0)

**Files:**
- Modify: `src/blrec/bili_upload/upos.py`
- Modify: `tests/bili_upload/test_upos.py`
- Modify: `tests/bili_upload/test_task_actions.py`

**Interfaces:**
- `upload_parts.upload_state='unknown_outcome'` becomes a durable automatic-retry fence.
- `upload_state='completing'` found after interruption is conservatively converted to `unknown_outcome` before any network call.
- Existing `UposUploadPaused` drives the coordinator/job into its existing manual-action state; no migration or new public API is needed.

- [ ] **Step 1: Replace the permissive completion test with failing no-repeat tests**

Change `test_unknown_complete_result_is_deferred_and_completed_on_retry` into the following contract and add the crash-boundary case. Keep the existing task-action refusal test and make it assert both `completing` and `unknown_outcome`.

```python
with pytest.raises(UposUploadPaused, match='outcome'):
    await uploader.upload_part(part_id, bundle=object(), claim=claim)
protocol.complete_error = None
with pytest.raises(UposUploadPaused, match='outcome'):
    await uploader.upload_part(part_id, bundle=object(), claim=claim)

assert protocol.complete_calls == 1
assert await database.scalar(
    'SELECT upload_state FROM upload_parts WHERE id=?', (part_id,)
) == 'unknown_outcome'
```

For the interrupted case, seed a fully uploaded part as `completing`, call `upload_part`, and assert `complete_calls == 0` plus final `unknown_outcome`.

- [ ] **Step 2: Run the focused red tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_upos.py tests/bili_upload/test_task_actions.py -q`

Expected: the two new assertions fail because `upload_part` currently rewrites `completing/unknown_outcome` to `uploading`, and the former regression expects two completion calls.

- [ ] **Step 3: Make completion unknown durable with the smallest state change**

At the start of `upload_part`, never normalize an uncertain completion back to `uploading`:

```python
if part.upload_state == 'completing':
    await self._update_part(
        part_id,
        claim,
        {'upload_state': 'unknown_outcome'},
    )
    raise UposUploadPaused('UPOS completion outcome requires manual confirmation')
if part.upload_state == 'unknown_outcome':
    raise UposUploadPaused('UPOS completion outcome requires manual confirmation')
```

In `_complete`, preserve `DefinitelyNotSent` and explicit transient Bilibili rejection as safe paths, but fence `RemoteOutcomeUnknown`:

```python
except RemoteOutcomeUnknown:
    await self._update_part(
        part_id,
        claim,
        {'upload_state': 'unknown_outcome'},
        expected_session_json=session_json,
    )
    audit(
        'upload_completion_unknown',
        level='WARNING',
        job_id=claim.id,
        part_id=part_id,
        result='manual_confirmation_required',
    )
    raise UposUploadPaused(
        'UPOS completion outcome requires manual confirmation'
    ) from None
```

Do not add an automatic “confirm again” action. Existing task actions already reject parts in `completing/unknown_outcome`.

- [ ] **Step 4: Verify safety, style, and types**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_upos.py tests/bili_upload/test_task_actions.py -q
.venv/bin/python -m black --check src/blrec/bili_upload/upos.py tests/bili_upload/test_upos.py tests/bili_upload/test_task_actions.py
.venv/bin/python -m isort --check-only src/blrec/bili_upload/upos.py tests/bili_upload/test_upos.py tests/bili_upload/test_task_actions.py
.venv/bin/python -m flake8 src/blrec/bili_upload/upos.py tests/bili_upload/test_upos.py tests/bili_upload/test_task_actions.py
.venv/bin/python -m mypy src/blrec/bili_upload/upos.py
```

**Budget and invariants:** completion requests per UPOS session are at most one after `headers_sent=True`; automatic completion requests after unknown/interrupted state are exactly zero. Chunk concurrency, attempts, admission rate, account gate, route affinity, and session pool are untouched. Production/test file budget: 1/2.

- [ ] **Step 5: Commit only this safety fence**

```bash
git add src/blrec/bili_upload/upos.py tests/bili_upload/test_upos.py tests/bili_upload/test_task_actions.py
git commit -m "fix: fence unknown UPOS completion"
```

---

### Task 2: Fence unknown and interrupted danmaku posts (O-02, P0)

**Files:**
- Modify: `src/blrec/bili_upload/danmaku_publish.py`
- Modify: `tests/bili_upload/test_danmaku_publish.py`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`

**Interfaces:**
- `_mark_unknown(claim, work, message)` persists the item as `unknown_outcome`, releases its lease, and pauses the job's danmaku branch.
- Startup recovery changes `in_flight` to `unknown_outcome`, clears stale leases, and leaves already-unknown rows unchanged.
- The existing recording-session detail remains the only UI surface: it displays the unknown count/items and exposes no automatic resend button.

- [ ] **Step 1: Rewrite all three automatic-requeue tests as failing safety tests**

Rename the tests to describe the new contract and assert no second protocol call:

```python
await worker.run_once()
clock.advance(300)
await worker.run_once()

assert len(protocol.calls) == 1
assert await database.scalar(
    'SELECT state FROM danmaku_items'
) == 'unknown_outcome'
assert await database.scalar(
    'SELECT danmaku_branch_state FROM upload_jobs WHERE id=1'
) == 'paused'
```

For a seeded `in_flight` row, the first `run_once` must issue zero posts and convert it to unknown. For `recover_interrupted`, assert every `in_flight/unknown_outcome` row is unknown, leases are cleared, the branch is paused, and a later `run_once` still issues zero posts. Preserve a separate `DefinitelyNotSent` test showing it remains `prepared` and cannot run sooner than 25 seconds.

Add a focused Angular assertion using the existing fixture data with `danmakuUnknown: 1` and `unknownDanmakuItems`: the warning text/item is visible after opening details and no button with an automatic resend label exists.

- [ ] **Step 2: Run the red backend and frontend tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_danmaku_publish.py -q
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts'
```

Expected: backend tests fail because unknown and interrupted rows currently return to `prepared`; the UI test documents the already-present safe display and may already pass.

- [ ] **Step 3: Persist unknown state and pause the branch atomically**

Replace `_retry_uncertain` with `_mark_unknown` and use it for both `RemoteOutcomeUnknown` and `ProtocolContractError` after send:

```python
async def _mark_unknown(
    self, claim: LeaseClaim, work: _DanmakuWork, message: str
) -> None:
    now = int(self._clock())

    def update(connection: sqlite3.Connection) -> None:
        changed = connection.execute(
            "UPDATE danmaku_items SET state='unknown_outcome',error_code=NULL,"
            'error_message=?,next_attempt_at=?,lease_owner=NULL,lease_until=NULL '
            'WHERE id=? AND lease_owner=? AND lease_generation=?',
            (
                message,
                _DORMANT_UNTIL,
                claim.id,
                claim.lease_owner,
                claim.lease_generation,
            ),
        )
        if changed.rowcount != 1:
            raise LeaseLost('danmaku item lease was lost')
        connection.execute(
            "UPDATE upload_jobs SET danmaku_branch_state='paused',"
            'review_reason=?,updated_at=? WHERE id=?',
            (message, now, work.job_id),
        )

    await self._database.write(update)
    audit(
        'danmaku_outcome_unknown',
        level='WARNING',
        job_id=work.job_id,
        part_id=work.part_id,
        item_id=work.id,
        result='manual_confirmation_required',
    )
```

When `_process` loads `in_flight`, call `_mark_unknown` before any gate/protocol work. In `recover_interrupted`, use one SQLite transaction to set `in_flight` rows to `unknown_outcome`, clear lease fields for both uncertain states, and set every affected job's branch to `paused`; never set it back to `publishing`. Do not change `_safe_retry`, breaker calculations, or candidate selection cadence.

- [ ] **Step 4: Verify backend, UI, and static checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_danmaku_publish.py tests/bili_upload/test_journal.py tests/bili_upload/test_task_actions.py -q
.venv/bin/python -m black --check src/blrec/bili_upload/danmaku_publish.py tests/bili_upload/test_danmaku_publish.py
.venv/bin/python -m isort --check-only src/blrec/bili_upload/danmaku_publish.py tests/bili_upload/test_danmaku_publish.py
.venv/bin/python -m flake8 src/blrec/bili_upload/danmaku_publish.py tests/bili_upload/test_danmaku_publish.py
.venv/bin/python -m mypy src/blrec/bili_upload/danmaku_publish.py
(cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts')
(cd webapp && npx eslint src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts)
```

**Budget and invariants:** posts after unknown/in-flight are zero. `DefinitelyNotSent` retains at most the existing five safe attempts, each separated by at least 25 seconds. There remains one sending line per account; no breaker, daily limit, fairness order, account gate, or UI manual fence is weakened. Production/test file budget: 1/2; no migration.

- [ ] **Step 5: Commit only the danmaku safety change**

```bash
git add src/blrec/bili_upload/danmaku_publish.py tests/bili_upload/test_danmaku_publish.py webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts
git commit -m "fix: preserve unknown danmaku outcomes"
```

---

### Task 3: Composite room refresh and task revision handoff (O-03/O-04, P1)

**Files:**
- Modify: `src/blrec/bili/live.py`
- Modify: `src/blrec/task/task.py`
- Modify: `src/blrec/task/task_manager.py`
- Modify: `src/blrec/application.py`
- Modify: `src/blrec/control/operations.py`
- Modify: `src/blrec/setting/setting_manager.py`
- Modify: `src/blrec/web/routers/tasks.py`
- Modify: `src/blrec/web/routers/browser_extension.py`
- Create: `tests/bili/test_live_info_refresh.py`
- Create: `tests/task/test_task_manager_outbound.py`
- Modify: `tests/control/test_operations.py`
- Modify: `tests/web/test_tasks_routes.py`
- Modify: `tests/web/test_browser_extension_routes.py`
- Modify: `tests/task/test_live_connection_controller.py`

**Interfaces:**
- `Live.info_revision: int` advances once per successful composite room+anchor application.
- `Live.update_info()` is same-instance single-flight and has one injected `_info_timeout_seconds=10` absolute deadline over the complete web→app→HTML fallback sequence; tests pass `0.01`.
- `RecordTask.info_revision` exposes the current `Live` revision.
- **Hard prerequisite:** Write/media Task 6 (`TaskControlReconciler`) and Task 7 (durable membership/control-operation journal) are merged first. This task extends those owners; it does not add a second route-local operation model.
- The durable operation step carries `reuse_info_revision` from add/collect to start and consumes it exactly once. HTTP `start`, `recorder_enable`, and browser collect only persist intent and return `accepted`; their final per-room result is read from the existing control-operation endpoint.
- `refresh` remains synchronous and read-only. Refresh work and durable remote-bearing control steps share fixed concurrency 2 and retain stable input-order results.
- `SettingsManager.change_task_desired_states(...)` is the only batch desired-state writer and performs at most one `dump_settings()` for the whole batch.

- [ ] **Step 1: Write failing composite refresh and cancellation tests**

Use fake WebApi/AppApi/HTML loaders and an in-flight counter. The core request-budget assertions are:

```python
results = await asyncio.gather(*(live.update_info(True) for _ in range(10)))
assert results == [True] * 10
assert web_info_by_room_calls == 1
assert live.room_info.uid == live.user_info.uid
assert live.info_revision == 1
assert max_in_flight == 1
```

Also cover: web failure calls app exactly once; web+app failure calls HTML exactly once; all failures complete within an injected `0.01`-second budget; canceling one waiter does not cancel the shared fetch; a finished task is not retained as a long-lived cache; `update_room_info` and `update_user_info` consume the same composite response when concurrent. Do not monkeypatch global `asyncio.wait_for`.

- [ ] **Step 2: Write failing durable-operation request-budget tests**

In `test_task_manager_outbound.py`, exercise the existing reconciler/control journal rather than calling lifecycle work from a route:

```python
operation = await control_operations.submit_collect(123, auto_record=True)
await control_worker.run_until_idle()
assert fake_live.composite_calls == 1
assert operation_store.get(operation.id).status == 'succeeded'

await control_operations.submit_batch_desired_state(seed_room_ids, enabled=True)
await control_worker.run_until_idle()
assert max_room_detail_in_flight == 2
assert completed_room_ids == seed_room_ids
assert settings_manager.dump_settings_calls == 1
```

Add route tests proving `start`, `recorder_enable`, and browser collect return C100 `accepted` without waiting for blocked lifecycle calls. Add worker tests proving max in-flight remote steps is 2, final results remain in input order, one room failure does not replay successful rooms, and restart resumes pending steps. Browser collect must produce and consume the exact revision inside one durable operation. For 58 rooms, assert `change_task_desired_states` is called once and `dump_settings_calls == 1`; never fan out calls to `app.start_task()` or `enable_task_recorder()` from HTTP. `refresh` alone may use route-local bounded gather because it is read-only. Assert `RecordTaskManager.add_task` no longer retries all of `setup()` after a failure.

- [ ] **Step 3: Run the red tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili/test_live_info_refresh.py tests/task/test_task_manager_outbound.py tests/control/test_operations.py tests/web/test_tasks_routes.py tests/web/test_browser_extension_routes.py tests/task/test_live_connection_controller.py -q
```

Expected: new interfaces are missing, a successful refresh makes two overlapping `getInfoByRoom` calls, add→start refreshes twice, batch desired state dumps per room or waits in HTTP, and durable worker concurrency/restart assertions fail.

- [ ] **Step 4: Implement one composite, single-flight refresh**

Remove the outer tenacity decorators from `get_room_info/get_user_info`; one logical deadline owns all fallback attempts. The task stored in `_info_refresh_task` performs parsing and state application so ten waiters cannot advance the revision ten times:

```python
@dataclass(frozen=True)
class LiveInfoSnapshot:
    room_info: RoomInfo
    user_info: UserInfo

async def _load_info_snapshot(self) -> LiveInfoSnapshot:
    loaders: Tuple[Callable[[], Awaitable[ResponseData]], ...] = (
        lambda: self._webapi.get_info_by_room(self._room_id),
        lambda: self._appapi.get_info_by_room(self._room_id),
        self._get_room_info_res_via_html_page,
    )
    for loader in loaders:
        try:
            data = await loader()
            return LiveInfoSnapshot(
                room_info=RoomInfo.from_data(data['room_info']),
                user_info=UserInfo.from_info_by_room(data),
            )
        except Exception:
            continue
    raise ApiRequestError('room information is unavailable')

async def _refresh_info_once(self) -> None:
    snapshot = await asyncio.wait_for(
        self._load_info_snapshot(), timeout=self._info_timeout_seconds
    )
    self._room_info = snapshot.room_info
    self._user_info = snapshot.user_info
    self._room_id = snapshot.room_info.room_id
    self._info_revision += 1
```

Create `_info_refresh_task` while holding an `asyncio.Lock`, await it through `asyncio.shield`, and clear it by identity in a done callback. `init`, `update_info`, `update_room_info`, and `update_user_info` all enter that boundary. Preserve each public method's `raise_exception=False` logging/boolean behavior. `deinit` cancels and awaits an outstanding refresh before closing its owned session.

- [ ] **Step 5: Pass the revision through the durable owner and batch settings once**

Remove the 60-second retry decorator from `RecordTaskManager.add_task`. Expose the `Live` revision through `RecordTask`, then let the existing membership/control-operation worker skip only an exact handoff:

```python
async def start_task(
    self, room_id: int, *, reuse_info_revision: Optional[int] = None
) -> None:
    task = self._get_task(room_id, check_ready=True)
    if reuse_info_revision is None or task.info_revision != reuse_info_revision:
        await task.update_info(raise_exception=True)
    await task.enable_monitor()
    await task.enable_recorder()
```

The reconciler/control worker owns a shared semaphore for room-disjoint remote steps; only the synchronous read-only `refresh` route may use the same bounded helper directly:

```python
semaphore = asyncio.Semaphore(2)

async def run(index: int, room_id: int) -> Tuple[int, TaskBatchActionResult]:
    async with semaphore:
        return index, await run_one(room_id)

indexed = await asyncio.gather(
    *(run(index, room_id) for index, room_id in enumerate(room_ids))
)
results = [result for _index, result in sorted(indexed)]
```

For a batch start/stop/recorder toggle, call `SettingsManager.change_task_desired_states(changes)` once before waking the reconciler; it computes one diff and performs zero or one dump. Do not call per-room settings mutators concurrently. `stop`, `force_stop`, recorder disable, `cut`, and `remove` keep their Write/media ownership and are not moved into a route-local gather. In browser collect, the durable membership step captures the revision after successful add, writes it into the next operation step, and the worker passes it to `start_task`; a resumed operation reads the persisted step data rather than refreshing from the route.

- [ ] **Step 6: Verify request counts, cleanup, and static checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili/test_live_info_refresh.py tests/task/test_task_manager_outbound.py tests/control/test_operations.py tests/web/test_tasks_routes.py tests/web/test_browser_extension_routes.py tests/task/test_live_connection_controller.py tests/bili/test_live_status_coordinator.py tests/integration/test_batch_live_monitor.py -q
.venv/bin/python -m black --check src/blrec/bili/live.py src/blrec/task/task.py src/blrec/task/task_manager.py src/blrec/application.py src/blrec/control/operations.py src/blrec/setting/setting_manager.py src/blrec/web/routers/tasks.py src/blrec/web/routers/browser_extension.py tests/bili/test_live_info_refresh.py tests/task/test_task_manager_outbound.py tests/control/test_operations.py tests/web/test_tasks_routes.py tests/web/test_browser_extension_routes.py tests/task/test_live_connection_controller.py
.venv/bin/python -m isort --check-only src/blrec/bili/live.py src/blrec/task/task.py src/blrec/task/task_manager.py src/blrec/application.py src/blrec/control/operations.py src/blrec/setting/setting_manager.py src/blrec/web/routers/tasks.py src/blrec/web/routers/browser_extension.py
.venv/bin/python -m flake8 src/blrec/bili/live.py src/blrec/task/task.py src/blrec/task/task_manager.py src/blrec/application.py src/blrec/control/operations.py src/blrec/setting/setting_manager.py src/blrec/web/routers/tasks.py src/blrec/web/routers/browser_extension.py
.venv/bin/python -m mypy src/blrec/bili src/blrec/task src/blrec/control src/blrec/setting/setting_manager.py src/blrec/application.py src/blrec/web/routers/tasks.py src/blrec/web/routers/browser_extension.py
```

**Budget and invariants:** successful logical refresh = 1 upstream room-detail call; concurrent same-instance refresh = 1; fallback attempts ≤3 and total wall time ≤10 seconds; add→start/collect = 1 composite refresh; room-disjoint remote action concurrency = 2; 58-room desired-state batch dumps settings exactly once. HTTP control latency stays within C100 because it returns accepted, restart resumes the same durable operation, and there is one control owner. No general TTL is added, status polling is not touched, and a failed setup is not replayed wholesale. Production/test file budget: 6/5 plus the already-owned Write/media control files needed to carry the revision; if this exceeds one focused operation-owner diff, revise the task before coding.

- [ ] **Step 7: Commit the atomic producer/consumer handoff**

```bash
git add src/blrec/bili/live.py src/blrec/task/task.py src/blrec/task/task_manager.py src/blrec/application.py src/blrec/control/operations.py src/blrec/setting/setting_manager.py src/blrec/web/routers/tasks.py src/blrec/web/routers/browser_extension.py tests/bili/test_live_info_refresh.py tests/task/test_task_manager_outbound.py tests/control/test_operations.py tests/web/test_tasks_routes.py tests/web/test_browser_extension_routes.py tests/task/test_live_connection_controller.py
git commit -m "perf: coalesce room detail refreshes"
```

---

### Task 4: Hand play resolution to the recorder and remove the validation GET (O-05, P1)

**Files:**
- Modify: `src/blrec/bili/live.py`
- Modify: `src/blrec/bili/live_monitor.py`
- Modify: `src/blrec/core/stream_recorder.py`
- Modify: `src/blrec/core/stream_recorder_impl.py`
- Modify: `src/blrec/core/operators/stream_url_resolver.py`
- Modify: `tests/bili/test_live_stream_url.py`
- Create: `tests/core/test_stream_request_reuse.py`
- Create: `tests/core/test_hls_integrity_guards.py`

**Interfaces:**
- `LiveStreamSnapshot` records requested quality/platform/format/codec/alternative selection, parsed streams, and monotonic observation time.
- `StreamResolution` records the full selection identity, URL, and server-selected quality.
- `Live.resolve_live_stream(qn, *, api_platform, stream_format, stream_codec, select_alternative, snapshot=None)` reuses only a caller-supplied snapshot for which `_snapshot_matches(...)` verifies the complete identity and monotonic age≤2 seconds. `snapshot=None` always performs the original fresh read; there is no ambient recent-snapshot lookup.
- `StreamURLResolver.seed(resolution)` accepts the fMP4 confirmation result; URL reuse becomes a pure identity check.

- [ ] **Step 1: Write failing monitor→debounce→resolver request-budget tests**

Use a fake play API and fake `requests.Session` whose `.get()` fails if invoked by `_can_resue_url`. Cover FLV and fMP4 separately:

```python
await monitor._check_if_stream_available()
await recorder._do_start()

assert play_api.calls == 1              # FLV monitor result is selected directly
assert requests_session.get_calls == 1  # real StreamFetcher GET only
assert requests_session.validation_get_calls == 0
```

For fMP4, make the monitor snapshot contain the target format, advance the injected monotonic clock by one second for confirmation, and assert exactly two total play-info calls: monitor/first success plus one debounce confirmation. Resolver adds zero. Also assert a mismatched quality/platform/format/codec/alternative flag or snapshot older than two seconds is not reused, `snapshot=None` always reads fresh, and a real 403 transfer error resets/rotates before resolving again.

Add an executable HLS guard in `test_hls_integrity_guards.py` using fake playlist/segment transports: init-section data is accepted only after two identical reads, size/CRC mismatches are rejected, and a real transfer failure retains the existing retry and route-rotation sequence. This is a preservation test, not new HLS behavior.

- [ ] **Step 2: Run the focused red tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/bili/test_live_stream_url.py tests/core/test_stream_request_reuse.py tests/core/test_hls_integrity_guards.py -q`

Expected: snapshot/resolution APIs are missing; current monitor, fMP4 wait, and resolver each fetch play info, and `_can_resue_url` performs its own GET.

- [ ] **Step 3: Add immutable selection snapshots at the `Live` boundary**

Keep `get_live_streams()` compatible, but add an internal/public snapshot-producing call whose immutable return value is handed to its immediate consumer. Do not install it on `Live`. Move URL selection into a pure helper used by both public methods:

```python
@dataclass(frozen=True)
class LiveStreamSnapshot:
    quality_number: QualityNumber
    api_platform: ApiPlatform
    stream_format: StreamFormat
    stream_codec: StreamCodec
    select_alternative: bool
    streams: Tuple[Any, ...]
    observed_at: float

@dataclass(frozen=True)
class StreamResolution:
    quality_number: QualityNumber
    api_platform: ApiPlatform
    stream_format: StreamFormat
    stream_codec: StreamCodec
    select_alternative: bool
    url: str
    real_quality_number: QualityNumber

async def resolve_live_stream(
    self,
    qn: QualityNumber = 10000,
    *,
    api_platform: ApiPlatform = 'web',
    stream_format: StreamFormat = 'flv',
    stream_codec: StreamCodec = 'avc',
    select_alternative: bool = False,
    snapshot: Optional[LiveStreamSnapshot] = None,
) -> StreamResolution:
    usable = snapshot if self._snapshot_matches(
        snapshot,
        qn=qn,
        api_platform=api_platform,
        stream_format=stream_format,
        stream_codec=stream_codec,
        select_alternative=select_alternative,
        max_age_seconds=2,
    ) else None
    streams = (
        list(usable.streams)
        if usable is not None
        else await self.get_live_streams(qn, api_platform)
    )
    return self._select_stream(streams, qn, api_platform, stream_format, stream_codec, select_alternative)
```

The two-second value is only an explicit producer→consumer handoff window, not a polling cache. `_snapshot_matches` compares all five request dimensions and uses an injected monotonic clock. The monitor passes the immutable object directly to fMP4 confirmation/recorder/resolver; no caller retrieves it from a `Live` ambient recent slot.

- [ ] **Step 4: Seed the recorder and make URL reuse side-effect free**

Change `_wait_fmp4_stream` to return `Optional[StreamResolution]`: consume the monitor snapshot as the first success, retain the one-second sleep, and force one fresh confirmation. After `_change_impl`, call `self._impl.seed_stream_resolution(confirmed)`. `StreamRecorderImpl` forwards that seed to its resolver.

```python
def seed(self, resolution: StreamResolution) -> None:
    self._stream_url = resolution.url
    self._stream_host = urlparse(resolution.url).hostname or ''
    self._stream_params = StreamParams(
        quality_number=resolution.quality_number,
        stream_format=resolution.stream_format,
        api_platform=resolution.api_platform,
        use_alternative_stream=resolution.select_alternative,
    )

def _can_resue_url(self, params: StreamParams) -> bool:
    return params == self._stream_params and bool(self._stream_url)
```

Delete the `requests.Session.get(stream=True, timeout=3)` validation. Keep `RequestExceptionHandler` as the owner of real-transfer retry/reset/route rotation; do not weaken HLS integrity or change request headers.

- [ ] **Step 5: Verify exact request counts and no response leaks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili/test_live_stream_url.py tests/core/test_stream_request_reuse.py tests/core/test_hls_integrity_guards.py tests/core/test_recorder_event_order.py -q
.venv/bin/python -m black --check src/blrec/bili/live.py src/blrec/bili/live_monitor.py src/blrec/core/stream_recorder.py src/blrec/core/stream_recorder_impl.py src/blrec/core/operators/stream_url_resolver.py tests/bili/test_live_stream_url.py tests/core/test_stream_request_reuse.py tests/core/test_hls_integrity_guards.py
.venv/bin/python -m isort --check-only src/blrec/bili/live.py src/blrec/bili/live_monitor.py src/blrec/core/stream_recorder.py src/blrec/core/stream_recorder_impl.py src/blrec/core/operators/stream_url_resolver.py
.venv/bin/python -m flake8 src/blrec/bili/live.py src/blrec/bili/live_monitor.py src/blrec/core/stream_recorder.py src/blrec/core/stream_recorder_impl.py src/blrec/core/operators/stream_url_resolver.py
.venv/bin/python -m mypy src/blrec/bili/live.py src/blrec/bili/live_monitor.py src/blrec/core
```

**Budget and invariants:** each one-second availability tick still makes at most one play-info request. A monitor result counts as fMP4 success one, and only one fresh confirmation is allowed. Resolver play-info calls after a matching explicit confirmation are zero; an absent/mismatched/stale snapshot gets one fresh read; pre-transfer validation GETs are zero. The first real FLV/HLS GET remains authoritative. Executable guards prove unchanged HLS init-section agreement, segment size/CRC rejection, and transfer retry/route rotation. Production/test file budget: 5/3.

- [ ] **Step 6: Commit the resolution handoff**

```bash
git add src/blrec/bili/live.py src/blrec/bili/live_monitor.py src/blrec/core/stream_recorder.py src/blrec/core/stream_recorder_impl.py src/blrec/core/operators/stream_url_resolver.py tests/bili/test_live_stream_url.py tests/core/test_stream_request_reuse.py tests/core/test_hls_integrity_guards.py
git commit -m "perf: reuse live stream resolutions"
```

---

### Task 5: Add explicit protocol budgets, `Retry-After`, and chunk jitter (O-06, P1)

**Files:**
- Modify: `src/blrec/bili_upload/errors.py`
- Modify: `src/blrec/bili_upload/protocol.py`
- Modify: `src/blrec/bili_upload/upos.py`
- Modify: `src/blrec/bili_upload/upload.py`
- Modify: `tests/bili_upload/test_protocol_matrix.py`
- Modify: `tests/bili_upload/test_upos.py`
- Modify: `tests/bili_upload/test_upload.py`

**Interfaces:**
- `BiliApiError.retry_after_seconds: Optional[int]` is parsed from HTTP `Retry-After` and clamped to 1–900 seconds.
- `AiohttpProtocolTransport` keeps total timeout 30 seconds and sets connect/sock-connect ≤5 seconds plus sock-read ≤20 seconds.
- `protocol_request_deadline(seconds)` supplies a task-local absolute deadline; transport requests use the smaller of their 30-second budget and the remaining operation budget, preserving headers-sent outcome taxonomy.
- `UposUploader` gains keyword-only `sleeper=asyncio.sleep` and `jitter=random.uniform` dependencies so short chunk retry spacing is deterministic in tests.
- Long server delays are persisted into `next_attempt_at` and release the account gate; they are not slept inside the worker.

- [ ] **Step 1: Add failing timeout/taxonomy/backoff tests**

In the protocol matrix tests, inspect the constructed `ClientTimeout`, cover delta-seconds and HTTP-date `Retry-After`, return `None` for negative/invalid values, clamp values over 900 to 900, and prove it never appears in `repr(error)`. Distinguish HTTP status from Bilibili JSON business code. Assert HTTP 5xx on a protocol-matrix idempotent request becomes retryable, while HTTP 5xx on non-idempotent completion/submission remains `RemoteOutcomeUnknown`. Under `protocol_request_deadline(0.01)`, assert the transport uses at most that injected budget, an already-expired deadline is `DefinitelyNotSent`, and expiry after headers are sent is `RemoteOutcomeUnknown` for a non-idempotent request.

In UPOS tests, inject `sleeper` and `jitter`:

```python
delays = []
uploader = UposUploader(
    database,
    protocol,
    chunk_size=4,
    concurrency=1,
    sleeper=lambda delay: append_delay(delays, delay),
    jitter=lambda low, high: high,
)
await uploader.upload_part(part_id, bundle=object(), claim=claim)
assert protocol.chunk_calls == 3
assert delays == [1.0, 2.0]
```

Add coordinator tests asserting a server value such as 240 seconds is persisted, the lease/gate is released, and the P0 completion/submission unknown tests still make zero blind repeat calls. Add the complete safe-retry matrix: preupload-init and chunk HTTP 500→success use at most three attempts; continuous HTTP 503 becomes one bounded persisted defer; completion and submission HTTP 500 become unknown with exactly one call.

- [ ] **Step 2: Run the focused red tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_protocol_matrix.py tests/bili_upload/test_upos.py tests/bili_upload/test_upload.py -q`

Expected: timeout fields and error metadata are absent, chunk transport failures loop immediately, and coordinator backoff ignores the header.

- [ ] **Step 3: Parse bounded server advice without leaking headers**

Add only the sanitized integer to `BiliApiError`:

```python
class BiliApiError(RuntimeError):
    def __init__(
        self,
        code: int,
        public_message: Optional[str] = None,
        *,
        operation: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
```

Use `email.utils.parsedate_to_datetime` for the HTTP-date form and an injected wall clock. Return `None` for malformed/non-positive values and `min(parsed, 900)` otherwise. Pass it only when raising `BiliApiError`; never store raw headers. For HTTP status ≥500, use `BiliApiError` only when the protocol matrix marks that operation idempotent; retain `RemoteOutcomeUnknown` for non-idempotent requests. A JSON business code returned in an HTTP 2xx response keeps its existing operation-specific mapping and is not treated as an HTTP 5xx.

Add a `ContextVar[Optional[float]]` containing a monotonic deadline and a synchronous context manager that always resets its token. `AiohttpProtocolTransport.send` computes the remaining seconds immediately before `session.request`; if none remain, raise `TransportFailure(headers_sent=False)`. Otherwise construct a per-call `ClientTimeout` whose fields are the minimum of the default field and remaining duration. Do not implement an outer task cancellation: aiohttp timeout must continue through the existing `headers_sent` classification.

- [ ] **Step 4: Apply explicit timeout fields and safe retry spacing**

```python
self._timeout = aiohttp.ClientTimeout(
    total=timeout_seconds,
    connect=min(5, timeout_seconds),
    sock_connect=min(5, timeout_seconds),
    sock_read=min(20, timeout_seconds),
)
```

Before chunk attempts 2 and 3, call the injected sleeper with jittered upper bounds one and two seconds. Do not jitter completion. For protocol-matrix idempotent preupload-init/chunk requests, include HTTP 500–599 in the existing at-most-three safe retry/defer path; after bounded attempts, persist a defer and release the gate rather than pausing permanently. For explicit 406/408/425/429 responses, prefer `error.retry_after_seconds` and clamp to 1–900; otherwise retain the existing computed delay. `_defer_chunk` and `_update_job` with `release=True` must persist the delay before returning to the broad loop. Non-idempotent completion/submission 5xx never enters this path. Re-run Task 1's completion tests while editing this code.

- [ ] **Step 5: Verify protocol safety and static checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_protocol_matrix.py tests/bili_upload/test_upos.py tests/bili_upload/test_upload.py tests/bili_upload/test_task_actions.py -q
.venv/bin/python -m black --check src/blrec/bili_upload/errors.py src/blrec/bili_upload/protocol.py src/blrec/bili_upload/upos.py src/blrec/bili_upload/upload.py tests/bili_upload/test_protocol_matrix.py tests/bili_upload/test_upos.py tests/bili_upload/test_upload.py
.venv/bin/python -m isort --check-only src/blrec/bili_upload/errors.py src/blrec/bili_upload/protocol.py src/blrec/bili_upload/upos.py src/blrec/bili_upload/upload.py
.venv/bin/python -m flake8 src/blrec/bili_upload/errors.py src/blrec/bili_upload/protocol.py src/blrec/bili_upload/upos.py src/blrec/bili_upload/upload.py
.venv/bin/python -m mypy src/blrec/bili_upload
```

**Budget and invariants:** request total ≤30 seconds or the smaller task-local remaining deadline, connect/sock-connect≤5, sock-read≤20; idempotent preupload/chunk HTTP 5xx attempts≤3 with delays `[0..1, 0..2]`; accepted `Retry-After` is clamped to 1–900 seconds. Long delay sleeps under an account gate are zero. Exhausted idempotent 5xx is deferred, not permanently paused. Completion/submission/danmaku unknown blind retries remain zero, chunk concurrency remains default 2/max 3, and the selected upload source is never changed by retry metadata. Production/test file budget: 4/3.

- [ ] **Step 6: Commit the protocol budget change**

```bash
git add src/blrec/bili_upload/errors.py src/blrec/bili_upload/protocol.py src/blrec/bili_upload/upos.py src/blrec/bili_upload/upload.py tests/bili_upload/test_protocol_matrix.py tests/bili_upload/test_upos.py tests/bili_upload/test_upload.py
git commit -m "perf: bound Bilibili protocol retries"
```

---

### Task 6: Share archive read snapshots and bound reconciliation cycles (O-07, P1)

**Files:**
- Create: `src/blrec/bili_upload/archive_reads.py`
- Modify: `src/blrec/bili_upload/review.py`
- Modify: `src/blrec/bili_upload/upload.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Create: `tests/bili_upload/test_archive_reads.py`
- Modify: `tests/bili_upload/test_review.py`
- Modify: `tests/bili_upload/test_upload.py`

**Interfaces:**
- `ArchiveReadService.list_page(bundle, *, account_id, credential_version, status, page_number, page_size)` returns an immutable tuple and single-flights/cache-scopes by every named field.
- `ArchiveReadService.detail(bundle, *, account_id, credential_version, bvid)` single-flights the read-only detail lookup.
- Completed reads are fresh for 30 seconds; failed/cancelled tasks are never cached.
- Review archive list/detail reads and submission read reconciliation each have an injected `_read_timeout_seconds=60` absolute limit; tests use `0.01`. Approval, branch creation, and every other local/non-idempotent state transition run outside that cancellation boundary. Detail calls remain sequential.

- [ ] **Step 1: Write failing cache/request-budget tests**

Use two consumers sharing one service instance. Assert 20 concurrent calls and a simultaneous Review/reconciliation read produce one list request for an identical key, but a credential-version, account, status, page, or page-size change does not reuse it.

```python
pages = await asyncio.gather(
    *(reader.list_page(bundle, account_id=7, credential_version=3,
        status=ARCHIVE_STATUS, page_number=1, page_size=50) for _ in range(20))
)
assert protocol.list_calls == 1
assert all(page == pages[0] for page in pages)
```

Also assert cancellation of one waiter does not cancel the shared read, exceptions evict the in-flight entry, completed entries expire after 30 seconds, and `close()` cancels/awaits no user-owned task (the service owns only tasks it creates).

- [ ] **Step 2: Add failing read-deadline, recovery, and candidate tests**

Preserve `test_waiting_jobs_are_grouped_into_one_read_per_account`. Inject `read_timeout_seconds=0.01` and add tests that a stuck page/detail ends only the read phase without affecting the next account; no approval/branch write has started when that timeout fires. Repeated page identities terminate early; at most 20 pages are read; at most 10 same-title candidate details are inspected sequentially. When candidate 11 appears, the submission remains `unknown_outcome` and `submit_archive` is never called.

Add a crash-boundary regression: interrupt after the approved transaction commits but before `_create_branches`, rebuild the watcher/runtime, and assert startup/cycle recovery creates every still-pending branch exactly once without calling `submit_archive` again. This recovery scans only `approved` jobs with a `pending` branch and relies on each branch's existing durable state/unknown fence.

- [ ] **Step 3: Run the red tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_archive_reads.py tests/bili_upload/test_review.py tests/bili_upload/test_upload.py -q`

Expected: the service and constructor injections are missing; cross-consumer calls duplicate pages; there is no whole-cycle deadline or candidate cap.

- [ ] **Step 4: Implement a narrow read-only service**

Normalize only the protocol's `data.arc_audits` list and `archive_view` mapping; do not place mutable bundles or raw query dictionaries in keys.

```python
@dataclass(frozen=True)
class _PageKey:
    account_id: int
    credential_version: int
    status: str
    page_number: int
    page_size: int

class ArchiveReadService:
    FRESH_SECONDS = 30

    async def list_page(
        self,
        bundle: Any,
        *,
        account_id: int,
        credential_version: int,
        status: str,
        page_number: int,
        page_size: int,
    ) -> Tuple[Mapping[str, Any], ...]:
        key = _PageKey(account_id, credential_version, status, page_number, page_size)
        return await self._singleflight(key, lambda: self._fetch_page(bundle, key))

    async def detail(
        self,
        bundle: Any,
        *,
        account_id: int,
        credential_version: int,
        bvid: str,
    ) -> Mapping[str, Any]:
        key = (account_id, credential_version, bvid)
        return await self._singleflight(key, lambda: self._fetch_detail(bundle, bvid))
```

Use `asyncio.shield` for waiters, clear failed/cancelled tasks by identity, and retain only successful `(expires_at, value)` entries. Do not add a stale-on-error result here: an uncertain submission must remain uncertain rather than be “confirmed” from expired data.

- [ ] **Step 5: Bound only remote reads and recover approved pending branches**

Construct one `ArchiveReadService` in `BiliUploadRuntime` and pass it to both `UploadCoordinator` and `ReviewWatcher`. Add `credential_version` to Review's account query. Replace direct list/detail calls with the service, preserving page size 50, page cap 20, repeated-page detection, and sequential iteration. A read function returns immutable review decisions; no database write or branch call occurs inside its `wait_for`. Close the shared reader during runtime shutdown.

```python
try:
    decisions = await asyncio.wait_for(
        self._read_account_decisions(
            bundle, account_id, credential_version, jobs
        ),
        timeout=self._read_timeout_seconds,
    )
except asyncio.TimeoutError:
    audit('upload_review_cycle_timed_out', account_scope='redacted')
    continue
for decision in decisions:
    changed += await self._apply_review_decision(decision)
```

Wrap only `_find_remote_submission`'s read-only list/detail reconciliation in its own injected timeout. Stop collecting after candidate 11, raise a private `ArchiveCandidateLimit`, then outside the timeout catch it beside existing read failures and call `_mark_unknown_submission`; never route that condition to `submit_archive`. Do not parallelize archive details.

Before the next remote review read (and once during runtime recovery), scan `approved` jobs with pending branch state and call the existing idempotent branch-creation boundary. This closes the approve→branch crash window without wrapping non-idempotent collection/comment/danmaku work in `wait_for`; branch durable states remain the owner of interrupted/unknown outcomes.

- [ ] **Step 6: Verify reuse, fences, and static checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_archive_reads.py tests/bili_upload/test_review.py tests/bili_upload/test_upload.py tests/bili_upload/test_submission_verifier.py -q
.venv/bin/python -m black --check src/blrec/bili_upload/archive_reads.py src/blrec/bili_upload/review.py src/blrec/bili_upload/upload.py src/blrec/bili_upload/runtime.py tests/bili_upload/test_archive_reads.py tests/bili_upload/test_review.py tests/bili_upload/test_upload.py
.venv/bin/python -m isort --check-only src/blrec/bili_upload/archive_reads.py src/blrec/bili_upload/review.py src/blrec/bili_upload/upload.py src/blrec/bili_upload/runtime.py
.venv/bin/python -m flake8 src/blrec/bili_upload/archive_reads.py src/blrec/bili_upload/review.py src/blrec/bili_upload/upload.py src/blrec/bili_upload/runtime.py
.venv/bin/python -m mypy src/blrec/bili_upload
```

**Budget and invariants:** identical account/version/query/page/detail reads ≤1 per 30 seconds; pages ≤20; same-title details ≤10 at concurrency 1; remote read/reconciliation phase ≤60 seconds. Timeout cancellation covers no approval, branch creation, SQLite write, or non-idempotent remote write. Approved+pending recovery makes zero submission calls. Review remains grouped per account and runs no sooner than every 900 seconds. Submission unknown still performs read reconciliation only and never blind resubmission. Comments are not moved to another worker or sped up. Production/test file budget: 4/3.

- [ ] **Step 7: Commit the shared archive reader**

```bash
git add src/blrec/bili_upload/archive_reads.py src/blrec/bili_upload/review.py src/blrec/bili_upload/upload.py src/blrec/bili_upload/runtime.py tests/bili_upload/test_archive_reads.py tests/bili_upload/test_review.py tests/bili_upload/test_upload.py
git commit -m "perf: share archive read snapshots"
```

---

### Task 7: Coalesce upload catalogs and remove the post-create double list (O-08, P1)

**Files:**
- Modify: `src/blrec/bili_upload/collections.py`
- Modify: `src/blrec/bili_upload/categories.py`
- Modify: `src/blrec/web/routers/bili_collections.py`
- Modify: `webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.service.ts`
- Modify: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.ts`
- Modify: `tests/bili_upload/test_collections.py`
- Modify: `tests/bili_upload/test_categories.py`
- Modify: `tests/web/test_bili_collections_routes.py`
- Modify: `webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.service.spec.ts`
- Modify: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.spec.ts`

**Interfaces:**
- `_ResolvedAccount` includes `credential_version`.
- `CollectionManager.list(account_mode, account_id, *, force_refresh=False)` caches by `(account_id, credential_version)` for 60 seconds and may return stale data for at most 15 minutes after a refresh error.
- `GET /bili/collections` accepts `forceRefresh: bool = False`.
- Only an explicit manual UI refresh sends `forceRefresh=true`; creation merges its returned `CollectionView` and does not immediately list again.
- `UploadCategoryCatalog` keeps its 24-hour SQLite cache/stale fallback, but normal and forced callers arriving during one refresh await the same generation instead of serially refetching.

- [ ] **Step 1: Write failing backend cache tests**

Use an injected clock and a protocol gate to assert 20 normal or forced concurrent callers collapse to one request. Cover fresh hit, expiry, credential-version change, stale-on-error within 15 minutes, error beyond stale, and failed-task eviction.

```python
catalogs = await asyncio.gather(
    *(manager.list('fixed', 7) for _ in range(20))
)
assert protocol.list_calls == 1
assert {catalog.account_id for catalog in catalogs} == {7}
```

For create success, assert cover upload ≤1, create ≤1, backend post-create list exactly 1, the refreshed catalog is installed, and the next ordinary list is local. For `RemoteOutcomeUnknown`, assert create calls=1, no automatic retry/list reconciliation, and the fresh entry is invalidated so a later operator refresh can observe reality.

In `test_categories.py`, preserve every existing 24-hour/credential-version/stale assertion and add one blocked refresh generation. Twenty normal callers on a miss and twenty simultaneous `force_refresh=True` callers must each produce one `archive_pre` call, not twenty serial calls. A later explicit force after that generation completes must still start exactly one new refresh.

- [ ] **Step 2: Write failing route and Angular request tests**

Assert the route forwards `forceRefresh=true` only when requested. In the service, verify the default URL has no force query and explicit refresh sends it. In the component:

```typescript
component.createCollection();
createRequest.flush(createdCollection);

expect(service.collections).toHaveBeenCalledTimes(1); // initial load only
expect(component.collections).toContain(jasmine.objectContaining({ id: 9 }));
```

- [ ] **Step 3: Run the red tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_collections.py tests/bili_upload/test_categories.py tests/web/test_bili_collections_routes.py -q
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/tasks/upload-policy-dialog/room-upload-policy.service.spec.ts' --include='src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.spec.ts'
```

Expected: every collection list reaches the protocol, force is unsupported, successful create calls `loadCollections()` again, and forced Category waiters refresh serially after acquiring the current per-account lock.

- [ ] **Step 4: Implement credential-scoped TTL and single-flight**

Keep the cache private to `CollectionManager` and avoid a general cache framework:

```python
@dataclass(frozen=True)
class _CatalogEntry:
    catalog: CollectionCatalogView
    fresh_until: float
    stale_until: float

async def list(
    self,
    account_mode: str,
    account_id: Optional[int],
    *,
    force_refresh: bool = False,
) -> CollectionCatalogView:
    account = await self._resolve_account(account_mode, account_id)
    key = (account.id, account.credential_version)
    current = self._catalogs.get(key)
    if not force_refresh and current is not None and self._clock() < current.fresh_until:
        return current.catalog
    return await self._refresh_catalog(key, account, stale=current)
```

`_refresh_catalog` always awaits an existing in-flight task, including concurrent forced callers. Use `asyncio.shield`, cache only success, and return the prior entry only when `now < stale_until`. On successful create, explicitly evict the completed generation, start one `_refresh_catalog` call for the resolved account key, and return the normalized created item; never perform a second backend list for the same create flow.

At the outside of `UploadCategoryCatalog`'s existing lock/double-check, track one task per `(account_id, credential_version)`. Both normal misses and forced callers first await that task; `force_refresh=True` bypasses the persisted fresh row only when it creates a new generation, not when it joins one already in flight. Clear by task identity on success/failure/cancellation. Do not change the 24-hour timestamps, persisted rows, stale return, protocol request, or account validation.

- [ ] **Step 5: Wire explicit refresh and merge the create result**

Route query aliases stay camelCase at the HTTP boundary. The Angular service uses `HttpParams` only when forced. `loadCollections(forceRefresh = false)` passes the flag; the refresh button passes true; initial/dialog/task-label loads use false. Replace the create-success `loadCollections()` call with an id-based immutable merge and selection of the returned collection.

- [ ] **Step 6: Verify backend/frontend behavior and lint**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_collections.py tests/bili_upload/test_categories.py tests/web/test_bili_collections_routes.py -q
.venv/bin/python -m black --check src/blrec/bili_upload/collections.py src/blrec/bili_upload/categories.py src/blrec/web/routers/bili_collections.py tests/bili_upload/test_collections.py tests/bili_upload/test_categories.py tests/web/test_bili_collections_routes.py
.venv/bin/python -m isort --check-only src/blrec/bili_upload/collections.py src/blrec/bili_upload/categories.py src/blrec/web/routers/bili_collections.py
.venv/bin/python -m flake8 src/blrec/bili_upload/collections.py src/blrec/bili_upload/categories.py src/blrec/web/routers/bili_collections.py
.venv/bin/python -m mypy src/blrec/bili_upload/collections.py src/blrec/bili_upload/categories.py src/blrec/web/routers/bili_collections.py
(cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/tasks/upload-policy-dialog/room-upload-policy.service.spec.ts' --include='src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.spec.ts')
(cd webapp && npx eslint src/app/tasks/upload-policy-dialog/room-upload-policy.service.ts src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.ts src/app/tasks/upload-policy-dialog/room-upload-policy.service.spec.ts src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.spec.ts)
(cd webapp && npx tsc --noEmit -p tsconfig.app.json)
```

**Budget and invariants:** collection fresh TTL 60 seconds and stale-if-error≤15 minutes; identical collection account/version in-flight lists=1. Category TTL remains 24 hours, stale behavior is unchanged, and each concurrent normal/forced generation makes one `archive_pre` call. A create flow has cover upload≤1, create≤1, and post-create list≤1 across backend+frontend. Creation unknown is not retried/reconciled automatically. This task does not yet change write serialization; that is Task 8's separate rollback boundary. Production/test file budget: 5/5.

- [ ] **Step 7: Commit the catalog cache**

```bash
git add src/blrec/bili_upload/collections.py src/blrec/bili_upload/categories.py src/blrec/web/routers/bili_collections.py webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.service.ts webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.ts tests/bili_upload/test_collections.py tests/bili_upload/test_categories.py tests/web/test_bili_collections_routes.py webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.service.spec.ts webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.spec.ts
git commit -m "perf: coalesce upload catalogs"
```

---

### Task 8: Serialize collection writes and bound UI account admission (O-09, P1)

**Files:**
- Modify: `src/blrec/bili_upload/errors.py`
- Modify: `src/blrec/bili_upload/accounts.py`
- Modify: `src/blrec/bili_upload/collections.py`
- Modify: `src/blrec/bili_upload/collection_publish.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/web/routers/bili_accounts.py`
- Modify: `src/blrec/web/routers/bili_collections.py`
- Modify: `tests/bili_upload/test_accounts.py`
- Modify: `tests/bili_upload/test_collections.py`
- Modify: `tests/bili_upload/test_collection_publish.py`
- Modify: `tests/web/test_bili_accounts_routes.py`
- Modify: `tests/web/test_bili_collections_routes.py`

**Interfaces:**
- `_PerAccountGate.hold(expected_credential_version, *, wait_timeout_seconds=None)` retains the post-acquire database recheck and raises `AccountWriteBusy` only when admission expires.
- `AccountManager.check_account_renewal(account_id, *, admission_timeout_seconds=None, operation_timeout_seconds=None)` applies one admission budget across `_auth_failure_lock` and the account gate, then uses Task 5's protocol deadline rather than unsafe outer cancellation.
- UI callers use 250 ms admission and 60-second operation budgets; background auth recovery passes no admission timeout.
- Both collection creation and collection episode publication hold the runtime's existing `AccountWriteGate` for the exact account/version and accept an internal `operation_timeout_seconds=60`; deadline tests pass `0.01`.

- [ ] **Step 1: Write failing gate primitive and renewal tests**

Hold an upload gate, then start a UI renewal and a background renewal. With a monotonic fake:

```python
with pytest.raises(AccountWriteBusy):
    await manager.check_account_renewal(
        account_id,
        admission_timeout_seconds=0.25,
        operation_timeout_seconds=60,
    )
assert elapsed <= 0.30
assert not background_waiter.done()
```

Repeat with `_auth_failure_lock` held to prove the same 250 ms budget covers both locks rather than resetting at the second lock. Preserve tests showing credential-version change is checked after acquisition and renewal unknown never auto-repeats.

- [ ] **Step 2: Write failing collection serialization and route tests**

Run a manager create, publisher add-episode, and upload write for one account; assert maximum remote-write concurrency is 1. A different account may proceed independently. Assert the UI routes return 409 with a retryable busy message by 250 ms, while publisher workers wait. Seed `RemoteOutcomeUnknown` for create/add-episode and assert calls remain 1 with existing manual/failure state. Inject `operation_timeout_seconds=0.01` for a blocked protocol fake so the test never waits 60 real seconds.

- [ ] **Step 3: Run the red tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_accounts.py tests/bili_upload/test_collections.py tests/bili_upload/test_collection_publish.py tests/web/test_bili_accounts_routes.py tests/web/test_bili_collections_routes.py -q`

Expected: gate admission has no timeout, collection writers are outside the gate, and routes cannot distinguish busy from remote uncertainty.

- [ ] **Step 4: Add timed admission without weakening credential checks**

Acquire explicitly so timeout covers only waiting, then execute the existing database checks inside the held lock:

```python
@asynccontextmanager
async def hold(
    self,
    expected_credential_version: int,
    *,
    wait_timeout_seconds: Optional[float] = None,
) -> AsyncIterator[None]:
    try:
        if wait_timeout_seconds is None:
            await self._lock.acquire()
        else:
            await asyncio.wait_for(
                self._lock.acquire(), timeout=max(0.0, wait_timeout_seconds)
            )
    except asyncio.TimeoutError:
        raise AccountWriteBusy('account write is busy') from None
    try:
        await self._check_account(expected_credential_version)
        yield
    finally:
        self._lock.release()
```

In `check_account_renewal`, compute one `time.monotonic()` admission deadline before acquiring `_auth_failure_lock`; pass only the remaining duration to the account gate. After admission, run renewal under `with protocol_request_deadline(operation_timeout_seconds):` so each request uses the remaining part of one 60-second budget and retains `DefinitelyNotSent`/`RemoteOutcomeUnknown` classification. Do not wrap non-idempotent renewal in an outer `asyncio.wait_for`. When no operation time remains, the protocol reports not-sent before opening a request.

- [ ] **Step 5: Put both collection writers behind the shared gate**

Inject `AccountWriteGate` from `BiliUploadRuntime`. `CollectionManager.create` uses the `credential_version` already resolved in Task 7 and holds the gate across cover resolution/upload, create, and the single post-create catalog refresh. Both it and `CollectionPublisher` run their remote sequence under `protocol_request_deadline(self._operation_timeout_seconds)`; the publisher loads account state/version with its job and holds the gate across exactly one `add_collection_episode` attempt. Reuse existing unknown catches and release/persist state before returning; do not add a retry loop or outer cancellation.

Routes pass UI budgets and map only `AccountWriteBusy` to 409 “账号正在执行其他写操作，请稍后重试.” Account/credential/outcome errors keep their existing status and manual-recovery messages.

- [ ] **Step 6: Verify per-account serialization and static checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_accounts.py tests/bili_upload/test_collections.py tests/bili_upload/test_collection_publish.py tests/bili_upload/test_upload.py tests/web/test_bili_accounts_routes.py tests/web/test_bili_collections_routes.py -q
.venv/bin/python -m black --check src/blrec/bili_upload/errors.py src/blrec/bili_upload/accounts.py src/blrec/bili_upload/collections.py src/blrec/bili_upload/collection_publish.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/bili_accounts.py src/blrec/web/routers/bili_collections.py tests/bili_upload/test_accounts.py tests/bili_upload/test_collections.py tests/bili_upload/test_collection_publish.py tests/web/test_bili_accounts_routes.py tests/web/test_bili_collections_routes.py
.venv/bin/python -m isort --check-only src/blrec/bili_upload/errors.py src/blrec/bili_upload/accounts.py src/blrec/bili_upload/collections.py src/blrec/bili_upload/collection_publish.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/bili_accounts.py src/blrec/web/routers/bili_collections.py
.venv/bin/python -m flake8 src/blrec/bili_upload/errors.py src/blrec/bili_upload/accounts.py src/blrec/bili_upload/collections.py src/blrec/bili_upload/collection_publish.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/bili_accounts.py src/blrec/web/routers/bili_collections.py
.venv/bin/python -m mypy src/blrec/bili_upload src/blrec/web/routers/bili_accounts.py src/blrec/web/routers/bili_collections.py
```

**Budget and invariants:** same-account remote-write concurrency=1; UI waits for both locks in aggregate ≤250 ms and has one ≤60-second operation; workers may wait without creating a second write line. Credential version is rechecked after admission. Account renewal, collection create, collection publication, UPOS, comments, and danmaku keep their unknown fences. No upload line/interface binding changes. Production/test file budget: 7/5. If this cannot be reviewed atomically, pause and revise the plan into explicit prerequisite/consumer tasks before implementation.

- [ ] **Step 7: Commit the account-scoped write boundary**

```bash
git add src/blrec/bili_upload/errors.py src/blrec/bili_upload/accounts.py src/blrec/bili_upload/collections.py src/blrec/bili_upload/collection_publish.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/bili_accounts.py src/blrec/web/routers/bili_collections.py tests/bili_upload/test_accounts.py tests/bili_upload/test_collections.py tests/bili_upload/test_collection_publish.py tests/web/test_bili_accounts_routes.py tests/web/test_bili_collections_routes.py
git commit -m "perf: serialize account collection writes"
```

---

### Task 9: Reuse broadcast cover bytes and pool live-cover downloads (O-10, P2)

**Files:**
- Modify: `src/blrec/core/cover_downloader.py`
- Modify: `src/blrec/bili_upload/covers.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Create: `tests/core/test_cover_downloader.py`
- Modify: `tests/bili_upload/test_covers.py`
- Modify: `tests/bili_upload/test_account_runtime.py`

**Interfaces:**
- `CoverDownloader` keys downloaded bytes by `(room_id, live_start_time, cover_url)` and performs at most one composite metadata fallback per broadcast.
- A private `_FAILED` sentinel records an optional cover GET failure for that exact broadcast key; it is never confused with empty bytes and is cleared only by `ROOM_CHANGE`/new `live_start_time`.
- File-save policy remains per part; only remote bytes are reused. A `ROOM_CHANGE` URL or new `live_start_time` creates a new key.
- `CoverResolver` owns one lifecycle session and single-flights transient `live_url` work by account plus source fingerprint. Path validation, resolve, stat, read, hashing, and writes execute off the event loop through one semaphore-bounded executor submission boundary.
- `CoverResolver.close()` is awaited by `BiliUploadRuntime.close()`.

- [ ] **Step 1: Write failing legacy cover request-count tests**

Complete three video parts in one broadcast with the same URL and assert one cover GET and zero room-detail refreshes. Change only the output part path and prove each required cover file is still written from cached bytes. Repeat with a failed GET and assert `_FAILED` is cached, later parts make no second request, and callers receive the same optional-cover absence. Emit `ROOM_CHANGE` with a new URL and then a new `live_start_time`; each key may fetch once. With missing metadata, assert one Task 3 composite fallback for the broadcast, never one per part.

- [ ] **Step 2: Write failing resolver coalescing/lifecycle tests**

Start 20 identical `live_url` calls and assert one trusted download and one cover-upload request. Change local file `mtime_ns/size` or source URL and assert no reuse. Inject blocking path resolve/stat/read/write functions and prove an event-loop heartbeat advances while they run and no file operation executes on the loop thread. Preserve/strengthen HTTPS-only, no-redirect, 2 MiB cutoff, and unknown upload no-blind-retry tests. Assert runtime close waits for/cleans the shared session and in-flight tasks.

- [ ] **Step 3: Run the red tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/core/test_cover_downloader.py tests/bili_upload/test_covers.py tests/bili_upload/test_account_runtime.py -q`

Expected: legacy completion refreshes/downloads each part, transient cover work does not coalesce, and every remote download creates a session.

- [ ] **Step 4: Cache bytes at the broadcast boundary before hashing/writing**

```python
def _broadcast_key(self, cover_url: str) -> Tuple[int, int, str]:
    return (
        self._live.room_info.room_id,
        self._live.room_info.live_start_time,
        cover_url,
    )

async def _cover_bytes_for_part(self) -> bytes:
    key = self._broadcast_key(self._live.room_info.cover)
    if key not in self._cover_bytes:
        self._cover_bytes = {key: await self._fetch_cover(key[2])}
    return self._cover_bytes[key]
```

Read metadata maintained by LIVE/ROOM_CHANGE first. If no usable URL exists, remember that the current `(room_id, live_start_time)` consumed its one composite fallback, then retry metadata once. Compute SHA1 from cached bytes before deciding whether to write. Reset only on broadcast identity change or destroy; do not turn this into a cross-broadcast disk cache.

Remove `_fetch_cover`'s three-attempt tenacity decorator: the one broadcast/source request is an optional artifact read, and repeating it for every completed part defeats the broadcast budget. Define `_FAILED = object()` and store it after a failed logical fetch; cache lookup returns the same optional-cover absence without reopening the URL. Never use `None` or empty bytes as an ambiguous failure marker. Only ROOM_CHANGE or a new `live_start_time` clears that key.

- [ ] **Step 5: Pool and single-flight `CoverResolver.live_url`**

Create one `aiohttp.ClientSession` lazily with `ClientTimeout(total=30, connect=5, sock_connect=5, sock_read=20)` and `DummyCookieJar`; preserve trusted-host validation, redirect denial, and streaming size enforcement. For local sources, one synchronous helper performs validate→resolve→stat→limited read and returns `(resolved_path, st_mtime_ns, st_size, bytes)`; submit that whole helper with `loop.run_in_executor` while holding a resolver-local semaphore of 2 (Python 3.8 compatible—do not use `asyncio.to_thread`). Hash/write helpers use the same off-loop boundary. Build the fingerprint from the returned metadata, never by calling `Path.resolve/stat/read_bytes` on the event loop. Shield shared work, remove the task by identity on completion, and never persist transient live-source results into the custom-asset cache. `close()` cancels/awaits in-flight tasks before closing the session.

- [ ] **Step 6: Verify cover safety and static checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/core/test_cover_downloader.py tests/bili_upload/test_covers.py tests/bili_upload/test_account_runtime.py -q
.venv/bin/python -m black --check src/blrec/core/cover_downloader.py src/blrec/bili_upload/covers.py src/blrec/bili_upload/runtime.py tests/core/test_cover_downloader.py tests/bili_upload/test_covers.py tests/bili_upload/test_account_runtime.py
.venv/bin/python -m isort --check-only src/blrec/core/cover_downloader.py src/blrec/bili_upload/covers.py src/blrec/bili_upload/runtime.py
.venv/bin/python -m flake8 src/blrec/core/cover_downloader.py src/blrec/bili_upload/covers.py src/blrec/bili_upload/runtime.py
.venv/bin/python -m mypy src/blrec/core/cover_downloader.py src/blrec/bili_upload/covers.py src/blrec/bili_upload/runtime.py
```

**Budget and invariants:** cover GET≤1 per `(broadcast, URL)`, including failed GETs; fallback room detail≤1 per broadcast; identical live source/account in-flight upload=1; resolver-local file work in-flight≤2 and event-loop file operations=0; download total≤30 seconds and bytes≤2 MiB. Persistent custom cover cache, HTTPS/trust/redirect policy, and non-idempotent unknown behavior remain unchanged. Production/test file budget: 3/3.

- [ ] **Step 7: Commit the cover reuse boundary**

```bash
git add src/blrec/core/cover_downloader.py src/blrec/bili_upload/covers.py src/blrec/bili_upload/runtime.py tests/core/test_cover_downloader.py tests/bili_upload/test_covers.py tests/bili_upload/test_account_runtime.py
git commit -m "perf: reuse recording cover requests"
```

---

### Task 10: Limit active QR sessions per manager subject (O-11, P2)

**Files:**
- Modify: `src/blrec/bili_upload/accounts.py`
- Modify: `tests/bili_upload/test_accounts.py`
- Modify: `tests/web/test_bili_accounts_routes.py`

**Interfaces:**
- `AccountManager.create_qr(*, manager_subject: str)` is single-flight per subject and returns the existing nonterminal session view.
- A subject can create a new upstream QR only after its prior session is confirmed, expired, cancelled, or failed.
- `status()` remains a local read; only the one background task created with the session calls `poll_qr`.

- [ ] **Step 1: Write failing concurrent-create and local-status tests**

Start 20 concurrent creates for one subject while the fake `create_qr` call is blocked:

```python
views = await asyncio.gather(
    *(manager.create_qr(manager_subject='manager-a') for _ in range(20))
)
assert protocol.create_calls == 1
assert len({view.id for view in views}) == 1
assert max_active_pollers == 1
```

Assert a second subject gets a distinct session, repeated status GETs make zero additional create/poll calls, cancel/terminal allows one new create, and `close()` leaves no poll task. Retain the existing one-second/180-second fake-clock expiry test.

- [ ] **Step 2: Run the red tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_accounts.py tests/web/test_bili_accounts_routes.py -q`

Expected: concurrent calls create distinct sessions and pollers; status-local assertions should already pass and lock that correction into the suite.

- [ ] **Step 3: Serialize create and reuse only nonterminal sessions**

```python
async def create_qr(self, *, manager_subject: str) -> QrSessionView:
    lock = self._qr_create_locks.setdefault(manager_subject, asyncio.Lock())
    async with lock:
        active = next(
            (
                runtime
                for runtime in self._runtimes.values()
                if runtime.manager_subject == manager_subject
                and runtime.state in self._NONTERMINAL_QR_STATES
            ),
            None,
        )
        if active is not None:
            return self._runtime_view(active)
        return await self._create_qr_locked(manager_subject)
```

Keep the existing database insert before `asyncio.create_task(self._poll(runtime))`. Do not start a second task when returning an active view. Terminal transition/cancel keeps its current task cancellation. Clear lock bookkeeping on manager close; never expose another subject's QR URL.

- [ ] **Step 4: Verify QR cadence, authorization, and cleanup**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_accounts.py tests/web/test_bili_accounts_routes.py -q
.venv/bin/python -m black --check src/blrec/bili_upload/accounts.py tests/bili_upload/test_accounts.py tests/web/test_bili_accounts_routes.py
.venv/bin/python -m isort --check-only src/blrec/bili_upload/accounts.py tests/bili_upload/test_accounts.py tests/web/test_bili_accounts_routes.py
.venv/bin/python -m flake8 src/blrec/bili_upload/accounts.py tests/bili_upload/test_accounts.py tests/web/test_bili_accounts_routes.py
.venv/bin/python -m mypy src/blrec/bili_upload/accounts.py src/blrec/web/routers/bili_accounts.py
```

**Budget and invariants:** per manager subject: nonterminal sessions≤1, upstream create in-flight≤1, pollers≤1. Poll interval remains one second, TTL remains 180 seconds, and browser status calls add zero upstream requests. QR create failure/unknown is not blindly repeated inside the lock. Production/test file budget: 1/2.

- [ ] **Step 5: Commit the QR single-flight**

```bash
git add src/blrec/bili_upload/accounts.py tests/bili_upload/test_accounts.py tests/web/test_bili_accounts_routes.py
git commit -m "perf: bound QR session pollers"
```

---

### Task 11: Replace detached notification sends with a bounded dispatcher (O-12, P1)

**Files:**
- Create: `src/blrec/notification/dispatcher.py`
- Modify: `src/blrec/notification/providers.py`
- Modify: `src/blrec/notification/notifiers.py`
- Modify: `src/blrec/notification/operational.py`
- Modify: `src/blrec/application.py`
- Modify: `src/blrec/web/main.py`
- Create: `tests/notification/test_dispatcher.py`
- Create: `tests/notification/test_providers.py`
- Create: `tests/notification/test_notifiers.py`
- Modify: `tests/notification/test_operational.py`
- Create: `tests/test_application_outbound_lifecycle.py`
- Modify: `tests/web/test_main_lifecycle.py`

**Interfaces:**
- `NotificationDispatcher.enqueue(channel, title, content, message_type, *, coalesce_key=None) -> bool` is non-blocking with a global pending capacity of 100.
- At most four channels deliver concurrently and each channel has exactly one ordered delivery line.
- Operational items use latest-wins key `(event_code, object_key, channel)`; unkeyed legacy events reject newest when full. Neither path creates an untracked per-message task.
- Constructor-injected budgets default to delivery 60 seconds, HTTP/SMTP attempt 10 seconds, and close 15 seconds; tests pass short values. One delivery has at most three attempts. HTTP 429/5xx and transport errors are transient; other 4xx and configuration errors are final.
- `web/main.py` is the single lifecycle owner: `start()` runs before `_bili_account_runtime.start()`, and final shutdown closes the dispatcher after both producer families are disabled. Pre-start `enqueue` may append only; it never creates a worker or touches a session.
- `start()` owns one cookie-less shared HTTP session. `close(drain_timeout_seconds=15)` disables intake, drains/cancels/awaits asyncio workers, waits for every tracked SMTP executor future, and closes the session. The 15-second bound covers one real ten-second SMTP attempt plus teardown; it does not pretend a running thread can be cancelled in five seconds.

- [ ] **Step 1: Write failing queue, ordering, overload, and retry tests**

Use injected senders/sleeper/clock. Assert pending never exceeds 100 under a 1,000-event storm, channel concurrency is 1, global concurrency≤4, same-channel order is stable, keyed operational updates replace pending content without growing the queue, and unkeyed overflow returns false/increments a counter without a new task.

```python
accepted = [
    dispatcher.enqueue('email', str(index), 'body', 'text')
    for index in range(1_000)
]
assert sum(accepted) == 100
assert dispatcher.pending_count == 100
assert dispatcher.dropped_count == 900
assert dispatcher.owned_task_count <= 6
```

Cover attempts `[1]` for 400/401/403/configuration errors and `[1, 2, 3]` for 429, 5xx, `aiohttp.ClientError`, timeout, and transient SMTP errors. Advance the fake deadline to prove total delivery≤60 seconds. Enqueue before start and assert pending grows while owned task count stays zero; start then drains it. Assert close drains accepted items when possible, cancels/awaits every owned asyncio worker, and waits for every tracked SMTP future.

- [ ] **Step 2: Write failing provider and operational-loop tests**

Use a loopback aiohttp server to prove every HTTP provider reuses the injected session, applies ten-second timeout, and never enables a cookie jar. Assert Pushplus target is `https://www.pushplus.plus/send`. Patch `smtplib.SMTP_SSL/SMTP` and inject a monotonic clock: every connect/STARTTLS/login/send phase receives only the remaining part of one ten-second attempt; after the budget expires, fallback or the next phase is not started. Block an executor future and assert close waits for it instead of reporting a false five-second cancellation.

In `OperationalNotificationCenter`, block the sender and prove `report()` returns after state persistence/enqueue rather than delaying the upload broad loop. Repeating the same unhealthy state still enqueues zero; a changed state uses the latest-wins key.

In `tests/notification/test_notifiers.py`, prove `MessageNotifier` only calls `enqueue`: patch `asyncio.create_task` to fail and assert no detached task is created. In `tests/web/test_main_lifecycle.py`, record real startup order and assert `dispatcher.start < bili_runtime.start < app.launch`; also verify partial-start failure closes the already-started dispatcher exactly once.

- [ ] **Step 3: Run the red tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/notification/test_dispatcher.py tests/notification/test_providers.py tests/notification/test_notifiers.py tests/notification/test_operational.py tests/test_application_outbound_lifecycle.py tests/web/test_main_lifecycle.py -q`

Expected: dispatcher/lifecycle APIs are missing, notifiers and operational scans await or detach direct sends, providers allocate sessions per message, and SMTP/Pushplus violate the budget.

- [ ] **Step 4: Implement the bounded per-channel scheduler**

Use one deque per channel plus a global pending count; the only tasks created are the finite active-channel workers tracked in `_workers`. A global semaphore limits active sends to four.

```python
def enqueue(
    self,
    channel: str,
    title: str,
    content: str,
    message_type: str,
    *,
    coalesce_key: Optional[Tuple[str, ...]] = None,
) -> bool:
    if self._closing:
        return False
    delivery = Delivery(
        channel=channel,
        title=title,
        content=content,
        message_type=message_type,
        coalesce_key=coalesce_key,
        deadline_at=self._monotonic() + self._delivery_timeout_seconds,
    )
    if coalesce_key is not None and self._replace_pending(coalesce_key, delivery):
        return True
    if self._pending_count >= 100:
        self._dropped_count += 1
        return False
    self._queues[channel].append(delivery)
    self._pending_count += 1
    if self._started:
        self._ensure_channel_worker(channel)
    return True
```

`start()` creates the shared session, marks `_started`, and starts workers for channels that were queued before startup. Workers decrement pending in `finally`, call `_deliver` under the global semaphore, and remove themselves by identity. `_deliver` checks remaining deadline before each attempt and injects jittered delays no greater than one and two seconds. Log channel name/error class/counters only—never title, content, provider key, token, email, or endpoint.

- [ ] **Step 5: Pool transports and route both producers through the dispatcher**

Give HTTP providers an injected/shared session and make `send_message` one attempt only. Configure the dispatcher session with `DummyCookieJar`, `raise_for_status=True`, and explicit timeout fields. SMTP uses one synchronous `send_with_deadline(deadline_at)` function in the executor: calculate `remaining=max(0, deadline_at-monotonic())` before constructor, fallback, STARTTLS, login, and send; pass `min(remaining, 10)` to constructors and update the live socket timeout before later phases. If no budget remains, abort before starting the phase. Track the executor future in a dispatcher-owned set; an asyncio timeout may stop awaiting the result but must not discard or falsely cancel the thread.

Inject the dispatcher into `MessageNotifier`; `_send_message` calls `enqueue` and has no `asyncio.create_task`. Change `OperationalNotificationCenter._dispatch` to enqueue with `(event, object_key, channel)` after the SQLite state transition. In `web/main.py`, construct the dispatcher once, pass its channel adapters to `BiliAccountRuntime`, and pass the dispatcher into `Application`, so legacy and operational paths share capacity rather than each owning a queue.

- [ ] **Step 6: Start and stop delivery once in the real web lifecycle**

Construct the dispatcher once in `web/main.py`, inject its channel adapters into `BiliAccountRuntime`, and pass it to `Application` only as a producer dependency. `on_startup` must `await dispatcher.start()` before `await _bili_account_runtime.start()`; `Application.launch()` only enables legacy notifier producers and does not start a second owner. On shutdown, `app.exit()` disables legacy subscriptions, runtime close disables operational producers, then `dispatcher.close(15)` runs last. Mirror that order in every partial-start failure branch. Preserve restart support by opening a fresh session/workers on the next `start()`.

- [ ] **Step 7: Verify bounded delivery, lifecycle, and static checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/notification/test_dispatcher.py tests/notification/test_providers.py tests/notification/test_notifiers.py tests/notification/test_operational.py tests/setting/test_operational_notifications.py tests/test_application_outbound_lifecycle.py tests/web/test_main_lifecycle.py -q
.venv/bin/python -m black --check src/blrec/notification/dispatcher.py src/blrec/notification/providers.py src/blrec/notification/notifiers.py src/blrec/notification/operational.py src/blrec/application.py src/blrec/web/main.py tests/notification/test_dispatcher.py tests/notification/test_providers.py tests/notification/test_notifiers.py tests/notification/test_operational.py tests/test_application_outbound_lifecycle.py tests/web/test_main_lifecycle.py
.venv/bin/python -m isort --check-only src/blrec/notification/dispatcher.py src/blrec/notification/providers.py src/blrec/notification/notifiers.py src/blrec/notification/operational.py src/blrec/application.py src/blrec/web/main.py
.venv/bin/python -m flake8 src/blrec/notification/dispatcher.py src/blrec/notification/providers.py src/blrec/notification/notifiers.py src/blrec/notification/operational.py src/blrec/application.py src/blrec/web/main.py
.venv/bin/python -m mypy src/blrec/notification src/blrec/application.py src/blrec/web/main.py
```

**Budget and invariants:** pending≤100; global active channels≤4; per-channel concurrency=1; request/SMTP attempt≤10 seconds; delivery attempts≤3 and deadline≤60 seconds; shutdown drain≤15 seconds and every SMTP future is observed. Pre-start workers=0; startup owner count=1. Operational state-transition suppression remains authoritative. No notification failure blocks or accelerates the upload broad loop, and no overload path spawns a side-channel task. Production/test file budget: 6/6.

- [ ] **Step 8: Commit the notification dispatcher**

```bash
git add src/blrec/notification/dispatcher.py src/blrec/notification/providers.py src/blrec/notification/notifiers.py src/blrec/notification/operational.py src/blrec/application.py src/blrec/web/main.py tests/notification/test_dispatcher.py tests/notification/test_providers.py tests/notification/test_notifiers.py tests/notification/test_operational.py tests/test_application_outbound_lifecycle.py tests/web/test_main_lifecycle.py
git commit -m "perf: bound notification delivery"
```

---

### Task 12: Replace detached webhook sends with a bounded ordered worker (O-13, P1)

**Files:**
- Modify: `src/blrec/webhook/webhook_emitter.py`
- Modify: `src/blrec/application.py`
- Create: `tests/webhook/test_webhook_emitter.py`
- Modify: `tests/test_application_outbound_lifecycle.py`

**Interfaces:**
- `WebHookEmitter.start()` owns one cookie-less shared session.
- `_send_request` becomes a non-blocking enqueue into a global capacity-100 queue.
- Global delivery concurrency≤4 and the same URL has concurrency 1 with stable order.
- Each delivery has injected request timeout≤10 seconds and total deadline≤60 seconds; tests pass `0.01`. Attempts≤3 and only 429/5xx/transport failures retry.
- `close(drain_timeout_seconds=5)` uses an injected short test budget, disables intake, drains/cancels/awaits, and closes its session. Metrics/logs redact the URL and payload.

- [ ] **Step 1: Write failing storm, ordering, retry, and close tests**

Configure 50 webhooks and emit enough matching events/exceptions to exceed capacity. Assert no more than 100 pending, no more than four active destinations, same-URL order, rejected-newest count, and a bounded owned-task count. Inject `request_timeout_seconds=0.01`, `delivery_timeout_seconds=0.03`, and `drain_timeout_seconds=0.01`; use loopback responses to prove 4xx attempts=1 and 429/5xx/connection timeout attempts≤3 without sleeping through production budgets. Assert an event payload is delivered unchanged but never appears in logs.

Close while requests are blocked and assert all tasks are awaited/cancelled, the session is closed once, and a post-close event is rejected without warning that contains the concrete URL.

- [ ] **Step 2: Run the red tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/webhook/test_webhook_emitter.py tests/test_application_outbound_lifecycle.py -q`

Expected: every delivery creates a detached task/session, the 180-second retry loop treats permanent failures as transient, and there is no drainable lifecycle.

- [ ] **Step 3: Implement a webhook-specific bounded scheduler**

Keep this implementation local rather than coupling its reject-newest semantics to Notification's latest-wins queue. Maintain per-URL deques, a global pending count, a semaphore of four, and one tracked worker per active URL. `_post` uses the shared session and one attempt. Retry classification is:

```python
def _is_transient(error: BaseException) -> bool:
    if isinstance(error, aiohttp.ClientResponseError):
        return error.status == 429 or error.status >= 500
    return isinstance(error, (aiohttp.ClientError, asyncio.TimeoutError, OSError))
```

Remove tenacity's 180-second window. Use injected sleeper/jitter for delays no greater than one/two seconds, stop at attempt 3 or the injected delivery deadline (default 60 seconds), and never log URL/payload/headers.

- [ ] **Step 4: Integrate Application start/disable/drain order**

`_setup_webhooks` may subscribe after its queue object exists, but `Application.launch()` must await `start()` before reporting launched. `_exit` disables EventCenter/ExceptionCenter subscriptions before `close(5)`, aggregates errors with other async teardown, then deletes the emitter. Restart creates a fresh session and empty scheduler.

- [ ] **Step 5: Verify lifecycle and static checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/webhook/test_webhook_emitter.py tests/test_application_outbound_lifecycle.py -q
.venv/bin/python -m black --check src/blrec/webhook/webhook_emitter.py src/blrec/application.py tests/webhook/test_webhook_emitter.py tests/test_application_outbound_lifecycle.py
.venv/bin/python -m isort --check-only src/blrec/webhook/webhook_emitter.py src/blrec/application.py tests/webhook/test_webhook_emitter.py tests/test_application_outbound_lifecycle.py
.venv/bin/python -m flake8 src/blrec/webhook/webhook_emitter.py src/blrec/application.py tests/webhook/test_webhook_emitter.py tests/test_application_outbound_lifecycle.py
.venv/bin/python -m mypy src/blrec/webhook/webhook_emitter.py src/blrec/application.py
```

**Budget and invariants:** pending≤100; global concurrency≤4; per URL concurrency=1; request≤10 seconds; attempts≤3; delivery≤60 seconds; shutdown drain≤5 seconds. Queue full rejects newest and records only redacted counters. No automatic request frequency or alternative outbound target is introduced. Production/test file budget: 2/2.

- [ ] **Step 6: Commit the webhook worker**

```bash
git add src/blrec/webhook/webhook_emitter.py src/blrec/application.py tests/webhook/test_webhook_emitter.py tests/test_application_outbound_lifecycle.py
git commit -m "perf: bound webhook delivery"
```

---

### Task 13: Cache Update metadata with one lifecycle client (O-14, P2)

**Files:**
- Modify: `src/blrec/update/helpers.py`
- Modify: `src/blrec/update/api.py`
- Modify: `src/blrec/application.py`
- Modify: `src/blrec/web/routers/update.py`
- Create: `tests/update/test_helpers.py`
- Create: `tests/web/test_update_routes.py`
- Modify: `tests/test_application_outbound_lifecycle.py`

**Interfaces:**
- `UpdateMetadataClient` owns one cookie-less session, a cache keyed by `('project', project_name)` or `('release', project_name, version)`, 30-minute freshness, 24-hour stale fallback, and per-key single-flight.
- A refresh makes one PyPI request with an injected `request_timeout_seconds=10` absolute deadline; tests pass `0.01`. A 404 is a cacheable `None`, while an error may return only a non-expired stale value.
- This task owns no Bilibili transport or cookie validation behavior.

- [ ] **Step 1: Write failing update cache and lifecycle tests**

Use injected monotonic clock, `request_timeout_seconds=0.01`, and a blocked fake response. Assert 20 concurrent calls for one key make one request, calls within 1,800 seconds are local, expiry makes one refresh, error returns stale only until 86,400 seconds, 404 is cached, project/release keys cannot collide, one waiter cancellation does not cancel shared work, and close cancels/awaits owned tasks and closes once.

```python
versions = await asyncio.gather(
    *(client.get_latest_version_string('blrec') for _ in range(20))
)
assert versions == ['2.0.0'] * 20
assert pypi_requests == 1
```

Route tests assert timeout/error returns stale or `''` promptly and never exposes an exception body. Do not monkeypatch global `asyncio.wait_for`.

- [ ] **Step 2: Run the focused red tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/update/test_helpers.py tests/web/test_update_routes.py tests/test_application_outbound_lifecycle.py -q
```

Expected: every call allocates a session, and update has no cache/single-flight/stale result or lifecycle close.

- [ ] **Step 3: Implement the update-only lifecycle client**

Remove the tenacity decorator from `PypiApi._get`; the cache boundary performs one request per refresh generation and returns stale on failure instead of adding burst retries.

```python
class UpdateMetadataClient:
    FRESH_SECONDS = 30 * 60
    STALE_SECONDS = 24 * 60 * 60

    async def get_latest_version_string(self, project_name: str) -> Optional[str]:
        metadata = await self._get(
            ('project', project_name),
            lambda api: api.get_project_metadata(project_name),
        )
        return None if metadata is None else metadata['info']['version']
```

The session uses `DummyCookieJar` and explicit `ClientTimeout` fields capped by the injected logical budget. Single-flight uses shield and identity cleanup; cache only validated metadata/`None`, not exception objects. Preserve `project_name` and `version` in internal keys but never log arbitrary input.

- [ ] **Step 4: Wire and close only the update resource**

The update router uses the application-owned client. Application startup creates/starts it before serving the route; exit stops intake, cancels/awaits in-flight refresh tasks, then closes the owned session once. Restart constructs a fresh session and empty in-flight map while retaining no process-global cache.

- [ ] **Step 5: Verify cache and static checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/update/test_helpers.py tests/web/test_update_routes.py tests/test_application_outbound_lifecycle.py -q
.venv/bin/python -m black --check src/blrec/update/helpers.py src/blrec/update/api.py src/blrec/application.py src/blrec/web/routers/update.py tests/update/test_helpers.py tests/web/test_update_routes.py tests/test_application_outbound_lifecycle.py
.venv/bin/python -m isort --check-only src/blrec/update/helpers.py src/blrec/update/api.py src/blrec/application.py src/blrec/web/routers/update.py
.venv/bin/python -m flake8 src/blrec/update/helpers.py src/blrec/update/api.py src/blrec/application.py src/blrec/web/routers/update.py
.venv/bin/python -m mypy src/blrec/update src/blrec/application.py src/blrec/web/routers/update.py
```

**Budget and invariants:** update request≤1 per project/30 minutes, in-flight per key=1, stale≤24 hours, refresh deadline≤10 seconds. No Bilibili request or credential path changes in this commit. Production/test file budget: 4/3.

- [ ] **Step 6: Commit the update client**

```bash
git add src/blrec/update/helpers.py src/blrec/update/api.py src/blrec/application.py src/blrec/web/routers/update.py tests/update/test_helpers.py tests/web/test_update_routes.py tests/test_application_outbound_lifecycle.py
git commit -m "perf: cache update metadata"
```

---

### Task 14: Pool cookie validation without caching credentials (O-15, P2)

**Files:**
- Modify: `src/blrec/bili/helpers.py`
- Modify: `src/blrec/application.py`
- Modify: `src/blrec/web/routers/validation.py`
- Create: `tests/bili/test_helpers.py`
- Create: `tests/web/test_validation_routes.py`
- Modify: `tests/test_application_outbound_lifecycle.py`

**Interfaces:**
- `get_nav(cookie, session)` requires an application-owned anonymous Bilibili session and sends the cookie only in that request's explicit header.
- `Application.validate_bili_cookie(cookie)` has injected `validation_timeout_seconds=10` (tests pass `0.01`) and cache TTL zero.
- The request explicitly enables HTTP status raising even when the shared anonymous pool defaults to `raise_for_status=False`; 401/429/500 preserve the old helper/route error mapping.

- [ ] **Step 1: Write failing transport, HTTP-semantics, and privacy tests**

Make two sequential validations with distinct cookie sentinel strings. Assert the same connector/session is reused, the jar remains empty, upstream calls=2, and each request receives only its own explicit `Cookie` value. Use loopback 401, 429, and 500 responses and assert the same exception/route status mapping as the old dedicated `ClientSession(raise_for_status=True)`; no JSON success path may swallow these statuses.

Inject `validation_timeout_seconds=0.01`, block the response, and assert the logical request is cancelled promptly. Capture logs, exceptions, and request metrics and assert neither sentinel occurs. Do not monkeypatch global `asyncio.wait_for`.

- [ ] **Step 2: Run the focused red tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili/test_helpers.py tests/web/test_validation_routes.py tests/test_application_outbound_lifecycle.py -q
```

Expected: each validation allocates a session, no whole-operation deadline exists, and directly swapping to the anonymous pool would change 401/429/500 handling.

- [ ] **Step 3: Use the anonymous pool with a request-level status override**

Change `get_nav` to accept a session and remove its local session block. Extend only the nav request boundary as needed so its underlying aiohttp request passes `raise_for_status=True`; do not change the anonymous pool default used by other callers.

```python
async def get_nav(cookie: str, session: Any) -> ResponseData:
    headers = {
        'Origin': 'https://passport.bilibili.com',
        'Referer': 'https://passport.bilibili.com/account/security',
        'Cookie': cookie,
    }
    return await WebApi(session, headers).get_nav(raise_for_status=True)
```

Application obtains `network_session_pool.client('bili_api', anonymous=True)` when routing is configured; otherwise it lazily owns a `DummyCookieJar` fallback session. Wrap only this idempotent validation read with `asyncio.wait_for(..., timeout=self._validation_timeout_seconds)`. Do not add a cookie hash, cache, or single-flight: two concurrent user validations remain two logical requests under the existing connector bound.

- [ ] **Step 4: Preserve lifecycle ownership**

The validation router calls the application method already injected through `web/main.py`. Exit closes only the owned fallback session; the network pool remains closed by its existing owner. Restart must not reuse the old fallback session or any credential state.

- [ ] **Step 5: Verify HTTP semantics, privacy, and static checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili/test_helpers.py tests/web/test_validation_routes.py tests/test_application_outbound_lifecycle.py -q
.venv/bin/python -m black --check src/blrec/bili/helpers.py src/blrec/application.py src/blrec/web/routers/validation.py tests/bili/test_helpers.py tests/web/test_validation_routes.py tests/test_application_outbound_lifecycle.py
.venv/bin/python -m isort --check-only src/blrec/bili/helpers.py src/blrec/application.py src/blrec/web/routers/validation.py
.venv/bin/python -m flake8 src/blrec/bili/helpers.py src/blrec/application.py src/blrec/web/routers/validation.py
.venv/bin/python -m mypy src/blrec/bili/helpers.py src/blrec/application.py src/blrec/web/routers/validation.py
```

**Budget and invariants:** validation success=1 nav request per user action, logical deadline≤10 seconds, cache TTL=0, jar persistence=0, and secret-bearing log/metric fields=0. HTTP 401/429/500 mapping is byte-for-byte compatible at the route response boundary. The anonymous Bilibili session keeps existing network-purpose selection; no upload route or cookie-bearing account session is reused. Production/test file budget: 3/3.

- [ ] **Step 6: Commit the validation transport separately**

```bash
git add src/blrec/bili/helpers.py src/blrec/application.py src/blrec/web/routers/validation.py tests/bili/test_helpers.py tests/web/test_validation_routes.py tests/test_application_outbound_lifecycle.py
git commit -m "perf: pool cookie validation transport"
```

---

### Task 15: Run the cross-group request audit and update the ledger

**Files:**
- Modify: `docs/performance/request-audit.md`
- Test only: all focused suites named below; no production file is changed in this task.

**Purpose:** This is the release gate for all 18 outbound groups and all 20 inbound triggers. It corrects the stale QR/Review findings in the ledger and records measured fake/loopback request budgets. It is not permission to tune cadence against production or the NAS.

- [ ] **Step 1: Run the non-negotiable P0 fence suite first**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_upos.py tests/bili_upload/test_task_actions.py tests/bili_upload/test_danmaku_publish.py tests/bili_upload/test_upload.py -q
```

Required evidence: completion calls after unknown/interrupted=0; danmaku posts after unknown/in-flight=0; submission remains read-reconciled and never blind resubmitted.

- [ ] **Step 2: Run all changed outbound request-budget suites**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili/test_live_info_refresh.py tests/task/test_task_manager_outbound.py tests/control/test_operations.py tests/web/test_tasks_routes.py tests/web/test_browser_extension_routes.py tests/bili/test_live_stream_url.py tests/core/test_stream_request_reuse.py tests/core/test_hls_integrity_guards.py tests/bili_upload/test_protocol_matrix.py tests/bili_upload/test_archive_reads.py tests/bili_upload/test_review.py tests/bili_upload/test_collections.py tests/bili_upload/test_collection_publish.py tests/bili_upload/test_covers.py tests/core/test_cover_downloader.py tests/bili_upload/test_accounts.py tests/notification/test_dispatcher.py tests/notification/test_providers.py tests/notification/test_notifiers.py tests/notification/test_operational.py tests/web/test_main_lifecycle.py tests/webhook/test_webhook_emitter.py tests/update/test_helpers.py tests/web/test_update_routes.py tests/bili/test_helpers.py tests/web/test_validation_routes.py tests/test_application_outbound_lifecycle.py -q
```

- [ ] **Step 3: Rerun preserved-group guards**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili/test_live_status_coordinator.py tests/integration/test_batch_live_monitor.py tests/task/test_live_connection_controller.py tests/bili/test_danmaku_client.py tests/bili/test_danmaku_connection.py tests/bili_upload/test_categories.py tests/bili_upload/test_comments.py tests/bili_upload/test_session_submission.py tests/bili_upload/test_submission_verifier.py tests/web/test_network_routes.py tests/networking/test_network_routing.py tests/networking/test_upload_rate_limit.py -q
```

Required evidence: 58 rooms still require at most two sequential batches; no shorter status/stream/QR/review/comment/danmaku cadence; category normal/forced concurrent callers are single-flight without changing 24-hour TTL; network probing stays explicit; HLS/UPOS route/limit guards pass.

- [ ] **Step 4: Run complete backend and frontend verification**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/python -m black --check src tests
.venv/bin/python -m isort --check-only src tests
.venv/bin/python -m flake8 src
.venv/bin/python -m mypy src/blrec
(cd webapp && npm test -- --watch=false --browsers=ChromeHeadless)
(cd webapp && npx tsc --noEmit -p tsconfig.app.json)
(cd webapp && npx ng lint)
(cd webapp && npm run build)
```

The lint command may exit 1 only with the five baseline file/rule pairs listed above; compare exact output and reject any new warning/error. Every other command exits 0.

- [ ] **Step 5: Update the ledger from verified evidence**

In `docs/performance/request-audit.md`:

- Replace the I-057 finding with “status is local; one background poller is created per active subject/session.”
- Replace the Review finding with “already grouped per account; shared archive page/detail snapshots remove cross-consumer duplication.”
- Record Tasks 1–14's exact call-count/deadline/concurrency results beside the affected 18 group rows and 20 inbound trigger rows.
- Mark Room status, Danmaku WS, Comments, Categories core semantics, Network probe, HLS integrity, UPOS route/admission, and all remote-unknown fences as retained—not removed or sped up.
- Cite test file/test name and fake/loopback fixture; do not claim NAS or live-Bilibili latency measurements.

- [ ] **Step 6: Mechanically verify coverage and repository hygiene**

Run:

```bash
git ls-files --error-unmatch docs/performance/outbound-request-audit.md
test "$(rg -c '^### Task [0-9]+:' docs/superpowers/plans/2026-07-20-outbound-request-performance.md)" -eq 15
test "$(rg -c '^\| O-[0-9]{2}' docs/superpowers/plans/2026-07-20-outbound-request-performance.md)" -eq 14
for id in $(seq -w 1 15); do rg -q "O-$id" docs/superpowers/plans/2026-07-20-outbound-request-performance.md; done
for id in 011 018 019 022 023 026 027 030 036 042 045 056 057 059 076 078 083 084 099 102; do rg -q "I-$id" docs/superpowers/plans/2026-07-20-outbound-request-performance.md; done
test "$(rg -c '^\| [0-9]+ \| [A-Z]' docs/superpowers/plans/2026-07-20-outbound-request-performance.md)" -eq 18
rg -q '^\| I-104 \| GET \| `/api/v1/recording-sessions/\{session_id\}`' docs/performance/request-audit.md
rg -q '^\| I-105 \| GET \| `/api/v1/highlights/sessions/\{session_id\}/marker-counts`' docs/performance/request-audit.md
test "$(rg -c '^\| I-[0-9]{3} \|' docs/performance/request-audit.md)" -ge 105
! rg -n 'T[B]D|TO-[D]O|implement[ ]later|same[ ]as[ ]above|similar[ ]to' docs/superpowers/plans/2026-07-20-outbound-request-performance.md
rg -q "target-version = \['py38'\]" pyproject.toml
! rg -n 'asyncio\.(timeout|TaskGroup)|except\s*\*' src/blrec tests
git diff --check
git status --short
```

The O-table has 14 rows because only O-03/O-04 are deliberately paired; the loop proves all 15 IDs are present. The route ledger may exceed 105 after the prerequisite Write/media plan, but I-104 and I-105 must retain their current meanings and this plan must not allocate another inbound ID. Black runs with the repository's `py38` target, the mechanical guard rejects 3.11-only asyncio/exception syntax, and the normal CI/runtime matrix must still run on Python 3.8. Before committing, inspect status and stage only the ledger.

**Budget and invariants:** this task makes zero outbound calls except loopback fakes and zero production-code changes. Completion requires 18/18 groups and 20/20 triggers accounted for, no higher cadence/concurrency, no upload-line change, no new lint errors, no leaked sessions/tasks, and no weakened unknown-outcome fence.

- [ ] **Step 7: Commit the verified ledger only**

```bash
git add docs/performance/request-audit.md
git commit -m "docs: record outbound request performance results"
```

---

## Final Preservation Checklist

Before calling the implementation complete, answer every item with a test name and observed value:

- [ ] UPOS completion unknown/interrupted and danmaku post unknown/in-flight each issue zero automatic follow-up writes.
- [ ] Submission, collection creation/publication, comment/pin, edit/repair, and account renewal retain their present unknown/manual-reconciliation fences.
- [ ] Room status remains default 30 seconds within 30–60, batch≤29 and sequential; stream probes remain one second; QR remains one second/180 seconds; Review remains 900 seconds; Comments remain one item plus five-second delay; danmaku remains ≥25 seconds and one line.
- [ ] UPOS remains chunk concurrency default 2/max 3, attempts≤3, admission 1→5/minute, cooldown≤15 minutes, and fixed source/route for an upload.
- [ ] HLS init-section agreement, segment integrity, real-transfer retry/rotation, cover trust/size/redirect rules, Categories 24-hour cache, and explicit-only Network probe all remain.
- [ ] Every new cache/single-flight key contains the complete identity: account and credential version for Bilibili account data; query/page for archives; project/release for PyPI; broadcast+URL or source fingerprint for covers. Cookie validation has no cache key at all.
- [ ] Every lifecycle-owned task/session has a close test; one caller's cancellation cannot cancel shared read work; failed/cancelled work is not cached.
- [ ] No test, log, audit field, exception, or ledger example contains a real cookie/token/account ID/webhook URL/local media path.

## Implementation Completion Criteria

The plan is complete only when all fifteen task commits exist in order, all verification commands pass under the documented lint baseline, `docs/performance/request-audit.md` contains evidence for all 18 groups and 20 triggers, `docs/performance/outbound-request-audit.md` is tracked, and `git status --short` contains no unreviewed files from this work.
