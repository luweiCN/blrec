# 排行榜 API 部署

API 在阿里云 ECS 上以独立 systemd 服务运行，只监听 `127.0.0.1:8787`，由 Nginx
通过 `https://vg-api.luwei.host/v1/` 对外提供。

API 通过受限 SSH 隧道连接移动云 PostgreSQL：

- `core` 是权威业务数据源。API 账号只对 `grant-core-read.sql` 列出的表拥有
  `SELECT` 权限；
- `public` 只保存结构化的历史榜单趋势、结算图片元数据和图片批次幂等记录；
- 当前榜单、对局和直播状态不复制到 `public`，也不保存为 JSON 文件或 JSON blob；
- API 根据 `core.dashboard_source_state.revision` 刷新进程内缓存，并通过 SSE 通知
  页面。刷新失败时继续提供上一份成功结果，下一轮自动重试。

首次切换前，由 PostgreSQL 管理员执行 `grant-core-read.sql`。部署脚本会逐一验证 API
角色能读取批准的 `core` 表；任何表缺少权限或 revision 无效都会在切换 release 前
终止。

每次切换 release 前，部署脚本会备份持久化的 `public` schema 到
`/var/lib/blrec-dashboard-api/backups/`，确认文件非空并能被
`pg_restore --list` 读取。SQLite 只用于本地测试，其备份仍执行
`PRAGMA quick_check`。任一检查失败都不会重启服务。

服务配置位于 `/etc/blrec-dashboard-api/api.env`，权限为 `0600`。默认情况下，同一
PostgreSQL URL 分别固定 `search_path=public` 和 `search_path=core`；也可通过
`DASHBOARD_API_SOURCE_DATABASE_URL` 使用独立的只读账号。NAS 只持有图片资产 API
的 Bearer 密钥，不持有 API 数据库密码。

站长视图使用单独的高熵 Bearer 令牌。服务端只在 `api.env` 保存令牌的 SHA-256：

```text
DASHBOARD_API_OWNER_TOKEN_SHA256=<64 位小写十六进制摘要>
```

令牌明文不能进入仓库、静态构建或 URL。Dashboard 只在当前浏览器会话的
`sessionStorage` 中保存明文；站长响应使用 `private, no-store`，不能进入 CDN
共享缓存。

SSH 隧道需要以下文件：

- `/etc/blrec-dashboard-api/db-tunnel.key`
- `/etc/blrec-dashboard-api/db-tunnel-known-hosts`
- `/etc/blrec-dashboard-api/db-tunnel-ssh.conf`

移动云 SSH 公钥应使用 `restrict,permitopen="127.0.0.1:5432"` 限制，仅允许端口
转发。PostgreSQL 不监听公网地址。

`deploy-public-dashboard-api.yml` 在 GitHub Actions 中构建 API 与共享计算代码的
wheel 和离线 wheelhouse，然后上传新 release。服务器健康检查失败时恢复上一个
release；成功后才 reload Nginx，并从公网验证健康、榜单和对局汇总接口。

GitHub Environment `vainglory-dashboard-production` 需要：

- `DASHBOARD_API_SSH_HOST`
- `DASHBOARD_API_SSH_PORT`
- `DASHBOARD_API_SSH_USER`
- `DASHBOARD_API_SSH_PRIVATE_KEY`
- `DASHBOARD_API_SSH_KNOWN_HOSTS`

API 图片写入密钥不是 GitHub Actions 部署密钥，不应进入 workflow 或构建产物。
