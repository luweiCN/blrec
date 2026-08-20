# 排行榜 API 部署

API 在阿里云 ECS 上以独立 systemd 服务运行，只监听 `127.0.0.1:8787`，由 Nginx
通过 `https://vg-api.luwei.host/v1/` 对外提供。

API 通过受限 SSH 隧道连接移动云 PostgreSQL：

- `core` 是权威业务数据源。API 账号只对 `grant-core-read.sql` 列出的表拥有
  `SELECT` 权限；
- `public` 保存按 `source_revision` 发布的 public/owner 缓存代次、可分页对局索引、
  结构化历史趋势、结算图片元数据和幂等记录；
- API 进程只常驻 public/owner 两份序列化榜单和很小的直播状态，对局、搜索和评分按页
  从 PostgreSQL 读取；
- 缓存重建在独立 Python 子进程中完成。public/owner 两套数据写入同一事务，全部成功后
  才同时切换版本指针；失败时 API 继续提供上一份成功结果。

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

## PostgreSQL 缓存切换

新 release 首次部署时保持 `DASHBOARD_API_REPOSITORY_MODE=direct`。数据库备份和迁移
成功后，用同一 release、同一受限服务账号执行
`python -m blrec_dashboard_api.cache_builder` 做影子重建。只有以下条件全部满足才能把
模式改成 `postgres`：

1. `dashboard_cache_state` 恰有 `public`、`owner` 两行且 revision 相同；
2. 该 revision 等于 `core.dashboard_source_state.revision`；
3. public/owner 榜单摘要、对局数量、分页/搜索/英雄筛选和回放权限回归全部通过；
4. API 公共响应字节与影子缓存字节一致，并记录构建耗时和峰值内存。

切换后，revision 变化会启动一次独立缓存构建进程；构建进程退出后内存立即归还给
系统。回滚时先恢复 `direct` 或上一个 release，不删除缓存表。旧的
`players`/`matches` 投影停在 2026-08-15，禁止把它们当作本次缓存直接启用。

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
