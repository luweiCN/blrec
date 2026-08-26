# 排行榜 API 部署

排行榜 API 只部署在 PVE 的 BLREC Platform 虚拟机。FastAPI 监听
`127.0.0.1:8787`，同机 Nginx 监听内网地址的 `8787` 端口；公网入口只负责经
WireGuard 把 `vg-api.luwei.host` 转发到该内网端口，不运行 API，也不保存数据库连接。

API 直接连接 PVE PostgreSQL：

- `core` 是权威业务数据源；
- `public` 保存结算图片元数据、写入幂等记录和可重建缓存，不是第二套权威对局数据；
- API 使用 `direct` 模式读取 `core`，按 `dashboard_source_state.revision` 刷新内存中的
  榜单、对局、搜索和评分；
- NAS Publisher 只发布图片资产和处理回放可见性，不发送玩家或对局增量。

服务配置位于 `/etc/blrec-dashboard-api/api.env`，权限为 `0600`。生产配置必须保持
`DASHBOARD_API_REPOSITORY_MODE=direct`。`DASHBOARD_API_DATABASE_URL` 与
`DASHBOARD_API_SOURCE_DATABASE_URL` 都应直连 PVE PostgreSQL，不得配置 SSH 隧道。
可选的 `DASHBOARD_API_PUBLIC_LISTEN_ADDRESS` 用于指定 Nginx 的内网监听地址；未设置时
部署脚本从 `192.168.50.0/24` 地址中自动选择。

每次切换 release 前，部署脚本会把 `public` schema 备份到
`/var/lib/blrec-dashboard-api/backups/`，确认文件非空并能被 `pg_restore --list`
读取。SQLite 只用于本地测试，备份会执行 `PRAGMA quick_check`。任一检查失败都不会
重启服务。

## 正式发布

`.github/workflows/deploy-public-dashboard-api.yml` 分为两步：

1. GitHub 托管 runner 运行测试、构建两个 wheel 和离线 wheelhouse，并上传不可变的
   `dashboard-api-release` artifact；
2. 标记为 `self-hosted, linux, x64, blrec-platform` 的 PVE runner 下载同一 artifact，
   在本机执行安装脚本、数据库备份、健康检查和回滚。

workflow 不包含远程 SSH 地址和密钥，也不存在可改名后误指向旧服务器的部署目标。
PVE runner 只需要对 `install-release.sh` 的非交互 sudo 权限。健康检查失败时脚本恢复
上一 release；成功后 reload 同机 Nginx，再验证公网接口。

API 图片写入密钥、数据库凭据和内网站长凭据都只存在 PVE 配置文件，不进入 workflow、
仓库或构建产物。
