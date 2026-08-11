# 群晖排行榜数据同步 worker

此 worker 是独立 Compose 项目 `blrec-dashboard`。它只读挂载 BLREC 配置目录，
不会重建、切换或停止正在录制的 `blrec-next` 容器。

## 准备凭据与目录

为 NAS 单独创建 RAM 用户，附加
[`../aliyun/data-publish-ram-policy.json`](../aliyun/data-publish-ram-policy.json)，
把 AccessKey 写入：

```text
/volume1/docker/blrec-next/secrets/dashboard-publisher.env
```

内容参考 `dashboard-publisher.env.example`，文件权限必须为 `0600`。不要把真实
凭据写入 Compose、日志或仓库。将本目录的 `compose.yml` 和
`publisher.env.example` 复制到
`/volume1/docker/blrec-next/dashboard-publisher/`，并将后者改名为
`publisher.env`。

同一文件还要写入 `DASHBOARD_API_TOKEN`。它只用于 NAS 向
`https://vg-api.luwei.host` 增量写入对局；服务端只保存该密钥的 SHA-256，日志
不会记录原文。结算图目录以只读方式挂载到 `/result-frames`，worker 会压缩为最长
边 1600 像素的 WebP，再写入同一 OSS 桶的 `data/match-images/` 前缀。现有
`acs:oss:*:*:luwei-vainglory/*` 资源范围已经覆盖该前缀，无需创建“目录”或新增
资源授权。

## 首次部署与升级

即使 worker 只读数据库，首次启动或更新镜像前也必须执行 BLREC 数据库备份。
先确认运行容器仍属于正确的 Compose 项目，并核对 `/cfg`、`/log`、`/rec`、
`/clips` 等挂载没有变化。随后在运行容器内执行：

```bash
docker exec blrec-next python scripts/backup_blrec_database.py \
  --label dashboard-publisher-<commit>
```

只有命令成功、输出备份文件非零字节且脚本内的 `PRAGMA quick_check` 为 `ok`
时才能继续。然后仅启动独立 worker：

```bash
cd /volume1/docker/blrec-next/dashboard-publisher
docker compose --project-name blrec-dashboard \
  --env-file publisher.env \
  -f compose.yml pull dashboard-publisher
docker compose --project-name blrec-dashboard \
  --env-file publisher.env \
  -f compose.yml up -d dashboard-publisher
```

群晖非交互 SSH 中 Docker 的完整路径为 `/usr/local/bin/docker`，需要管理员
权限时使用 `sudo`。不要操作项目根目录中的旧 `compose.yaml`。

## 验证

```bash
docker inspect blrec-next
docker inspect blrec-dashboard-publisher
docker logs --tail 100 blrec-dashboard-publisher
curl -fsS https://vg-api.luwei.host/v1/health
curl -fsS https://vg-api.luwei.host/v1/matches/summary
curl -fsS https://vg-api.luwei.host/v1/dashboard >/dev/null
```

生产配置下日志应出现 `static_json=disabled`，以及 `api_sync=synced` 或
`api_sync=current`，并包含 `purpose=dashboard_publish`、所选网卡、源地址、
数据源进度和上传字节数。失败批次会留在
`/state/api-outbox/`，容器重启后会使用相同幂等键重试；API 确认后才更新本地
水位，因此不会因超时而重复创建对局。

常驻 worker 每秒只读取一个持久化变更版本号。排行榜相关数据变化后会等待 2 秒，
把同一批数据库写入合并成一次同步；没有变化时不会扫描完整数据，也不会发送网络请求。
容器重启后会先主动校验一次，并且每天做一次完整兜底校验。历史对局补录会按原始
直播时间写入，服务端再从该玩家最早的对局开始重算其段位，并原子更新全部榜单。
旧的静态 manifest、趋势和快照不再更新，但会继续作为前端的故障回退。

worker 从现有 `settings.toml` 读取网络分工。旧配置尚无
`network.dashboard_publish` 时会继承 `network.upload` 的固定线路；后续可在
BLREC 网络管理页单独修改“排行榜数据发布”。固定线路会同时绑定源地址和该
网卡 DNS；未选网卡时使用并记录系统默认网卡。

## 人工检查一次数据

需要立即检查源数据时，先停掉常驻 worker，避免单例锁阻止一次性任务，再运行：

```bash
docker compose --project-name blrec-dashboard \
  --env-file publisher.env \
  -f compose.yml stop dashboard-publisher
docker compose --project-name blrec-dashboard \
  --env-file publisher.env \
  -f compose.yml run --rm dashboard-publisher --once
docker compose --project-name blrec-dashboard \
  --env-file publisher.env \
  -f compose.yml up -d dashboard-publisher
```

没有数据变化时这次检查会直接跳过 API 写入。评分或榜单算法更新由 API 发布触发，
不需要重新上传源对局。

## 停止与回滚

```bash
docker compose --project-name blrec-dashboard \
  --env-file publisher.env \
  -f compose.yml stop dashboard-publisher
```

停止 worker 不会改变当前线上 manifest，站点会继续显示最近一次成功快照。
需要回滚 worker 时，只修改 `DASHBOARD_PUBLISHER_IMAGE_TAG` 为已验证的版本号或
提交 SHA，再次执行 `pull` 和 `up -d`；仍须先备份并验证数据库。镜像仓库固定为
`ghcr.io/luweicn/blrec-dashboard-publisher`，不要改回 Server 镜像仓库。
