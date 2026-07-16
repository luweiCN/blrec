# Local Media Playback and Indexing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make completed, active, and interrupted FLV recordings open automatically on a NAS without infinite loading or full-file probing in the request path.

**Architecture:** Classify each part as seekable, sequential-only, indexing, or failed. Media access remains HTTP Range and returns a bounded playback descriptor; a persistent background worker repairs interrupted FLV metadata once, while the browser can immediately use sequential playback. UI requests retry only for known transient active-recording states and every stage has a finite timeout.

**Tech Stack:** Python FLV parser/injector, FFmpeg/FFprobe, SQLite, FastAPI StreamingResponse, mpegts.js, Angular, pytest, Jasmine.

## Global Constraints

- Never download a full recording before playback starts.
- Never run a full-file FFprobe scan inside a media-access HTTP request.
- Completed indexed FLV must show a first frame within 3 seconds on the LAN; active stable FLV within 5 seconds.
- Every loading state must end in success or an actionable error.

---

### Task 1: Reproduce metadata and recovery failures

**Files:**
- Modify: `tests/bili_upload/test_artifact_recovery.py`
- Modify: `tests/bili_upload/test_recording_content.py`
- Modify: `tests/web/test_recording_sessions_routes.py`

- [ ] **Step 1: Add fixtures for valid, growing, and zero-duration FLV files**

```python
resource = await reader.media(interrupted_part_id)
assert resource.playback_mode == 'sequential'
assert recovered.duration_seconds is None
```

- [ ] **Step 2: Run tests and verify RED**

Run: `pytest tests/bili_upload/test_artifact_recovery.py tests/bili_upload/test_recording_content.py tests/web/test_recording_sessions_routes.py -q`
Expected: FAIL because zero duration is treated as valid and no playback mode exists.

- [ ] **Step 3: Make zero duration invalid and expose playback classification**

Treat non-empty interrupted media with `duration <= 0` as unknown duration. Extend `MediaResource` with `playback_mode: Literal['seekable','sequential','active_snapshot']` determined from FLV metadata and recording state.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/bili_upload/test_artifact_recovery.py tests/bili_upload/test_recording_content.py tests/web/test_recording_sessions_routes.py src/blrec/bili_upload/artifact_recovery.py src/blrec/bili_upload/recording_content.py
git commit -m "fix: classify interrupted FLV playback"
```

### Task 2: Add persistent FLV indexing recovery

**Files:**
- Create: `src/blrec/bili_upload/migrations/0020_initial.sql`
- Create: `src/blrec/bili_upload/media_index.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `tests/bili_upload/test_database.py`
- Create: `tests/bili_upload/test_media_index.py`

**Interfaces:**
- Produces `MediaIndexWorker.run_once() -> Optional[int]`.
- Persists `media_index_state`, `media_index_error`, `media_index_progress`, and `media_index_updated_at` on each recording part.

- [ ] **Step 1: Add failing migration/recovery tests**

Assert one invalid ready FLV is claimed once, writes to a temporary indexed file, atomically replaces or selects the repaired output, survives restart, and never touches active recordings.

- [ ] **Step 2: Run tests and verify RED**

Run: `pytest tests/bili_upload/test_database.py tests/bili_upload/test_media_index.py -q`
Expected: FAIL because migration 20 and the worker do not exist.

- [ ] **Step 3: Implement worker with existing FLV analysis/injection primitives**

```python
class MediaIndexWorker:
    async def run_once(self) -> Optional[int]:
        # claim pending part, analyse tags sequentially, inject duration/keyframes
        # into a temporary file, fsync/replace, update progress and audit
```

Schedule invalid recovered FLVs as pending; use leases so crashes resume safely and duplicate workers cannot process the same part.

- [ ] **Step 4: Run tests and verify GREEN**

Run the command from Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/migrations/0020_initial.sql src/blrec/bili_upload/media_index.py src/blrec/bili_upload/runtime.py src/blrec/bili_upload/journal.py tests/bili_upload/test_database.py tests/bili_upload/test_media_index.py
git commit -m "feat: recover FLV media indexes"
```

### Task 3: Return bounded media descriptors and diagnostics

**Files:**
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `src/blrec/logging/audit.py`
- Modify: `tests/web/test_recording_sessions_routes.py`

**Interfaces:**
- `MediaAccessResponse` adds `playbackMode`, `indexState`, `retryAfterMs`, and `requestId`.
- Audits `media_access_started/completed/failed` and sampled `media_range_failed` without tokens or URLs.

- [ ] **Step 1: Add failing response and timing tests**

Assert seekable, sequential and transient active responses; assert 206 ranges; assert audit fields contain IDs, bytes and elapsed time but no token.

- [ ] **Step 2: Run tests and verify RED**

Run: `pytest tests/web/test_recording_sessions_routes.py tests/logging/test_audit.py -q`
Expected: FAIL on missing descriptor fields/events.

- [ ] **Step 3: Implement response state and finite server work**

Media access may inspect only the first FLV metadata tag and current in-memory recorder metadata. Return a transient retry descriptor instead of silently falling back when an active snapshot has too few keyframes.

- [ ] **Step 4: Run tests and verify GREEN**

Run the command from Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/web/routers/recording_sessions.py src/blrec/logging/audit.py tests/web/test_recording_sessions_routes.py tests/logging/test_audit.py
git commit -m "feat: expose bounded media playback state"
```

### Task 4: Make playback automatic and finite

**Files:**
- Modify: `webapp/src/app/recordings/part-video-dialog/*`
- Modify: `webapp/src/app/recordings/shared/recording-session.model.ts`
- Modify: `webapp/src/app/recordings/shared/recording-session.service.ts`
- Modify: `webapp/src/app/recordings/highlight-editor/*`

**Interfaces:**
- Player factory receives `playbackMode` and reports `attached`, `first_frame`, `stalled`, and `error` transitions.
- Active transient access retries with backend `retryAfterMs` until a 10-second deadline.

- [ ] **Step 1: Add failing dialog/player/editor tests**

Use fake timers to prove automatic retry, successful first-frame completion, sequential fallback, timeout error, teardown, and initial editor media load without pressing refresh.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/recordings/**/*.spec.ts'`
Expected: FAIL on missing descriptors and infinite spinner behavior.

- [ ] **Step 3: Implement explicit request state machines**

Use a discriminated union `idle|access_loading|player_loading|playing|error`; do not represent loading with unrelated booleans. Sequential FLV config uses live-style append playback without advertising seekability; indexed and snapshot modes retain Range seeking.

- [ ] **Step 4: Run Angular tests and build**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless && npm run build`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/src/app/recordings
git commit -m "fix: open local recordings automatically"
```

### Task 5: Verify against NAS media

**Files:**
- Modify: `docs/operations/synology-pilot-runbook.md` if the runbook exists; otherwise create `docs/operations/media-playback-acceptance.md`.

- [ ] **Step 1: Run full backend and frontend verification**

Run: `pytest -q`; `black --check src tests`; `isort --check-only src tests`; `flake8 src tests`; `mypy src/blrec`; `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless && npm run build`.
Expected: all tests/checks pass, aside from documented existing Angular lint findings.

- [ ] **Step 2: Deploy a tagged image to NAS and test three real parts**

Verify one indexed completed FLV, one active FLV, and the known interrupted 1.4 GB FLV. Record first-frame time, Range throughput, index progress, seek behavior and finite errors.

- [ ] **Step 3: Commit the acceptance evidence**

```bash
git add docs/operations
git commit -m "docs: record NAS media playback acceptance"
```
