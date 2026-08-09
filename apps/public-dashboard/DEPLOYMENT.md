# 虚荣排行榜发布

公开站点采用两条彼此隔离的发布链路：

| 链路 | 运行位置 | 写入范围 | 触发方式 |
| --- | --- | --- | --- |
| 页面发布 | GitHub Actions | OSS 根目录页面与静态资源，不含 `data/**` | `master` 上页面代码变更或手动触发 |
| 数据发布 | 群晖 NAS worker | `data/manifest.json`、`data/trends.json` 与 `data/snapshots/**` | 每天 00:05、启动补跑、失败重试 |

访问统计仍由阿里云服务器单独维护 `data/site-stats.json` 和
`data/site-stats-history.json`，两条新链路都无权覆盖它们。CDN 继续使用
`vg.luwei.host`；页面入口、manifest 和趋势数据使用 `no-store`，因此正常发布不
需要刷新 CDN 缓存。

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

## NAS 数据发布

NAS worker 的镜像由 `.github/workflows/publish-dashboard-worker.yml` 构建为：

```text
ghcr.io/luweicn/blrec-dashboard-publisher:master
```

具体安装、备份、验证和回滚步骤见
[`deploy/nas/README.md`](deploy/nas/README.md)。NAS AccessKey 只需附加
`deploy/aliyun/data-publish-ram-policy.json`，其权限被限定到排行榜 manifest、
紧凑趋势数据和不可变快照。

worker 每次发布会：

1. 读取远端 manifest；当天已经发布则跳过。
2. 在 SQLite 只读事务中生成快照，并校验长度与 SHA-256。
3. 生成最多保留最近 30 次发布的排名与榜单分趋势；同日重发只替换当天记录。
4. 先上传、复核不可变快照，再更新趋势数据。
5. 最后替换 manifest，并再次下载核对提交结果。

容器在零点后恢复时会立即检查漏发；失败后每 15 分钟重试。manifest 提交失败
时，本地 `/state/pending` 会保留同一份快照供下次复用。远端数据源进度高于
本地时会停止覆盖，防止旧数据库回退线上榜单。
