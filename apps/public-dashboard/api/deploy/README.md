# 排行榜对局 API 部署

API 在现有阿里云 ECS 上以独立的 systemd 服务运行，只监听
`127.0.0.1:8787`，由 Nginx 通过 `https://vg-api.luwei.host/v1/` 对外提供。
SQLite 固定保存在 `/var/lib/blrec-dashboard-api/dashboard.sqlite3`，不会放进
发布目录，也不会随代码版本回滚。

每次切换 release 前，部署脚本都会使用 SQLite 在线备份 API 数据库到
`/var/lib/blrec-dashboard-api/backups/`，并分别确认源库与备份库的
`PRAGMA quick_check` 为 `ok`、备份文件非空；任一检查失败都会终止部署，不执行
迁移或服务重启。

服务端配置位于 `/etc/blrec-dashboard-api/api.env`，只保存 NAS 写入密钥的
SHA-256；NAS 保存密钥原文。首次部署前按 `api.env.example` 创建文件并设为
`0600`，以后 GitHub Actions 不会覆盖它。

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
