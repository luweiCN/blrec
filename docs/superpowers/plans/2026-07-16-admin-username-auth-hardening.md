# Admin Username Auth Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为单管理员认证增加环境变量用户名，并让初始化、恢复和登录具备一致且不可绕过的低成本限速保护。

**Architecture:** `BLREC_ADMIN_USERNAME` 是不落库的唯一用户名，`AdminAuthStore` 始终同时校验用户名和 Argon2id 密码。初始化与恢复继续使用 `BLREC_API_KEY`，但通过带作用域的持久限速器保护；Uvicorn 只信任显式配置的代理地址。

**Tech Stack:** Python 3.9、FastAPI、Pydantic、SQLite、argon2-cffi、Angular 15、Jasmine/Karma。

## Global Constraints

- 日常登录只提交用户名和密码，不提交或保存 API Key。
- 配置 `BLREC_API_KEY` 时，首次设置和密码恢复必须同时校验用户名与 API Key；未配置时仅保留回环地址本地操作兼容。
- 用户名为 1～64 个可见字符，区分大小写，拒绝首尾空白和控制字符。
- 同一客户端五分钟内失败五次后锁定十五分钟；登录和初始化恢复分别计数。
- 不新增验证码、短信、TOTP、Redis、外部身份服务或全局账号锁定。
- 不使用 worktree，不改动无关代码，不输出或提交任何真实凭据。

---

### Task 1: 环境配置与可信代理边界

**Files:**
- Modify: `src/blrec/setting/models.py`
- Modify: `src/blrec/cli/main.py`
- Modify: `compose.synology.yml`
- Test: `tests/setting/test_env_credentials.py`

**Interfaces:**
- Produces: `EnvSettings.admin_username: str`，环境变量 `BLREC_ADMIN_USERNAME`；CLI 读取 `BLREC_FORWARDED_ALLOW_IPS`。

- [ ] **Step 1: 写失败测试**

```python
def test_admin_username_defaults_and_rejects_ambiguous_values(monkeypatch):
    monkeypatch.delenv('BLREC_ADMIN_USERNAME', raising=False)
    assert EnvSettings().admin_username == 'admin'
    monkeypatch.setenv('BLREC_ADMIN_USERNAME', ' admin ')
    with pytest.raises(ValueError, match='administrator username'):
        EnvSettings()

def test_cli_trusts_only_configured_forwarding_proxies(monkeypatch):
    monkeypatch.delenv('BLREC_FORWARDED_ALLOW_IPS', raising=False)
    assert forwarded_allow_ips() == '127.0.0.1'
    monkeypatch.setenv('BLREC_FORWARDED_ALLOW_IPS', '172.17.0.1')
    assert forwarded_allow_ips() == '172.17.0.1'
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest -q tests/setting/test_env_credentials.py`
Expected: FAIL，`EnvSettings` 尚无 `admin_username`。

- [ ] **Step 3: 增加严格用户名字段并收紧代理信任**

```python
admin_username: Annotated[
    str,
    Field(env='BLREC_ADMIN_USERNAME', min_length=1, max_length=64),
] = 'admin'

@validator('admin_username')
def validate_admin_username(cls, value: str) -> str:
    if value != value.strip() or any(not char.isprintable() for char in value):
        raise ValueError('administrator username must contain visible characters')
    return value
```

在 `src/blrec/cli/main.py` 增加 `forwarded_allow_ips() -> str`，读取 `BLREC_FORWARDED_ALLOW_IPS` 并默认返回 `127.0.0.1`；`cli_main()` 用它替换 `forwarded_allow_ips='*'`。群晖 compose 强制声明 `BLREC_ADMIN_USERNAME`，并允许显式传入可信代理地址。

- [ ] **Step 4: 运行聚焦测试并提交**

Run: `.venv/bin/python -m pytest -q tests/setting/test_env_credentials.py`
Expected: PASS。

```bash
git add src/blrec/setting/models.py src/blrec/cli/main.py compose.synology.yml tests/setting/test_env_credentials.py
git commit -m "feat: configure administrator username"
```

### Task 2: 用户名校验与分作用域持久限速

**Files:**
- Modify: `src/blrec/web/auth_store.py`
- Test: `tests/web/test_auth_store.py`

**Interfaces:**
- Consumes: `admin_username: str`。
- Produces: `AdminAuthStore(..., admin_username: str = 'admin')`、`initialize(username, password)`、`login(username, password, client_key=...)`、`verify_bootstrap_attempt(username, credential_valid, client_key=...)`。

- [ ] **Step 1: 写用户名与限速隔离失败测试**

```python
def test_username_is_required_and_bootstrap_limit_is_separate(tmp_path):
    clock = Clock()
    auth = AdminAuthStore(
        str(tmp_path / 'auth.sqlite3'),
        admin_username='owner',
        clock=clock,
        max_failed_attempts=3,
        failure_window_seconds=60,
        lockout_seconds=120,
    )
    auth.open()
    with pytest.raises(AuthenticationFailed):
        auth.initialize('wrong', 'correct horse battery staple')
    auth.initialize('owner', 'correct horse battery staple')
    with pytest.raises(AuthenticationFailed):
        auth.login('wrong', 'correct horse battery staple', client_key='client')
    with pytest.raises(AuthenticationFailed):
        auth.verify_bootstrap_attempt('owner', False, client_key='client')
    assert auth.login('owner', 'correct horse battery staple', client_key='client')
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest -q tests/web/test_auth_store.py`
Expected: FAIL，现有方法只接收密码且限速键没有作用域。

- [ ] **Step 3: 最小实现双凭据校验**

用户名使用 UTF-8 字节的 `secrets.compare_digest`。`login()` 即使用户名错误也必须执行现有 Argon2id `verify()`；失败键改为 `login:<client>`。初始化/恢复失败使用 `bootstrap:<client>`，成功只清除同作用域记录。

```python
def _username_matches(self, username: str) -> bool:
    return secrets.compare_digest(
        username.encode('utf8'), self._admin_username.encode('utf8')
    )
```

- [ ] **Step 4: 验证锁定持久化、作用域隔离和成功清理**

Run: `.venv/bin/python -m pytest -q tests/web/test_auth_store.py`
Expected: PASS，用户名错误、密码错误和初始化安全码错误都计入正确作用域。

```bash
git add src/blrec/web/auth_store.py tests/web/test_auth_store.py
git commit -m "feat: harden administrator credential checks"
```

### Task 3: FastAPI 初始化、登录与恢复契约

**Files:**
- Modify: `src/blrec/web/main.py`
- Modify: `src/blrec/web/routers/auth.py`
- Test: `tests/web/test_auth_routes.py`

**Interfaces:**
- Consumes: `EnvSettings.admin_username` 和 Task 2 的 `AdminAuthStore` 方法。
- Produces: 三个请求体：`setup(username, apiKey, password)`、`login(username, password)`、`recover(username, apiKey, newPassword)`。

- [ ] **Step 1: 写失败路由矩阵测试**

```python
def test_daily_login_requires_username_but_not_api_key(client):
    setup_admin(client, username='owner')
    client.cookies.clear()
    response = client.post('/api/v1/auth/login',
        headers={'origin': 'https://testserver'}, json={
        'username': 'owner', 'password': 'correct horse battery staple'
    })
    assert response.status_code == 200
    assert 'apiKey' not in response.request.content.decode()
```

同时覆盖错误用户名/错误密码统一为 `Invalid administrator credentials`，初始化恢复错误统一为 `Invalid initialization credentials`，第五次失败返回 `429` 和 `Retry-After`。

```python
def test_missing_api_key_keeps_loopback_only_setup(tmp_path):
    local = make_client(tmp_path, bootstrap_api_key='', client=('127.0.0.1', 1234))
    assert local.post('/api/v1/auth/setup', headers=SAME_ORIGIN, json={
        'username': 'owner', 'apiKey': '',
        'password': 'correct horse battery staple'
    }).status_code == 200
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest -q tests/web/test_auth_routes.py`
Expected: FAIL，请求模型尚无 `username`。

- [ ] **Step 3: 修改请求模型和路由接线**

```python
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=1024)
```

`setup()` 与 `recover()` 在比较 API Key 后将组合结果交给持久限速器；所有认证失败保持同源检查且不得回显用户名、API Key 或具体错误字段。

- [ ] **Step 4: 运行认证与 WebSocket 回归并提交**

Run: `.venv/bin/python -m pytest -q tests/web/test_auth_routes.py tests/web/test_websockets_auth.py`
Expected: PASS。

```bash
git add src/blrec/web/main.py src/blrec/web/routers/auth.py tests/web/test_auth_routes.py
git commit -m "feat: require administrator username"
```

### Task 4: Angular 三种认证表单

**Files:**
- Modify: `webapp/src/app/core/services/auth.service.ts`
- Modify: `webapp/src/app/core/services/auth.service.spec.ts`
- Modify: `webapp/src/app/auth/auth.component.ts`
- Modify: `webapp/src/app/auth/auth.component.html`
- Modify: `webapp/src/app/auth/auth.component.spec.ts`

**Interfaces:**
- Produces: `setup(username, apiKey, password)`、`login(username, password)`、`recover(username, apiKey, newPassword)`。

- [ ] **Step 1: 写失败组件与服务测试**

```typescript
expect(loginRequest.request.body).toEqual({
  username: 'owner',
  password: 'correct horse battery staple',
});
expect(loginRequest.request.body.apiKey).toBeUndefined();
```

验证首次设置显示三项、登录只显示用户名和密码、恢复显示用户名/API Key/新密码，用户名使用 `autocomplete="username"`，首次密码使用 `new-password`，`429` 显示等待时间。

- [ ] **Step 2: 运行聚焦测试确认失败**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/auth/**/*.spec.ts' --include='src/app/core/services/auth.service.spec.ts'`
Expected: FAIL，服务签名和模板字段尚未改变。

- [ ] **Step 3: 实现最小表单变化**

```typescript
login(username: string, password: string): Observable<AuthSession> {
  return this.http.post<AuthSession>(this.url.makeApiUrl('/api/v1/auth/login'), {
    username,
    password,
  }).pipe(tap((session) => (this.session = session)));
}
```

API Key 只在 `setup`/`recover` 模式存在，切换模式时清空；不得写入 `localStorage` 或 `sessionStorage`。

- [ ] **Step 4: 运行聚焦测试并提交**

Run: `npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/auth/**/*.spec.ts' --include='src/app/core/services/auth.service.spec.ts'`
Expected: PASS。

```bash
git add webapp/src/app/auth webapp/src/app/core/services/auth.service.ts webapp/src/app/core/services/auth.service.spec.ts
git commit -m "feat: add administrator username login"
```

### Task 5: 文档与完整验收

**Files:**
- Modify: `README.md`
- Modify: `docs/operations/synology-multi-network.md`

- [ ] **Step 1: 更新部署示例**

文档明确 `BLREC_ADMIN_USERNAME`、`BLREC_API_KEY` 和 `BLREC_FORWARDED_ALLOW_IPS` 的职责；NAS 示例不得使用 `blrec-local-dev` 等弱安全码。

- [ ] **Step 2: 运行完整验证**

Run: `.venv/bin/python -m pytest -q`
Expected: 全部 PASS。

Run: `.venv/bin/python -m black --check src && .venv/bin/python -m isort --check-only src && .venv/bin/python -m flake8 src && .venv/bin/python -m mypy src/blrec`
Expected: 全部 PASS。

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless && npm run build`
Expected: 全部 PASS，仅允许已记录的体积预算警告。

- [ ] **Step 3: 浏览器冒烟并提交文档**

使用临时开发认证库完成首次设置、退出、用户名密码登录、错误锁定和用户名/API Key 恢复；完成后删除临时管理员和会话，保留服务运行。

```bash
git add README.md docs/operations/synology-multi-network.md src/blrec/data/webapp
git commit -m "docs: document administrator authentication"
```
