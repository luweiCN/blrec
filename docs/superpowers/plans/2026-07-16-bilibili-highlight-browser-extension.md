# Bilibili Highlight Browser Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供一个 Chromium 浏览器插件，在 B 站直播页顶部显示“收录 / 收录并投稿 / 添加高光”，使用 BLREC 地址和管理员用户名完成轻量配对，并把播放器延迟修正后的高光书签写入 BLREC。

**Architecture:** 插件采用 Manifest V3，内容脚本只负责页面识别、按钮和播放器观测，所有跨域请求通过 service worker 发出。后端在独立路由中签发权限受限的长期令牌；插件只能查询房间、添加录制任务、应用默认投稿规则和创建高光，不能访问管理员设置或投稿凭据。

**Tech Stack:** Chromium Manifest V3、TypeScript 4.9、esbuild、Vitest/jsdom、FastAPI、现有 `AdminAuthStore`、现有高光和录制任务服务。

## Global Constraints

- 本计划在高光剪辑核心计划完成后执行；依赖 `HighlightService.create_marker()`。
- 插件设置只输入 BLREC 地址和管理员用户名，不输入或保存密码/API Key。
- 管理员用户名匹配属于可信内网的轻量授权，不宣称适合公网暴露。
- 后端签发随机令牌；数据库只保存哈希，令牌只能用于 `/api/v1/browser-extension/*`。
- 未收录显示“收录”和“收录并投稿”；已收录未录制不显示；正在录制只显示“添加高光”。
- “添加高光”立即保存、允许重复点击，不弹出编辑表单。
- 插件不调用额外 B 站房间 API，不读取 Cookie，不接触投稿账号凭据。
- B 站 DOM 变化只能使按钮失效，不能影响 BLREC 后台录制。
- 首版只支持 Chromium Manifest V3，不增加 Firefox 兼容层。

---

## File Structure

### Backend

- Create `src/blrec/web/routers/browser_extension.py`: 配对、房间状态、收录和高光接口。
- Modify `src/blrec/web/auth_store.py`: 插件令牌哈希、签发、校验、撤销和审计。
- Modify `src/blrec/web/security.py`: 明确隔离插件路由与管理员会话路由。
- Modify `src/blrec/web/main.py`, `src/blrec/web/routers/__init__.py`: 组装应用、高光和投稿规则依赖。
- Consume `src/blrec/bili_upload/policies.py`: 使用核心计划提供的默认规则命令。
- Modify `src/blrec/web/routers/auth.py`: 管理员查看和撤销插件令牌。
- Create `tests/web/test_browser_extension_routes.py`.
- Modify `tests/web/test_auth_store.py`, `tests/web/test_auth_routes.py`.
- Create `webapp/src/app/settings/browser-extension-tokens/*`: 管理员查看和撤销插件授权。
- Modify `webapp/src/app/settings/settings.component.html`, `settings.module.ts`.

### Extension

- Create `browser-extension/package.json`, `package-lock.json`, `tsconfig.json`, `build.mjs`.
- Create `browser-extension/src/manifest.json`.
- Create `browser-extension/src/background.ts`: 配对、受限 API 请求和消息路由。
- Create `browser-extension/src/options.html`, `options.ts`, `options.css`: 后端地址和用户名设置。
- Create `browser-extension/src/content.ts`, `content.css`: B 站页面按钮与提示。
- Create `browser-extension/src/shared/api.ts`, `settings.ts`, `room.ts`, `player.ts`, `messages.ts`.
- Create `browser-extension/tests/*.spec.ts`.
- Create `browser-extension/README.md`.
- Modify `.github/workflows/test.yml`, `.github/workflows/release.yml`.

---

### Task 1: Add scoped, username-only browser-extension pairing

**Files:**
- Modify: `src/blrec/web/auth_store.py`
- Modify: `src/blrec/web/security.py`
- Modify: `src/blrec/web/routers/auth.py`
- Modify: `tests/web/test_auth_store.py`
- Modify: `tests/web/test_auth_routes.py`
- Create: `webapp/src/app/settings/browser-extension-tokens/browser-extension-token.service.ts`
- Create: `webapp/src/app/settings/browser-extension-tokens/browser-extension-token.service.spec.ts`
- Create: `webapp/src/app/settings/browser-extension-tokens/browser-extension-tokens.component.ts`
- Create: `webapp/src/app/settings/browser-extension-tokens/browser-extension-tokens.component.html`
- Create: `webapp/src/app/settings/browser-extension-tokens/browser-extension-tokens.component.scss`
- Create: `webapp/src/app/settings/browser-extension-tokens/browser-extension-tokens.component.spec.ts`
- Modify: `webapp/src/app/settings/settings.component.html`
- Modify: `webapp/src/app/settings/settings.module.ts`

**Interfaces:**
- Produces: `AdminAuthStore.issue_extension_token(username, client_key) -> ExtensionCredentials`.
- Produces: `authenticate_extension(token) -> Optional[ExtensionIdentity]`, `list_extension_tokens()`, `revoke_extension_token(id)`.
- Produces admin routes `GET /api/v1/auth/extensions` and `DELETE /api/v1/auth/extensions/{token_id}`.

- [ ] **Step 1: Write failing auth-store tests**

Cover successful username match, wrong-name rate limiting, hash-only persistence, last-used updates and revocation:

```python
credentials = store.issue_extension_token('luwei', client_key='192.168.50.8')
assert credentials.token.startswith('blrec_ext_')
identity = store.authenticate_extension(credentials.token)
assert identity is not None
assert identity.token_id == credentials.token_id
assert store.authenticate_extension(credentials.token) is not None

store.revoke_extension_token(credentials.token_id)
assert store.authenticate_extension(credentials.token) is None
```

Read SQLite directly and assert the plaintext token is absent from every `extension_tokens` text/blob column.

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/web/test_auth_store.py -q`

Expected: FAIL because extension token methods and schema do not exist.

- [ ] **Step 3: Extend the auth database schema**

Add this idempotent table to `_create_schema()`:

```sql
CREATE TABLE IF NOT EXISTS extension_tokens (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL,
    revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS extension_tokens_active_idx
ON extension_tokens(revoked_at,last_used_at,id);
```

Generate `blrec_ext_` plus 32 random URL-safe bytes, save only SHA-256, compare hashes with `secrets.compare_digest`, and record `extension_pair_succeeded`, `extension_token_used`, and `extension_token_revoked` in auth audit. Reuse the existing failed-login window with scope `extension_pair` for wrong usernames.

- [ ] **Step 4: Expose administrator token management**

Add authenticated `GET /api/v1/auth/extensions` and
`DELETE /api/v1/auth/extensions/{token_id}` routes. The GET handler maps every
`ExtensionIdentity` to ID, created time, last-used time and revoked time; the
DELETE handler calls `revoke_extension_token(token_id)` and returns an empty
204 response. Never return token hashes.

- [ ] **Step 5: Add security isolation tests**

Assert an extension token is rejected on `/api/v1/settings`, `/api/v1/bili-accounts`, and `/api/v1/recording-sessions`, even when supplied in the correct extension header. Administrator sessions continue to work unchanged.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/web/test_auth_store.py tests/web/test_auth_routes.py -q`

Expected: PASS.

- [ ] **Step 7: Add the administrator token list**

Add a final “浏览器插件授权” page section in settings. Load
`GET /api/v1/auth/extensions`, show created/last-used time, and provide one
danger-styled “撤销” action per active token with a confirmation modal. The
component never displays or accepts token text, administrator username,
password or API Key. Test load, empty state, confirmed revoke and cancelled
revoke.

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/settings/browser-extension-tokens/browser-extension-token.service.spec.ts' --include='src/app/settings/browser-extension-tokens/browser-extension-tokens.component.spec.ts'`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/blrec/web/auth_store.py src/blrec/web/security.py src/blrec/web/routers/auth.py tests/web/test_auth_store.py tests/web/test_auth_routes.py webapp/src/app/settings/browser-extension-tokens webapp/src/app/settings/settings.component.html webapp/src/app/settings/settings.module.ts
git commit -m "feat: add scoped browser extension tokens"
```

---

### Task 2: Add the restricted browser-extension backend API

**Files:**
- Create: `src/blrec/web/routers/browser_extension.py`
- Modify: `src/blrec/web/routers/__init__.py`
- Modify: `src/blrec/web/main.py`
- Modify: `src/blrec/web/security.py`
- Consume: `src/blrec/bili_upload/policies.py`
- Create: `tests/web/test_browser_extension_routes.py`

**Interfaces:**
- Produces: `POST /api/v1/browser-extension/pair`.
- Produces: `GET /api/v1/browser-extension/rooms/{room_id}`.
- Produces: `POST /api/v1/browser-extension/rooms/{room_id}/collect`.
- Produces: `POST /api/v1/browser-extension/rooms/{room_id}/highlights`.
- Consumes: `Application`, `HighlightService`, `RoomUploadPolicyManager`.

- [ ] **Step 1: Write failing route tests**

Test these exact responses:

```json
{"collected": false, "recording": false}
```

for a missing room, and:

```json
{"collected": true, "recording": true}
```

when `task_status.running_status == RECORDING`. Assert the three UI states derive from these two booleans without a separate “已收录” label.

Test pair with `{"username":"luwei"}` and no password, collect with `{"upload":false}` or `{"upload":true}`, and highlight creation with observation/player fields. Assert every non-pair route rejects missing, revoked, or malformed `X-BLREC-Extension-Token`.

- [ ] **Step 2: Isolate authentication by path**

In the global `security.authenticate()` dependency, allow `/api/v1/browser-extension/*` to reach its route-specific dependency without treating an extension token as an administrator session. Implement:

```python
def authenticated_extension(request: Request) -> ExtensionIdentity:
    token = request.headers.get('x-blrec-extension-token', '')
    identity = _store().authenticate_extension(token)
    if identity is None:
        raise HTTPException(status_code=401, detail='浏览器插件授权无效')
    return identity
```

Only `/pair` is public inside this router; it validates the username and client IP through `issue_extension_token()`.

- [ ] **Step 3: Reuse the default upload policy command**

Consume the `default_room_upload_policy()` function delivered by the core
plan. Its fixed content is:

```python
def default_room_upload_policy() -> RoomUploadPolicyCommand:
    return RoomUploadPolicyCommand(
        account_mode='primary',
        account_id=None,
        enabled=True,
        title_template='【直播回放】【{{ anchor_name }}】{{ title }} '
        '{{ live_start_time | date: "%Y年%m月%d日%H点%M分" }}',
        description_template='直播录像\n{{ anchor_name }}直播间：'
        'https://live.bilibili.com/{{ room_id }}',
        part_title_template='P{{ part_index }}-{{ area_name }}-'
        '{{ live_start_time | date: "%m月%d日%H点%M分" }}',
        dynamic_template='直播录像\n{{ anchor_name }}直播间：'
        'https://live.bilibili.com/{{ room_id }}',
        tid=21,
        tags='直播回放,{{ anchor_name }},{{ area_name }}',
        creation_statement_id=-2,
        original_authorization=False,
        source='https://live.bilibili.com/{{ room_id }}',
        is_only_self=False,
        publish_dynamic=True,
        up_selection_reply=False,
        up_close_reply=False,
        up_close_danmu=False,
        auto_comment=True,
        danmaku_backfill=True,
        filters={},
        retention_mode='submitted',
        retention_days=5,
    )
```

Do not define a second copy in the extension router. For “收录并投稿”,
preserve every existing room-policy field but force `enabled=True`; persist
this default only when no policy exists. If no active primary account or the
default category/statement is unavailable, return 409 with a concise
instruction to configure the room in BLREC.

- [ ] **Step 4: Implement room actions**

`collect` must be idempotent. If missing, call `app.add_task(room_id)`; then ensure the task is started and its recorder enabled. With `upload=true`, upsert the default policy after the real room ID is known. With `upload=false`, do not create or delete any policy.

`highlights` calls:

```python
await highlight_service.create_marker(
    room_id=room_id,
    observed_at_ms=command.observed_at_ms,
    player_delay_ms=command.player_delay_ms,
    title=command.title,
    anchor_name=command.anchor_name,
    source='browser_extension',
)
```

It must save the bookmark even if recording stops between the status request and the click.

- [ ] **Step 5: Wire and test**

Set router globals during startup and clear them during shutdown. Run:

`python -m pytest tests/web/test_browser_extension_routes.py tests/web/test_auth_routes.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/blrec/web/routers/browser_extension.py src/blrec/web/routers/__init__.py src/blrec/web/main.py src/blrec/web/security.py tests/web/test_browser_extension_routes.py
git commit -m "feat: expose browser extension recording actions"
```

---

### Task 3: Scaffold the Manifest V3 extension and options page

**Files:**
- Create: `browser-extension/package.json`
- Create: `browser-extension/package-lock.json`
- Create: `browser-extension/tsconfig.json`
- Create: `browser-extension/build.mjs`
- Create: `browser-extension/src/manifest.json`
- Create: `browser-extension/src/shared/settings.ts`
- Create: `browser-extension/src/shared/api.ts`
- Create: `browser-extension/src/shared/messages.ts`
- Create: `browser-extension/src/background.ts`
- Create: `browser-extension/src/options.html`
- Create: `browser-extension/src/options.ts`
- Create: `browser-extension/src/options.css`
- Create: `browser-extension/tests/settings.spec.ts`
- Create: `browser-extension/tests/background.spec.ts`

**Interfaces:**
- Produces settings `{backendUrl, username, token}` in `chrome.storage.local`.
- Produces runtime messages `PAIR`, `ROOM_STATUS`, `COLLECT`, `ADD_HIGHLIGHT`.
- Consumes restricted backend endpoints from Task 2.

- [ ] **Step 1: Create package metadata and install lockfile**

Use scripts:

```json
{
  "scripts": {
    "build": "node build.mjs",
    "test": "vitest run",
    "typecheck": "tsc --noEmit"
  },
  "devDependencies": {
    "@types/chrome": "^0.0.268",
    "esbuild": "^0.21.5",
    "jsdom": "^24.1.0",
    "typescript": "~4.9.5",
    "vitest": "^1.6.0"
  }
}
```

Run: `cd browser-extension && npm install`

Expected: `package-lock.json` is generated with no production dependencies.

- [ ] **Step 2: Write failing normalization and pairing tests**

Assert `192.168.1.100:2233` normalizes to `http://192.168.1.100:2233`, trailing slashes are removed, non-http(s) schemes are rejected, username is trimmed but case is preserved, and a successful pair stores the returned token.

- [ ] **Step 3: Implement manifest and build**

Use a fixed manifest:

```json
{
  "manifest_version": 3,
  "name": "BLREC 工具",
  "version": "0.1.0",
  "permissions": ["storage", "activeTab", "scripting"],
  "optional_host_permissions": ["http://*/*", "https://*/*"],
  "host_permissions": ["https://live.bilibili.com/*"],
  "background": {"service_worker": "background.js", "type": "module"},
  "options_page": "options.html",
  "content_scripts": [{
    "matches": ["https://live.bilibili.com/*"],
    "js": ["content.js"],
    "css": ["content.css"],
    "run_at": "document_idle"
  }]
}
```

`build.mjs` bundles `background.ts`, `content.ts`, and `options.ts` with esbuild, copies HTML/CSS/manifest into `dist`, and deletes the old `dist` first.

- [ ] **Step 4: Implement the options page**

The page contains only backend address, administrator username, “连接” and a connection result. On save, request optional origin permission for exactly the normalized backend origin, send `PAIR` to the background worker, and store the token only after a 2xx response. Never render a password or API-key field.

- [ ] **Step 5: Implement the background API client**

Use `fetch` from the service worker with JSON and `X-BLREC-Extension-Token`. Pairing omits the token header. Reject redirects to a different origin, use a 10-second `AbortController` timeout, and return structured `{ok, data?, message?}` responses to the content script.

- [ ] **Step 6: Run extension checks**

```bash
cd browser-extension
npm test
npm run typecheck
npm run build
```

Expected: tests pass and `dist/manifest.json`, `background.js`, `content.js`, and `options.html` exist.

- [ ] **Step 7: Commit**

```bash
git add browser-extension/package.json browser-extension/package-lock.json browser-extension/tsconfig.json browser-extension/build.mjs browser-extension/src browser-extension/tests
git commit -m "feat: scaffold BLREC highlight extension"
```

---

### Task 4: Inject room actions and capture player-adjusted highlights

**Files:**
- Create: `browser-extension/src/shared/room.ts`
- Create: `browser-extension/src/shared/player.ts`
- Create: `browser-extension/src/content.ts`
- Create: `browser-extension/src/content.css`
- Create: `browser-extension/tests/room.spec.ts`
- Create: `browser-extension/tests/player.spec.ts`
- Create: `browser-extension/tests/content.spec.ts`

**Interfaces:**
- Produces: `parseRoomId(location, document) -> number | null`.
- Produces: `observePlayer(document, nowMs) -> PlayerObservation`.
- Consumes background messages `ROOM_STATUS`, `COLLECT`, `ADD_HIGHLIGHT`.

- [ ] **Step 1: Write failing room and player tests**

Cover numeric room paths, short-room canonical IDs exposed by page data, invalid paths, no `<video>`, and this delay calculation:

```typescript
expect(observePlayer(document, 1_000_000)).toEqual({
  observedAtMs: 1_000_000,
  playerDelayMs: 18_500,
});
```

where `video.currentTime = 100.5` and the last seekable end is `119.0`. Clamp delay to `0..300000`; missing ranges produce zero.

- [ ] **Step 2: Write failing button-state tests**

Assert:

- `{collected:false, recording:false}` renders “收录” and “收录并投稿”;
- `{collected:true, recording:false}` renders no action container;
- `{collected:true, recording:true}` renders only “添加高光”;
- running initialization twice leaves one container;
- a MutationObserver refresh restores a removed container without duplicating it.

- [ ] **Step 3: Implement resilient room detection**

Prefer the canonical room ID from B 站's embedded page state when present; fall back to the numeric pathname only when no canonical value exists. Do not request a B 站 API. Return null rather than guessing if neither source is valid.

- [ ] **Step 4: Implement button injection and feedback**

Insert one `.blrec-highlight-actions` element into the top navigation action area, with a fallback fixed container near the top-right if the known anchor is missing. Match the dark header style, keep buttons keyboard accessible, and provide a small non-blocking toast for success/failure.

For “收录” and “收录并投稿”, disable both buttons during the request and reload status afterward. For “添加高光”, allow repeated clicks after each request completes and send room ID, document title, best-effort anchor name, observation time and player delay. Do not open a dialog.

- [ ] **Step 5: Run extension tests**

Run: `cd browser-extension && npm test && npm run typecheck && npm run build`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add browser-extension/src/shared/room.ts browser-extension/src/shared/player.ts browser-extension/src/content.ts browser-extension/src/content.css browser-extension/tests
git commit -m "feat: add Bilibili live highlight controls"
```

---

### Task 5: Package, document, and verify the extension end to end

**Files:**
- Create: `browser-extension/README.md`
- Modify: `.github/workflows/test.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `tests/release/test_github_release_workflows.py`

**Interfaces:**
- Produces CI artifact and release asset `blrec-browser-extension-<version>.zip`.
- Consumes completed backend and extension builds.

- [ ] **Step 1: Add extension CI**

Add a separate `extension` job using Node 18 and `browser-extension/package-lock.json` cache. Run `npm ci`, `npm test`, `npm run typecheck`, and `npm run build`. Do not fold it into the Angular job because the projects have independent lockfiles.

- [ ] **Step 2: Add deterministic release packaging**

In the release workflow, build the extension and map the application version to a Chromium-safe numeric version (`3.0.0-beta.3` becomes `3.0.0.3`). Pass that value to `build.mjs`, then zip the contents of `browser-extension/dist` so `manifest.json` is at the archive root:

```bash
manifest_version="$(python - "$version" <<'PY'
import re
import sys

value = sys.argv[1]
match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?', value)
if match is None:
    raise SystemExit('unsupported extension version: ' + value)
parts = [match.group(1), match.group(2), match.group(3)]
if match.group(4) is not None:
    parts.append(match.group(4))
print('.'.join(parts))
PY
)"
cd browser-extension
npm ci
BLREC_EXTENSION_VERSION="$manifest_version" npm run build
cd dist
zip -r "../../blrec-browser-extension-${{ steps.version.outputs.value }}.zip" .
```

Attach the zip beside `compose.synology.yml` and `synology.env.example` in `gh release create`.

- [ ] **Step 3: Test the workflow contract**

Extend `test_github_release_workflows.py` to assert the extension job runs all four commands and the release command references the versioned zip. Run:

`python -m pytest tests/release/test_github_release_workflows.py -q`

Expected: PASS.

- [ ] **Step 4: Document manual installation and pairing**

Document Chromium “加载已解压的扩展程序” for development and release-zip extraction for NAS users. Include: open options, enter `http://<NAS-IP>:2233`, enter administrator username, connect, then visit a B 站 live room. State that username-only pairing is for trusted LAN use and show how to revoke a token from BLREC.

- [ ] **Step 5: Perform browser acceptance**

Use an unpacked production build and verify all three states against a real BLREC server. Add one unrecorded room with each collection button, wait for recording, click “添加高光” three times, and confirm three independent rows appear in BLREC with measured delays. Confirm a revoked token causes a concise reconnect prompt and cannot read `/api/v1/settings`.

- [ ] **Step 6: Run all gates**

```bash
python -m pytest
black --check src tests
isort --check-only src tests
flake8 src tests
mypy src/blrec
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless && npm run build
cd ../browser-extension && npm test && npm run typecheck && npm run build
```

Expected: every command exits 0.

- [ ] **Step 7: Commit**

```bash
git add browser-extension/README.md .github/workflows/test.yml .github/workflows/release.yml tests/release/test_github_release_workflows.py
git commit -m "release: package BLREC highlight extension"
```
