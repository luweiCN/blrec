# 虚荣排行榜发布

公开站点采用三条彼此隔离的发布链路：

| 链路 | 运行位置 | 写入范围 | 触发方式 |
| --- | --- | --- | --- |
| 页面发布 | GitHub Actions | OSS 根目录页面与静态资源，不含 `data/**` | `master` 上页面代码变更或手动触发 |
| API 发布 | GitHub Actions | ECS 上的 API release；数据库独立持久化 | `master` 上 API 代码变更或手动触发 |
| 数据同步 | 群晖 NAS worker | API 对局底账；OSS `data/match-images/**` | 每 15 分钟检查、内容变化时写入、失败重试 |

访问统计仍由阿里云服务器单独维护 `data/site-stats.json` 和
`data/site-stats-history.json`。CDN 继续使用 `vg.luwei.host`；旧 manifest、趋势
与最后一份静态快照冻结为只读故障回退，不再承担日常数据发布。

## GitHub 页面发布

Workflow 为 `.github/workflows/deploy-public-dashboard.yml`。它按顺序执行依赖
安装、测试、lint、生产构建和 OSS 上传，并将 `index.html` 固定为最后一个上传
对象。构建产物若包含真实 `data/**` 文件会立即失败；仓库的
`data/.gitignore` 空目录占位符会被忽略且不会上传。

在 GitHub 仓库中创建 Environment `vainglory-dashboard-production`，并配置以下
Environment secrets：

```text
ALIBABA_CLOUD_ACCESS_KEY_ID
ALIBABA_CLOUD_ACCESS_KEY_SECRET
```

AccessKey 应属于只负责页面发布的 RAM 用户，并附加
`deploy/aliyun/page-deploy-ram-policy.json`。该策略显式拒绝写入和删除
`data/**`。不要与 NAS 数据发布、CDN 证书同步或访问统计共用 AccessKey。

## NAS 数据同步

NAS worker 的镜像由 `.github/workflows/publish-dashboard-worker.yml` 构建为：

```text
ghcr.io/luweicn/blrec-dashboard-publisher:master
```

具体安装、备份、验证和回滚步骤见
[`deploy/nas/README.md`](deploy/nas/README.md)。NAS AccessKey 只需附加
`deploy/aliyun/data-publish-ram-policy.json`；OSS 权限只用于结算图和仍保留的应急
静态发布能力。API 写入使用独立 Bearer 密钥。

worker 每次检查会：

1. 在 SQLite 只读事务中提取稳定玩家、别名和符合条件的规范化对局。
2. 与本地已确认水位比较；内容没有变化时跳过写入。
3. 压缩新增或变化的结算图并写入 OSS。
4. 把带幂等键的批次写入 API；失败批次保留在 outbox 中重试。
5. API 在一个事务中更新对局底账，按直播时间重算段位、榜单、英雄统计和当天趋势。

容器启动后会立即检查，成功后每 15 分钟再次检查，失败后也每 15 分钟重试。
API 确认前不会推进本地水位；超时重试继续使用同一幂等键。API 数据库在每次代码
部署前都会生成经 `quick_check` 验证的在线备份。完整对局由分页接口提供，首页
榜单响应不嵌入全部对局，因此对局量增长不会线性放大首页下载。
