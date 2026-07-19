# Paused Upload and Legacy Clip Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent media indexing from invalidating upload snapshots, delete missing legacy clip records safely, recover the old page-order false pause, and reconcile the five known NAS upload jobs without touching Bilibili archives.

**Architecture:** SQLite state is the serialization boundary between `MediaIndexWorker` and `UploadCoordinator`: a finalized upload waits while an uploadable part is `pending` or `indexing`, while the media worker may claim parts belonging to a `waiting_artifacts` job. Clip deletion skips only missing paths outside `/clips`; existing outside-root files remain protected. Historical review recovery matches one exact legacy reason, while NAS-only data repairs use narrow preconditions and a transaction.

**Tech Stack:** Python 3.8, asyncio, SQLite, pytest, Docker/GHCR, Synology Compose.

## Global Constraints

- Do not use a git worktree or subagent.
- Do not delete a Bilibili archive or any local recording/highlight file during reconciliation.
- Do not re-upload the already submitted job whose page order was falsely rejected.
- Back up `/cfg/blrec.sqlite3` and pass `PRAGMA quick_check` before NAS mutations.
- Use test-first red-green cycles for every production change.

---

### Task 1: Serialize finalized upload preparation with media indexing

**Files:**
- Modify: `src/blrec/bili_upload/media_index.py`
- Modify: `src/blrec/bili_upload/upload.py`
- Test: `tests/bili_upload/test_media_index.py`
- Test: `tests/bili_upload/test_upload.py`

**Interfaces:**
- Produces: `UploadCoordinator._has_pending_media_index(session_id: int) -> Awaitable[bool]`.
- Consumes: `recording_parts.media_index_state` values `pending`, `indexing`, `ready`, `failed`, and `not_required`.

- [ ] **Step 1: Write failing coordination tests**

Add one media-index test that inserts a `waiting_artifacts` upload job and asserts `_claim()` still changes the part from `pending` to `indexing`. Add upload tests proving a finalized job remains `waiting_artifacts` while the part is `pending` or `indexing`, then snapshots the rebuilt file only after `media_index_state='ready'`.

```python
assert await worker.prepare_waiting_jobs() == []
assert await database.scalar(
    "SELECT state FROM upload_jobs WHERE id=1"
) == 'waiting_artifacts'

path.write_bytes(b'rebuilt-final-file')
await database.execute(
    "UPDATE recording_parts SET media_index_state='ready',updated_at=2 WHERE id=1"
)
assert await worker.prepare_waiting_jobs() == [1]
```

- [ ] **Step 2: Verify the tests fail for the diagnosed race**

Run: `.venv/bin/pytest tests/bili_upload/test_media_index.py tests/bili_upload/test_upload.py -q`

Expected: the waiting job blocks media indexing or the upload job snapshots a `pending/indexing` part.

- [ ] **Step 3: Implement minimal database-state serialization**

Allow the media worker when no job exists outside `waiting_artifacts/approved/completed/rejected`. In finalized upload preparation, return early while an uploadable part has an unresolved media index, include `media_index_state` in the snapshot query, and re-check that state in the write transaction before inserting `upload_parts`.

```sql
AND NOT EXISTS(
  SELECT 1 FROM upload_jobs job
  WHERE job.session_id=part.session_id
    AND job.state NOT IN ('waiting_artifacts','approved','completed','rejected')
)
```

- [ ] **Step 4: Verify focused tests pass**

Run: `.venv/bin/pytest tests/bili_upload/test_media_index.py tests/bili_upload/test_upload.py -q`

Expected: all focused tests pass.

### Task 2: Delete missing legacy clip records without widening file access

**Files:**
- Modify: `src/blrec/bili_upload/highlights.py`
- Test: `tests/bili_upload/test_highlights.py`

**Interfaces:**
- Produces: `HighlightService.delete_clip()` ignores an outside-root output only when both the declared path and `<path>.partial` are absent.
- Preserves: `_owned_highlight_path()` remains strict for playback, download, upload, and existing files.

- [ ] **Step 1: Write failing legacy deletion tests**

Create a failed clip whose output paths point under a missing legacy `recording_root/highlights` directory. Assert deletion removes the row. Create an actual file outside `clip_root` and assert deletion still raises `ValueError` and leaves both the row and file intact.

```python
await service.delete_clip(clip_id)
assert await database.scalar(
    'SELECT COUNT(*) FROM highlight_clips WHERE id=?', (clip_id,)
) == 0
```

- [ ] **Step 2: Verify the tests fail**

Run: `.venv/bin/pytest tests/bili_upload/test_highlights.py -q`

Expected: missing legacy path fails with `highlight output path is outside recording root`.

- [ ] **Step 3: Implement the narrow missing-path exception and audit rejection**

In `_clip_output_paths`, catch only the ownership `ValueError`; skip that value only when neither the path nor its partial exists. Audit rejected validation as `highlight_clip_delete_rejected` with clip ID, room ID, stage, and truncated reason, then re-raise.

- [ ] **Step 4: Verify highlight tests pass**

Run: `.venv/bin/pytest tests/bili_upload/test_highlights.py -q`

Expected: all highlight tests pass and existing outside-root files remain untouched.

### Task 3: Recover the beta16 page-order false pause

**Files:**
- Modify: `src/blrec/bili_upload/review.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Test: `tests/bili_upload/test_review.py`

**Interfaces:**
- Produces: `ReviewWatcher.recover_legacy_page_order_pauses() -> Awaitable[int]`.
- Matches only: `state='paused'`, `submit_state='confirmed'`, `operator_paused=0`, non-null AID/BVID, and reason `远端分 P 页码与本地顺序不一致`.

- [ ] **Step 1: Write failing targeted recovery test**

Seed one exact legacy pause plus near misses for operator pause, missing BVID, another reason, and an unconfirmed submission. Assert only the exact legacy row becomes `waiting_review`, clears its reason and retry time, and releases its lease.

```python
assert await review.recover_legacy_page_order_pauses() == 1
assert dict(await database.fetchone(
    'SELECT state,review_reason FROM upload_jobs WHERE id=1'
)) == {'state': 'waiting_review', 'review_reason': None}
```

- [ ] **Step 2: Verify the test fails**

Run: `.venv/bin/pytest tests/bili_upload/test_review.py -q`

Expected: `ReviewWatcher` has no recovery method.

- [ ] **Step 3: Implement and invoke the exact recovery**

Perform one guarded SQL update, emit `upload_review_legacy_pause_recovered` with the recovered count, and call the method once during runtime startup after constructing `ReviewWatcher` and before starting the upload loop.

- [ ] **Step 4: Verify review and runtime tests pass**

Run: `.venv/bin/pytest tests/bili_upload/test_review.py tests/bili_upload/test_runtime.py -q`

Expected: all tests pass; no other pause class is changed.

### Task 4: Release and reconcile NAS data

**Files:**
- Modify: `src/blrec/__init__.py`
- Modify: `.github/workflows/test.yml`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `compose.synology.yml`
- Modify: `synology.env.example`
- Modify: `docs/operations/synology-multi-network.md`
- Create: `docs/releases/3.0.0-beta.18.md`
- Modify: release contract tests under `tests/release/`

**Interfaces:**
- Produces: GHCR image `ghcr.io/luweicn/blrec:3.0.0-beta.18`.
- Consumes: NAS Compose project `/volume1/docker/blrec-next/workspace/compose.yml`.

- [ ] **Step 1: Run full repository verification**

Run backend tests and static checks, Angular tests/lint/build, extension tests/typecheck/build, release contract tests, `python -m build`, and `git diff --check`.

- [ ] **Step 2: Prepare beta18 metadata test-first**

Update release contract expectations first and confirm they fail against beta17. Then update version-bearing production/docs files, rerun the release tests, commit, push `master`, tag `v3.0.0-beta.18`, and wait for the release workflow and multi-architecture manifest.

- [ ] **Step 3: Back up and update the NAS**

Create a mode-700 timestamped backup under `/volume1/docker/blrec-next/backups`, copy the database and Compose file, verify the copied database with `PRAGMA quick_check`, update only the image tag, pull, and recreate `blrec-next` through the existing Container Manager Compose project.

- [ ] **Step 4: Run guarded one-time reconciliation**

Inside one SQLite transaction, abort unless all diagnosed preconditions still hold. Remove upload jobs 2 and 3 plus their highlight upload sessions while setting the corresponding `highlight_clips.upload_session_id` to null. Remove job 24 and children, mark recording part 62 excluded for being under 60 seconds, and reset session 28 to `upload_resolution_state='pending'`. Restore job 32/part 81 only if it has no remote filename, upload session, chunks, AID, or BVID; clear its stored identity, restore `artifact_state='ready'` and `upload_state='prepared'`, and set the job to `ready`. Insert one `management_audit` row per repaired target.

- [ ] **Step 5: Verify live state after reconciliation**

Confirm the database quick check, container health and restart count; job 27 transitions through current review without upload calls; session 28 creates a replacement job excluding part 62; job 32 starts from a new local identity; clips 1 and 2 remain available without upload tasks; legacy failed clips 3/4/5/7/8 can be deleted; and no Bilibili archive deletion request appears in logs.
