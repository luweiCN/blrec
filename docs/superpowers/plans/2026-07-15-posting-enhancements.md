# Posting Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add account-scoped collections, reusable manual covers, live-cover upload, and native Bilibili scheduled publishing to room posting rules.

**Architecture:** Migration 9 stores policy fields, manual cover assets, per-account uploaded cover URLs, and collection branch state. Focused collection and cover services wrap `BiliProtocolClient`; `UploadCoordinator` freezes resolved values in the existing policy snapshot and builds `cover`/`dtime`. `ReviewWatcher` launches collection insertion only after CID verification.

**Tech Stack:** Python 3.8+, SQLite migrations, aiohttp protocol client, FastAPI, Pillow-free image header validation, Angular 15/ng-zorro, Jasmine/Karma.

## Global Constraints

- Collections are owned by the resolved posting account; switching account clears the selection.
- Only manually uploaded covers enter the reusable cover library.
- Cover modes are exactly `live` and `custom`; no automatic video-frame cover.
- Scheduled publish uses Bilibili `dtime`, with delay 2 hours through 15 days.
- Collection failure never changes a successfully approved archive to failed.

---

### Task 1: Migration 9 and policy contract

**Files:**
- Create: `src/blrec/bili_upload/migrations/0009_initial.sql`
- Modify: `src/blrec/bili_upload/database.py`
- Modify: `src/blrec/bili_upload/policies.py`
- Modify: `src/blrec/web/routers/room_upload_policies.py`
- Test: `tests/bili_upload/test_database.py`
- Test: `tests/bili_upload/test_policies.py`
- Test: `tests/web/test_room_upload_policies_routes.py`

**Interfaces:**
- Policy fields: `collection_season_id`, `collection_section_id`, `cover_mode`, `cover_asset_id`, `publish_delay_seconds`.
- Job fields: `scheduled_publish_at`, `collection_branch_state`, `collection_error`.

- [ ] **Step 1: Write failing migration and validation tests**

```python
assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 9
with pytest.raises(InvalidRoomUploadPolicy):
    await manager.upsert(100, command(cover_mode='custom', cover_asset_id=None))
with pytest.raises(InvalidRoomUploadPolicy):
    await manager.upsert(100, command(publish_delay_seconds=3600))
```

- [ ] **Step 2: Verify RED**

Run: `pytest -q tests/bili_upload/test_database.py tests/bili_upload/test_policies.py tests/web/test_room_upload_policies_routes.py`

Expected: schema version remains 8 and new command fields are rejected by constructors.

- [ ] **Step 3: Add constrained schema and parameterized policy persistence**

Create `cover_assets` with unique SHA-256, safe local path, MIME, dimensions, byte size and timestamps; create `cover_asset_uploads` keyed by `(asset_id, account_id)`; add policy/job columns with CHECK and foreign keys. Set `BiliUploadDatabase.latest_version` behavior to 9 and include the new tables in tests.

- [ ] **Step 4: Run tests and commit**

Run the Step 2 command; expected PASS.

Commit: `git commit -m "feat: persist collection cover and schedule settings"`

### Task 2: Bilibili collection and cover protocol operations

**Files:**
- Modify: `src/blrec/bili_upload/signing.py`
- Modify: `src/blrec/bili_upload/protocol.py`
- Modify: `tests/bili_upload/fixtures/protocol/responses.json`
- Test: `tests/bili_upload/test_protocol_matrix.py`

**Interfaces:**
- `list_collections(bundle)`, `create_collection(bundle, title, description)`, `add_collection_episode(bundle, section_id, aid, cid, title)`.
- `upload_cover(bundle, filename, mime_type, content) -> str`.

- [ ] **Step 1: Add failing request-shape tests**

Assert method, official member/API path, CSRF placement, referer, safe request representation, response shape, and uncertain-outcome behavior for each write.

- [ ] **Step 2: Verify RED**

Run: `pytest -q tests/bili_upload/test_protocol_matrix.py`

Expected: operation keys and client methods are missing.

- [ ] **Step 3: Implement protocol methods using existing request helpers**

```python
async def add_collection_episode(self, bundle, params):
    return await self._csrf_request('add_collection_episode', bundle, params)
```

Cover upload uses the protocol-matrix multipart field names captured by its request-shape fixture and returns only a validated HTTPS URL. Writes remain non-idempotent so a sent-but-unknown request is never silently repeated.

- [ ] **Step 4: Run and commit**

Run the Step 2 command; expected PASS.

Commit: `git commit -m "feat: add collection and cover protocol operations"`

### Task 3: Account-scoped collection catalog and creation API

**Files:**
- Create: `src/blrec/bili_upload/collections.py`
- Create: `src/blrec/web/routers/bili_collections.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/web/routers/__init__.py`
- Modify: `src/blrec/web/main.py`
- Test: `tests/bili_upload/test_collections.py`
- Test: `tests/web/test_bili_collections_routes.py`

**Interfaces:**
- `CollectionManager.list(account_mode, account_id)` returns seasons with sections.
- `CollectionManager.create(account_mode, account_id, title, description)` creates, refreshes, and returns the new default section.

- [ ] **Step 1: Write failing account isolation, validation and create-refresh tests**

Test primary/fixed account resolution, inactive account rejection, blank/oversized text, response normalization, and that a newly created collection is fetched before return.

- [ ] **Step 2: Verify RED**

Run: `pytest -q tests/bili_upload/test_collections.py tests/web/test_bili_collections_routes.py`

Expected: modules are missing.

- [ ] **Step 3: Implement manager, routes and runtime wiring**

Use `CredentialStore` bundle loading from runtime. Routes are authenticated and map validation to 409, account/protocol unavailability to 503. Never accept a UID or credential from the client.

- [ ] **Step 4: Run and commit**

Run the Step 2 command; expected PASS.

Commit: `git commit -m "feat: manage account collections"`

### Task 4: Manual cover library and upload API

**Files:**
- Create: `src/blrec/bili_upload/covers.py`
- Create: `src/blrec/web/routers/upload_covers.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/web/routers/__init__.py`
- Modify: `src/blrec/web/main.py`
- Test: `tests/bili_upload/test_covers.py`
- Test: `tests/web/test_upload_covers_routes.py`

**Interfaces:**
- `CoverLibrary.add(content, filename) -> CoverAssetView`, `list()`, `open(asset_id)`.
- `CoverResolver.remote_url(asset_id, account_id) -> str` caches by asset/account.

- [ ] **Step 1: Write failing tests**

Cover JPEG/PNG signature parsing, minimum 1146×717 dimensions, maximum 2 MiB, hash dedupe, path containment, content-disposition safety, account-specific remote cache, and upload failure without DB cache mutation.

- [ ] **Step 2: Verify RED**

Run: `pytest -q tests/bili_upload/test_covers.py tests/web/test_upload_covers_routes.py`

Expected: modules/routes are missing.

- [ ] **Step 3: Implement the focused library**

Store assets under a runtime-owned `cover-assets/<sha256>.<ext>` directory using exclusive creation and mode 0600. Parse only image headers needed for dimensions; do not trust extension. Serve thumbnails/originals by asset ID through authenticated endpoints.

- [ ] **Step 4: Run and commit**

Run the Step 2 command; expected PASS.

Commit: `git commit -m "feat: add reusable manual cover library"`

### Task 5: Upload, schedule and post-approval collection flow

**Files:**
- Modify: `src/blrec/bili_upload/upload.py`
- Modify: `src/blrec/bili_upload/review.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Test: `tests/bili_upload/test_upload.py`
- Test: `tests/bili_upload/test_review.py`

**Interfaces:**
- Snapshot contains resolved account, season/section, cover mode/asset and publish delay.
- Review normalizes scheduled state separately and starts collection branch after CID verification.

- [ ] **Step 1: Add failing coordinator/review tests**

Test local live cover, remote live-cover download, manual cover account cache, `dtime = submit_clock + delay`, immediate omission, `-40` scheduled waiting display, and collection add after verified CID only.

- [ ] **Step 2: Verify RED**

Run: `pytest -q tests/bili_upload/test_upload.py tests/bili_upload/test_review.py`

Expected: payload lacks cover resolution/dtime and review treats scheduled state as ordinary waiting.

- [ ] **Step 3: Implement minimal integration**

Resolve/freeze all values before submission. Store `scheduled_publish_at` in the same transaction that enters submitting. On approval, atomically change `collection_branch_state` from pending to running before calling Bilibili; record completed/failed independently.

- [ ] **Step 4: Run and commit**

Run the Step 2 command; expected PASS.

Commit: `git commit -m "feat: apply covers schedules and collections to uploads"`

### Task 6: Posting-rule UI

**Files:**
- Modify: `webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.model.ts`
- Modify: `webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.service.ts`
- Modify: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.{ts,html,scss,spec.ts}`
- Create: `webapp/src/app/tasks/upload-policy-dialog/cover-library/cover-library.component.{ts,html,scss,spec.ts}`
- Modify: `webapp/src/app/tasks/tasks.module.ts`

**Interfaces:**
- Draft fields mirror migration fields exactly.
- Child cover library emits selected `coverAssetId` and owns upload/list states.

- [ ] **Step 1: Write failing UI tests**

Cover account changes clearing collection, create collection auto-selection, live/custom cover selection, library-only manual images, immediate/delayed validation, keyboard-accessible modal/menu, and save payload.

- [ ] **Step 2: Verify RED**

Run focused Karma tests for `upload-policy-dialog` and `cover-library`; expected missing fields/components.

- [ ] **Step 3: Implement compact form sections and services**

Use persistent labels, ng-zorro modal focus management, visible validation, and no explanatory paragraphs that duplicate the control label. Delay input is enabled only for delayed publish and validates 2–360 hours.

- [ ] **Step 4: Run full frontend verification and commit**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless && npx ng lint && npm run build`

Expected: PASS.

Commit: `git commit -m "feat: configure collections covers and scheduled publishing"`

### Task 7: Real acceptance

- [ ] Submit one immediate live-cover archive and verify cover/metadata in Bilibili.
- [ ] Submit one scheduled custom-cover archive and verify planned time without waiting for external notifications.
- [ ] Create a collection, approve a test archive, and verify it joins the selected section.
- [ ] Record AID/BVID and delete test archives only after the user-visible checks are captured.
