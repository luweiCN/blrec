# Recording and Upload Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate room configuration, recording sessions, and upload jobs while resolving the final upload decision only after recording ends.

**Architecture:** Keep `recording_sessions` as the local recording aggregate and `upload_jobs` as the immutable remote workflow. Add a session-level three-state decision plus an optional full settings override; a resolver creates one `waiting_artifacts` job after `live_end_time` is recorded, and a preparer attaches ready parts later. Angular gets separate room, recording, and upload list routes backed by shared typed settings controls.

**Tech Stack:** Python 3.8+, SQLite migrations, FastAPI/Pydantic, Angular 12, RxJS, NG-ZORRO, pytest, Jasmine/Karma.

## Global Constraints

- Do not create an upload job while a recording session is open.
- Resolve settings in this order: session override, latest room policy, safe application defaults.
- Missing required account/category data must not call Bilibili; expose an actionable configuration error.
- Existing upload jobs keep their `policy_snapshot_json` unchanged.
- Work in the current checkout; do not use a worktree.

---

### Task 1: Persist session submission decisions and overrides

**Files:**
- Create: `src/blrec/bili_upload/migrations/0019_initial.sql`
- Create: `src/blrec/bili_upload/session_submission.py`
- Modify: `src/blrec/bili_upload/__init__.py`
- Modify: `tests/bili_upload/test_database.py`
- Create: `tests/bili_upload/test_session_submission.py`

**Interfaces:**
- Produces: `SubmissionDecision = Literal['follow_room', 'upload', 'skip']`.
- Produces: `SessionSubmissionManager.get/save_override/clear_override/set_decision`.
- Stores a full validated `RoomUploadPolicyCommand` JSON value only after an explicit session save.

- [ ] **Step 1: Write failing migration and manager tests**

```python
assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 19
view = await manager.get(session_id)
assert view.decision == 'follow_room'
assert view.inherited is True
await manager.save_override(session_id, command, manager_subject='administrator')
assert (await manager.get(session_id)).inherited is False
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `pytest tests/bili_upload/test_database.py tests/bili_upload/test_session_submission.py -q`
Expected: FAIL because migration 19 and `SessionSubmissionManager` do not exist.

- [ ] **Step 3: Add schema and minimal manager**

```sql
ALTER TABLE recording_sessions ADD COLUMN upload_decision TEXT NOT NULL
DEFAULT 'follow_room' CHECK (upload_decision IN ('follow_room','upload','skip'));
ALTER TABLE recording_sessions ADD COLUMN upload_override_json TEXT;
ALTER TABLE recording_sessions ADD COLUMN upload_resolution_state TEXT NOT NULL
DEFAULT 'pending' CHECK (upload_resolution_state IN
('pending','not_requested','configuration_required','job_created'));
ALTER TABLE recording_sessions ADD COLUMN upload_resolution_error TEXT;
ALTER TABLE recording_sessions ADD COLUMN upload_resolved_at INTEGER;
```

Backfill existing jobs as `job_created`, closed historical sessions without jobs as `not_requested`, and open live sessions from their legacy `upload_intent` value. Encode/decode every `RoomUploadPolicyCommand` field, validate through `RoomUploadPolicyManager`, and write management audit rows for decision/override changes.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `pytest tests/bili_upload/test_database.py tests/bili_upload/test_session_submission.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/migrations/0019_initial.sql src/blrec/bili_upload/session_submission.py src/blrec/bili_upload/__init__.py tests/bili_upload/test_database.py tests/bili_upload/test_session_submission.py
git commit -m "feat: persist recording submission overrides"
```

### Task 2: Resolve uploads after recording ends

**Files:**
- Modify: `src/blrec/bili_upload/upload.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `tests/bili_upload/test_upload.py`
- Modify: `tests/bili_upload/test_journal.py`
- Modify: `tests/bili_upload/test_account_runtime.py`

**Interfaces:**
- Produces: `UploadCoordinator.resolve_finished_sessions() -> List[int]` returning created job IDs.
- Produces: `UploadCoordinator.prepare_waiting_jobs() -> List[int]` returning jobs advanced to `ready`.
- Consumes: session decision and override codec from Task 1.

- [ ] **Step 1: Add failing lifecycle tests**

```python
await journal.recording_started(100, live_start_time=800)
assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
await policy_manager.upsert(100, disabled_policy)
await journal.recording_finished(run_id)
assert await coordinator.resolve_finished_sessions() == []
assert await database.scalar("SELECT upload_resolution_state FROM recording_sessions") == 'not_requested'
```

Also cover `upload`, `skip`, session override precedence, missing account, a repeated finish event, and `waiting_artifacts` becoming `ready` only after every part is ready.

- [ ] **Step 2: Run tests and verify RED**

Run: `pytest tests/bili_upload/test_upload.py tests/bili_upload/test_journal.py tests/bili_upload/test_account_runtime.py -q`
Expected: FAIL because current sessions snapshot `upload_intent` on start and jobs are created only from ready closed sessions.

- [ ] **Step 3: Implement the resolver and preparer**

```python
async def resolve_finished_sessions(self) -> List[int]:
    # claim sessions with live_end_time, pending resolution and no upload job
    # evaluate decision, load override or current room policy, validate, snapshot,
    # insert exactly one waiting_artifacts job and audit the outcome

async def prepare_waiting_jobs(self) -> List[int]:
    # create upload_parts after all recording_parts are ready, then set job ready
```

Set every new recording session to `follow_room`; remove start-time policy snapshotting. The worker loop calls resolve, prepare, then upload. Use the existing unique `upload_jobs.session_id` constraint and transaction checks for idempotency.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `pytest tests/bili_upload/test_upload.py tests/bili_upload/test_journal.py tests/bili_upload/test_account_runtime.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/upload.py src/blrec/bili_upload/runtime.py src/blrec/bili_upload/journal.py tests/bili_upload/test_upload.py tests/bili_upload/test_journal.py tests/bili_upload/test_account_runtime.py
git commit -m "feat: resolve uploads after recording ends"
```

### Task 3: Expose recording and upload scopes plus session settings

**Files:**
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `src/blrec/web/main.py`
- Modify: `tests/web/test_recording_sessions_routes.py`

**Interfaces:**
- Adds `scope=recordings|uploads` to `GET /api/v1/recording-sessions`.
- Adds `GET/PUT/DELETE /api/v1/recording-sessions/{id}/submission-settings`.
- Recording response includes `uploadDecision`, `submissionInherited`, `uploadResolutionState`, and `uploadResolutionError`.

- [ ] **Step 1: Write failing route tests**

```python
response = await client.get('/api/v1/recording-sessions?scope=uploads')
assert all(item['uploadJob'] is not None for item in response.json()['sessions'])
response = await client.put('/api/v1/recording-sessions/1/submission-settings', json=payload)
assert response.json()['inherited'] is False
```

- [ ] **Step 2: Run route tests and verify RED**

Run: `pytest tests/web/test_recording_sessions_routes.py -q`
Expected: FAIL with 404/unknown query behavior.

- [ ] **Step 3: Implement typed API models and filters**

Reuse the room-policy request fields for session overrides, return resolved effective settings, and make `scope=uploads` add `job.id IS NOT NULL`. Keep `scope=recordings` limited to `source_kind='live'` so derived highlight upload adapters never appear as original recordings.

- [ ] **Step 4: Run route tests and verify GREEN**

Run: `pytest tests/web/test_recording_sessions_routes.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/journal.py src/blrec/web/routers/recording_sessions.py src/blrec/web/main.py tests/web/test_recording_sessions_routes.py
git commit -m "feat: expose recording submission settings"
```

### Task 4: Build one reusable submission settings dialog

**Files:**
- Create: `webapp/src/app/shared/submission-settings/submission-settings.model.ts`
- Create: `webapp/src/app/shared/submission-settings/submission-settings-form.component.{ts,html,scss,spec.ts}`
- Create: `webapp/src/app/shared/submission-settings/submission-settings-dialog.component.{ts,html,spec.ts}`
- Modify: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.{ts,html,spec.ts}`
- Modify: `webapp/src/app/upload-tasks/task-edit-dialog/task-edit-dialog.component.{ts,html}`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.module.ts`

**Interfaces:**
- Produces `SubmissionSettingsValue` with every existing room policy field except `enabled`.
- Produces dialog context union `{kind:'room'|'recording'|'upload'|'clip'; targetId:number}`.
- Emits `saved: SubmissionSettingsValue`; wrapper services choose the endpoint.

- [ ] **Step 1: Add failing component tests**

```typescript
expect(fixture.nativeElement.textContent).toContain('投稿账号');
expect(component.value.enabled).toBeUndefined();
component.context = { kind: 'recording', targetId: 7 };
expect(component.title).toBe('本场投稿设置');
```

- [ ] **Step 2: Run tests and verify RED**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/shared/submission-settings/**/*.spec.ts'`
Expected: FAIL because the shared components do not exist.

- [ ] **Step 3: Extract the existing complete form**

Move category mapping, creation statements, collections, covers, validation and all posting fields into the shared form. Keep room enablement in the room wrapper and replace the reduced upload-task form with the same component. Do not duplicate request models.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2 plus existing upload-policy and task-edit specs.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/src/app/shared/submission-settings webapp/src/app/tasks/upload-policy-dialog webapp/src/app/upload-tasks/task-edit-dialog webapp/src/app/upload-tasks/upload-tasks.module.ts
git commit -m "refactor: share submission settings form"
```

### Task 5: Redesign room management

**Files:**
- Modify: `webapp/src/app/app.component.{html,spec.ts}`
- Modify: `webapp/src/app/tasks/tasks.component.{ts,html,spec.ts}`
- Modify: `webapp/src/app/tasks/toolbar/toolbar.component.{ts,html,scss,spec.ts}`
- Modify: `webapp/src/app/tasks/task-list/task-list.component.{ts,html,scss,spec.ts}`
- Modify: `webapp/src/app/tasks/task-item/task-item.component.{ts,html,scss,spec.ts}`
- Modify: `webapp/src/app/tasks/shared/pipes/filter-tasks.pipe.{ts,spec.ts}`

**Interfaces:**
- Navigation label becomes `房间管理`.
- Filters include live status, monitor/record state, automatic submission state, area, search and sort.
- Automatic submission switch opens settings when no valid policy exists.

- [ ] **Step 1: Update tests first**

Assert the five columns `房间/直播状态/监控录制/自动投稿/操作`, filter behavior, first-enable dialog behavior, and batch posting settings.

- [ ] **Step 2: Run focused Angular tests and verify RED**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/tasks/**/*.spec.ts'`
Expected: FAIL on old labels and missing policy state.

- [ ] **Step 3: Implement the compact list and filters**

Load room policies once, merge by room ID, preserve the unified monitor/record switch, and remove the room-list date range. Keep actions familiar and responsive without adding nested cards.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/src/app/app.component.* webapp/src/app/tasks
git commit -m "feat: redesign room management"
```

### Task 6: Split recording and upload pages

**Files:**
- Create: `webapp/src/app/recordings/recordings.module.ts`
- Create: `webapp/src/app/recordings/recordings-routing.module.ts`
- Create: `webapp/src/app/recordings/recording-list/*`
- Create: `webapp/src/app/recordings/shared/*`
- Modify: `webapp/src/app/app-routing.module.ts`
- Modify: `webapp/src/app/app.component.{html,spec.ts}`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.{ts,html,scss,spec.ts}`
- Modify: `webapp/src/app/upload-tasks/upload-tasks-routing.module.ts`

**Interfaces:**
- `/recordings` lists all live recording sessions and owns play, danmaku, clip, delete and per-session submission settings.
- `/upload-tasks` requests `scope=uploads`, has no local play/clip actions, and opens Bilibili links after approval.
- `/upload-tasks/highlights/:id` redirects to `/recordings/:id/edit`.

- [ ] **Step 1: Add failing routing and list tests**

Assert navigation order, recording-only rows without jobs, upload-only rows with jobs, local action ownership, and legacy redirect.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/{app.component.spec.ts,recordings/**/*.spec.ts,upload-tasks/**/*.spec.ts}'`
Expected: FAIL because `/recordings` and the split views do not exist.

- [ ] **Step 3: Implement focused components**

Move local content dialogs and clip links into the recording module. Slim the existing 1,100-line mixed component into upload-job-only behavior instead of adding another mode flag.

- [ ] **Step 4: Run Angular tests and production build**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless && npm run build`
Expected: all tests and build PASS, with only existing bundle-budget warnings.

- [ ] **Step 5: Commit**

```bash
git add webapp/src/app/app-routing.module.ts webapp/src/app/app.component.* webapp/src/app/recordings webapp/src/app/upload-tasks
git commit -m "feat: separate recordings from upload tasks"
```
