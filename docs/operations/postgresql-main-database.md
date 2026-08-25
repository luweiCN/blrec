# PVE PostgreSQL 主库

## 数据边界

NAS 的在线业务数据统一写入 PVE 虚拟机 `blrec-platform`
（`192.168.50.62`）上的 PostgreSQL 数据库 `blrec_dashboard`：

- `core` schema 是唯一业务主库，保存稿件、分 P、录像会话、上传任务、对局、模型结果、实时预分析、Worker 状态、训练候选元数据和访问日志归档。
- Public Dashboard API 直接只读 `core` 中经过批准的公开业务表；当前榜单和对局不再复制到另一套投影表。`public` schema 只保存结构化历史趋势、结算图片元数据和写入幂等记录。
- 录像、结算截图、训练图片、模型文件、日志和缓存仍是 NAS 文件，数据库只保存路径、摘要和状态。
- `auth.sqlite3`、`control.sqlite3` 与 `recording-journal.sqlite3` 继续留在 `/cfg`。前两者分别负责后台登录会话和断网时的本机控制幂等性；`recording-journal.sqlite3` 只保存 Recorder/Postprocessor 的本地有序事件和 source/run 关联，供 PostgreSQL 恢复后幂等投影。三者都不提供业务列表查询，也不是第二业务主库。
- 原 `blrec.sqlite3` 在切换后只作为回滚快照，不再接受在线写入。

主库不可达时，账号、投稿、审核、分析和公开数据等远端业务数据库操作必须暂停，禁止把这些业务表自动写回 SQLite，否则恢复连接后会形成两套互相冲突的数据。录像文件仍写入 `/rec`，录制生命周期只追加到本地 `recording-journal.sqlite3`；后台按 sequence 重试，远端事务提交后才在本地确认。该 outbox 是采集日志，不属于业务双主。

本地 journal 使用 WAL、`synchronous=FULL`、`foreign_keys=ON`、固定 application/schema version 和 `0600` 权限。可通过认证接口 `GET /api/v1/recording-sessions/outbox-status` 查看本地是否 ready、积压数量、最老积压年龄、最近同步时间和错误；该接口不依赖 PostgreSQL 录制列表。

旧部署升级后会在设置文件同目录自动创建 `recording-journal.sqlite3`，无需从旧 `blrec.sqlite3` 迁表；可用 `BLREC_RECORDING_JOURNAL_DATABASE` 仅覆盖该本地文件路径。首版保留全部已同步事件用于审计和恢复，不自动清理；容量评估和备份必须包含它。

## 连接与权限

PostgreSQL 在受信任 LAN 的 `192.168.50.62:5432` 提供服务，不向公网开放。NAS 直接
连接该地址，不再经过公网 SSH 隧道：

- 主应用使用 `blrec_core`，Publisher 使用只读的 `blrec_core_reader`；
- Dashboard API 在同一 PVE 虚拟机内通过 `127.0.0.1:5432` 连接，账号只对
  `grant-core-read.sql` 明列的 `core` 表拥有 `SELECT`，对 `public` 保持所需写权限；
- `core` 的唯一在线写入者是 NAS `blrec-next`，Analysis Worker 不直连数据库；
- 旧 `compose.postgres.yml` 和 `blrec-database-tunnel` 只用于已备份的旧云回滚，不得与
  PVE 直连配置同时作为生产入口。

生产连接串必须包含 `connect_timeout=5`，并用
`options=-csearch_path%3Dcore` 把 schema 固定为 `core`。连接串只保存在权限为 `0600`
的 NAS 环境文件中，不写入 Compose、日志或仓库。生产使用
`compose.lan-postgres.yml` 和权限为 `0600` 的环境文件；示例见
`synology.lan-postgres.env.example`。

## 首次迁移

迁移需要短暂停写。开始前先核对运行容器的 Compose 标签、工作目录以及 `/cfg`、
`/log`、`/rec`、`/clips` 挂载。随后在仍运行旧 SQLite 主库的容器中执行：

```bash
python /app/scripts/backup_blrec_database.py --label before-postgres-cutover
```

只有输出文件非空且显示 `integrity=ok` 才能继续。停止主服务和 Dashboard Publisher，
确认没有进程继续写 `blrec.sqlite3`。在 PVE PostgreSQL 创建空的 `core` schema，并让
`blrec_core` 成为其 owner；目标 schema 中不能已有业务表。

把目标版本的 `compose.synology.yml`、`compose.lan-postgres.yml` 和环境文件放入正确的
Container Manager 项目目录。先验证直连配置可以解析：

```bash
docker compose --env-file .env \
  -f compose.synology.yml -f compose.lan-postgres.yml config >/dev/null
```

再从停止写入后的 SQLite 复制到空的 `core`。迁移器会再次创建 SQLite 备份、检查源库
和备份的 `PRAGMA quick_check`、核对 schema 版本、逐表复制并比较行数；任一步失败都会
回滚 PostgreSQL 事务：

```bash
docker compose --env-file .env \
  -f compose.synology.yml -f compose.lan-postgres.yml \
  run --rm --entrypoint python blrec-next \
  /app/scripts/migrate_blrec_sqlite_to_postgres.py \
  --sqlite /cfg/blrec.sqlite3 \
  --backup-directory /cfg/backups \
  --expected-database blrec_dashboard \
  --expected-schema core \
  --apply
```

迁移完成后给 `blrec_core_reader` 授予 `core` 的 `USAGE`、所有现有表和 sequence 的
只读权限，并为 `blrec_core` 后续创建的表设置相同 default privileges。随后启动主服务：

```bash
docker compose --env-file .env \
  -f compose.synology.yml -f compose.lan-postgres.yml \
  up -d
```

最后把 Dashboard Publisher 的 `DASHBOARD_DATABASE_URL` 改为只读连接串，重建该独立
Compose 项目。不得再配置 `DASHBOARD_DATABASE=/cfg/blrec.sqlite3`。

## 从旧云迁移到 PVE

旧云 `core` 与 PVE `core` 一旦分别运行过，不能按主键只追加新行：队列清理、状态更新和
删除会让目标端行数反而更高。切换必须把仍在接受 `blrec-next` 写入的旧云 `core` 视为
源，在一个明确停写点整体刷新 PVE `core`，同时保留 PVE `public`：

1. 在运行中的 NAS 容器执行 `backup_blrec_database.py`，并在 PVE 用 `pg_dump` 备份完整
   数据库及 globals；两份都必须非空且通过 `pg_restore --list`。
2. 冻结旧云 `core` 写入或停止唯一写入者，生成最终 `--schema=core` custom dump；确认
   recording journal 没有无法解释的 pending 事件。
3. 暂停 PVE Dashboard API 和 NAS Publisher，确认目标库没有其他 client session；仅替换
   `core`，不得清理 `public`。恢复必须使用单事务，并在完成后恢复
   `blrec_core_reader`、`blrec_dashboard` 的只读授权和 default privileges。
4. 对两端全部 core 表做精确行数、主键范围、时间列和 schema digest 对账；完全一致后
   才把 `blrec-next` 改用 LAN 环境文件。
5. 切换后确认旧云没有业务 client、PVE revision 继续增长，再做一次 PVE core 与
   recording journal 备份。旧 tunnel 只保留在明确的回滚窗口内，不得继续作为生产入口。

## 验收

切换后至少检查：

1. 主容器配置和服务端 `inet_server_addr()` 都是 `192.168.50.62`，PostgreSQL 实际 `current_database()` 为 `blrec_dashboard`、`current_schema()` 为 `core`、schema 版本与应用一致。
2. SQLite 与 PostgreSQL 的全部 core 表及逐表行数一致，关键的稿件、分 P、对局、Worker、实时分析队列数量一致。
3. 管理页登录、列表、任务领取与心跳、实时预分析、录播分析和 Publisher 各完成一次真实读写。
4. Public Dashboard API 能直接读取 `core`，榜单与迁移前相同；修改业务数据后 revision 增长、进程缓存刷新且外网页面的 SSE 更新正常。
5. 切换后执行一次 PostgreSQL 备份；输出必须非空并显示 `integrity=ok`。
6. 检查 outbox status 为 ready；正常稳定期 pending 应很快回到 0。模拟短暂断库时，确认录像文件和本地事件继续增长，恢复后积压归零且场次/分 P 不重复。

```bash
docker exec blrec-next python /app/scripts/backup_blrec_database.py \
  --label after-postgres-cutover
```

PostgreSQL 模式下该脚本使用 16 版 `pg_dump` 生成自定义格式文件，并用
`pg_restore --list` 验证；若 `/cfg/recording-journal.sqlite3` 已存在，脚本同时通过 SQLite
backup API 生成一致快照并对源与备份执行 `PRAGMA quick_check`，不会遗漏运行中的 WAL。
两份备份都保存在 NAS `/cfg/backups`。日常升级、迁移和批量重算前都必须先执行同一脚本，
任一份为空或完整性失败都必须终止更新。

## 回滚

只允许整体回滚，不能让 PostgreSQL 和 SQLite 同时写入：

1. 停止主服务和 Publisher，记录 PostgreSQL 最后写入时间并保留最新 `.dump`。
2. 查询 outbox status；pending 不为 0 时必须先恢复 PostgreSQL 并完成投影，或保留 journal 交给仍支持它的版本处理，禁止直接启动不识别 outbox 的旧镜像丢弃事件。
3. 若 PostgreSQL 已产生新业务数据，先恢复问题或把新数据完整迁回 SQLite；不得直接启动旧快照覆盖这些写入。
4. 只有确认 PVE PostgreSQL 自切换后没有新写入，才能改回已备份的旧云环境文件和 tunnel，恢复迁移前的 `/cfg` 备份与旧镜像。恢复或替换 `recording-journal.sqlite3` 时服务必须停止，主文件、WAL/SHM 不得分开裸拷贝。
5. 启动后对业务 SQLite 和 recording journal 分别执行 `PRAGMA quick_check`，再核对关键表数量和页面数据。

后续每次提升 `BiliUploadDatabase.LATEST_SCHEMA_VERSION`，都必须同时提供并测试
PostgreSQL 迁移；应用遇到版本不一致会拒绝启动，不会猜测或静默修改生产 schema。

## 后续版本升级

主库切换到 PostgreSQL 后，版本升级仍需短暂停写。先在旧容器中执行并验证 PostgreSQL
与 recording journal 两份备份，
再停止主服务；迁移工具会尝试取得与主服务相同的数据库独占锁，若仍有主服务持有锁会
直接拒绝执行。使用目标版本镜像运行：

```bash
python /app/scripts/migrate_blrec_postgres_schema.py \
  --expected-database blrec_dashboard \
  --expected-schema core \
  --apply
```

全部待执行版本必须出现在工具的明确支持列表中。所有 DDL、数据修复和
`schema_migrations` 写入位于同一个事务；任一语句失败都会整体回滚。迁移成功后再启动
目标版本主服务，并重新执行一次 PostgreSQL 备份和版本检查。
