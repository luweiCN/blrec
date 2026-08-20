# 虚荣排行榜发布

公开站点由三条独立链路组成：

| 链路 | 运行位置 | 职责 | 触发方式 |
| --- | --- | --- | --- |
| 页面发布 | GitHub Actions | OSS 页面与静态资源，不含榜单数据文件 | `master` 页面变更或手动触发 |
| API 发布 | GitHub Actions / 阿里云 ECS | 读取 PostgreSQL 缓存、提供 HTTP API 与 SSE | `master` API 变更或手动触发 |
| 图片资产 | 群晖 NAS Publisher | 结算图上传 OSS，并写入结构化图片元数据 | revision 变化或每日校验 |

## 数据读写路径

- BLREC 和 Worker 直接写移动云 PostgreSQL `core` schema；它是玩家、对局、阵容和
  直播状态的唯一数据源。
- API 使用受限只读权限读取 `core`。独立构建进程按 revision 把 public/owner 榜单
  字节和对局查询索引原子发布到 `public` schema；页面请求不等待 NAS Publisher。
- API 主进程只常驻两份榜单响应字节；分页、搜索、英雄筛选和评分从 PostgreSQL 缓存
  查询，不再保留全量 Python 对象图。
- Publisher 不复制玩家或对局，只处理 NAS 本地图片；API 确认图片批次前不会推进本地
  水位。

API 每秒检查一次持久 revision。发现变化后，独立进程在只读、可重复读事务中取得
一致业务数据，再在一个 PostgreSQL 事务中发布 public/owner 缓存并切换两个版本指针。
构建失败不会清空当前页面；后续检查会继续重试。对局接口从同一份已确认缓存分页和
筛选，结算图片按页从辅助表补充。

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

## NAS 图片资产 Publisher

镜像由 `.github/workflows/publish-dashboard-worker.yml` 构建：

```text
ghcr.io/luweicn/blrec-dashboard-publisher:master
```

安装、备份、验证和回滚见
[`deploy/nas/README.md`](deploy/nas/README.md)。OSS RAM 权限只需覆盖
`data/match-images/**`；API 使用独立 Bearer 密钥接收图片元数据。

访问统计是独立链路，不参与榜单、对局或缓存刷新。
