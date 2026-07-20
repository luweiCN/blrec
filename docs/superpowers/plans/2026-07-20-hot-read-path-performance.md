# Hot Read Path Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make recording, upload-policy, retention, and highlight read paths scale with one page rather than one query or filesystem check per row, while loading only the Angular code and data needed by the active view.

**Architecture:** Keep FastAPI, SQLite, and the existing Angular routes, but split list summaries from detail projections. Build list responses from bulk SQL aggregates, keep paths and diagnostic collections behind detail endpoints, replace timeline-for-counts with a count projection, and add only indexes justified by the final query plans. In Angular, cancel stale list requests, render rows as OnPush components, and isolate the editor and clip library in genuinely lazy bundles.

**Tech Stack:** Python 3.9, FastAPI, SQLite, pytest, Angular 15, RxJS, Jasmine/Karma.

---

## Constraints and budgets

- Do not use git worktrees.
- Do not change upload, review, comment, danmaku, room-status, or stream polling frequency.
- Do not expose local media paths, credentials, query values, or concrete account identifiers in logs or performance fixtures.
- Preserve existing list filters, sort order, display states, available actions, realtime progress merge behavior, detail drawers, and route URLs.
- Preserve full recording/upload diagnostics in the detail response; the list deliberately omits paths, per-part upload rows, unknown danmaku items, policy JSON, and submission-verification JSON.
- Write the failing test before each behavior change. Do not add FTS while the `%LIKE%` search remains within budget.
- A 20-row recording/upload list uses at most two business-database calls (count plus summary query), performs zero `exists`, `stat`, `getsize`, or directory calls, and has warm NAS p95 below 150 ms.
- Policy list query count is constant and at most one business-database call.
- Retention status uses one aggregate database call and zero per-recording filesystem calls; warm p95 is below 100 ms.
- Highlight marker counts use at most two database calls and no full timeline/path projection; warm p95 is below 100 ms.
- Ordinary detail GETs remain below 100 ms excluding media/probe work.
- Filtering, pagination, and explicit refresh have one active HTTP request; an older response cannot overwrite newer criteria.
- A realtime update for one upload job changes one row input identity and does not rebuild the other 19 row view models.
- The production `upload-tasks` list chunk is at most 70 KiB estimated transfer size. The highlight editor and clip library must have separate lazy chunks and must not be preloaded on first entry.
- Full `ng lint` must introduce no errors beyond the five errors already present at `57361f7`; every changed/new frontend file must pass targeted ESLint.

## File map

- `src/blrec/bili_upload/journal.py`: typed recording/upload summary projections and the existing full detail projection.
- `src/blrec/web/routers/recording_sessions.py`: summary response, new detail endpoint, and response mapping.
- `src/blrec/bili_upload/policies.py`: one-query policy/account resolution.
- `src/blrec/bili_upload/retention.py`: persisted-size aggregate for status.
- `src/blrec/bili_upload/highlights.py`: lightweight per-part marker counts.
- `src/blrec/web/routers/highlights.py`: marker-count response endpoint.
- `src/blrec/bili_upload/migrations/0024_initial.sql`: read-path indexes proven by `EXPLAIN QUERY PLAN`.
- `src/blrec/bili_upload/database.py`: migration version 24.
- `tests/bili_upload/test_journal.py`, `tests/web/test_recording_sessions_routes.py`: list budgets and summary/detail contracts.
- `tests/bili_upload/test_policies.py`: policy list query budget.
- `tests/bili_upload/test_retention.py`, `tests/web/test_recording_retention_routes.py`: status aggregate and route contract.
- `tests/bili_upload/test_highlights.py`, `tests/web/test_highlights_routes.py`: marker-count correctness and budget.
- `tests/bili_upload/test_database.py`: migration/index and query-plan evidence.
- `webapp/src/app/upload-tasks/shared/recording-session.model.ts`: summary/detail TypeScript contracts.
- `webapp/src/app/upload-tasks/shared/recording-session.service.ts`: list/detail requests.
- `webapp/src/app/upload-tasks/shared/highlight.model.ts`, `highlight.service.ts`: marker-count contract and request.
- `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.*`: cancellable list/detail orchestration and OnPush parent.
- `webapp/src/app/upload-tasks/recording-sessions/recording-session-row.component.*`: isolated OnPush row rendering.
- `webapp/src/app/upload-tasks/clip-library/clip-library.module.ts`, `clip-library-routing.module.ts`: clip-library lazy boundary.
- `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.module.ts`, `highlight-editor-routing.module.ts`: editor lazy boundary.
- `webapp/src/app/upload-tasks/part-video-dialog/part-player.loader.ts`: open-action dynamic boundary for the FLV runtime.
- `webapp/src/app/upload-tasks/upload-tasks.module.ts`, `upload-tasks-routing.module.ts`, `webapp/src/app/app-routing.module.ts`: lightweight list module and route loading policy.

### Task 1: Recording and upload list summaries

**Files:**
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `tests/bili_upload/test_journal.py`
- Modify: `tests/web/test_recording_sessions_routes.py`

- [ ] **Step 1: Write a NAS-shaped failing summary budget test**

Seed 20 sessions with multiple recording parts, upload parts, chunks, and danmaku rows. Wrap `database.scalar` and `database.fetchall`, and monkeypatch `os.path.exists`, `os.path.getsize`, and `Path.stat` to fail if the list calls them.

```python
summaries = await journal.list_session_summaries(
    limit=20,
    offset=0,
    scope='uploads',
    sort_order='newest',
)
assert len(summaries) == 20
assert database_calls == 1  # list query; route count is the second call
assert filesystem_calls == []
assert summaries[0].upload_job is not None
assert not hasattr(summaries[0].upload_job, 'parts')
assert not hasattr(summaries[0].upload_job, 'unknown_danmaku_items')
```

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_journal.py -k 'session_summary or list_session_summary' -q
```

Expected: FAIL because `list_session_summaries` and summary types do not exist.

- [ ] **Step 2: Define the exact summary contracts**

Add immutable `RecordingSessionSummary` and `UploadJobSummary` models. The session summary contains the scalar fields used by list rows: identity, state/time/title/cover/anchor/area names, aggregate part/danmaku/size/duration, upload intent/decision/resolution/suppression, deletion state, source kind, and highlight link. The upload summary contains every scalar state/progress/action field currently rendered by the row, including account display name, submission/branch/repair state, remote archive identifiers, aggregate bytes/parts/danmaku counts, errors, and action booleans.

Do not put these detail-only fields in either summary:

```python
SUMMARY_FORBIDDEN_FIELDS = frozenset(
    {
        'broadcast_session_key',
        'cover_path',
        'source_path',
        'final_path',
        'xml_path',
        'parts',
        'unknown_danmaku_items',
        'policy_snapshot_json',
        'submission_verification',
    }
)
```

- [ ] **Step 3: Implement one bulk summary query**

Use CTEs grouped independently by `session_id` or `job_id` so joining them cannot multiply chunks by danmaku rows. The final query returns one row per session and calculates only aggregate counts/bytes. Keep `count_sessions()` separate, so the route has exactly two database calls.

Required query shape:

```sql
WITH part_summary AS (
    SELECT session_id,
           COUNT(*) AS part_count,
           COALESCE(SUM(danmaku_count), 0) AS danmaku_count,
           COALESCE(SUM(file_size_bytes), 0) AS total_file_size_bytes,
           COALESCE(SUM(record_duration_seconds), 0) AS record_duration_seconds
    FROM recording_parts
    GROUP BY session_id
),
chunk_summary AS (
    SELECT part.job_id,
           COALESCE(SUM(chunk.size), 0) AS total_bytes,
           COALESCE(SUM(CASE WHEN chunk.state='confirmed' THEN chunk.size ELSE 0 END), 0)
               AS confirmed_bytes,
           COUNT(DISTINCT part.id) AS discovered_part_count,
           COUNT(DISTINCT CASE WHEN part.upload_state='confirmed' THEN part.id END)
               AS confirmed_part_count
    FROM upload_parts part
    LEFT JOIN upload_chunks chunk ON chunk.part_id=part.id
    GROUP BY part.job_id
),
danmaku_summary AS (
    SELECT part.job_id,
           COUNT(*) AS total,
           SUM(CASE WHEN item.state='confirmed' THEN 1 ELSE 0 END) AS confirmed,
           SUM(CASE WHEN item.state IN ('prepared','in_flight') THEN 1 ELSE 0 END)
               AS pending,
           SUM(CASE WHEN item.state='unknown_outcome' THEN 1 ELSE 0 END) AS unknown_count,
           SUM(CASE WHEN item.state='failed_permanent' THEN 1 ELSE 0 END) AS failed
    FROM danmaku_items item
    JOIN upload_parts part ON part.id=item.part_id
    GROUP BY part.job_id
)
```

After the CTEs, the final `SELECT` must name every scalar field in
`RecordingSessionSummary` and `UploadJobSummary` explicitly. Join
`recording_sessions` to `upload_jobs`, `bili_accounts`, `upload_suppressions`,
`room_upload_policies`, `highlight_clips`, and the three CTEs. Reuse
`_session_filters()` verbatim, then apply the existing stable `started_at,id`
order and bounded `LIMIT/OFFSET`. Do not use `SELECT *`.

Keep the existing `list_sessions()` and `upload_jobs_for_sessions()` as the full detail path; do not weaken their diagnostics.

- [ ] **Step 4: Split list and detail HTTP contracts**

Change `GET /api/v1/recording-sessions` to return
`RecordingSessionSummaryResponse` entries. Add
`RecordingJournalBridge.get_session(session_id)`: select the existing scalar
session projection by ID, raise `ValueError` if absent, and call
`parts_for_session(session_id)` only for this one detail. Then add this route
with the same manager dependency and authentication dependency used by the list:

```python
@router.get('/{session_id}', response_model=RecordingSessionResponse)
async def get_recording_session(
    session_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    recording_journal: RecordingJournalBridge = Depends(get_recording_journal),
) -> RecordingSessionResponse:
    session = await recording_journal.get_session(session_id)
    upload_jobs = await recording_journal.upload_jobs_for_sessions((session_id,))
    return _session_response(session, upload_jobs.get(session_id))
```

The detail route keeps `parts`, paths, unknown danmaku items, and submission verification. The list response must not serialize any field in `SUMMARY_FORBIDDEN_FIELDS`.

- [ ] **Step 5: Add route contract and database-call assertions**

```python
response = client.get('/api/v1/recording-sessions', headers=auth())
assert response.status_code == 200
item = response.json()['sessions'][0]
assert 'parts' not in item
assert 'coverPath' not in item
assert 'unknownDanmakuItems' not in str(item)

detail = client.get('/api/v1/recording-sessions/1', headers=auth())
assert detail.status_code == 200
assert 'parts' in detail.json()
```

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_journal.py tests/web/test_recording_sessions_routes.py -q
```

Expected: PASS; 20-row route uses two business-database calls and zero list-time filesystem calls.

- [ ] **Step 6: Commit**

```bash
git add src/blrec/bili_upload/journal.py src/blrec/web/routers/recording_sessions.py tests/bili_upload/test_journal.py tests/web/test_recording_sessions_routes.py
git commit -m "perf: add lightweight recording summaries"
```

### Task 2: Remove room-policy account N+1

**Files:**
- Modify: `src/blrec/bili_upload/policies.py`
- Modify: `tests/bili_upload/test_policies.py`

- [ ] **Step 1: Write the failing query-budget test**

Seed one primary policy and many fixed-account policies. Count database methods.

```python
policies = await manager.list()
assert len(policies) == policy_count
assert fetchall_calls == 1
assert fetchone_calls == 0
assert policies[0].resolved_account_name
```

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_policies.py -k list -q`

Expected: FAIL because `_view()` performs one account query per policy.

- [ ] **Step 2: Join both account modes in the list query**

Join the selected primary account once and the fixed account once, then resolve with `CASE`:

```sql
LEFT JOIN bili_account_selection selection ON selection.id=1
LEFT JOIN bili_accounts primary_account
       ON primary_account.id=selection.primary_account_id
LEFT JOIN bili_accounts fixed_account
       ON fixed_account.id=policy.account_id
```

Select `resolved_account_id`, `resolved_account_name`, and `resolved_account_state` with `CASE policy.account_mode`. Make `_view(row)` synchronous and consume those aliases. Keep `get()`/`upsert()` correct by using the same joined projection with `WHERE policy.room_id=?`; do not restore per-row lookups.

- [ ] **Step 3: Verify behavior and budget**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_policies.py tests/web/test_room_upload_policies_routes.py -q
```

Expected: PASS; list SQL count is one for 1, 20, and 100 policies.

- [ ] **Step 4: Commit**

```bash
git add src/blrec/bili_upload/policies.py tests/bili_upload/test_policies.py
git commit -m "perf: batch room policy account resolution"
```

### Task 3: Make retention status an aggregate read

**Files:**
- Modify: `src/blrec/bili_upload/retention.py`
- Modify: `tests/bili_upload/test_retention.py`
- Modify: `tests/web/test_recording_retention_routes.py`

- [ ] **Step 1: Write a failing no-filesystem status test**

Seed many live recording parts with persisted `file_size_bytes`, including deleted and highlight-source rows. Make path-size functions raise.

```python
status = await manager.status()
assert status.managed_video_bytes == expected_live_non_deleted_bytes
assert database_calls == 1
assert filesystem_calls == []
```

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_retention.py -k status -q`

Expected: FAIL because `_managed_video_bytes()` currently loads paths and calls `lstat` for each one.

- [ ] **Step 2: Replace request-time path scans with persisted aggregation**

Use this status query:

```sql
SELECT COALESCE(SUM(COALESCE(part.file_size_bytes, 0)), 0)
FROM recording_parts part
JOIN recording_sessions session ON session.id=part.session_id
WHERE part.video_deleted_at IS NULL
  AND session.source_kind='live'
```

Keep `_candidate_size()` and `_paths_size()` for destructive retention execution, where actual file size is required. Status is observational and uses journaled size; missing size is zero until the existing journal/index workers persist it.

- [ ] **Step 3: Verify route and deletion correctness**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_retention.py tests/web/test_recording_retention_routes.py -q
```

Expected: PASS; status performs one aggregate query and no path IO, while deletion/reclaim tests still use real file sizes in the worker path.

- [ ] **Step 4: Commit**

```bash
git add src/blrec/bili_upload/retention.py tests/bili_upload/test_retention.py tests/web/test_recording_retention_routes.py
git commit -m "perf: aggregate recording retention status"
```

### Task 4: Add lightweight highlight marker counts

**Files:**
- Modify: `src/blrec/bili_upload/highlights.py`
- Modify: `src/blrec/web/routers/highlights.py`
- Modify: `tests/bili_upload/test_highlights.py`
- Modify: `tests/web/test_highlights_routes.py`

- [ ] **Step 1: Write failing count-projection tests**

Cover markers with a persisted `recording_part_id`, legacy markers that require time-range mapping, parts with zero markers, and a marker from another room/session.

```python
counts = await service.marker_counts(session_id)
assert counts == {first_part_id: 2, second_part_id: 1, empty_part_id: 0}
assert database_calls <= 2
assert filesystem_calls == []
```

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_highlights.py -k marker_counts -q`

Expected: FAIL because `marker_counts()` does not exist.

- [ ] **Step 2: Implement the minimal count projection**

First query the session's part IDs, `part_index`, minimal time bounds
(`record_start_time`, `timeline_start_at_ms`, `record_duration_seconds`),
`artifact_state`, and `video_deleted_at`. Apply the same eligible-state and
not-deleted rules as `timeline()` without resolving a filesystem path. Second query
markers for the room with only `recording_part_id` and `content_at_ms`. Count
persisted mappings directly; map legacy null references with the same half-open
interval rule used by `timeline()`. Do not select paths, marker notes, clip rows, or
upload progress.

Expose:

```python
@dataclass(frozen=True)
class HighlightMarkerCount:
    part_id: int
    count: int
```

The public method is
`marker_counts(self, session_id: int) -> Sequence[HighlightMarkerCount]`.
It returns one entry for every eligible part, including zero-count parts, in
`part_index` order.

- [ ] **Step 3: Add the count endpoint**

```python
class MarkerCountResponse(ApiModel):
    part_id: int
    count: int


@router.get(
    '/sessions/{session_id}/marker-counts',
    response_model=List[MarkerCountResponse],
)
async def get_marker_counts(
    session_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    highlight_service: HighlightService = Depends(get_service),
) -> List[MarkerCountResponse]:
    try:
        values = await highlight_service.marker_counts(session_id)
    except ValueError as error:
        raise _not_found(error) from None
    return [MarkerCountResponse(part_id=value.part_id, count=value.count) for value in values]
```

Return 404 for a missing/non-live session. Keep the full timeline endpoint unchanged for the editor.

- [ ] **Step 4: Verify counts and timeline regression**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_highlights.py tests/web/test_highlights_routes.py -q
```

Expected: PASS; count endpoint uses at most two queries and timeline/clip behavior remains unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/highlights.py src/blrec/web/routers/highlights.py tests/bili_upload/test_highlights.py tests/web/test_highlights_routes.py
git commit -m "perf: add lightweight highlight marker counts"
```

### Task 5: Add only proven list indexes

**Files:**
- Create: `src/blrec/bili_upload/migrations/0024_initial.sql`
- Modify: `src/blrec/bili_upload/database.py`
- Modify: `tests/bili_upload/test_database.py`
- Modify: `tests/bili_upload/test_journal.py`
- Modify: `tests/bili_upload/test_highlights.py`

- [ ] **Step 1: Write failing migration and query-plan tests**

Assert schema version 24 and inspect `EXPLAIN QUERY PLAN` for the exact unsearched list predicates. The target indexes are:

```sql
CREATE INDEX recording_sessions_source_started_idx
ON recording_sessions(source_kind,started_at DESC,id DESC);

CREATE INDEX upload_jobs_state_session_idx
ON upload_jobs(state,session_id);

CREATE INDEX highlight_clips_library_idx
ON highlight_clips(created_at DESC,id DESC)
WHERE state!='cancelled';
```

The test must assert the index name appears in the query plan and that sorted results are unchanged. Do not assert an index for leading-wildcard search; SQLite cannot use these indexes for that predicate.

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_database.py tests/bili_upload/test_journal.py tests/bili_upload/test_highlights.py -k 'migration or query_plan or index' -q
```

Expected: FAIL because migration 24 and the indexes do not exist.

- [ ] **Step 2: Add migration 24 and bump the migration ceiling**

Create the three indexes above and change `latest_version = 23` to `latest_version = 24`. Update all explicit schema-version assertions in `tests/bili_upload/test_database.py` to 24.

- [ ] **Step 3: Verify migrations from empty and version-23 databases**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_database.py tests/bili_upload/test_journal.py tests/bili_upload/test_highlights.py -q
```

Expected: PASS. If an `EXPLAIN` test shows an index is unused by its production query, remove that index and its assertion instead of keeping speculative schema.

- [ ] **Step 4: Commit**

```bash
git add src/blrec/bili_upload/migrations/0024_initial.sql src/blrec/bili_upload/database.py tests/bili_upload/test_database.py tests/bili_upload/test_journal.py tests/bili_upload/test_highlights.py
git commit -m "perf: index hot recording lists"
```

### Task 6: Consume summaries and cancel stale Angular reads

**Files:**
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.model.ts`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.service.ts`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.service.spec.ts`
- Modify: `webapp/src/app/upload-tasks/shared/highlight.model.ts`
- Modify: `webapp/src/app/upload-tasks/shared/highlight.service.ts`
- Modify: `webapp/src/app/upload-tasks/shared/highlight.service.spec.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`

- [ ] **Step 1: Write failing service-contract tests**

Add exact requests for detail and marker counts:

```typescript
service.getSession(7).subscribe();
expect(http.expectOne('/api/v1/recording-sessions/7').request.method).toBe('GET');

highlights.getMarkerCounts(7).subscribe();
expect(
  http.expectOne('/api/v1/highlights/sessions/7/marker-counts').request.method,
).toBe('GET');
```

Split `RecordingSessionSummary` from `RecordingSessionDetail`; only the detail has `parts` and full `UploadJobProgress`.

- [ ] **Step 2: Write failing stale-request and lazy-detail tests**

Use controllable Subjects. Issue two loads, resolve the older one last, and assert it cannot change the view. Opening a drawer must request detail and marker counts but not a full timeline.

```typescript
component.applyFilters();
component.pageChanged(2);
older.next(oldResponse);
newer.next(newResponse);
expect(component.sessions).toEqual(newResponse.sessions);

component.openDetails(summary);
expect(recordingSessions.getSession).toHaveBeenCalledOnceWith(summary.id);
expect(highlights.getMarkerCounts).toHaveBeenCalledOnceWith(summary.id);
expect(highlights.getTimeline).not.toHaveBeenCalled();
```

Run:

```bash
cd webapp
npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/shared/recording-session.service.spec.ts' --include='src/app/upload-tasks/shared/highlight.service.spec.ts' --include='src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts'
```

Expected: FAIL because list and detail use one type, loads are independent subscriptions, and details reuse list data/full timeline.

- [ ] **Step 3: Implement one cancellable list pipeline**

Use a private request Subject and `switchMap`; manual refresh emits the current immutable request. Do not set loading or apply results outside the switched subscription.

```typescript
private readonly listRequests = new Subject<RecordingListRequest>();

this.listSubscription = this.listRequests
  .pipe(
    tap(() => {
      this.view = { state: 'loading' };
      this.changeDetector.markForCheck();
    }),
    switchMap((request) =>
      this.recordingSessions.listSessions(
        request.limit,
        request.offset,
        request.filters,
      ),
    ),
  )
  .subscribe((response) => this.applyListResponse(response));
```

Unsubscribe it in `ngOnDestroy`.

- [ ] **Step 4: Load full detail and counts only on open**

Use `forkJoin({detail: getSession(id), counts: getMarkerCounts(id)})` for live recording sessions and `getSession(id)` alone otherwise. Guard the result with the selected ID. Keep the full timeline request exclusively in `HighlightEditorComponent`.

- [ ] **Step 5: Run focused Angular tests**

Run the command from Step 2. Expected: all included specs pass and the HTTP test controller reports no unmatched requests.

- [ ] **Step 6: Commit**

```bash
git add webapp/src/app/upload-tasks/shared/recording-session.model.ts webapp/src/app/upload-tasks/shared/recording-session.service.ts webapp/src/app/upload-tasks/shared/recording-session.service.spec.ts webapp/src/app/upload-tasks/shared/highlight.model.ts webapp/src/app/upload-tasks/shared/highlight.service.ts webapp/src/app/upload-tasks/shared/highlight.service.spec.ts webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts
git commit -m "perf: load recording details on demand"
```

### Task 7: Isolate recording rows with OnPush

**Files:**
- Create: `webapp/src/app/upload-tasks/recording-sessions/recording-session-row.component.ts`
- Create: `webapp/src/app/upload-tasks/recording-sessions/recording-session-row.component.html`
- Create: `webapp/src/app/upload-tasks/recording-sessions/recording-session-row.component.scss`
- Create: `webapp/src/app/upload-tasks/recording-sessions/recording-session-row.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.html`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.scss`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.module.ts`

- [ ] **Step 1: Write failing OnPush row tests**

The row is an attribute component so table markup remains valid:

```typescript
@Component({
  selector: 'tr[app-recording-session-row]',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './recording-session-row.component.html',
})
export class RecordingSessionRowComponent {
  @Input() session!: RecordingSessionSummary;
  @Input() selected = false;
  @Input() scope: RecordingSessionScope = 'uploads';
  @Output() rowAction = new EventEmitter<RecordingSessionRowAction>();
}
```

Assert the component metadata is OnPush and that typed events carry only stable IDs/action names, not mutable parent state.

- [ ] **Step 2: Write the one-row realtime regression test**

Render 20 row components, emit progress for one job, and compare input references:

```typescript
const before = rowInputs(fixture);
realtime.emit(uploadProgressFor(targetJobId));
fixture.detectChanges();
const after = rowInputs(fixture);
expect(after.filter((value, index) => value !== before[index]).length).toBe(1);
expect(recordingSessions.listSessions).toHaveBeenCalledTimes(1);
```

Run:

```bash
cd webapp
npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/recording-sessions/recording-session-row.component.spec.ts' --include='src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts'
```

Expected: FAIL because rows are currently one large default-change-detection template.

- [ ] **Step 3: Move only row cells into the new component**

Keep filters, pagination, dialogs, and batch actions in the parent. The row owns display-only labels and emits this closed union:

```typescript
export type RecordingSessionRowAction =
  | { readonly type: 'selected'; readonly sessionId: number; readonly selected: boolean }
  | { readonly type: 'details'; readonly sessionId: number }
  | { readonly type: 'play'; readonly sessionId: number }
  | { readonly type: 'session-action'; readonly sessionId: number; readonly action: RecordingSessionAction }
  | { readonly type: 'edit-task'; readonly jobId: number };
```

Mark the parent `RecordingSessionsComponent` OnPush as well. When merging realtime progress, replace only the matching summary and retain every unaffected object reference.

- [ ] **Step 4: Run row and page tests**

Run the Step 2 command. Expected: PASS; exactly one input reference changes and there is no list reload for progress-only SSE.

- [ ] **Step 5: Commit**

```bash
git add webapp/src/app/upload-tasks/recording-sessions webapp/src/app/upload-tasks/upload-tasks.module.ts
git commit -m "perf: render recording rows with OnPush"
```

### Task 8: Split heavyweight Angular routes and disable eager preloading

**Files:**
- Create: `webapp/src/app/upload-tasks/clip-library/clip-library.module.ts`
- Create: `webapp/src/app/upload-tasks/clip-library/clip-library-routing.module.ts`
- Create: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.module.ts`
- Create: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor-routing.module.ts`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.module.ts`
- Modify: `webapp/src/app/upload-tasks/upload-tasks-routing.module.ts`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.component.ts`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.component.html`
- Modify: `webapp/src/app/app-routing.module.ts`
- Create: `webapp/src/app/app-routing.module.spec.ts`
- Modify: `webapp/src/app/core/services/realtime.service.spec.ts`
- Modify: `webapp/src/app/upload-tasks/clip-library/clip-library.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts`
- Create: `webapp/src/app/upload-tasks/part-video-dialog/part-player.loader.ts`
- Modify: `webapp/src/app/upload-tasks/part-video-dialog/part-player.factory.ts`
- Modify: `webapp/src/app/upload-tasks/part-video-dialog/part-video-dialog.component.ts`
- Modify: `webapp/src/app/upload-tasks/part-video-dialog/part-video-dialog.component.spec.ts`

- [ ] **Step 1: Write failing route-boundary tests**

Assert the list module no longer declares/imports `ClipLibraryComponent` or `HighlightEditorComponent`. Assert `/clips` lazy-loads `ClipLibraryModule`, and each supported `*/highlights/:sessionId` route lazy-loads `HighlightEditorModule`. Keep the existing realtime topic mapping assertions for nested routes.

Also assert that constructing and rendering a closed recording list does not call
the FLV player loader. Opening native media must not call it; opening FLV media calls
it once after media access succeeds.

Run:

```bash
cd webapp
npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/app-routing.module.spec.ts' --include='src/app/core/services/realtime.service.spec.ts' --include='src/app/upload-tasks/clip-library/clip-library.component.spec.ts' --include='src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts' --include='src/app/upload-tasks/part-video-dialog/part-video-dialog.component.spec.ts'
```

Expected: FAIL because all three pages are declared in `UploadTasksModule`, share
one 103 KiB estimated-transfer chunk, and the list statically imports the FLV
runtime.

- [ ] **Step 2: Defer the FLV runtime until a player opens**

Move `PartPlayer`, `PartPlayerEvent`, `PartPlayerEventHandler`, and
`FlvPlaybackSource` into `part-player.loader.ts`. Export this injectable loader
contract:

```typescript
export interface PartPlayerFactoryLike {
  attachFlv(
    element: HTMLVideoElement,
    url: string,
    source: FlvPlaybackSource,
    onEvent: PartPlayerEventHandler,
  ): PartPlayer | null;
}

export const PART_PLAYER_LOADER = new InjectionToken<
  () => Promise<PartPlayerFactoryLike>
>('PART_PLAYER_LOADER');

export const loadPartPlayerFactory = async (): Promise<PartPlayerFactoryLike> => {
  const module = await import('./part-player.factory');
  return new module.PartPlayerFactory();
};
```

Provide `loadPartPlayerFactory` from the lightweight list module. Inject the loader
into `PartVideoDialogComponent`, call it only in the FLV branch after media access,
and ignore a resolved loader if the dialog/part changed while the chunk loaded.
Keep native browser playback free of the FLV runtime.

- [ ] **Step 3: Create dedicated feature modules**

Each new routing module exports one empty-path component route. Move the component declaration and only its NgZorro/player dependencies into that feature module. Remove the `clipLibrary` switch from `UploadTasksComponent`; it becomes the recording/upload-list shell only.

Configure lazy routes before their parent list routes:

```typescript
{
  path: 'recordings/highlights/:sessionId',
  loadChildren: () =>
    import('./upload-tasks/highlight-editor/highlight-editor.module').then(
      (m) => m.HighlightEditorModule,
    ),
},
{
  path: 'clips',
  loadChildren: () =>
    import('./upload-tasks/clip-library/clip-library.module').then(
      (m) => m.ClipLibraryModule,
    ),
},
```

Add the same `HighlightEditorModule` lazy loader at
`upload-tasks/highlights/:sessionId` and `clips/highlights/:sessionId` so all
three currently accepted nested routes remain valid. Route order must place all
three editor routes before the generic `recordings`, `upload-tasks`, and `clips`
routes.

- [ ] **Step 4: Stop preloading every feature**

Replace `PreloadAllModules` with `NoPreloading` in `AppRoutingModule`. This ensures editor/clip chunks are not downloaded after the first list navigation merely because the app is idle.

- [ ] **Step 5: Run route tests and a production stats build**

```bash
cd webapp
npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/app-routing.module.spec.ts' --include='src/app/core/services/realtime.service.spec.ts' --include='src/app/upload-tasks/clip-library/clip-library.component.spec.ts' --include='src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts' --include='src/app/upload-tasks/part-video-dialog/part-video-dialog.component.spec.ts'
npm run build -- --stats-json --output-path=/tmp/blrec-hot-read-build
```

Expected: PASS. Build output has separate list, editor, clip-library, and FLV
runtime lazy chunks; the list chunk is at most 70 KiB estimated transfer and
contains neither editor, clip-library, nor `mpegts.js` code.

- [ ] **Step 6: Commit**

```bash
git add webapp/src/app/app-routing.module.ts webapp/src/app/app-routing.module.spec.ts webapp/src/app/core/services/realtime.service.spec.ts webapp/src/app/upload-tasks
git commit -m "perf: split heavyweight upload routes"
```

### Task 9: Integration verification and budget record

**Files:**
- Modify: `docs/performance/request-audit.md`
- Create: `docs/performance/hot-read-benchmark.md`

- [ ] **Step 1: Run backend regression and static checks**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/web tests/networking tests/bili_upload -q
.venv/bin/python -m black --check src tests
.venv/bin/python -m isort --check-only src tests
.venv/bin/python -m flake8 src tests
.venv/bin/python -m mypy src/blrec
```

Expected: all commands pass.

- [ ] **Step 2: Run frontend regression and changed-file lint**

```bash
cd webapp
npm test -- --watch=false --browsers=ChromeHeadless
npx eslint src/app/upload-tasks src/app/app-routing.module.ts src/app/core/services/realtime.service.spec.ts
npx ng lint
npm run build -- --stats-json --output-path=/tmp/blrec-hot-read-build
```

Expected: Karma, targeted ESLint, and build pass. Full `ng lint` reports only the same five pre-existing errors from `57361f7`; no changed file appears in that output.

- [ ] **Step 3: Run deterministic query/file budgets**

Run the focused tests with 20 and 100 seeded rows and record:

```text
recording/upload list: 2 database calls, 0 filesystem calls
room-policy list: 1 database call
retention status: 1 database call, 0 per-recording filesystem calls
highlight marker counts: <=2 database calls, 0 filesystem calls
```

On a warm NAS deployment, sample normalized request metrics without request values and record p50/p95 for list and status routes. Do not benchmark destructive endpoints or increase upstream request rates.

- [ ] **Step 4: Update the audit dispositions**

Mark inbound rows I-060, I-070, I-074, I-075, I-088, and I-092 with the
actual test/metric evidence. Append I-104 for
`GET /api/v1/recording-sessions/{session_id}` and I-105 for
`GET /api/v1/highlights/sessions/{session_id}/marker-counts`, then change the
machine-count assertion and summary to 105 endpoints. Re-run the registered-route
comparison so the appended methods, paths, and routers match the application. If a
row was not changed by this plan, retain its previous disposition; do not mark it
fixed by association.

- [ ] **Step 5: Commit**

```bash
git add docs/performance/request-audit.md docs/performance/hot-read-benchmark.md
git commit -m "docs: record hot read performance budgets"
```

## Completion gate

The plan is complete only when all regression commands have fresh passing evidence, the five lint baseline errors are unchanged, the summary/list file and query budgets are deterministic, and the production build proves separate lazy chunks within the size budget. A passing unit suite alone is not evidence for the NAS p95 targets; record those deployment measurements separately without request values or local paths.
