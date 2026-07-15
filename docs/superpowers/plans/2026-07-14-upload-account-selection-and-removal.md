# 主账号、投稿账号与账号移除 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让主账号切换立即完成且不中断正常弹幕连接，并实现上传账号快照、关联关系预览和可控的账号移除/迁移流程。

**Architecture:** 新增独立 `AccountLifecycle` 领域服务负责关系查询和单事务归档，`AccountManager` 继续负责凭据及运行时通知。房间策略保存“跟随主账号/固定账号”模式，上传任务始终保存具体账号；Angular 账号页在主账号切换和账号移除前调用同一关系接口展示影响。

**Tech Stack:** Python 3.8+、FastAPI、SQLite/WAL、pytest、Angular 15、TypeScript 4.9、RxJS 7、ng-zorro、Jasmine/Karma。

## Global Constraints

- 不使用 Worktree，不启用子代理；在当前工作目录内串行执行。
- 正常工作的弹幕 WebSocket 不因 Cookie 或主账号切换而重启。
- 读取请求可在再次确认凭据失效后使用备用账号；投稿、评论和弹幕回灌绝不自动换号。
- 新房间策略默认跟随主账号；现有策略迁移为固定账号。
- `upload_jobs.account_id` 是任务身份快照；出现任何远端副作用后禁止改绑。
- 账号使用归档而非物理删除；历史任务保留原账号关系，登录凭据被清除。
- 用户自己的未跟踪 `AGENTS.md` 不得修改、暂存或提交。

---

### Task 0: 固化当前已验证的主账号功能基线

**Files:**
- Commit existing changes under: `src/`, `tests/`, `webapp/`
- Exclude: `AGENTS.md`

**Interfaces:**
- Consumes: 已通过测试的主账号、结构化 Cookie 和录制读取故障转移实现。
- Produces: 后续任务可独立审阅的干净 Git 基线。

- [ ] **Step 1: 重新验证当前基线**

Run: `.venv/bin/python -m pytest -q`

Expected: `302 passed`。

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless`

Expected: `TOTAL: 127 SUCCESS`。

- [ ] **Step 2: 只提交当前功能文件**

```bash
git add src tests webapp
git diff --cached --check
git diff --cached --name-only | rg '^AGENTS\.md$' && exit 1 || true
git commit -m "feat: manage primary Bilibili account credentials"
```

Expected: 提交成功，`AGENTS.md` 仍为未跟踪文件。

### Task 1: 主账号切换不重启弹幕连接

**Files:**
- Modify: `src/blrec/task/task_manager.py:361`
- Modify: `tests/task/test_task_manager_managed_cookie.py`

**Interfaces:**
- Consumes: `managed_cookie_provider(url) -> Optional[str]` 和 `RecordTask.cookie` setter。
- Produces: `refresh_managed_cookie()` 只更新内存请求头，不调用 `restart_danmaku_client()`。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_refresh_managed_cookie_keeps_active_danmaku_connection() -> None:
    provider = AsyncMock(return_value='SESSDATA=next')
    manager = RecordTaskManager(
        object(), managed_cookie_provider=provider  # type: ignore[arg-type]
    )
    task = FakeTask()
    task.cookie = 'SESSDATA=old'
    manager._tasks = {100: task}  # type: ignore[assignment]

    await manager.refresh_managed_cookie()

    assert task.cookie == 'SESSDATA=next'
    task.restart_danmaku_client.assert_not_awaited()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/task/test_task_manager_managed_cookie.py::test_refresh_managed_cookie_keeps_active_danmaku_connection -q`

Expected: FAIL，现有实现调用了一次 `restart_danmaku_client()`。

- [ ] **Step 3: 实现最小修改**

```python
async def refresh_managed_cookie(self) -> None:
    if self._managed_cookie_provider is None:
        return
    cookie = await self._managed_cookie_provider(self._MANAGED_COOKIE_URL)
    if cookie is None:
        return
    for task in self._tasks.values():
        if not task.ready or task.cookie == cookie:
            continue
        task.cookie = cookie
```

- [ ] **Step 4: 验证并提交**

Run: `.venv/bin/python -m pytest tests/task/test_task_manager_managed_cookie.py tests/test_application_live_status.py -q`

Expected: PASS。

```bash
git add src/blrec/task/task_manager.py tests/task/test_task_manager_managed_cookie.py
git commit -m "perf: keep active danmaku connections on account changes"
```

### Task 2: 增加房间账号模式迁移

**Files:**
- Create: `src/blrec/bili_upload/migrations/0004_initial.sql`
- Modify: `src/blrec/bili_upload/database.py:376`
- Modify: `tests/bili_upload/test_database.py`

**Interfaces:**
- Consumes: 现有 `room_upload_policies.account_id`。
- Produces: `account_mode IN ('primary','fixed')`；`primary` 时 `account_id IS NULL`，`fixed` 时 `account_id IS NOT NULL`。

- [ ] **Step 1: 写迁移失败测试**

```python
account_mode_columns = {
    row['name']
    for row in await database.fetchall('PRAGMA table_info(room_upload_policies)')
}
assert 'account_mode' in account_mode_columns
assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 4
```

在旧库迁移测试中插入一个房间策略，并断言升级后为固定模式：

```python
row = await database.fetchone(
    'SELECT account_mode,account_id FROM room_upload_policies WHERE room_id=100'
)
assert dict(row) == {'account_mode': 'fixed', 'account_id': 1}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/bili_upload/test_database.py -q`

Expected: FAIL，最新版本仍为 3 且没有 `account_mode`。

- [ ] **Step 3: 编写迁移**

```sql
CREATE TABLE room_upload_policies_v4 (
    room_id INTEGER PRIMARY KEY,
    account_mode TEXT NOT NULL CHECK (account_mode IN ('primary','fixed')),
    account_id INTEGER REFERENCES bili_accounts(id),
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
    title_template TEXT NOT NULL,
    description_template TEXT NOT NULL,
    tid INTEGER NOT NULL CHECK (tid > 0),
    tags TEXT NOT NULL,
    copyright INTEGER NOT NULL CHECK (copyright IN (1,2)),
    source TEXT NOT NULL,
    auto_comment INTEGER NOT NULL CHECK (auto_comment IN (0,1)),
    danmaku_backfill INTEGER NOT NULL CHECK (danmaku_backfill IN (0,1)),
    filter_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CHECK (
        (account_mode='primary' AND account_id IS NULL) OR
        (account_mode='fixed' AND account_id IS NOT NULL)
    )
);

INSERT INTO room_upload_policies_v4 (
    room_id,account_mode,account_id,enabled,title_template,
    description_template,tid,tags,copyright,source,auto_comment,
    danmaku_backfill,filter_json,created_at,updated_at
)
SELECT room_id,'fixed',account_id,enabled,title_template,description_template,
       tid,tags,copyright,source,auto_comment,danmaku_backfill,filter_json,
       created_at,updated_at
FROM room_upload_policies;

DROP TABLE room_upload_policies;
ALTER TABLE room_upload_policies_v4 RENAME TO room_upload_policies;
```

把 `latest_version` 更新为 `4`。

- [ ] **Step 4: 验证约束和提交**

Run: `.venv/bin/python -m pytest tests/bili_upload/test_database.py -q`

Expected: PASS，并验证非法模式组合触发 `sqlite3.IntegrityError`。

```bash
git add src/blrec/bili_upload/migrations/0004_initial.sql src/blrec/bili_upload/database.py tests/bili_upload/test_database.py
git commit -m "feat: add upload account policy modes"
```

### Task 3: 实现关联查询与事务化账号移除

**Files:**
- Create: `src/blrec/bili_upload/account_lifecycle.py`
- Create: `tests/bili_upload/test_account_lifecycle.py`
- Modify: `src/blrec/bili_upload/accounts.py`
- Modify: `src/blrec/bili_upload/__init__.py`
- Modify: `tests/bili_upload/test_accounts.py`

**Interfaces:**
- Consumes: `BiliUploadDatabase.write()`、`bili_account_selection`、`room_upload_policies`、`upload_jobs` 和 `upload_parts`。
- Produces: `AccountLifecycle.relationships(account_id)`、`AccountLifecycle.remove(account_id, command, manager_subject)`、`AccountManager.account_relationships()` 和 `AccountManager.remove_account()`。

- [ ] **Step 1: 定义领域类型和失败测试**

```python
class RemovalMode(str, Enum):
    FOLLOW_PRIMARY = 'follow_primary'
    FIXED = 'fixed'
    DISABLE = 'disable'

@dataclass(frozen=True)
class RelatedUploadJob:
    id: int
    room_id: int
    state: str

@dataclass(frozen=True)
class AccountRelationships:
    account_id: int
    is_primary: bool
    follow_primary_room_ids: Tuple[int, ...]
    fixed_room_ids: Tuple[int, ...]
    reassignable_jobs: Tuple[RelatedUploadJob, ...]
    blocking_jobs: Tuple[RelatedUploadJob, ...]
    historical_job_count: int

@dataclass(frozen=True)
class AccountRemovalCommand:
    mode: RemovalMode
    replacement_account_id: Optional[int] = None
    new_primary_account_id: Optional[int] = None

@dataclass(frozen=True)
class AccountRemovalResult:
    account_id: int
    state: str = 'archived'
```

测试分别建立：固定房间、跟随主账号房间、`ready/prepared` 可迁移任务、已产生预上传状态的阻塞任务和 `completed` 历史任务，断言分类准确。

- [ ] **Step 2: 运行分类测试确认失败**

Run: `.venv/bin/python -m pytest tests/bili_upload/test_account_lifecycle.py -q`

Expected: FAIL，模块尚不存在。

- [ ] **Step 3: 实现关系查询**

`AccountLifecycle.relationships()` 在一次数据库读操作中：

```python
reassignable = (
    job_state in {'waiting_artifacts', 'ready', 'paused'}
    and submit_state == 'prepared'
    and not has_started_part
)
historical = job_state in {'completed', 'rejected'}
blocking = not reassignable and not historical
```

生命周期模块定义自己的 `LifecycleAccountNotFound`、`AccountRemovalBlocked`
和 `InvalidAccountReplacement`，避免与 `accounts.py` 形成循环导入；
`AccountManager` 将不存在账号转换为现有 `AccountNotFound`。归档账号仍可供历史查询，
但不能作为替代账号。

- [ ] **Step 4: 写三种移除方式的失败测试**

```python
await lifecycle.remove(
    account_id=1,
    command=AccountRemovalCommand(RemovalMode.FOLLOW_PRIMARY, new_primary_account_id=2),
    manager_subject='operator-hash',
)
assert await database.scalar(
    'SELECT account_mode FROM room_upload_policies WHERE room_id=100'
) == 'primary'
assert await database.scalar('SELECT account_id FROM upload_jobs WHERE id=1') == 2
assert await database.scalar('SELECT state FROM bili_accounts WHERE id=1') == 'archived'
```

另测 `FIXED` 批量改绑、`DISABLE` 关闭房间并暂停任务、阻塞任务拒绝、当前主账号无替代账号限制，以及异常时整个事务回滚。

- [ ] **Step 5: 实现单事务移除**

`remove()` 在 `database.write()` 的同一个 `BEGIN IMMEDIATE` 事务中重新计算关系并执行：

```python
if relationships.blocking_jobs:
    raise AccountRemovalBlocked(relationships.blocking_jobs)

# 根据 command 更新主账号、房间策略和可迁移任务；随后归档并清除凭据。
connection.execute(
    "UPDATE bili_accounts SET state='archived',pause_reason=?,"
    "credential_ciphertext=X'',key_id='archived',credential_expires_at=0,"
    "updated_at=? WHERE id=? AND state!='archived'",
    ('removed by operator', now, account_id),
)
```

同时写入 `management_audit`。`AccountManager.remove_account()` 成功后调用一次凭据变更通知；`list_accounts()` 默认排除 `archived`。同 UID 再次扫码通过现有 `CredentialStore.put()` 恢复为 `active`。

- [ ] **Step 6: 验证并提交**

Run: `.venv/bin/python -m pytest tests/bili_upload/test_account_lifecycle.py tests/bili_upload/test_accounts.py -q`

Expected: PASS。

```bash
git add src/blrec/bili_upload/account_lifecycle.py src/blrec/bili_upload/accounts.py src/blrec/bili_upload/__init__.py tests/bili_upload/test_account_lifecycle.py tests/bili_upload/test_accounts.py
git commit -m "feat: manage Bilibili account relationships and removal"
```

### Task 4: 暴露关联查询与移除 API

**Files:**
- Modify: `src/blrec/web/routers/bili_accounts.py`
- Modify: `tests/web/test_bili_accounts_routes.py`

**Interfaces:**
- Consumes: `AccountManager.account_relationships()` 和 `remove_account()`。
- Produces: `GET /api/v1/bili-accounts/{id}/relationships`、`POST /api/v1/bili-accounts/{id}/removal`。

- [ ] **Step 1: 写路由失败测试**

```python
response = client.get(
    '/api/v1/bili-accounts/7/relationships', headers=auth_headers()
)
assert response.status_code == 200
assert response.json()['isPrimary'] is True
assert response.json()['fixedRoomIds'] == [100]

removed = client.post(
    '/api/v1/bili-accounts/7/removal',
    headers=auth_headers(),
    json={'mode': 'fixed', 'replacementAccountId': 8},
)
assert removed.status_code == 200
assert removed.json() == {'accountId': 7, 'state': 'archived'}
```

另测缺失账号 `404`、非法替代账号及阻塞任务 `409`，并确认响应中没有 token/Cookie。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/web/test_bili_accounts_routes.py -q`

Expected: FAIL，路由返回 404。

- [ ] **Step 3: 实现 Pydantic 模型和路由**

```python
class AccountRemovalRequest(ApiModel):
    mode: RemovalMode
    replacement_account_id: Optional[int] = None
    new_primary_account_id: Optional[int] = None

@router.get('/{account_id}/relationships', response_model=AccountRelationshipsResponse)
async def account_relationships(...):
    return await account_manager.account_relationships(account_id)

@router.post('/{account_id}/removal', response_model=AccountRemovalResponse)
async def remove_account(payload: AccountRemovalRequest, subject: str = Depends(...), ...):
    return await account_manager.remove_account(account_id, payload.to_command(), subject)
```

- [ ] **Step 4: 验证并提交**

Run: `.venv/bin/python -m pytest tests/web/test_bili_accounts_routes.py -q`

Expected: PASS。

```bash
git add src/blrec/web/routers/bili_accounts.py tests/web/test_bili_accounts_routes.py
git commit -m "feat: expose Bilibili account lifecycle API"
```

### Task 5: 增加主账号影响和账号移除弹窗

**Files:**
- Modify: `webapp/src/app/uploads/shared/bili-account.model.ts`
- Modify: `webapp/src/app/uploads/shared/bili-account.service.ts`
- Modify: `webapp/src/app/uploads/shared/bili-account.service.spec.ts`
- Modify: `webapp/src/app/uploads/uploads.component.ts`
- Modify: `webapp/src/app/uploads/uploads.component.html`
- Modify: `webapp/src/app/uploads/uploads.component.scss`
- Modify: `webapp/src/app/uploads/uploads.component.spec.ts`
- Modify: `webapp/src/app/uploads/uploads.module.ts`

**Interfaces:**
- Consumes: 关系查询和账号移除 API。
- Produces: 切换主账号影响预览、账号移除影响预览、三种关联处理选项和替代账号选择。

- [ ] **Step 1: 增加前端模型和服务失败测试**

```typescript
export interface AccountRelationships {
  accountId: number;
  isPrimary: boolean;
  followPrimaryRoomIds: number[];
  fixedRoomIds: number[];
  reassignableJobs: RelatedUploadJob[];
  blockingJobs: RelatedUploadJob[];
  historicalJobCount: number;
}

export type RemovalMode = 'follow_primary' | 'fixed' | 'disable';

export interface AccountRemovalRequest {
  mode: RemovalMode;
  replacementAccountId?: number;
  newPrimaryAccountId?: number;
}
```

服务测试断言关系查询使用 GET、移除使用 POST 且 body 保持 camelCase。

- [ ] **Step 2: 运行服务测试确认失败**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/uploads/shared/bili-account.service.spec.ts'`

Workdir: `webapp`

Expected: FAIL，方法不存在。

- [ ] **Step 3: 写组件交互失败测试**

覆盖以下行为：

```typescript
it('previews relationships before changing the primary account', () => {
  click('[data-testid="set-primary"]');
  expect(accountService.getRelationships).toHaveBeenCalledOnceWith(8);
  expect(accountService.setPrimaryAccount).not.toHaveBeenCalled();
  expect(document.body.textContent).toContain('已创建的上传任务不会改绑');
});

it('requires an explicit removal policy', () => {
  click('[data-testid="remove-account"]');
  expect(document.body.textContent).toContain('改为跟随新主账号');
  expect(document.body.textContent).toContain('固定切换到指定账号');
  expect(document.body.textContent).toContain('不迁移');
});

it('blocks removal when an upload job has remote side effects', () => {
  accountService.getRelationships.and.returnValue(of(blockedRelationships));
  click('[data-testid="remove-account"]');
  expect(document.body.textContent).toContain('必须先处理以下任务');
  expect(query('[data-testid="confirm-remove"]')).toBeDisabled();
});
```

- [ ] **Step 4: 实现弹窗状态和模板**

`UploadsComponent` 保存当前操作账号、关系加载状态、移除模式、替代账号和新主账号。点击“设为主账号”只打开预览；用户确认后才调用原 PUT。每个账号增加“移除账号”危险按钮。

模板使用 `nz-modal`、`nz-radio-group`、`nz-select` 和可展开分类列表。默认移除模式为 `follow_primary`；固定模式必须选择其他 active 账号；阻塞任务存在时禁用确认。当前主账号被移除时必须选择新主账号；无其他账号时强制 `disable`。

在 `UploadsModule` 增加 `FormsModule`、`NzRadioModule`、`NzSelectModule` 和 `NzCollapseModule`。

- [ ] **Step 5: 验证组件并构建**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/uploads/**/*.spec.ts'`

Expected: 上传模块测试全部 PASS。

Run: `npx eslint 'src/app/uploads/**/*.ts'`

Expected: 退出码 0。

Run: `npm run build`

Expected: production build 成功并更新 `src/blrec/data/webapp/`。

- [ ] **Step 6: 提交前端和生成包**

```bash
git add webapp/src/app/uploads src/blrec/data/webapp
git commit -m "feat: add Bilibili account relationship dialogs"
```

### Task 6: 完整验证和本机冒烟

**Files:**
- Test only; no new production files.

**Interfaces:**
- Consumes: 完整账号生命周期和正在运行的本机服务。
- Produces: 可交付的自动化与本机证据。

- [ ] **Step 1: 运行完整后端验证**

Run: `.venv/bin/python -m pytest -q`

Run: `.venv/bin/python -m mypy src/blrec`

Run: 对所有本次修改的 Python 文件执行 Black、isort 和 Flake8 检查。

Expected: 全部退出码为 0。

- [ ] **Step 2: 运行完整前端验证**

Run: `npm test -- --watch=false --browsers=ChromeHeadless`

Run: `npm run build`

Workdir: `webapp`

Expected: 全部测试 PASS，生产构建成功；只允许既有预算和 CommonJS 警告。

- [ ] **Step 3: 本机验证主账号切换**

确认 `PUT /api/v1/bili-accounts/{id}/primary` 在本地事务与请求头更新后立即返回；日志中没有由该操作触发的 `Restarting danmaku client`，正在录制的 FLV 和 XML 持续增长。

- [ ] **Step 4: 本机验证关联与移除保护**

使用只读关系接口检查两个真实账号的预览。不要实际移除用户账号；通过自动化数据库 fixture 验证归档和迁移写操作，页面只冒烟到确认弹窗。

- [ ] **Step 5: 最终工作区检查**

Run: `git diff --check`

Run: `git status --short`

Expected: 没有未提交的功能文件；`AGENTS.md` 保持未跟踪且未提交。
