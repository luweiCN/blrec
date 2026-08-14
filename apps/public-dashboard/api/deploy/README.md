# 排行榜对局 API 部署

API 在现有阿里云 ECS 上以独立的 systemd 服务运行，只监听
`127.0.0.1:8787`，由 Nginx 通过 `https://vg-api.luwei.host/v1/` 对外提供。
公开排行榜与对局的权威数据保存在移动云 PostgreSQL。PostgreSQL 只监听移动云
`127.0.0.1:5432`，阿里云 API 通过受限 SSH 隧道的 `127.0.0.1:5433` 连接；NAS
仍通过原有带网络选路的 HTTPS ingest 写入 API，不直接持有数据库密码。

每次切换 release 前，部署脚本都会备份当前数据源到
`/var/lib/blrec-dashboard-api/backups/`。SQLite 使用在线备份并检查源库、备份库的
`PRAGMA quick_check`；PostgreSQL 使用 custom-format `pg_dump`，并检查文件非空且
可被 `pg_restore --list` 读取。任一检查失败都会终止部署，不执行服务重启。

服务端配置位于 `/etc/blrec-dashboard-api/api.env`，保存 NAS 写入密钥的 SHA-256
以及只连接本机隧道的 PostgreSQL URL；NAS 保存写入密钥原文，不保存数据库密码。
首次部署前按 `api.env.example` 创建文件并设为 `0600`，以后 GitHub Actions 不会
覆盖它。

首次切换 PostgreSQL 前，还要在阿里云 API 主机创建以下三个只允许
`blrec-dashboard-api` 用户读取的文件：

- `/etc/blrec-dashboard-api/db-tunnel.key`：移动云受限 SSH 账号的专用私钥；
- `/etc/blrec-dashboard-api/db-tunnel-known-hosts`：移动云 SSH 主机指纹；
- `/etc/blrec-dashboard-api/db-tunnel-ssh.conf`：以
  `db-tunnel-ssh.conf.example` 为模板的连接配置。

移动云 SSH 公钥必须使用 `restrict,permitopen="127.0.0.1:5432"` 约束，账号不提供
交互 Shell。部署脚本发现 `DASHBOARD_API_DATABASE_URL` 后才会安装并启动
`blrec-dashboard-db-tunnel.service`；隧道建立失败时 `pg_dump` 备份失败，发布会在
切换 release 前终止。数据库端口不对公网监听，NAS 也不通过这条隧道直接访问数据库。

`deploy-public-dashboard-api.yml` 在 GitHub Actions 中构建 API 与评分算法 wheel，
下载 Python 3.12 的完整离线 wheelhouse，再通过专用 SSH 密钥上传。服务器先在新
release 中创建虚拟环境，切换软链接后执行本机健康检查；失败会恢复上一个 release，
成功后才 reload Nginx。部署后还会从公网验证健康、榜单和对局汇总三个接口；
页面原有静态 JSON 回退不会被 API 发布影响。

需要在 GitHub Environment `vainglory-dashboard-production` 中配置：

- `DASHBOARD_API_SSH_HOST`
- `DASHBOARD_API_SSH_PORT`
- `DASHBOARD_API_SSH_USER`
- `DASHBOARD_API_SSH_PRIVATE_KEY`
- `DASHBOARD_API_SSH_KNOWN_HOSTS`

API 写入密钥不是 GitHub Actions 部署密钥，不应写进 workflow 或构建产物。
