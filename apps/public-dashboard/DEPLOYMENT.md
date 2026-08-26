# 虚荣排行榜发布

公开站点由三条独立链路组成：

| 链路 | 运行位置 | 职责 | 触发方式 |
| --- | --- | --- | --- |
| 页面发布 | GitHub Actions | OSS 页面与静态资源，不含榜单数据文件 | `master` 页面变更或手动触发 |
| API 发布 | GitHub Actions / PVE BLREC Platform | 直接读取 PostgreSQL `core`、提供 HTTP API 与 SSE | `master` API 变更或手动触发 |
| 内网站长页面 | GitHub Actions / PVE BLREC Platform | 修改 AI/OCR 对局事实、回流训练纠错样本 | `master` 页面变更或手动触发 |
| 图片资产 | 群晖 NAS Publisher | 结算图上传 OSS，并写入结构化图片元数据 | revision 变化或每日校验 |

## 数据读写路径

- BLREC 和 Worker 直接写 PVE PostgreSQL `core` schema；它是玩家、对局、阵容和
  直播状态的唯一数据源。
- API 使用受限只读权限直接读取 `core`，按 revision 刷新进程内的 public/owner 榜单与
  对局查询缓存；页面请求不等待 NAS Publisher。
- Publisher 不复制玩家或对局，只处理 NAS 本地图片；API 确认图片批次前不会推进本地
  水位。

API 定期检查持久 revision。发现变化后，在只读、可重复读事务中取得一致业务数据并
原子替换进程内缓存。构建失败不会清空当前页面，后续检查会继续从 `core` 重试。结算
图片按页从 `public` 的辅助表补充。

旧 `public` 投影表和 OSS 上的 manifest/快照会在首轮切换期间冻结保留，目的是让自动
release 回滚仍可启动旧版本；新版本不会读写它们。稳定运行并跨过回滚窗口后，再用
单独迁移删除，避免同一次部署既改代码又破坏回滚数据。

## GitHub 页面发布

Workflow 为 `.github/workflows/deploy-public-dashboard.yml`。它依次执行依赖安装、
测试、lint、生产构建和 OSS 上传，并将 `index.html` 最后上传。构建产物若包含真实
`data/**` 榜单文件会失败。

Environment `vainglory-dashboard-production` 需要页面发布 RAM 用户的：

```text
ALIBABA_CLOUD_ACCESS_KEY_ID
ALIBABA_CLOUD_ACCESS_KEY_SECRET
```

该用户只负责页面发布，并使用
`deploy/aliyun/page-deploy-ram-policy.json`；不要与 NAS 图片发布、证书同步或访问
统计共用 AccessKey。

## API 发布

API 的构建和部署流程见 [`api/deploy/README.md`](api/deploy/README.md)。发布前必须：

1. 验证 API 角色可以读取批准的 `core` 表；
2. 备份并校验 `public` schema；
3. 在新 release 中完成结构化趋势和图片表迁移；
4. 健康检查、榜单和对局接口通过后再切换流量。

API 只运行在 PVE；阿里云不安装 API、不保存数据库连接，也不存在数据库 SSH 隧道。
阿里云只继续承担公开静态站点的 OSS/CDN 和公网到 PVE 的反向代理入口。

## 内网站长页面

同一页面源码另行构建为 PVE 内网版本，部署和可信反代边界见
[`deploy/pve-admin/README.md`](deploy/pve-admin/README.md)。公开构建不包含编辑入口，
管理密钥不会进入浏览器。

## NAS 图片资产 Publisher

镜像由 `.github/workflows/publish-dashboard-worker.yml` 构建：

```text
ghcr.io/luweicn/blrec-dashboard-publisher:master
```

安装、备份、验证和回滚见
[`deploy/nas/README.md`](deploy/nas/README.md)。OSS RAM 权限只需覆盖
`data/match-images/**`；API 使用独立 Bearer 密钥接收图片元数据。

访问统计是独立链路，不参与榜单、对局或缓存刷新。
