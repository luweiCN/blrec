# Upload Task Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit row/bulk upload-task operations and repair Bilibili terminal transcode failures without creating duplicate submissions.

**Architecture:** Migration 11 adds a leased action queue and per-part repair history. `UploadTaskActionManager` validates state-specific commands and enqueues at most one active action per target. `TranscodeRepairCoordinator` reads the existing archive, replaces only failed/missing parts on the same AID, and escalates from original-file reupload to FFmpeg stream-copy remux only after Bilibili reports terminal failure again.

**Tech Stack:** Python 3.8+, SQLite WAL leases, aiohttp protocol client, FFmpeg/ffprobe, FastAPI, Angular 15/ng-zorro, Jasmine/Karma.

## Global Constraints

- Row and bulk actions enqueue work and return immediately; bulk requests contain at most 100 job IDs.
- Never change the posting account captured by the job and never silently create a new archive.
- Do not retry uncertain non-idempotent writes automatically.
- Repair stage 1 reuploads the original local video and edits the same AID.
- Repair stage 2 runs `ffmpeg -fflags +genpts -i INPUT -map 0 -c copy -avoid_negative_ts make_zero OUTPUT`, validates OUTPUT with ffprobe, uploads it, and edits the same AID.
- No automatic full re-encode and no infinite repair loop. Missing local media produces an actionable failure.

---

### Task 1: Migration 11 and action queue

**Files:**
- Create: `src/blrec/bili_upload/migrations/0011_initial.sql`
- Modify: `src/blrec/bili_upload/database.py`
- Create: `src/blrec/bili_upload/actions.py`
- Test: `tests/bili_upload/test_database.py`
- Test: `tests/bili_upload/test_actions.py`

**Interfaces:** Commands are `create_upload`, `retry_failed`, `repair_transcode`, `resubmit_rejected`, `retry_comment`, `retry_danmaku`, and `delete_local_media`; each action records target job/part, state, stage, attempt, result/error, lease, actor, and timestamps.

- [ ] Write failing migration and manager tests for state validation, active-action deduplication, FIFO claims, lease recovery, fixed account retention, and audit rows.
- [ ] Run `pytest -q tests/bili_upload/test_database.py tests/bili_upload/test_actions.py`; verify schema version 10 and missing action manager fail.
- [ ] Add migration 11, claim-table allow-list entry, partial unique/index support, and parameterized manager operations.
- [ ] Re-run focused tests; expected PASS.
- [ ] Commit with `feat: add upload task action queue`.

### Task 2: Archive edit and part-status protocol contract

**Files:**
- Modify: `src/blrec/bili_upload/signing.py`
- Modify: `src/blrec/bili_upload/protocol.py`
- Modify: `tests/bili_upload/fixtures/protocol/responses.json`
- Test: `tests/bili_upload/test_protocol_matrix.py`

**Interfaces:** `archive_view` supplies current part/CID/transcode state; `edit_archive(bundle, aid, payload)` submits the full preserved archive payload with replaced filenames.

- [ ] Add failing matrix tests for the edit endpoint's method/path/CSRF/referer/body and for normalization of processing, success, and terminal-failure part states.
- [ ] Implement a non-idempotent JSON edit request using the same credential and archive metadata; retain remote parts not selected for replacement.
- [ ] Run `pytest -q tests/bili_upload/test_protocol_matrix.py`; expected PASS.
- [ ] Commit with `feat: support editing existing archives`.

### Task 3: Original-file and stream-copy transcode repair

**Files:**
- Create: `src/blrec/bili_upload/transcode_repair.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Test: `tests/bili_upload/test_transcode_repair.py`

**Interfaces:** `TranscodeRepairCoordinator.run(action_claim)` returns `waiting_remote`, `completed`, or `failed`; persisted stages are `original`, `remux`, and `exhausted`.

- [ ] Write failing tests for terminal-state gating, same-AID edit, one-part replacement, original-stage success, second terminal failure escalation, ffmpeg/ffprobe argument safety, invalid remux cleanup, restart resume, unknown edit outcome pause, and exhausted-stage refusal.
- [ ] Reuse the existing UPOS uploader for replacement files. Invoke subprocesses with argument arrays and `shell=False`; write remux output beside the input using an exclusive temporary name and remove it after upload or failure.
- [ ] Poll Bilibili only through the existing review cadence; processing is not failure. Persist stage transitions before each external write.
- [ ] Run `pytest -q tests/bili_upload/test_transcode_repair.py tests/bili_upload/test_upload.py tests/bili_upload/test_review.py`; expected PASS.
- [ ] Commit with `feat: repair terminal transcode failures`.

### Task 4: Safe row/bulk action API

**Files:**
- Create: `src/blrec/web/routers/upload_task_actions.py`
- Modify: `src/blrec/web/main.py`
- Test: `tests/web/test_upload_task_actions_routes.py`

**Interfaces:** `GET /api/v1/upload-task-actions/capabilities?job_id=N`, `POST /api/v1/upload-task-actions`, and `POST /api/v1/upload-task-actions/bulk`; bulk response reports accepted/rejected per job.

- [ ] Write failing authentication, validation, 100-item limit, partial-result, disabled-action, and enqueue-only latency tests.
- [ ] Implement authenticated routes that derive capabilities from server state and never accept paths, AIDs, account IDs, or retry stage from the browser.
- [ ] Run `pytest -q tests/web/test_upload_task_actions_routes.py`; expected PASS.
- [ ] Commit with `feat: expose safe upload task actions`.

### Task 5: Upload-task list actions

**Files:**
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.model.ts`
- Create: `webapp/src/app/upload-tasks/shared/upload-task-action.service.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.{ts,html,scss,spec.ts}`

- [ ] Write failing tests for row menus, current-page selection, select-all-on-page, disabled reasons, confirmation for destructive actions, bulk partial results, selection reset after filtering, and accessible labels/focus.
- [ ] Implement compact action menus and a sticky bulk bar; show only server-approved capabilities and refresh without opening the detail drawer.
- [ ] Run the focused component tests, then `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless && npx ng lint && npm run build`; expected PASS.
- [ ] Commit with `feat: add upload task recovery actions`.

### Task 6: Recovery verification

- [ ] Run `pytest -q`; expected all tests PASS.
- [ ] Use a disposable rejected archive to verify same-AID resubmission and no duplicate archive.
- [ ] Use a fixture marked terminal transcode failure to verify original-file repair, then forced remux escalation, and confirm the BVID remains unchanged.
