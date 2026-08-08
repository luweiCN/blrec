# 群晖排行榜数据 worker

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
curl -fsS https://vg.luwei.host/data/manifest.json
```

日志应出现 `publication=published` 或 `publication=current`，并包含
`purpose=dashboard_publish`、所选网卡、源地址、数据源进度和上传字节数。首次
发布后再次以 `--once` 运行应显示当天已发布且上传字节为 0。

worker 从现有 `settings.toml` 读取网络分工。旧配置尚无
`network.dashboard_publish` 时会继承 `network.upload` 的固定线路；后续可在
BLREC 网络管理页单独修改“排行榜数据发布”。固定线路会同时绑定源地址和该
网卡 DNS；未选网卡时使用并记录系统默认网卡。

## 人工重算当天数据

算法升级或数据修正后需要在同一天重新发布时，先停掉常驻 worker，避免单例锁
阻止一次性任务，再显式使用 `--force`：

```bash
docker compose --project-name blrec-dashboard \
  --env-file publisher.env \
  -f compose.yml stop dashboard-publisher
docker compose --project-name blrec-dashboard \
  --env-file publisher.env \
  -f compose.yml run --rm dashboard-publisher --once --force
docker compose --project-name blrec-dashboard \
  --env-file publisher.env \
  -f compose.yml up -d dashboard-publisher
```

`--force` 只允许与 `--once` 同用。它会忽略本地当天待发布快照并重新读取
SQLite，但仍执行源数据水位防回退、不可变快照优先和 manifest 最后提交校验；
常驻每日任务不会自动强制覆盖当天数据。

## 停止与回滚

```bash
docker compose --project-name blrec-dashboard \
  --env-file publisher.env \
  -f compose.yml stop dashboard-publisher
```

停止 worker 不会改变当前线上 manifest，站点会继续显示最近一次成功快照。
需要回滚 worker 时，只修改 `DASHBOARD_PUBLISHER_IMAGE_TAG` 为已验证的
`dashboard-publisher-<commit>`，再次执行 `pull` 和 `up -d`；仍须先备份并验证
数据库。
