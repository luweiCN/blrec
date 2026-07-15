# Recording Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace directory-wide cleanup with database-owned video retention, per-room policies, a global recording-capacity limit, and an in-app low-capacity warning.

**Architecture:** Migration 10 records each room's current retention rule and each recording part's media lifecycle. `RecordingRetentionManager` evaluates current room policy against existing and future parts, claims eligible rows transactionally, deletes only resolved video/HLS media, and records every result. `RecordingStorageMonitor` computes database-owned usage and filesystem headroom for the settings page and warning banner.

**Tech Stack:** Python 3.8+, SQLite WAL, FastAPI, Pydantic, Angular 15/ng-zorro, Jasmine/Karma.

## Global Constraints

- Default policy is `submitted` with a five-day delay, including rooms without an explicit policy row.
- Modes are `never`, `capacity`, `recording_finished`, `uploaded`, `submitted`, and `approved`.
- A saved room policy applies to all existing parts for that room; the UI confirms when the change makes local videos immediately eligible.
- Capacity cleanup considers only rooms whose current mode is `capacity`; it never overrides `never` or event-based modes.
- Delete only database-owned video/HLS media. Preserve danmaku XML, covers, metadata, database history, and remote uploads.
- Never delete recording, postprocessing, uploading, submitting, or unconfirmed-upload source media.

---

### Task 1: Migration 10 and retention policy contract

**Files:**
- Create: `src/blrec/bili_upload/migrations/0010_initial.sql`
- Modify: `src/blrec/bili_upload/database.py`
- Modify: `src/blrec/bili_upload/policies.py`
- Modify: `src/blrec/web/routers/room_upload_policies.py`
- Test: `tests/bili_upload/test_database.py`
- Test: `tests/bili_upload/test_policies.py`
- Test: `tests/web/test_room_upload_policies_routes.py`

**Interfaces:** Policy fields are `retention_mode` and `retention_days`; part lifecycle fields are `media_state`, `media_deleted_at`, `media_delete_reason`, `media_released_bytes`, and `media_delete_error`. Upload events expose `uploaded_at`, `submitted_at`, and `approved_at`.

- [ ] Write failing migration/default/validation tests, including `submitted + 5 days` when no policy exists and `retention_days >= 0`.
- [ ] Run `pytest -q tests/bili_upload/test_database.py tests/bili_upload/test_policies.py tests/web/test_room_upload_policies_routes.py`; verify schema version 9 and missing fields fail.
- [ ] Add migration 10, parameterized persistence, response fields, and indexes on media state/event timestamps without changing existing policy semantics.
- [ ] Re-run the focused tests; expected PASS.
- [ ] Commit with `feat: persist recording retention policies`.

### Task 2: Event timestamps and safe candidate selection

**Files:**
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `src/blrec/bili_upload/upload.py`
- Modify: `src/blrec/bili_upload/review.py`
- Create: `src/blrec/bili_upload/retention.py`
- Test: `tests/bili_upload/test_retention.py`
- Test: `tests/bili_upload/test_upload.py`
- Test: `tests/bili_upload/test_review.py`

**Interfaces:** `RecordingRetentionManager.preview_room_policy(room_id, mode, days)` returns count/bytes; `run_event_retention(now)` returns a deletion summary; candidate ordering is event time, session ID, part index.

- [ ] Write failing tests for every mode, delay boundary, current-policy retroactivity, upload/submission/approval timestamps, active-work exclusions, final/source path deduplication, and preservation of XML/image files.
- [ ] Run the focused tests and verify failures are caused by the absent manager/columns.
- [ ] Implement transactional `present -> deleting` claims, containment checks against configured output roots, video/HLS suffix allow-listing, idempotent missing-file handling, and `deleted`/`delete_failed` outcomes.
- [ ] Re-run the focused tests; expected PASS.
- [ ] Commit with `feat: enforce safe recording retention`.

### Task 3: Capacity limit and low-capacity status

**Files:**
- Modify: `src/blrec/setting/models.py`
- Modify: `src/blrec/setting/setting_manager.py`
- Create: `src/blrec/disk_space/recording_storage.py`
- Modify: `src/blrec/disk_space/space_reclaimer.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Create: `src/blrec/web/routers/recording_storage.py`
- Modify: `src/blrec/web/main.py`
- Test: `tests/disk_space/test_recording_storage.py`
- Test: `tests/web/test_recording_storage_routes.py`

**Interfaces:** Settings use `recording_capacity_bytes` (`0` means unlimited) and `capacity_warning_remaining_bytes`; `GET /api/v1/recording-storage` returns used, limit, remaining, filesystem free bytes, warning state, and last cleanup summary.

- [ ] Write failing tests for unlimited capacity, threshold equality, oldest-first capacity candidates, capacity-policy isolation, no safe candidate, partial deletion failure, and restart recovery of `deleting` rows.
- [ ] Remove the glob-based `recycle_records` behavior from runtime wiring and implement the DB-owned manager as the only deletion path.
- [ ] Run `pytest -q tests/disk_space/test_recording_storage.py tests/web/test_recording_storage_routes.py`; expected PASS.
- [ ] Commit with `feat: add recording capacity management`.

### Task 4: Retention and storage UI

**Files:**
- Modify: `webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.model.ts`
- Modify: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.{ts,html,scss,spec.ts}`
- Modify: `webapp/src/app/settings/shared/setting.model.ts`
- Modify: `webapp/src/app/settings/disk-space-settings/disk-space-settings.component.{ts,html,spec.ts}`
- Create: `webapp/src/app/core/recording-storage/recording-storage.service.ts`
- Create: `webapp/src/app/core/recording-storage/recording-storage-warning.component.{ts,html,scss,spec.ts}`
- Modify: `webapp/src/app/app.module.ts`

- [ ] Write failing tests for all six room modes, conditional day input, immediate-impact confirmation, byte-unit conversion, unlimited mode, and warning visibility/accessibility.
- [ ] Implement concise controls and a persistent warning with an `aria-live="polite"` status; do not expose the removed `recycleRecords` switch.
- [ ] Run `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless && npx ng lint && npm run build`; expected PASS.
- [ ] Commit with `feat: configure recording retention and capacity`.

### Task 5: Regression verification

- [ ] Run `pytest -q`; expected all tests PASS.
- [ ] With a disposable recording, verify event-delay deletion removes video only and the session remains visible.
- [ ] Set a small capacity, verify only `capacity` rooms are reclaimed, and confirm the warning clears after safe cleanup.
