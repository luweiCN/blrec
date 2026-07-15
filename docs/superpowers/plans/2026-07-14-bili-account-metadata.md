# B 站账号信息与按需续期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 Angular 账号页展示头像、添加时间、凭据过期时间和版本说明，并让手动按钮只在确有需要时续期。

**Architecture:** SQLite 迁移保存头像 URL 与凭据过期时间，`AccountManager` 统一完成身份校验、元数据更新和按需续期，FastAPI 仅暴露非敏感元数据。Angular 15 继续使用当前 `UploadsComponent`、RxJS 和 ng-zorro，不引入新框架或状态库。

**Tech Stack:** Python 3.9、SQLite、FastAPI/Pydantic、pytest；Angular 15、TypeScript 4.9、RxJS 7、ng-zorro、Jasmine/Karma。

## Global Constraints

- 不改变 Angular 15 + RxJS + ng-zorro 架构。
- 列表接口不得返回或记录 token、Cookie、refresh token 或上游完整响应。
- 账号列表加载不得为展示额外请求 B 站，也不得逐条解密凭据。
- 时间使用 Unix 秒传输，前端按浏览器本地时区显示。
- 头像缺失或加载失败时显示昵称首字。

---

### Task 1: 持久化账号展示元数据

**Files:**
- Create: `src/blrec/bili_upload/migrations/0002_initial.sql`
- Modify: `src/blrec/bili_upload/database.py`
- Modify: `src/blrec/bili_upload/credentials.py`
- Test: `tests/bili_upload/test_database.py`
- Test: `tests/bili_upload/test_credentials.py`

**Interfaces:**
- Consumes: `CredentialBundle.expires_at`、现有 `bili_accounts.created_at`。
- Produces: `CredentialStore.put(..., avatar_url: str, ...)` 与 `CredentialStore.update_metadata(account_id, account_uid, display_name, avatar_url, credential_expires_at, now=None)`。

- [ ] **Step 1: 写迁移与存储层失败测试**

```python
assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 2
columns = {row['name'] for row in await database.fetchall('PRAGMA table_info(bili_accounts)')}
assert {'avatar_url', 'credential_expires_at'} <= columns

await store.put(
    account_id=1,
    account_uid=42,
    display_name='fixture',
    avatar_url='https://i0.hdslb.com/face.jpg',
    bundle=stored_bundle(expires_at=2_000_000),
    cipher=cipher,
    now=100,
)
row = await database.fetchone(
    'SELECT avatar_url,credential_expires_at,created_at FROM bili_accounts WHERE id=1'
)
assert dict(row) == {
    'avatar_url': 'https://i0.hdslb.com/face.jpg',
    'credential_expires_at': 2_000_000,
    'created_at': 100,
}
```

- [ ] **Step 2: 运行测试确认因缺少迁移和参数而失败**

Run: `.venv/bin/python -m pytest -c setup.cfg -q tests/bili_upload/test_database.py tests/bili_upload/test_credentials.py`

Expected: FAIL，提示 schema 版本仍为 1、缺少字段或 `avatar_url` 参数。

- [ ] **Step 3: 实现迁移和元数据原子更新**

```sql
ALTER TABLE bili_accounts ADD COLUMN avatar_url TEXT NOT NULL DEFAULT '';
ALTER TABLE bili_accounts
    ADD COLUMN credential_expires_at INTEGER NOT NULL DEFAULT 0
    CHECK (credential_expires_at >= 0);
```

`CredentialStore.put` 在 INSERT/UPDATE 中同步写入 `avatar_url` 与 `bundle.expires_at`，但续期以外的 `update_metadata` 不修改 `credential_ciphertext`、`credential_version` 或 `created_at`。`database.py` 将最高 schema 版本改为 2，并依次应用 0001、0002。

- [ ] **Step 4: 运行存储层测试**

Run: `.venv/bin/python -m pytest -c setup.cfg -q tests/bili_upload/test_database.py tests/bili_upload/test_credentials.py`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/blrec/bili_upload/migrations/0002_initial.sql src/blrec/bili_upload/database.py src/blrec/bili_upload/credentials.py tests/bili_upload/test_database.py tests/bili_upload/test_credentials.py
git commit -m "feat: persist Bilibili account metadata"
```

### Task 2: 检查并按需续期

**Files:**
- Modify: `src/blrec/bili_upload/accounts.py`
- Modify: `src/blrec/web/routers/bili_accounts.py`
- Test: `tests/bili_upload/test_accounts.py`
- Test: `tests/web/test_bili_accounts_routes.py`

**Interfaces:**
- Consumes: Task 1 的 `CredentialStore.put`、`CredentialStore.update_metadata`。
- Produces: `AccountView.avatar_url/created_at/credential_expires_at`、`RenewalCheckResult(credential_version, refreshed)`、`AccountManager.check_account_renewal(account_id)`，API 返回 `avatarUrl`、`createdAt`、`credentialExpiresAt`、`refreshed`。

- [ ] **Step 1: 写账号逻辑与 API 失败测试**

```python
result = await manager.check_account_renewal(1)
assert result.refreshed is False
assert result.credential_version == 1
assert protocol.refresh_calls == 0

clock.advance(180 * 24 * 3600 - 71 * 3600)
result = await manager.check_account_renewal(1)
assert result.refreshed is True
assert result.credential_version == 2
assert protocol.refresh_calls == 1

assert client.get('/api/v1/bili-accounts', headers=auth_headers()).json() == [{
    'id': 7,
    'uid': 42,
    'displayName': 'fixture',
    'avatarUrl': 'https://i0.hdslb.com/face.jpg',
    'credentialVersion': 3,
    'credentialExpiresAt': 2_000_000,
    'createdAt': 100,
    'state': 'active',
}]
```

- [ ] **Step 2: 运行测试确认缺少接口与字段**

Run: `.venv/bin/python -m pytest -c setup.cfg -q tests/bili_upload/test_accounts.py tests/web/test_bili_accounts_routes.py`

Expected: FAIL，提示缺少 `check_account_renewal`、`avatarUrl` 或 `refreshed`。

- [ ] **Step 3: 实现统一身份结果与按需续期**

```python
@dataclass(frozen=True)
class RenewalCheckResult:
    credential_version: int
    refreshed: bool

@dataclass(frozen=True)
class _IdentityView:
    display_name: str
    avatar_url: str
    refresh_requested: bool
```

`_validate_identity` 从 OAuth 结果读取 `refresh`，从 Web nav 读取 `uname` 与 `face`。`check_account_renewal` 在账号写锁内校验现有凭据；仅当 `bundle.expires_at - now < 72 * 3600` 或 `refresh_requested` 为真时调用刷新，否则仅更新非敏感元数据并保持版本。每日健康检查复用同一方法。

- [ ] **Step 4: 扩展 Pydantic 响应并让现有 `/refresh` 路由执行按需检查**

```python
class AccountResponse(ApiModel):
    id: int
    uid: int
    display_name: str
    avatar_url: str
    credential_version: int
    credential_expires_at: int
    created_at: int
    state: str

class RefreshResponse(ApiModel):
    credential_version: int
    refreshed: bool
```

- [ ] **Step 5: 运行账号与路由测试**

Run: `.venv/bin/python -m pytest -c setup.cfg -q tests/bili_upload/test_accounts.py tests/web/test_bili_accounts_routes.py`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/blrec/bili_upload/accounts.py src/blrec/web/routers/bili_accounts.py tests/bili_upload/test_accounts.py tests/web/test_bili_accounts_routes.py
git commit -m "feat: check Bilibili renewal only when needed"
```

### Task 3: 完善 Angular 账号卡片

**Files:**
- Modify: `webapp/src/app/uploads/shared/bili-account.model.ts`
- Modify: `webapp/src/app/uploads/shared/bili-account.service.ts`
- Modify: `webapp/src/app/uploads/uploads.component.ts`
- Modify: `webapp/src/app/uploads/uploads.component.html`
- Modify: `webapp/src/app/uploads/uploads.component.scss`
- Modify: `webapp/src/app/uploads/uploads.module.ts`
- Test: `webapp/src/app/uploads/uploads.component.spec.ts`
- Test: `webapp/src/app/uploads/shared/bili-account.service.spec.ts`

**Interfaces:**
- Consumes: Task 2 的 camelCase 账号 DTO 与 `{ credentialVersion, refreshed }`。
- Produces: 头像/昵称/UID/状态/时间/版本卡片、“检查并按需续期”交互和成功提示。

- [ ] **Step 1: 写 Angular 失败测试**

```typescript
expect(text).toContain('添加时间');
expect(text).toContain('凭据过期时间');
expect(text).toContain('检查并按需续期');
expect(text).toContain('每次成功更换登录凭据后递增');
expect(fixture.nativeElement.querySelector('[data-testid="account-avatar"]')).not.toBeNull();
expect(accountService.refreshAccount).toHaveBeenCalledOnceWith(7);
```

- [ ] **Step 2: 运行组件测试确认缺少字段和界面**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/uploads/**/*.spec.ts'`

Workdir: `webapp`

Expected: FAIL，提示新 DTO 字段或页面文本不存在。

- [ ] **Step 3: 扩展 TypeScript DTO 与组件状态**

```typescript
export interface BiliAccount {
  id: number;
  uid: number;
  displayName: string;
  avatarUrl: string;
  credentialVersion: number;
  credentialExpiresAt: number;
  createdAt: number;
  state: AccountState;
}

export interface RefreshResult {
  credentialVersion: number;
  refreshed: boolean;
}
```

组件用现有 OnPush 数据流更新账号列表，并依据 `refreshed` 显示“凭据已续期”或“凭据当前有效，暂不需要续期”。时间使用 Angular `date` 管道按本地时区格式化，0 显示“暂未获取”。

- [ ] **Step 4: 更新模板、样式和 ng-zorro 模块**

使用 `nz-avatar` 的 `nzSrc` 与昵称首字回退；使用 `nz-tooltip` 解释凭据版本；账号内容仍位于现有列表项，不新建页面或全局状态。

- [ ] **Step 5: 运行 Angular 测试与构建**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/uploads/**/*.spec.ts'`

Run: `npm run build`

Workdir: `webapp`

Expected: 测试 PASS，production build 成功。

- [ ] **Step 6: 提交**

```bash
git add webapp/src/app/uploads
git commit -m "feat: show Bilibili account details"
```

### Task 4: 集成验证

**Files:**
- Test only; no new production files.

**Interfaces:**
- Consumes: Tasks 1–3 的数据库、API 和 Angular 页面。
- Produces: 可在本机现有服务验证的完整账号卡片与按需续期行为。

- [ ] **Step 1: 运行完整相关测试**

Run: `.venv/bin/python -m pytest -c setup.cfg -q tests/bili_upload tests/web/test_bili_accounts_routes.py`

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/uploads/**/*.spec.ts'`

Run: `npm run build`

Expected: 全部 PASS。

- [ ] **Step 2: 检查差异和敏感信息**

Run: `git diff --check && rg -n "access_token|refresh_token|Cookie=" webapp/src/app/uploads src/blrec/web/routers/bili_accounts.py`

Expected: `git diff --check` 无输出；界面与响应模型不包含敏感字段。

- [ ] **Step 3: 重启本机唯一后端并验证现有账号**

重启 `localhost:2233` 后确认 schema 版本为 2、账号仍存在、列表 API 返回非敏感元数据；打开 `localhost:4200` 确认头像、添加时间、过期时间、版本提示和按需续期按钮可见。正在录制的文件应正常封口并在启动后续录新分段。
