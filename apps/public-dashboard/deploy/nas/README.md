# 群晖排行榜增量、资产与回放核验 worker

该 worker 是独立 Compose 项目 `blrec-dashboard`。它不会重建、切换或停止正在录制的
`blrec-next` 容器。它用只读数据库账号发送玩家/对局增量、同步结算图，并按 Dashboard
API 队列匿名核验 B 站稿件是否可公开回放；榜单与评分仍由 API 侧计算。

## 凭据与目录

为 NAS 创建只允许写 `data/match-images/**` 的 RAM 用户，把 AccessKey 写入：

```text
/volume1/docker/blrec-next/secrets/dashboard-publisher.env
```

同一文件还保存 `DASHBOARD_API_TOKEN`，用于写入缓存增量和图片元数据，以及领取和回写
回放可见性任务。不要把凭据写入 Compose、日志或仓库。

`DASHBOARD_DATABASE_URL` 使用移动云主库的只读账号，连接 NAS 上的
`127.0.0.1:15432` 隧道，并固定 `search_path=core`。结算图目录只读挂载为
`/result-frames`。worker 会压缩为最长边 1600 像素的 WebP，再上传到
`data/match-images/`。

Publisher 的 `/state` 保存玩家/对局内容哈希、图片签名、水位和两个失败 outbox；这些
JSON 文件不是网页数据源。回放可见性队列和 15 分钟缓存持久化在 Dashboard API 数据库
中。数据库隧道不可用时停止本轮同步，不回退到旧 SQLite。

## 部署与升级

更新前仍须按照仓库 NAS 运维规范，核对 Compose 项目与挂载，并在运行中的
`blrec-next` 容器内完成非空、可恢复的数据库备份。然后只更新独立 worker：

```bash
cd /volume1/docker/blrec-next/dashboard-publisher
docker compose --project-name blrec-dashboard \
  --env-file publisher.env \
  -f compose.yml pull dashboard-publisher
docker compose --project-name blrec-dashboard \
  --env-file publisher.env \
  -f compose.yml up -d dashboard-publisher
```

群晖非交互 SSH 中 Docker 使用 `/usr/local/bin/docker`；不要操作项目根目录的旧
`compose.yaml`。

## 验证

```bash
docker inspect blrec-next
docker inspect blrec-dashboard-publisher
docker logs --tail 100 blrec-dashboard-publisher
curl -fsS https://vg-api.luwei.host/v1/health
curl -fsS https://vg-api.luwei.host/v1/matches/summary
curl -fsS https://vg-api.luwei.host/v1/dashboard >/dev/null
```

日志应出现 `asset_sync=synced|current`、`cache_sync=synced|current`，以及
`replay_visibility=public|unavailable|retry`。网络审计应记录 `dashboard_publish` 和 `bili_api` 用途的线路与源地址。失败批次保留在
`/state/api-outbox/` 或 `/state/cache-api-outbox/`，容器重启后用相同幂等键重试。

常驻 worker 每秒读取 source revision；变化后等待 2 秒合并连续写入。初次缓存引导每批
最多 500 场且只在末批发布，之后只发送内容哈希变化的对局；纯 revision 变化不触发榜单
重算。回放核验使用独立长轮询，没有页面请求产生的任务时不访问 B 站；同一稿件合并为
一条任务。

## 人工检查与回滚

人工检查一次图片同步时，先停止常驻实例以释放单例锁：

```bash
docker compose --project-name blrec-dashboard --env-file publisher.env \
  -f compose.yml stop dashboard-publisher
docker compose --project-name blrec-dashboard --env-file publisher.env \
  -f compose.yml run --rm dashboard-publisher --once
docker compose --project-name blrec-dashboard --env-file publisher.env \
  -f compose.yml up -d dashboard-publisher
```

回滚时先把 API 模式恢复为 `direct`，再把 `DASHBOARD_PUBLISHER_IMAGE_TAG` 改为已验证
的提交 SHA 并执行 `pull`、`up -d`；仍须先备份数据库。旧 API 不支持缓存批次端点时，
必须同时回滚 Publisher，否则它会保留 outbox 并持续重试。
