# Room Upload Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move room upload policies into each recording-room card, expose the supported archive settings, and load Bilibili categories through an account-scoped cached backend endpoint.

**Architecture:** Extend the SQLite policy and immutable upload snapshot first, then add a small `UploadCategoryCatalog` that proxies `/x/vupre/web/archive/pre` through the existing credential/protocol boundary and persists its last successful response. Replace the standalone Angular page with a lazily-created room dialog so 58 cards do not trigger 58 policy or category requests.

**Tech Stack:** Python 3, SQLite, FastAPI/Pydantic, python-liquid, pytest; Angular 15, TypeScript 4.9, NG-ZORRO 15, Jasmine/Karma.

## Global Constraints

- Do not use a git worktree or start duplicate frontend/backend processes.
- Existing policy rows retain current effective behavior; only newly created rules receive the new UI defaults.
- New rules default `enabled`, `publish_dynamic`, `no_reprint`, `auto_comment`, and `danmaku_backfill` to true; close-comment, close-danmaku, and selected-comment default to false.
- Keep `/x/vu/app/add` with the BiliTV token signer; do not switch submission protocol.
- Cache categories for 24 hours per account credential version and return stale data after an upstream failure.
- Do not implement collections, custom cover upload, or scheduled publishing in this plan.
- Preserve the untracked root `AGENTS.md` and do not stage it.

---

### Task 1: Persist and validate complete room policies

**Files:**
- Create: `src/blrec/bili_upload/migrations/0007_initial.sql`
- Modify: `src/blrec/bili_upload/database.py`
- Modify: `src/blrec/bili_upload/policies.py`
- Modify: `src/blrec/web/routers/room_upload_policies.py`
- Test: `tests/bili_upload/test_database.py`
- Test: `tests/bili_upload/test_policies.py`
- Test: `tests/web/test_room_upload_policies_routes.py`

**Interfaces:**
- Consumes: existing `RoomUploadPolicyManager.upsert/get/list` and camel-case FastAPI models.
- Produces: policy fields `part_title_template`, `dynamic_template`, `is_only_self`, `publish_dynamic`, `no_reprint`, `up_selection_reply`, `up_close_reply`, and `up_close_danmu`; `GET /api/v1/room-upload-policies/{room_id}`.

- [ ] **Step 1: Write failing migration and policy tests**

Add assertions equivalent to:

```python
assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 7
assert {
    'part_title_template', 'dynamic_template', 'is_only_self',
    'publish_dynamic', 'no_reprint', 'up_selection_reply',
    'up_close_reply', 'up_close_danmu',
} <= policy_columns

with pytest.raises(InvalidRoomUploadPolicy, match='comments must remain open'):
    await manager.upsert(
        100,
        command(auto_comment=True, up_close_reply=True),
    )
```

Extend route fixtures and assert a single-room GET returns all camel-case fields.

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
pytest -q tests/bili_upload/test_database.py tests/bili_upload/test_policies.py tests/web/test_room_upload_policies_routes.py
```

Expected: failures for schema version 6, missing dataclass fields, and missing GET route.

- [ ] **Step 3: Add migration and minimal domain/API implementation**

The migration adds effective-behavior defaults and a persistent category cache:

```sql
ALTER TABLE room_upload_policies
ADD COLUMN part_title_template TEXT NOT NULL DEFAULT 'P{{ part_index }}';
ALTER TABLE room_upload_policies
ADD COLUMN dynamic_template TEXT NOT NULL DEFAULT '';
ALTER TABLE room_upload_policies
ADD COLUMN is_only_self INTEGER NOT NULL DEFAULT 0 CHECK (is_only_self IN (0,1));
ALTER TABLE room_upload_policies
ADD COLUMN publish_dynamic INTEGER NOT NULL DEFAULT 1 CHECK (publish_dynamic IN (0,1));
ALTER TABLE room_upload_policies
ADD COLUMN no_reprint INTEGER NOT NULL DEFAULT 1 CHECK (no_reprint IN (0,1));
ALTER TABLE room_upload_policies
ADD COLUMN up_selection_reply INTEGER NOT NULL DEFAULT 0 CHECK (up_selection_reply IN (0,1));
ALTER TABLE room_upload_policies
ADD COLUMN up_close_reply INTEGER NOT NULL DEFAULT 0 CHECK (up_close_reply IN (0,1));
ALTER TABLE room_upload_policies
ADD COLUMN up_close_danmu INTEGER NOT NULL DEFAULT 0 CHECK (up_close_danmu IN (0,1));

CREATE TABLE upload_category_cache (
    account_id INTEGER PRIMARY KEY REFERENCES bili_accounts(id) ON DELETE CASCADE,
    credential_version INTEGER NOT NULL CHECK (credential_version > 0),
    payload_json TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);
```

Validate Liquid syntax for all templates and reject these conflicts:

```python
if command.auto_comment and command.up_close_reply:
    raise InvalidRoomUploadPolicy('comments must remain open for automatic comments')
if command.danmaku_backfill and command.up_close_danmu:
    raise InvalidRoomUploadPolicy('danmaku must remain open for backfill')
if command.up_selection_reply and command.up_close_reply:
    raise InvalidRoomUploadPolicy('selected comments require open comments')
```

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the command from Step 2. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/migrations/0007_initial.sql src/blrec/bili_upload/database.py src/blrec/bili_upload/policies.py src/blrec/web/routers/room_upload_policies.py tests/bili_upload/test_database.py tests/bili_upload/test_policies.py tests/web/test_room_upload_policies_routes.py
git commit -m "feat: extend room upload policies"
```

### Task 2: Fetch and cache account-specific upload categories

**Files:**
- Create: `src/blrec/bili_upload/categories.py`
- Modify: `src/blrec/bili_upload/signing.py`
- Modify: `src/blrec/bili_upload/protocol.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/web/main.py`
- Modify: `src/blrec/web/routers/room_upload_policies.py`
- Test: `tests/bili_upload/test_categories.py`
- Test: `tests/bili_upload/test_protocol_matrix.py`
- Test: `tests/web/test_room_upload_policies_routes.py`

**Interfaces:**
- Consumes: `bundle_loader(account_id)`, `BiliProtocolClient.archive_pre(bundle)`, active account/primary-account rows, and `upload_category_cache`.
- Produces: `UploadCategoryCatalog.list(account_mode, account_id, force_refresh=False) -> UploadCategoryCatalogView`; `GET /api/v1/room-upload-policies/categories`.

- [ ] **Step 1: Write failing catalog and route tests**

Cover one fresh fetch, a second call served from cache, credential-version refresh, forced refresh, stale fallback, malformed upstream data, and no-cache failure. Use a fake protocol returning:

```python
{
    'code': 0,
    'data': {
        'typelist': [{
            'id': 4,
            'name': '游戏',
            'children': [{
                'id': 17,
                'name': '单机游戏',
                'desc': '以单机或主机游戏为主要内容',
                'show': True,
            }],
        }],
    },
}
```

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
pytest -q tests/bili_upload/test_categories.py tests/bili_upload/test_protocol_matrix.py tests/web/test_room_upload_policies_routes.py
```

Expected: imports/routes/operation do not exist.

- [ ] **Step 3: Implement protocol operation and catalog**

Add the protocol operation:

```python
'archive_pre': OperationSpec(
    'GET', 'member_api', '/x/vupre/web/archive/pre', 'web_cookie', True
)

async def archive_pre(self, bundle: CredentialBundle) -> Mapping[str, Any]:
    return await self._web_request('archive_pre', bundle)
```

Normalize upstream objects to immutable parent/child views, filter hidden/invalid children, store only normalized JSON, and never store raw responses. Resolve `primary` or `fixed` against an active account. Return `stale=True` after a failed refresh when cached data exists; otherwise surface an unavailable error as HTTP 503.

- [ ] **Step 4: Wire runtime and router, then confirm GREEN**

Expose the catalog from `BiliAccountRuntime`, assign it to the router on startup, and clear it on shutdown. Run the Step 2 tests; expected all pass.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/categories.py src/blrec/bili_upload/signing.py src/blrec/bili_upload/protocol.py src/blrec/bili_upload/runtime.py src/blrec/web/main.py src/blrec/web/routers/room_upload_policies.py tests/bili_upload/test_categories.py tests/bili_upload/test_protocol_matrix.py tests/web/test_room_upload_policies_routes.py
git commit -m "feat: provide cached upload categories"
```

### Task 3: Apply policy fields to immutable jobs and APP submission

**Files:**
- Modify: `src/blrec/bili_upload/upload.py`
- Test: `tests/bili_upload/test_upload.py`

**Interfaces:**
- Consumes: complete `room_upload_policies` row and ordered `_CandidatePart` values.
- Produces: snapshot format version 2 and `/x/vu/app/add` fields including dynamic visibility and interaction controls.

- [ ] **Step 1: Write failing snapshot and payload tests**

Seed a rule containing templates and settings, then assert:

```python
assert snapshot['part_titles'] == ['第 1 P', '第 2 P']
assert snapshot['dynamic'] == '测试直播｜测试主播'
assert snapshot['publish_dynamic'] is False

assert payload['dynamic'] == ''
assert payload['no_disturbance'] == 1
assert payload['no_reprint'] == 0
assert payload['is_only_self'] == 1
assert payload['up_selection_reply'] is True
assert payload['up_close_reply'] is False
assert payload['up_close_danmu'] is False
```

Add a companion case proving `publish_dynamic=True` maps to `no_disturbance=0` and uses the rendered text.

- [ ] **Step 2: Run focused test and confirm RED**

```bash
pytest -q tests/bili_upload/test_upload.py
```

Expected: old fixed `P1` titles and hard-coded submission fields fail assertions.

- [ ] **Step 3: Render and snapshot fields minimally**

Render part titles independently:

```python
part_titles = [
    self._liquid.from_string(str(row['part_title_template'])).render(
        **context,
        part_index=part.part_index,
    ).strip()
    for part in parts
]
```

Render the dynamic template once, write all booleans into the snapshot, accept both format versions 1 and 2 for already-created jobs, and map:

```python
'dynamic': snapshot.get('dynamic', '') if snapshot.get('publish_dynamic') else '',
'no_disturbance': 0 if snapshot.get('publish_dynamic') else 1,
'no_reprint': 1 if snapshot.get('no_reprint', True) else 0,
'is_only_self': 1 if snapshot.get('is_only_self') else 0,
```

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run Step 2. Expected: all upload tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/upload.py tests/bili_upload/test_upload.py
git commit -m "feat: submit configured archive settings"
```

### Task 4: Build the per-room Angular upload-settings dialog

**Files:**
- Create: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.ts`
- Create: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.html`
- Create: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.scss`
- Create: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.spec.ts`
- Create: `webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.model.ts`
- Create: `webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.service.ts`
- Create: `webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.service.spec.ts`
- Modify: `webapp/src/app/tasks/task-item/task-item.component.ts`
- Modify: `webapp/src/app/tasks/task-item/task-item.component.html`
- Modify: `webapp/src/app/tasks/task-item/task-item.component.spec.ts`
- Modify: `webapp/src/app/tasks/tasks.module.ts`

**Interfaces:**
- Consumes: room ID/name, account list, single-policy GET/PUT/DELETE, and category catalog response.
- Produces: `<app-upload-policy-dialog>` and a fifth card action labeled “投稿设置”.

- [ ] **Step 1: Write failing service/dialog/card tests**

Assert the service URLs, lazy card opening, new-rule defaults, existing-rule preservation, category path selection, publish-dynamic disabling of its textarea, conflict messages, save payload, and delete behavior. New defaults must be:

```typescript
{
  enabled: true,
  partTitleTemplate: 'P{{ part_index }}',
  dynamicTemplate: '{{ title }} 录播',
  isOnlySelf: false,
  publishDynamic: true,
  noReprint: true,
  upSelectionReply: false,
  upCloseReply: false,
  upCloseDanmu: false,
  autoComment: true,
  danmakuBackfill: true,
}
```

- [ ] **Step 2: Run focused Angular tests and confirm RED**

```bash
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/tasks/upload-policy-dialog/*.spec.ts' --include='src/app/tasks/task-item/task-item.component.spec.ts'
```

Expected: new files/component/action do not exist.

- [ ] **Step 3: Implement the typed service and single-column dialog**

Use a searchable `nz-cascader` for `[parentId, tid]`, full-width tag input, four visual sections, field help text, cached/stale status, and a refresh button. Do not create the dialog until the card action is clicked. Use front-end conflict getters only for immediate feedback; backend remains authoritative.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run Step 2. Expected: all selected specs pass.

- [ ] **Step 5: Commit**

```bash
git add webapp/src/app/tasks/upload-policy-dialog webapp/src/app/tasks/task-item webapp/src/app/tasks/tasks.module.ts
git commit -m "feat: configure uploads from recording rooms"
```

### Task 5: Remove standalone policy navigation and verify the feature

**Files:**
- Delete: `webapp/src/app/upload-policies/`
- Modify: `webapp/src/app/app-routing.module.ts`
- Modify: `webapp/src/app/app.component.html`
- Modify: `webapp/src/app/app.component.spec.ts`
- Test: all Python and Angular suites

**Interfaces:**
- Consumes: the Task 4 dialog.
- Produces: “录制任务” navigation and `/upload-policies -> /tasks` compatibility redirect.

- [ ] **Step 1: Write failing navigation assertions**

```typescript
expect(compiled.querySelector('a[href="/upload-policies"]')).toBeNull();
expect(compiled.querySelector('a[href="/tasks"]')?.textContent?.trim()).toBe('录制任务');
```

Add a router assertion that navigating to `/upload-policies` resolves to `/tasks`.

- [ ] **Step 2: Run the app spec and confirm RED**

```bash
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/app.component.spec.ts'
```

Expected: old link and label remain.

- [ ] **Step 3: Remove page/module and add redirect**

Replace the lazy route with:

```typescript
{ path: 'upload-policies', pathMatch: 'full', redirectTo: '/tasks' }
```

Delete only the now-unreferenced standalone page, routing module, and its old local shared files.

- [ ] **Step 4: Run complete verification**

```bash
pytest -q
black --check src tests
isort --check-only src tests
flake8 src tests
mypy src/blrec
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless
cd webapp && npx ng lint
cd webapp && npm run build
```

Expected: all tests/checks pass; only pre-existing Angular build warnings remain.

- [ ] **Step 5: Commit**

```bash
git add -A webapp/src/app/upload-policies webapp/src/app/app-routing.module.ts webapp/src/app/app.component.html webapp/src/app/app.component.spec.ts
git commit -m "refactor: move upload rules into recording tasks"
```
