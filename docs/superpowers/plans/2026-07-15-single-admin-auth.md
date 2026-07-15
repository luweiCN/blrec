# Single Admin Session Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用安全的单管理员密码会话替代浏览器 API Key 存储和输入弹窗。

**Architecture:** 独立 `AdminAuthStore` 持久化 Argon2id 哈希、会话哈希、CSRF 哈希和媒体签名密钥。公开认证路由完成初始化/登录，其余 HTTP 和 WebSocket 默认会话校验；Angular 根路由由认证状态守卫。

**Tech Stack:** FastAPI 0.88、SQLite、argon2-cffi、HttpOnly Cookie、Angular 15、RxJS、Jasmine/Karma。

### Task 1: 认证存储

**Files:**
- Modify: `setup.cfg`
- Create: `src/blrec/web/auth_store.py`
- Test: `tests/web/test_auth_store.py`

- [ ] 写失败测试：数据库权限、首次密钥、Argon2id 哈希、会话只存哈希、30 天过期/滑动续期、撤销和失败窗口限速。
- [ ] 添加 `argon2-cffi` 依赖并实现参数化 SQLite 存储；会话和 CSRF 使用 `secrets.token_urlsafe(32)`。
- [ ] 运行 `pytest -q tests/web/test_auth_store.py`。

### Task 2: HTTP 认证、CSRF 与安全头

**Files:**
- Create: `src/blrec/web/auth.py`
- Create: `src/blrec/web/routers/auth.py`
- Create: `src/blrec/web/middlewares/security_headers.py`
- Modify: `src/blrec/web/main.py`
- Modify: `src/blrec/web/security.py`
- Test: `tests/web/test_auth_routes.py`
- Test: `tests/web/test_security.py`

- [ ] 写失败测试：一次性初始化、错误 API Key、登录限速、Cookie 属性、会话续期、退出、改密/恢复撤销、CSRF、同源和所有业务 API 默认拒绝未登录。
- [ ] 公开挂载认证路由；全局依赖允许公开认证和有效媒体签名，其余请求要求会话，修改请求要求 CSRF。
- [ ] 媒体 HMAC 改用认证库持久密钥，添加 `nosniff`、`DENY/frame-ancestors` 和 referrer policy。
- [ ] 运行两个聚焦测试文件。

### Task 3: WebSocket 认证

**Files:**
- Modify: `src/blrec/web/routers/websockets.py`
- Test: `tests/web/test_websockets_auth.py`

- [ ] 写失败测试：无 Cookie、过期会话、跨站 Origin 在 accept 前关闭；同源和 `localhost:4200` 开发 Origin 成功。
- [ ] 注入认证存储并在两个端点握手前统一校验。
- [ ] 运行 `pytest -q tests/web/test_websockets_auth.py`。

### Task 4: Angular 登录和拦截器

**Files:**
- Create: `webapp/src/app/auth/auth.component.{ts,html,scss,spec.ts}`
- Create: `webapp/src/app/auth/auth.module.ts`
- Create: `webapp/src/app/auth/auth-routing.module.ts`
- Create: `webapp/src/app/core/services/auth.guard.ts`
- Modify: `webapp/src/app/core/services/auth.service.ts`
- Modify: `webapp/src/app/core/http-interceptors/auth.interceptor.ts`
- Modify: `webapp/src/app/app-routing.module.ts`
- Modify: `webapp/src/app/app.component.{ts,html,spec.ts}`
- Test: `webapp/src/app/core/services/auth.service.spec.ts`
- Test: `webapp/src/app/core/http-interceptors/auth.interceptor.spec.ts`

- [ ] 写失败测试：初始化/登录表单、CSRF 仅加到写请求、`withCredentials`、401 跳登录、无 prompt/localStorage、刷新恢复会话和退出。
- [ ] 实现内存 CSRF、认证守卫和简洁登录页；所有 API 请求携带 Cookie，写请求携带 CSRF。
- [ ] 运行聚焦测试和完整 Angular 测试。

### Task 5: 部署文档和浏览器验收

**Files:**
- Modify: `README.md`
- Modify: `docs/operations/synology-multi-network.md`

- [ ] 修正文档中凭据密钥为 Base64 32 字节，并说明 API Key 只用于初始化/恢复。
- [ ] 重启本机服务，用浏览器完成初始化、退出、登录和刷新。
- [ ] 在开发者工具确认无 `X-API-KEY`、无本地凭据、Cookie 为 HttpOnly、写请求带 CSRF。
