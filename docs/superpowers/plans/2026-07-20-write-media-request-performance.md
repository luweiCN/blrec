# Write and Media Request Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让全部 36 条 Write/media 入站请求在慢密码散列、事件突发、文件删除、FLV 快照、FFprobe、批量状态修改和媒体读取压力下仍具有明确的 admission、恢复边界与延迟预算，同时不改变 B 站请求频率和既有业务语义。

**Architecture:** 保留 FastAPI、Angular、SQLite、现有录制核心和上传运行时。同步 CPU/文件工作进入各自的有界工作器；需要等待生命周期、删除或重启的控制动作先持久化 intent，再由可恢复 worker 收敛；纯本地批量写入合并为单事务；完成媒体统一使用一个轻量资源与 HTTP response helper。I-055 保持原实现，混合标记请求只处理本地层，远端复用与重试留给 Outbound 计划。

**Tech Stack:** Python 3.8+、FastAPI、SQLite、pytest、Argon2、FFmpeg/FFprobe、Angular 15、RxJS、Jasmine/Karma。

---

## 全局约束与预算

- 不使用 git worktree；每个任务独立提交、独立回滚，并按本计划顺序执行。
- 保持 Python 3.8 兼容：不得使用 `asyncio.timeout`、`TaskGroup`、`str | None` 等更高版本才提供的 API/语法。
- 先写能稳定复现问题的失败测试，再做最小实现；不以提高线程数、队列长度或 B 站请求频率掩盖阻塞。
- `C100`：本地控制动作在 100 ms 内确认；纯本地 58 项批量修改在 2 秒内完成。
- `D100`：普通本地数据库读取 p95 小于 100 ms。
- `T150`：媒体 access/首字节 p95 小于 150 ms；持续传输不按连接总时长判慢。
- `STR`：WebSocket 分开记录握手、首事件、持续时长、事件/字节、峰值积压及断开原因。
- event-loop harness：工作线程内的阻塞 fake 运行期间，每 10 ms heartbeat 的额外延迟 p95 小于 25 ms。
- 单元测试使用 barrier、fake clock、线程归属、调用次数和队列容量证明结构边界，不把共享 CI 的真实 100/150 ms 墙钟作为通过条件；C100/D100/T150、heartbeat p95 和 58 项 2 秒目标只写入可重复 benchmark，并在 NAS 做一次允许的暖读验证。
- 破坏性删除只使用 `tmp_path`、fake manager 和一条受控 fixture；不得在 NAS 批量删除或压力测试。
- 媒体测试只使用仓库 fixture/临时文件；不得在 NAS 对真实大文件做并发 probe、剪辑、Range 或吞吐压测。
- 不修改房间轮询、直播流、弹幕、投稿、审核、评论、合集和通知的远端 cadence/retry。混合请求中的 `ensure_room_id`、room/play info、category catalog 等工作只保留现状并观测。
- 日志与指标不得包含密码、Cookie、token、异常正文、请求正文、查询值或本地媒体路径。

## 依赖与实施顺序

| 顺序 | 任务 | 优先级与原因 | 依赖 |
| --- | --- | --- | --- |
| 1 | WM-01 有界密码工作器 | P0：同步 Argon2 与 store 锁队头阻塞整个事件循环 | 无 |
| 2 | WM-02 有界 WebSocket pump | P0：每事件一个无界 task，既无背压也无连接指标 | 无 |
| 3 | WM-03 可恢复删除队列 | P0：HTTP 可等待上传循环或最长一小时的高光 worker | 无 |
| 4 | WM-04 活动 FLV 快照 single-flight | P1：同步 FLV 重写且播放/高光重复构造 | 无 |
| 5 | WM-05 有界高光检查 | P0/P1：源数量和总 probe 时间无界且检查被重复三次 | WM-04 |
| 6 | WM-06 task desired-state reconciler 与 control journal | P1：最多 100 个生命周期动作串行占用请求，且无逐项最终状态 | 无 |
| 7 | WM-07 membership/control operation | P1：add/remove/collect 无可恢复 operation 边界 | WM-06 的 journal/reconciler |
| 8 | WM-08 设置持久化与 apply 分离 | P1：请求解析同步目录 IO，写入非原子，重启前台等待 | WM-07 |
| 9 | WM-09 上传/场次批量事务 | P1：每项一个事务，retry-failed 无 LIMIT | WM-03 |
| 10 | WM-10 统一媒体 HTTP 语义 | P1：重复 hydration/stat/open，完成媒体无条件缓存 | WM-04 |
| 11 | WM-11 有界弹幕 cursor | P2：cache miss 会从头扫描到任意 cursor | 无 |
| 12 | WM-12 封面文件工作器 | P2：JPEG 扫描、hash、失败清理仍在 event loop | 无 |

## 36/36 请求覆盖

| 任务 | 请求 ID | 本阶段处置 |
| --- | --- | --- |
| WM-01 | I-002、I-003、I-006、I-009 | hash/verify 有界 off-loop，短锁读取与 CAS 提交 |
| WM-02 | I-043、I-044 | 每连接单 sender、128 queue、1013 overflow、STR 指标 |
| WM-03 | I-065(delete)、I-068(delete/control)、I-097 | 删除只持久化请求，单 worker 分片恢复 |
| WM-04 | I-071，并服务 I-089、I-090 | 活动 FLV 快照 single-flight 与共享 duration |
| WM-05 | I-089、I-090 | 产品固定单 source、30 秒绝对期限、检查 token/指纹复用 |
| WM-06 | I-011(state)、I-022、I-023、I-024、I-025、I-026、I-027、I-028、I-029 | desired state 一次持久化，后台合并收敛 |
| WM-07 | I-011(delete)、I-030、I-031、I-032、I-102 | 持久化 operation journal 与单 worker |
| WM-08 | I-034、I-036、I-039、I-041 | 原子设置写入、目录 worker、后台 apply/restart |
| WM-09 | I-065(non-delete)、I-068(non-delete)、I-069 | 单事务/SAVEPOINT、LIMIT 100、一次 wakeup |
| WM-10 | I-072、I-095、I-096 | lightweight resource、Range/ETag/If-Range/cache/首字节指标 |
| WM-11 | I-073 | 顺序 continuation，cache miss 不再 O(cursor) |
| WM-12 | I-081 | 2+8 有界 validation/hash/store/cleanup |
| 保留 | I-055 | **唯一整项无需修改**；只跑既有事务/回滚测试 |

表中去重后为 36 个 ID；I-011、I-065、I-068、I-089、I-090 按动作或共享服务跨任务出现，但每个入口只保持一份最终路由契约。阶段基线显式冻结为：

```text
I-002 I-003 I-006 I-009 I-011 I-022 I-023 I-024 I-025 I-026 I-027
I-028 I-029 I-030 I-031 I-032 I-034 I-036 I-039 I-041 I-043 I-044
I-055 I-065 I-068 I-069 I-071 I-072 I-073 I-081 I-089 I-090 I-095
I-096 I-097 I-102
```

实施前 FastAPI/台账基线为 105 条（I-104 为录制场次详情，I-105 为分 P 高光计数）；本计划保留两条领域状态 GET：WM-05 登记 I-106 inspection status，WM-06 登记 I-107 control-operation status，实施后总数必须为 107。I-070 只属于 Hot-read 预览，retry mutation 全部归 I-069。

## 只保留、不另造任务的现有行为

- **I-055 整项保留：**账号移除继续使用一次 `BiliUploadDatabase.write`、`BEGIN IMMEDIATE/COMMIT/ROLLBACK`、关系校验和凭据归档；只执行既有 fake-backed destructive regression。它是本计划唯一完整 `Keep` 的 Write/media 请求。
- I-011 的 `cut` 继续做内存触发；`refresh` 的远端刷新留给 Outbound。
- I-022/I-024/I-026/I-028/I-031 的 all-path 当前每批只 dump 一次；迁移后不得退化为逐项 dump。
- I-011/I-065/I-068 的 1--100 上限与唯一 ID 校验继续保留；只有 I-069 补上 LIMIT 100。
- 认证继续保留 hash 参数、用户名/密码错误同质化、持久化限流、bootstrap 校验、改密/恢复后的全 session revoke。
- I-071 的 reader 文件解析、I-073 的 XML 解析、I-081 的文件写入、I-097 的 unlink 已 off-loop 的部分不得搬回事件循环。
- I-072/I-096 的普通 Range、suffix Range、206、416 `Content-Range`、媒体签名 token 和下载文件名全部保留。
- 活动 FLV 继续 `no-store`；`MediaSnapshotStore` 继续最多 64 项。
- 弹幕继续 `limit <= 500`、`limit + 1` 首屏、XXE/DOCTYPE 防护；封面继续 2 MiB 前置上限、`O_EXCL`、0600、fsync 与 content-addressed 复用。
- FFprobe 继续 `shell=False` 和单命令 timeout；高光剪辑继续 worker lease/fence/restart recovery。
- 删除继续校验 dedicated clip root/ownership，不删除 recording source，不删除 B 站稿件；上传 unknown-outcome 与 lease fence 不变。

## 与 Outbound 计划的 owner 交接

- Write/media 先落地 journal 与 lane owner，Outbound 只能增强这些 owner 内的远端步骤，不能在 HTTP route、BackgroundTasks 或第二套 worker 中重复执行。
- Outbound Task 3 将 `reuse_info_revision` 的生产/消费和不同 room 并发 2 加入 WM-07 `room-membership` owner；不得重新等待 start/recorder/collect，也不得逐房间 dump settings。
- Outbound Task 4 的 stream-resolution handoff 进入 WM-06 `task-state` reconciler/recorder owner；固定线路或远端解析失败都由同一个 generation/postcondition 契约收敛。
- Outbound 实施基线是本计划完成后的 107 条路由；I-104/I-105 含义不变，I-106/I-107 分别属于 inspection/control status。两份计划共同改动的 `application.py`、`task_manager.py`、`browser_extension.py`、`runtime.py`、`main.py` 必须以后提交者基于当前 public operation contract 编写测试。

### Task 1: WM-01 有界密码工作器与无锁 Argon2 阶段

**覆盖：** I-002、I-003、I-006、I-009。

**Files:**
- Create: `src/blrec/web/password_work.py`
- Modify: `src/blrec/web/auth_store.py`
- Modify: `src/blrec/web/routers/auth.py`
- Modify: `src/blrec/web/main.py`
- Modify: `tests/web/test_auth_store.py`
- Modify: `tests/web/test_auth_routes.py`

**接口：** `PasswordWorkCoordinator.run(callable)` 提供 1 个活动 job、4 个等待 job；满载抛 `PasswordWorkSaturated(retry_after=1)`。`AdminAuthStore` 暴露短锁内 prepare/commit 方法，router 只在 coordinator 中运行 `hash_password`/`verify_password`。

- [ ] **Step 1: 写失败的锁隔离、heartbeat 与过载测试**

在 `test_auth_store.py` 用可阻塞 hasher 启动 login/change；阻塞 verify/hash 后并行调用 `authenticate_session()`，断言不会等待 hasher。再覆盖旧 hash 在 verify 后被并发改密时 CAS 拒绝、错误密码仍计入 persistent throttle、change/recover 成功仍撤销所有 session。

在 `test_auth_routes.py` 同时发出 6 个由 barrier 阻塞的密码请求：1 个 running、4 个 queued，第 6 个不得进入 executor 并返回 503 与 `Retry-After: 1`；过载不得增加 login failure。对 setup/login/change/recover 各跑 heartbeat，单元测试只证明 event loop 可继续推进，不以真实毫秒墙钟断言。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/web/test_auth_store.py tests/web/test_auth_routes.py -k 'password_worker or hash_lock or overload or revoke or rate_limit' -q
```

Expected: FAIL，因为当前 hash/verify 在 async handler 或 `AdminAuthStore._lock` 内运行，且不存在 admission boundary。

- [ ] **Step 2: 拆分 prepare、CPU work 与 CAS commit**

新增不可变 ticket：`LoginPasswordTicket(encoded_hash, admin_exists, username_matches, rate_limit_key, observed_version)` 与 `PasswordChangeTicket(encoded_hash, observed_version)`。prepare 阶段只在锁内读取 hash/version 并检查 rate-limit；worker 阶段做 dummy/real verify、needs-rehash 和新 hash；commit 阶段重新检查 `admin.updated_at/password_hash`，以 `UPDATE ... WHERE password_hash=? AND updated_at=?` 提交。旧值变化时安全失败，不允许用旧密码创建 session。

setup 先在 worker hash，再在一次事务中确认未初始化并创建 session；change/reset 在同一 CAS 事务更新 hash、写 audit、撤销全部 session。login invalid 在 commit 锁内调用现有 `_record_failed_login`；worker saturation 不进入该路径。

- [ ] **Step 3: 接入有界 coordinator 与 503 映射**

`password_work.py` 使用专用 `ThreadPoolExecutor(max_workers=1, thread_name_prefix='blrec-password')` 和锁保护的 `active + waiting <= 5` 计数；拒绝发生在提交 executor 之前。`auth.configure()` 同时接收 coordinator，四个 handler `await` 其工作；`main.py` startup 创建/配置。shutdown 先停止 admission，再追踪并等待全部 running+queued future 得到明确结果，最后关闭 executor；不得只取消 asyncio wrapper 并假定已运行线程停止。503 只返回固定 detail 和 `Retry-After`。

- [ ] **Step 4: 验证安全语义与预算**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/web/test_auth_store.py tests/web/test_auth_routes.py -q
black --check src/blrec/web/password_work.py src/blrec/web/auth_store.py src/blrec/web/routers/auth.py tests/web/test_auth_store.py tests/web/test_auth_routes.py
flake8 src/blrec/web/password_work.py src/blrec/web/auth_store.py src/blrec/web/routers/auth.py
mypy src/blrec/web/password_work.py src/blrec/web/auth_store.py src/blrec/web/routers/auth.py
```

Expected: PASS；活动 Argon2 恒为 1、等待不超过 4、过载不进入 executor、shutdown 无遗留 future，既有 rate-limit/revoke/hash 参数全部通过；C100/heartbeat 数值另写 benchmark。

- [ ] **Step 5: Commit**

```bash
git add src/blrec/web/password_work.py src/blrec/web/auth_store.py src/blrec/web/routers/auth.py src/blrec/web/main.py tests/web/test_auth_store.py tests/web/test_auth_routes.py
git commit -m "perf: bound password hashing work"
```

### Task 2: WM-02 单发送者 WebSocket pump

**覆盖：** I-043、I-044。

**Files:**
- Modify: `src/blrec/web/routers/websockets.py`
- Create: `tests/web/test_websockets_streams.py`
- Modify: `tests/web/test_websockets_auth.py`

**接口：** 私有 `_run_connection_pump(websocket, route, subscribe, serialize)`；每连接一个 `asyncio.Queue(maxsize=128)`、一个 sender task、一个 Rx subscription。

- [ ] **Step 1: 写 1,000 事件突发和清理失败测试**

阻塞 fake `send_text`，同步推送 1,000 个事件/异常；记录创建的 sender task、queue 峰值、发送顺序和 subscription dispose 次数。断言当前实现会创建多于一个 send task，作为 RED。增加正常断开、send error、client disconnect 与 overflow 四条路径；exception 测试用敏感 marker，断言 audit/metric 不含 marker。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/web/test_websockets_auth.py tests/web/test_websockets_streams.py -q
```

Expected: FAIL，因为 callback 当前每个事件都 `asyncio.create_task()`，无 queue、1013 或统一 finally。

- [ ] **Step 2: 实现严格保序的有界 pump**

callback 只调用 `loop.call_soon_threadsafe(enqueue, item)`，因此 Rx 从其他线程发事件时也不直接触碰 asyncio queue；loop 已 closing/closed 时安全忽略 enqueue 并进入一次性 dispose。`enqueue` 在 loop 内 `queue.put_nowait()`。队列满时立刻终止可能阻塞在 `send_text` 的 sender，并由独立 control path 主动 close 1013，不能只设置无人消费的 flag，也不能静默丢 event/exception。sender 是唯一 serializer/`send_text` 调用者，逐条保序发送。所有退出统一进入 `finally`：dispose 一次、取消并 await sender、关闭 socket（如尚未关闭）、完成一次终止 future。

- [ ] **Step 3: 添加不含 payload 的 STR 指标**

在 `finally` 用 `audit('websocket_connection', ...)` 写 route、handshake_ms、first_event_ms、duration_ms、events、bytes、peak_backlog、disconnect_reason、disconnect_code；bytes 按实际发送的 UTF-8 字节数计算，序列化后的正文和 exception 文本都不进入字段。事件/异常两个 route 只提供不同 serializer，复用同一 pump。

- [ ] **Step 4: 验证容量、指标和认证回归**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/web/test_websockets_auth.py tests/web/test_websockets_streams.py -q
black --check src/blrec/web/routers/websockets.py tests/web/test_websockets_streams.py
flake8 src/blrec/web/routers/websockets.py tests/web/test_websockets_streams.py
mypy src/blrec/web/routers/websockets.py
```

Expected: PASS；每连接恰好一个 sender、queue <= 128、overflow=1013、顺序不变、断开后无遗留 task/subscription，STR 字段完整且不含 payload。

- [ ] **Step 5: Commit**

```bash
git add src/blrec/web/routers/websockets.py tests/web/test_websockets_auth.py tests/web/test_websockets_streams.py
git commit -m "perf: bound websocket event delivery"
```

### Task 3: WM-03 可恢复、分片执行的本地删除队列

**覆盖：** I-065 `delete_local`、I-068 delete/control 分支、I-097。

**Files:**
- Create: `src/blrec/bili_upload/deletion_worker.py`
- Create: `src/blrec/bili_upload/migrations/0026_initial.sql`
- Modify: `src/blrec/bili_upload/database.py`
- Modify: `src/blrec/bili_upload/task_actions.py`
- Modify: `src/blrec/bili_upload/highlights.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `src/blrec/bili_upload/media_index.py`
- Modify: `src/blrec/bili_upload/upload.py`
- Modify: `src/blrec/bili_upload/upos.py`
- Modify: `src/blrec/bili_upload/transcode_remux.py`
- Modify: `src/blrec/bili_upload/comments.py`
- Modify: `src/blrec/bili_upload/danmaku_import.py`
- Modify: `src/blrec/bili_upload/danmaku_publish.py`
- Modify: `src/blrec/bili_upload/collection_publish.py`
- Modify: `src/blrec/bili_upload/highlight_worker.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `src/blrec/web/routers/highlights.py`
- Modify: `tests/bili_upload/test_database.py`
- Create: `tests/bili_upload/test_deletion_worker.py`
- Modify: `tests/bili_upload/test_task_actions.py`
- Modify: `tests/bili_upload/test_highlights.py`
- Modify: `tests/bili_upload/test_journal.py`
- Modify: `tests/bili_upload/test_media_index.py`
- Modify: `tests/bili_upload/test_upload.py`
- Modify: `tests/bili_upload/test_upos.py`
- Modify: `tests/bili_upload/test_transcode_remux.py`
- Modify: `tests/bili_upload/test_comments.py`
- Modify: `tests/bili_upload/test_danmaku_import.py`
- Modify: `tests/bili_upload/test_danmaku_publish.py`
- Modify: `tests/bili_upload/test_collection_publish.py`
- Modify: `tests/bili_upload/test_highlight_worker.py`
- Modify: `tests/bili_upload/test_account_runtime.py`
- Modify: `tests/web/test_recording_sessions_routes.py`
- Modify: `tests/web/test_highlights_routes.py`

**接口：** `LocalDeletionWorker.request_session/request_clip` 在一个事务中只写 `deletion_state='requested'` 并递增 `cancellation_generation`，随后 `wake()`；不得伪造或清空仍被 owner 持有的 lease。worker 并发 1，每次 lease quantum 最多处理 128 个已去重且通过 ownership guard 的 path，并在每个 item 前检查 stop。

- [ ] **Step 1: 写请求不等待、129 path 分片与崩溃恢复测试**

用阻塞 recorder/upload/UPOS/repair/comment/danmaku/collection/highlight/media-index owner fake 调 DELETE，断言 HTTP 只提交 intent、不会等待 owner，也不会调用全局 `_stop_upload_worker()`/`_stop_highlight_worker()`。用 barrier 证明任一 owner 未确认交接前不会快照路径或 unlink；特别覆盖 `media_index_state='indexing'`、`collection_branch_state='running'`、UPOS chunk/completion 请求在途。用 `tmp_path` 创建 129 个 owned files，第一次 `run_once()` 最多处理 128 个并持久化 cursor；关闭并重建 runtime 后处理剩余项。分别注入 unlink 失败、数据库提交前崩溃、提交后重跑、非 owned path 与 recording source。

加入完整 crash matrix：intent 前/后、远端请求发出前/后、lease 释放前/后、unlink 前/后、DB children 删除前/后。对 UPOS completion、collection add 与 media-index rebuild 分别在 side-effect barrier 中递增 generation：确认成功/失败只能进入 handoff outcome，响应丢失或重启必须收敛为 `unknown_terminal`/`cancelled_local` 并释放 owner，deletion 随后能继续且不会重发远端写。每个 case 都断言不会删除仍被使用的文件，旧 generation 的 owner 普通回写必然被 CAS 拒绝。单元测试用 barrier/调用次序判断快速确认，不用真实 C100 墙钟。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_deletion_worker.py tests/bili_upload/test_task_actions.py tests/bili_upload/test_highlights.py tests/bili_upload/test_account_runtime.py tests/web/test_recording_sessions_routes.py tests/web/test_highlights_routes.py -k 'delet' -q
```

Expected: FAIL，因为 session/job 删除仍可在请求内继续，clip delete 会等待两个全局 worker，clip 也没有可恢复 deletion state。

- [ ] **Step 2: 增加 migration 26 的持久状态**

新增 `local_deletion_items(id,owner_kind,owner_id,cancellation_generation,path,state,error)`，以 `(owner_kind,owner_id,cancellation_generation,path)` 唯一并按 `(state,id)` 索引；它就是持久化 cursor。新增 `owner_handoff_outcomes(owner_kind,owner_id,side_effect_key,source_generation,outcome_state,outcome_json,acknowledged_at)`，唯一键为 `(owner_kind,owner_id,side_effect_key,source_generation)`，`outcome_state` 仅允许 `in_flight/confirmed_success/confirmed_failure/unknown_terminal/cancelled_local`；只有后四项是 terminal acknowledgement。`recording_sessions` 与 `highlight_clips` 增加单调递增的 `cancellation_generation`；clip 同时增加 `deletion_state`（`none/requested/quiescing/deleting/failed`）、`deletion_error`、`deletion_requested_at`，并建立 `(deletion_state,deletion_requested_at,id)` 索引。将 database latest version 更新为 26，并让 migration test 验证 CHECK、唯一约束、默认值、generation 递增和升级后的既有行。

- [ ] **Step 3: 统一 session/job 与 clip 删除状态机**

`delete_local_task()` 先解析到 session，再复用 `_request_session_deletion()`；不再保留第二套 prepare/delete/finish。请求事务只递增 generation、记录 requested 并清空旧 error。deletion worker 进入 quiescing 后先请求 `active_session_canceller`，然后等待：无活动 recording run、upload/UPOS/repair/comment/danmaku/collection/highlight 无有效 lease或 running branch、无 `media_index_state='indexing'`、无 processing clip。`completing/unknown_outcome` 只有在同 generation owner 仍活动时阻塞；owner 已写 terminal handoff acknowledgement 后不再永久阻塞。满足全部条件后才能在同一 generation 下快照候选路径并 set-based 写入 `local_deletion_items`。

recorder 通过 `active_session_canceller` 停止并由 journal 以 generation fence 拒绝迟到的 part/run 事件；deletion worker 必须观察到 run 已结束才继续。MediaIndexWorker 在打开/重建 FLV 前后检查 generation，generation 改变时只写 `cancelled_local` handoff 并释放 `media_index_owner`，不得回写 rebuilt path。upload、UPOS chunk/completion、repair、comment planner/publisher、danmaku importer/publisher、CollectionPublisher 和 highlight worker 在每个本地或远端 side-effect 前后读取当前 generation；claim/普通 commit SQL 必须带 generation CAS。

不可取消远端请求在发出前先持久化 `(side_effect_key,source_generation,in_flight)` intent。若响应返回时 generation 未变，按普通 lease CAS 提交；若 generation 已变，只允许一个专用 handoff transaction 在**新 generation 的删除 fence 下**写 `confirmed_success/confirmed_failure` 的安全 outcome、清理旧 lease/running branch并 acknowledgement，绝不恢复普通业务状态。若响应丢失或进程在请求后崩溃，startup recovery 把该 intent 收敛为 `unknown_terminal`、禁止自动重发并释放 lease；这也是可删除的 terminal acknowledgement，不再让 `completing/unknown_outcome` 永久卡住。outcome JSON 只保留安全远端标识/状态，不保存 token、Cookie、请求正文或错误正文。deletion worker 不用 `Future.cancel()` 假定线程、FFmpeg、unlink 或已发出的远端请求已经停止。

clip 删除保留既有产品语义：允许删除已绑定 clip 的本地 upload job/高光 upload session，在安全交接后把它们标记 cancelled 并从本地数据库删除；**不得拒绝该删除，也不得删除 B 站稿件**。worker 只允许删除 dedicated clip root 下的 output video/XML，不删除 source recording。unlink 与 item commit 之间崩溃时，重跑发现文件不存在视为成功；owner 完成后删除对应 `local_deletion_items`，避免表永久增长。失败写 `failed + deletion_error`，启动时恢复 `quiescing/deleting`。

- [ ] **Step 4: runtime 和路由只排队，不停全局 worker**

`BiliAccountRuntime` startup 创建并恢复一个 `LocalDeletionWorker`；shutdown 停止 admission，worker 在每个 path 前检查 stop，提交当前 item 后退出，不能强制等待一整个 128 项慢 NAS quantum。`run_recording_session_action` 的 delete 分支和 `delete_highlight_clip` 只调用 request/wake；pause/resume/set-upload/set-skip 直接依赖已有 DB lease/state fence 并 wake upload worker，不再停启全局 worker。两个 batch route 保持逐项 accepted/message；highlight DELETE 保持 204 兼容，但 detail/list 可读取 deletion state/error。

- [ ] **Step 5: 验证迁移、恢复、保护和预算**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_database.py tests/bili_upload/test_deletion_worker.py tests/bili_upload/test_task_actions.py tests/bili_upload/test_highlights.py tests/bili_upload/test_journal.py tests/bili_upload/test_media_index.py tests/bili_upload/test_upload.py tests/bili_upload/test_upos.py tests/bili_upload/test_transcode_remux.py tests/bili_upload/test_comments.py tests/bili_upload/test_danmaku_import.py tests/bili_upload/test_danmaku_publish.py tests/bili_upload/test_collection_publish.py tests/bili_upload/test_highlight_worker.py tests/bili_upload/test_account_runtime.py tests/web/test_recording_sessions_routes.py tests/web/test_highlights_routes.py -q
black --check src/blrec/bili_upload/deletion_worker.py src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/highlights.py src/blrec/bili_upload/journal.py src/blrec/bili_upload/media_index.py src/blrec/bili_upload/upload.py src/blrec/bili_upload/upos.py src/blrec/bili_upload/transcode_remux.py src/blrec/bili_upload/comments.py src/blrec/bili_upload/danmaku_import.py src/blrec/bili_upload/danmaku_publish.py src/blrec/bili_upload/collection_publish.py src/blrec/bili_upload/highlight_worker.py src/blrec/bili_upload/runtime.py
flake8 src/blrec/bili_upload/deletion_worker.py src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/highlights.py src/blrec/bili_upload/journal.py src/blrec/bili_upload/media_index.py src/blrec/bili_upload/upload.py src/blrec/bili_upload/upos.py src/blrec/bili_upload/transcode_remux.py src/blrec/bili_upload/comments.py src/blrec/bili_upload/danmaku_import.py src/blrec/bili_upload/danmaku_publish.py src/blrec/bili_upload/collection_publish.py src/blrec/bili_upload/highlight_worker.py src/blrec/bili_upload/runtime.py
mypy src/blrec/bili_upload/deletion_worker.py src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/highlights.py src/blrec/bili_upload/media_index.py src/blrec/bili_upload/upload.py src/blrec/bili_upload/upos.py src/blrec/bili_upload/comments.py src/blrec/bili_upload/danmaku_import.py src/blrec/bili_upload/danmaku_publish.py src/blrec/bili_upload/collection_publish.py src/blrec/bili_upload/highlight_worker.py
```

Expected: PASS；请求只提交 intent、worker 并发 1、quantum <=128、recorder/media-index/upload/UPOS/repair/comment/danmaku/collection/highlight 全部完成 generation-aware handoff 后才 unlink、不可取消请求最终有 terminal acknowledgement、clip 本地绑定任务可删除且远端稿件不动；C100 数值另写 benchmark。

- [ ] **Step 6: Commit**

```bash
git add src/blrec/bili_upload/deletion_worker.py src/blrec/bili_upload/migrations/0026_initial.sql src/blrec/bili_upload/database.py src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/highlights.py src/blrec/bili_upload/journal.py src/blrec/bili_upload/media_index.py src/blrec/bili_upload/upload.py src/blrec/bili_upload/upos.py src/blrec/bili_upload/transcode_remux.py src/blrec/bili_upload/comments.py src/blrec/bili_upload/danmaku_import.py src/blrec/bili_upload/danmaku_publish.py src/blrec/bili_upload/collection_publish.py src/blrec/bili_upload/highlight_worker.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/recording_sessions.py src/blrec/web/routers/highlights.py tests/bili_upload/test_database.py tests/bili_upload/test_deletion_worker.py tests/bili_upload/test_task_actions.py tests/bili_upload/test_highlights.py tests/bili_upload/test_journal.py tests/bili_upload/test_media_index.py tests/bili_upload/test_upload.py tests/bili_upload/test_upos.py tests/bili_upload/test_transcode_remux.py tests/bili_upload/test_comments.py tests/bili_upload/test_danmaku_import.py tests/bili_upload/test_danmaku_publish.py tests/bili_upload/test_collection_publish.py tests/bili_upload/test_highlight_worker.py tests/bili_upload/test_account_runtime.py tests/web/test_recording_sessions_routes.py tests/web/test_highlights_routes.py
git commit -m "perf: queue recoverable local deletions"
```

### Task 4: WM-04 活动 FLV 快照 single-flight

**覆盖：** I-071，并为 I-089/I-090 提供活动 duration。

**Files:**
- Create: `src/blrec/bili_upload/active_media.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `src/blrec/web/main.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `tests/bili_upload/test_journal.py`
- Modify: `tests/bili_upload/test_recording_content.py`
- Create: `tests/bili_upload/test_active_media.py`
- Create: `tests/web/test_main_active_media.py`
- Modify: `tests/web/test_recording_sessions_routes.py`

**接口：** `ActiveMediaService.snapshot(part_id, path, source_size, metadata)` 返回 `FlvMediaSnapshot`；in-flight key 使用不触盘的 `(part_id, abspath, source_size, lastkeyframelocation, lastkeyframetimestamp)`，worker 再解析 realpath 并形成 identity。2 个活动 job、8 个等待 job，相同 key single-flight；service 完成结果 cache 固定为 **0 entries / 0 prefix bytes**，旧 token 版本仍只由既有 `MediaSnapshotStore(max_items=64)` 持有。

- [ ] **Step 1: 写同步 FS/FLV、single-flight 与失效测试**

让 `realpath/open/FlvMediaSnapshot.create` 在 fake 中阻塞并记录线程；并发请求相同 key，断言只构造一次。future 完成后必须立刻从 in-flight map 移除；之后 source size 或两个 O(1) metadata revision 字段变化时重建。分别跑同一 part 的 100 次增长 revision 和 100 个不同 part，全部完成后 service retained entries/prefix bytes 必须恒为 0，而 `MediaSnapshotStore` 自己仍保持既有 64 项上限。构造第三个并发 job 证明 active <=2，第 11 个总 admission 不进入 executor 并得到 busy。给 journal 增加 `active_part_for_session(session_id)` 的失败契约：一场多个完成 part 和一个活动 part 时只返回最新 `recording/postprocessing` part，完成场次返回 None。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_recording_content.py tests/bili_upload/test_journal.py tests/bili_upload/test_active_media.py tests/web/test_main_active_media.py tests/web/test_recording_sessions_routes.py -k 'snapshot or active_media or active_duration or active_part' -q
```

Expected: FAIL，因为 route/main 当前在 event loop realpath/open/parse，相同 access 重复创建，highlights 遍历整场 parts。

- [ ] **Step 2: 实现有界 ActiveMediaService**

使用专用两线程 executor 和显式 10 项 admission 计数；realpath、FLV header/read、metadata 重写与 keyframe 遍历全部在 worker。in-flight future 按 revision key 共享并在 success/error/cancel 的 finally 移除；service **不保留任何 completed snapshot/prefix**，因此不同 part 也不会形成无界 dict。旧播放 token 所需版本只由既有 `MediaSnapshotStore(max_items=64)` 持有。

- [ ] **Step 3: 播放和高光共用同一活动快照**

`create_recording_media_access()` await service；busy 映射 503 与固定 `Retry-After: 1`，损坏 FLV 仍回退 frozen snapshot。`RecordingJournalBridge.active_part_for_session()` 用一个 `artifact_state IN ('recording','postprocessing') ORDER BY part_index DESC LIMIT 1` 查询；`web/main.py:_active_highlight_durations` 只对该 part 调用同一 service，不遍历已完成 parts。`_active_recording_metadata` 不再做 event-loop realpath。startup 创建 service；shutdown 先停止 admission，追踪并等待全部 running+queued future 明确完成，再关闭 executor，不得假定取消 wrapper 会停止正在解析的线程。

- [ ] **Step 4: 验证 heartbeat、T150 与 active no-store**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_recording_content.py tests/bili_upload/test_journal.py tests/bili_upload/test_active_media.py tests/web/test_main_active_media.py tests/web/test_recording_sessions_routes.py -q
black --check src/blrec/bili_upload/active_media.py src/blrec/bili_upload/journal.py src/blrec/web/main.py src/blrec/web/routers/recording_sessions.py tests/bili_upload/test_active_media.py tests/web/test_main_active_media.py
flake8 src/blrec/bili_upload/active_media.py src/blrec/bili_upload/journal.py src/blrec/web/main.py src/blrec/web/routers/recording_sessions.py
mypy src/blrec/bili_upload/active_media.py src/blrec/bili_upload/journal.py src/blrec/web/routers/recording_sessions.py
```

Expected: PASS；active <=2、waiting <=8、相同 in-flight key 一次构造、同 part 100 revisions 与 100 different parts 后 completed cache 均为 0/0、shutdown 无遗留 future，活动 token/media 仍 `no-store`；T150/heartbeat 数值另写 benchmark。

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/active_media.py src/blrec/bili_upload/journal.py src/blrec/web/main.py src/blrec/web/routers/recording_sessions.py tests/bili_upload/test_recording_content.py tests/bili_upload/test_journal.py tests/bili_upload/test_active_media.py tests/web/test_main_active_media.py tests/web/test_recording_sessions_routes.py
git commit -m "perf: share bounded active media snapshots"
```

### Task 5: WM-05 有界高光检查与 inspection 复用

**覆盖：** I-089、I-090；依赖 Task 4 的活动媒体 duration。

**Files:**
- Create: `src/blrec/bili_upload/migrations/0027_initial.sql`
- Modify: `src/blrec/bili_upload/database.py`
- Modify: `src/blrec/bili_upload/highlight_cut.py`
- Modify: `src/blrec/bili_upload/highlights.py`
- Modify: `src/blrec/bili_upload/highlight_worker.py`
- Modify: `src/blrec/web/routers/highlights.py`
- Modify: `tests/bili_upload/test_database.py`
- Modify: `tests/bili_upload/test_highlight_cut.py`
- Modify: `tests/bili_upload/test_highlights.py`
- Modify: `tests/bili_upload/test_highlight_worker.py`
- Modify: `tests/web/test_highlights_routes.py`
- Modify: `webapp/src/app/upload-tasks/shared/highlight.model.ts`
- Modify: `webapp/src/app/upload-tasks/shared/highlight.service.ts`
- Modify: `webapp/src/app/upload-tasks/shared/highlight.service.spec.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts`
- Modify: `docs/performance/request-audit.md`

**接口：** 产品已固定“每次只剪一个分 P”，因此 inspect/create 必须恰好一个 source，不保留多 source 合并语义。inspection coordinator 最多 2 个活动、8 个等待，绝对期限 30 秒。客户端在 inspect 前生成 `idempotencyKey` 与 `claimKey`；cold request 立即返回 202 `operationId/retryAfterMs`，I-106 `GET /api/v1/highlights/inspections/{operation_id}` 通过 `X-BLREC-Inspection-Claim` request header 在终态安全交换一次性 `inspectionToken`，避免 claim 进入 URL/access log。明文 token 只出现在响应 body，不持久化、不进入日志。`CreateClipRequest` 必须带同一个 `idempotencyKey`。

- [ ] **Step 1: 写 source、总期限、queue 与重复 probe 的失败测试**

在 `test_highlight_cut.py` 构造 0/1/2 sources，断言只有单 source 被接受。用 fake monotonic clock 让两个 ffprobe 消耗预算，断言第二个命令只收到 `deadline-now` 的剩余 timeout，超过 30 秒预算时不再启动 subprocess，argv 仍为 list/tuple 且 `shell=False`。在 service/route tests 并发 11 个 barrier inspect，断言第 11 个不进入 executor并返回 503；相同 fingerprint/range single-flight。

跑完整 `cold inspect 202 -> I-106 terminal+token -> create -> worker`，记录源文件 ffprobe 次数必须为一轮。重复同 claimKey 模拟 token response 丢失，必须在 expiry 前得到同一明文 token；create response 丢失后以同 idempotency key 重提必须返回同一 clip；把 token 换成第二个 idempotency key 必须拒绝且不能消费原绑定。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_highlight_cut.py tests/bili_upload/test_highlights.py tests/bili_upload/test_highlight_worker.py tests/web/test_highlights_routes.py -k 'deadline or source_limit or inspection or probe_reuse or overload' -q
```

Expected: FAIL，因为 API 仍允许多 source、timeout 为每命令预算、create/worker 会重新 probe，也没有 durable inspection operation/token。

- [ ] **Step 2: 给 clip 持久化已验证 inspection**

Migration 27 在 `highlight_clips` 增加 `inspection_json TEXT`、`source_fingerprint_json TEXT` 与 `idempotency_key TEXT UNIQUE`；前两列只保存规范化 profile/keyframe range 与单 source `(part_id, realpath,size,mtime_ns)`，不得保存临时 token。新增 `highlight_inspections` 持久表，状态为 `accepted/running/succeeded/failed`，只保存安全 result/error code、fingerprint、range、idempotency key、claim-key hash、token hash/expiry/consumed_at；**不保存 claim key 或 token 明文**。accepted/running 受 10 项 admission 上限保护且绝不被 TTL/LRU 淘汰，只有 terminal 且过期的行可清理。database latest version 更新为 27，启动时把 interrupted running 恢复为 accepted；迁移测试验证旧 clip 列为 NULL、唯一约束及既有 clip 仍可由 worker按原路径处理。

- [ ] **Step 3: 实现 deadline-aware clipper 与 coordinator**

`LosslessClipper.inspect(..., deadline_monotonic)` 在进入每次 subprocess 前计算剩余秒数并取 `min(probe_timeout_seconds, remaining)`；剩余 <=0 立即抛固定 timeout。service 在 worker 内完成单 source path existence、fingerprint 和 probe；相同 fingerprint/range 共用 future。ready token TTL 120 秒且单次消费；accepted/running operation 不清理，terminal operation 过期后按固定 quantum 清理。shutdown 先停止 admission，再等待 running+queued inspection future 明确完成；不得用 cancel 假定已运行 FFprobe 停止。

I-106 在 succeeded 状态下以一个短数据库事务 claim token：首次 claim 保存 claim-key hash/expiry/token hash，并用持久 application secret 对 `operation_id|idempotency_key|claim_key|expiry` 做 HMAC 后编码明文 token；同 claimKey 在未过期、未消费前可确定性重取相同明文，其他 claimKey 返回 409。过期未消费的 claim 可由一次新 inspect attempt 重新签发；消费后不再返回 token。token/claimKey 必须从 request/response audit、异常和 access log 字段中过滤。

- [ ] **Step 4: 让 create 和 worker 复用同一轮结果**

`CreateClipRequest` 增加 `inspectionToken` 与客户端 UUID `idempotencyKey`，inspect request 也携带该 key。token 有效、绑定 key 一致且 fingerprint 未变时，在一个数据库事务中原子消费 token、按 idempotency key 插入 clip 并写入 inspection/fingerprint；第二个 idempotency key 即使持有明文 token 也必须拒绝。HTTP response 丢失后，重提相同 key 必须返回同一 clip，即使 token 已消费。token 缺失或 stale 时只提交/复用同一 coordinator inspection并返回 202，不在 create HTTP 内等待 probe。worker 加载持久 inspection：fingerprint 一致则直接使用 profile/keyframe 结果；变化才在单 source/30 秒预算内重验并更新。FFmpeg 产物验证仍独立执行，不计作源文件重复 probe。

- [ ] **Step 5: 前端轮询且旧 range 不能覆盖新选择**

service 将 200 ready、202 pending、503 busy 映射为显式 union。editor 为每次创建意图生成并保留一对 idempotency/claim key，用 `switchMap` 轮询 operation；用户更改 range 或关闭页面即取消客户端轮询，旧 operation 返回不得覆盖新 range，但服务端 running operation 保留到终态。I-106 succeeded response 领取 token 后立即 create；轮询响应丢失时复用同 claimKey，token stale/expired 时只重新 inspect 一次。create 必须发送当前 ready token 与同一 idempotency key。

将 inspection status route 作为 **I-106** 追加到 request audit；此时 route ledger 从基线 105 增至 106。route 返回 accepted/running/succeeded/failed、稳定 error code 与安全 result；仅 succeeded token exchange 可在 response body 返回 claim 对应的明文一次性 token，任何状态都不返回本地 path、probe stderr、持久 secret 或 token hash。

- [ ] **Step 6: 验证端到端预算与回归**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_database.py tests/bili_upload/test_highlight_cut.py tests/bili_upload/test_highlights.py tests/bili_upload/test_highlight_worker.py tests/web/test_highlights_routes.py -q
(cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/shared/highlight.service.spec.ts' --include='src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts' && npx eslint src/app/upload-tasks/shared/highlight.model.ts src/app/upload-tasks/shared/highlight.service.ts src/app/upload-tasks/shared/highlight.service.spec.ts src/app/upload-tasks/highlight-editor/highlight-editor.component.ts src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts)
```

Expected: PASS；active <=2、waiting <=8、恰好单 source、fake-clock absolute deadline <=30 秒、cold 202 链路可领取并消费 token、响应丢失可恢复、token 不可跨 idempotency key 重放、running operation 不被清理、同一未变化链路只 probe 一轮源文件；T150/C100 数值另写 benchmark。

- [ ] **Step 7: Commit**

```bash
git add src/blrec/bili_upload/migrations/0027_initial.sql src/blrec/bili_upload/database.py src/blrec/bili_upload/highlight_cut.py src/blrec/bili_upload/highlights.py src/blrec/bili_upload/highlight_worker.py src/blrec/web/routers/highlights.py tests/bili_upload/test_database.py tests/bili_upload/test_highlight_cut.py tests/bili_upload/test_highlights.py tests/bili_upload/test_highlight_worker.py tests/web/test_highlights_routes.py webapp/src/app/upload-tasks/shared/highlight.model.ts webapp/src/app/upload-tasks/shared/highlight.service.ts webapp/src/app/upload-tasks/shared/highlight.service.spec.ts webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.ts webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts docs/performance/request-audit.md
git commit -m "perf: bound and reuse highlight inspections"
```

### Task 6: WM-06 durable control journal 与 task desired-state reconciler

**覆盖：** I-011 的 start/stop/recorder actions，I-022--I-029。

**Files:**
- Create: `src/blrec/control/__init__.py`
- Create: `src/blrec/control/operations.py`
- Create: `src/blrec/task/control_reconciler.py`
- Create: `src/blrec/web/routers/control_operations.py`
- Modify: `src/blrec/setting/setting_manager.py`
- Modify: `src/blrec/application.py`
- Modify: `src/blrec/task/task_manager.py`
- Modify: `src/blrec/web/routers/tasks.py`
- Modify: `src/blrec/web/main.py`
- Create: `tests/control/test_operations.py`
- Create: `tests/task/test_control_reconciler.py`
- Create: `tests/test_application_task_controls.py`
- Modify: `tests/web/test_tasks_routes.py`
- Create: `webapp/src/app/core/services/control-operation.service.ts`
- Create: `webapp/src/app/core/services/control-operation.service.spec.ts`
- Modify: `webapp/src/app/tasks/shared/task.model.ts`
- Modify: `webapp/src/app/tasks/shared/services/task.service.ts`
- Modify: `webapp/src/app/tasks/shared/services/task.service.spec.ts`
- Modify: `webapp/src/app/tasks/shared/services/task-manager.service.ts`
- Modify: `webapp/src/app/tasks/shared/services/task-manager.service.spec.ts`
- Modify: `docs/performance/request-audit.md`

**接口：** 独立 `control.sqlite3` journal 只持久化 operation/step/generation/result，不执行领域行为。operation 状态为 `accepted/running/succeeded/failed`，step/逐项结果为 `queued/rejected/running/succeeded/failed` 并带稳定 `errorCode`。I-107 `GET /api/v1/control-operations/{operation_id}` 是统一控制状态读取；执行 owner 按 lane 隔离：本任务建立 `task-state` lane 并发 1，Task 7 增加 `room-membership` lane 1，Task 8 增加 `settings-apply` lane 1，inspection/deletion 继续由各自 domain worker 负责。

- [ ] **Step 1: 写 journal、58 项单 dump、crash recovery 与消费者契约失败测试**

创建 58 个 fake tasks；batch start/stop/recorder enable/disable 时统计一次 dump。用 barrier 阻塞 task lifecycle，断言 route 已返回 202 且没有等待 lifecycle；连续对同 room 发 start-stop-start，最终按最后 desired state 收敛。注入 `dump_settings()` 成功后、wake 前崩溃，重启后必须扫描全部 task 的 desired monitor/recorder 与实际状态差异并恢复。每个批量 operation 必须持久化 58 项各自的最终 `succeeded/failed + errorCode`。

Angular service 测试覆盖 202 admission 后轮询 I-107、逐项结果显示、组件销毁停止轮询，以及 terminal failed 不再轮询。单元测试使用 barrier/调用顺序，不以真实 C100/2 秒墙钟作为断言。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/control/test_operations.py tests/task/test_control_reconciler.py tests/test_application_task_controls.py tests/web/test_tasks_routes.py -k 'desired or operation or recovery or batch or noop or coalesce' -q
(cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/core/services/control-operation.service.spec.ts' --include='src/app/tasks/shared/services/task.service.spec.ts' --include='src/app/tasks/shared/services/task-manager.service.spec.ts')
```

Expected: FAIL，因为 batch route 当前逐项 await/dump，BackgroundTasks 不可恢复，且没有可查询的逐项终态。

- [ ] **Step 2: 实现通用 journal 与 I-107 status route**

SQLite 文件放在 settings 文件同目录，0600、DELETE journal、FULL synchronous、专用单线程 executor。operation 表含 id/lane/kind/target_key/attempt/generation/status/result_json/error_code/created_at/updated_at；step 表含 operation_id/key/generation/status/result_json/error_code。partial unique index只去重 accepted/running 的 `(lane,kind,target_key)`；terminal failed 重提创建新 attempt，绝不被旧 failed 永久挡住。只允许每 lane 100 个非终态；只清理 terminal 且过期记录。error/result 只保存稳定错误码与安全字段。

status route 只读 journal。将它作为 **I-107** 追加到 request audit；连同 Task 5 的 I-106，route ledger 必须从 105 变成 107。`main.py` 在 task load 前 open/recover；shutdown 先停止 HTTP admission，再依次停止所有 producer 和 lane worker，最后 drain/close journal executor，确保没有 worker 在 journal close 后回写。

- [ ] **Step 3: 一次持久化 desired state 并创建逐项 operation**

`change_task_desired_states` 在一个 async mutation lock 内验证全部 room，计算 `enable_monitor/enable_recorder` 的实际 diff，只修改变化项并调用一次 `dump_settings()`；no-op 零 dump。force 只影响本次 stop/disable 收敛，不写成长期设置。持久化成功后在一个 operation 中写入逐房间 queued/rejected admission；I-011 保留 `TaskBatchActionResponse.results`，每项含 `roomId/status/operationId/errorCode`，不能退化为笼统 `pendingRoomIds`。I-022--I-029 的单项/all 响应同样返回 operation ID 与逐项 admission。

- [ ] **Step 4: task-state lane 以 generation 收敛并可重启恢复**

reconciler 不维护不可恢复的内存真相；worker 从 journal/desired settings 取任务，单 worker 按 room coalesce。每个 lifecycle step 开始前读取实际 postcondition，已满足则直接恢复成功；执行前后以 durable generation CAS 更新，处理中 desired 再变化时完成当前安全边界后重读最新 revision。startup/load 完成后扫描所有 task 的 desired/actual 差异并补建或唤醒 operation；失败写逐项 terminal 状态而不是只写日志。Outbound 后续只能在**同一个 task-state owner** 内增加受控远端 handoff，不能在 route 另建 gather/worker。

- [ ] **Step 5: Angular 接受 202 并轮询终态**

`ControlOperationService` 以有限间隔短 GET 轮询 I-107；任务 service/manager 将 202 admission 映射成 typed union，保留逐项 queued/rejected，终态再刷新一次 task 状态。组件取消订阅只停止客户端轮询，不取消 durable operation；稳定 error code 显示为现有通知文案。旧 `background` body 字段继续接受但不再改变执行路径。I-011 的 `cut` 保持本地触发，`refresh` 仍归 Outbound。

- [ ] **Step 6: 验证业务语义、路由台账和生命周期**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/control/test_operations.py tests/task/test_control_reconciler.py tests/test_application_task_controls.py tests/task/test_task_manager_cleanup.py tests/web/test_tasks_routes.py -q
black --check src/blrec/control src/blrec/task/control_reconciler.py src/blrec/setting/setting_manager.py src/blrec/application.py src/blrec/task/task_manager.py src/blrec/web/routers/tasks.py src/blrec/web/routers/control_operations.py
flake8 src/blrec/control src/blrec/task/control_reconciler.py src/blrec/setting/setting_manager.py src/blrec/application.py src/blrec/task/task_manager.py src/blrec/web/routers/tasks.py src/blrec/web/routers/control_operations.py
mypy src/blrec/control src/blrec/task/control_reconciler.py src/blrec/setting/setting_manager.py src/blrec/application.py
(cd webapp && npx eslint src/app/core/services/control-operation.service.ts src/app/core/services/control-operation.service.spec.ts src/app/tasks/shared/task.model.ts src/app/tasks/shared/services/task.service.ts src/app/tasks/shared/services/task.service.spec.ts src/app/tasks/shared/services/task-manager.service.ts src/app/tasks/shared/services/task-manager.service.spec.ts)
test "$(rg -c '^\| I-[0-9]{3} \|' docs/performance/request-audit.md)" = 107
```

Expected: PASS；一次 desired dump、no-op 零 dump、逐项 admission/终态可查询、dump→crash→restart 可恢复、lane/shutdown 顺序确定；C100/58 项 2 秒另写 benchmark。

- [ ] **Step 7: Commit**

```bash
git add src/blrec/control src/blrec/task/control_reconciler.py src/blrec/setting/setting_manager.py src/blrec/application.py src/blrec/task/task_manager.py src/blrec/web/routers/tasks.py src/blrec/web/routers/control_operations.py src/blrec/web/main.py tests/control/test_operations.py tests/task/test_control_reconciler.py tests/test_application_task_controls.py tests/web/test_tasks_routes.py webapp/src/app/core/services/control-operation.service.ts webapp/src/app/core/services/control-operation.service.spec.ts webapp/src/app/tasks/shared/task.model.ts webapp/src/app/tasks/shared/services/task.service.ts webapp/src/app/tasks/shared/services/task.service.spec.ts webapp/src/app/tasks/shared/services/task-manager.service.ts webapp/src/app/tasks/shared/services/task-manager.service.spec.ts docs/performance/request-audit.md
git commit -m "perf: reconcile durable task control operations"
```

### Task 7: WM-07 task membership 与 browser collect operation

**覆盖：** I-011 delete、I-030--I-032、I-102。

**Files:**
- Modify: `src/blrec/control/operations.py`
- Modify: `src/blrec/application.py`
- Modify: `src/blrec/task/task_manager.py`
- Modify: `src/blrec/web/main.py`
- Modify: `src/blrec/web/routers/tasks.py`
- Modify: `src/blrec/web/routers/browser_extension.py`
- Modify: `tests/control/test_operations.py`
- Modify: `tests/task/test_task_manager_cleanup.py`
- Modify: `tests/web/test_tasks_routes.py`
- Modify: `tests/web/test_browser_extension_routes.py`
- Modify: `webapp/src/app/tasks/add-task-dialog/add-task-dialog.component.ts`
- Modify: `webapp/src/app/tasks/add-task-dialog/add-task-dialog.component.spec.ts`
- Modify: `webapp/src/app/tasks/task-item/task-item.component.ts`
- Modify: `webapp/src/app/tasks/task-item/task-item.component.spec.ts`
- Modify: `browser-extension/src/shared/api.ts`
- Modify: `browser-extension/src/content.ts`
- Modify: `browser-extension/tests/content.spec.ts`

**接口：** 所有入口返回 202。admission 至少含 `operationId/status/requestedRoomId`；I-102 终态 result 必须含 `resolvedRoomId/collected/upload` 与逐 step 结果。`room-membership` lane 本阶段并发 1；Outbound 只能在同一 owner 内升级为“不同 room 并发 2”，不能新增第二个执行 owner。

- [ ] **Step 1: 写 dedupe、retry attempt、observe-before-act 与调用方失败测试**

并发提交两个相同 running add/remove/collect，断言返回同一 ID；terminal failed 修复 policy 后重提必须创建新 attempt，并从已满足 postcondition/上一 attempt 成功 step 继续。分别在 resolve/add/desired-state/policy 外部或内存 side-effect 后、step CAS 前崩溃；重启后 add 已存在、remove 已不存在均按 postcondition 视为成功，不能重复执行。100 个非终态后第 101 个不得进入 lane 并返回 503。

Angular add/remove 与浏览器插件测试接受 202、显示“已提交”、轮询 I-107，并在终态使用 `resolvedRoomId/collected/upload`。插件取消页面只停止轮询，失败显示稳定 error code，不丢真实 room ID。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/control/test_operations.py tests/task/test_task_manager_cleanup.py tests/web/test_tasks_routes.py tests/web/test_browser_extension_routes.py -k 'operation or attempt or collect or membership or remove or recovery' -q
(cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/tasks/add-task-dialog/add-task-dialog.component.spec.ts' --include='src/app/tasks/task-item/task-item.component.spec.ts')
(cd browser-extension && npm test)
```

Expected: FAIL，因为 add/remove/collect 当前占用请求，调用方要求同步真实 room ID，且没有 generation/postcondition 恢复。

- [ ] **Step 2: 增加 room-membership lane 与 generation step**

add/remove/remove-all/collect 先提交 operation；HTTP 不等待 room normalize、retry、lifecycle 或文件 finalization。每个 step 保存 durable generation，执行前读取目标 postcondition，执行后用 generation CAS 提交；崩溃重启从首个未满足 step 继续。remove operation 在 task load 阶段即被视为 desired-absent；完成 teardown 后才一次移除 settings。add 成功后交给 Task 6 的 task-state lane 收敛 desired state。每个 logical membership operation 最多一次 settings dump。

terminal failed resubmit 明确创建新 attempt；新 attempt 读取当前 postcondition并复用已满足步骤，不返回旧 failed。startup 恢复 accepted/running；shutdown 在 journal 关闭前等待 membership worker 到安全 step 边界。Outbound Task 3 只能把 `reuse_info_revision` 的生产/消费和 room-disjoint concurrency=2 加入该 owner，HTTP route 不得另建 bounded gather 或逐房间 dump。

- [ ] **Step 3: browser collect 作为幂等组合 operation**

`collect_room` steps 固定为 resolve/add/desired-state/policy；每步成功即 CAS 提交。202 返回 requested room ID；终态 result 保存 resolved room ID、collected/upload 和每步结果。category/room 远端调用仍使用现有 cadence/retry，本任务不增加请求。policy 失败保留 partial steps；修复后新 attempt 跳过已满足 add，只重做尚未满足的 policy。

- [ ] **Step 4: 迁移 Angular 与插件到 202+poll**

Angular add/remove 复用 Task 6 `ControlOperationService`，先结束按钮 loading 并显示已提交，终态后刷新列表。浏览器插件的 `CollectResult` 改为 admission/terminal union，保存 operation ID 并短轮询 I-107；终态才以 `resolvedRoomId/collected/upload` 判断结果。轮询具有固定总期限和可取消订阅，不使用长连接，也不要求用户重新点击。

- [ ] **Step 5: 验证恢复、消费者与 owner 单一性**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/control/test_operations.py tests/task/test_task_manager_cleanup.py tests/web/test_tasks_routes.py tests/web/test_browser_extension_routes.py -q
black --check src/blrec/control/operations.py src/blrec/application.py src/blrec/task/task_manager.py src/blrec/web/routers/tasks.py src/blrec/web/routers/browser_extension.py
flake8 src/blrec/control/operations.py src/blrec/application.py src/blrec/task/task_manager.py src/blrec/web/routers/tasks.py src/blrec/web/routers/browser_extension.py
mypy src/blrec/control/operations.py src/blrec/web/routers/browser_extension.py
(cd webapp && npx eslint src/app/tasks/add-task-dialog/add-task-dialog.component.ts src/app/tasks/add-task-dialog/add-task-dialog.component.spec.ts src/app/tasks/task-item/task-item.component.ts src/app/tasks/task-item/task-item.component.spec.ts)
(cd browser-extension && npm test)
```

Expected: PASS；running 去重、failed 新 attempt、逐 step generation/postcondition 恢复、Angular/插件均完成 202+poll，membership 只有一个 owner；C100 数值另写 benchmark。

- [ ] **Step 6: Commit**

```bash
git add src/blrec/control/operations.py src/blrec/application.py src/blrec/task/task_manager.py src/blrec/web/main.py src/blrec/web/routers/tasks.py src/blrec/web/routers/browser_extension.py tests/control/test_operations.py tests/task/test_task_manager_cleanup.py tests/web/test_tasks_routes.py tests/web/test_browser_extension_routes.py webapp/src/app/tasks/add-task-dialog/add-task-dialog.component.ts webapp/src/app/tasks/add-task-dialog/add-task-dialog.component.spec.ts webapp/src/app/tasks/task-item/task-item.component.ts webapp/src/app/tasks/task-item/task-item.component.spec.ts browser-extension/src/shared/api.ts browser-extension/src/content.ts browser-extension/tests/content.spec.ts
git commit -m "perf: persist room membership operations"
```

### Task 8: WM-08 原子设置写入与后台 apply

**覆盖：** I-034、I-036、I-039、I-041。

**Files:**
- Create: `src/blrec/setting/file_work.py`
- Modify: `src/blrec/setting/models.py`
- Modify: `src/blrec/setting/setting_manager.py`
- Modify: `src/blrec/control/operations.py`
- Modify: `src/blrec/application.py`
- Modify: `src/blrec/web/routers/settings.py`
- Modify: `src/blrec/web/routers/application.py`
- Modify: `src/blrec/web/routers/validation.py`
- Modify: `src/blrec/web/main.py`
- Create: `tests/setting/test_file_work.py`
- Create: `tests/setting/test_settings_persistence.py`
- Modify: `tests/test_application_live_status.py`
- Modify: `tests/web/test_settings_routes.py`
- Create: `tests/web/test_application_routes.py`
- Create: `tests/web/test_validation_routes.py`

**接口：** `SettingsFileWorkCoordinator` 提供 2 个活动、8 个等待 job；一个 admitted unit 完成 validate/serialize/temp write/file fsync/replace/directory fsync/cleanup 全生命周期。PATCH 先一次原子持久化 desired settings，再把 section 的 `desired_revision` 提交到 Task 6 `settings-apply` lane；worker 只有追到 `applied_revision == desired_revision` 才把 operation 标为 succeeded。响应正文保持原 Settings/TaskOptions，若有 apply operation 则加 `X-BLREC-Operation-ID`。

- [ ] **Step 1: 写 request-model FS、原子失败与慢 apply 的失败测试**

patch `os.makedirs/isdir/access` 为阻塞 fake，构造并提交 `SettingsIn(output=...)`/`TaskOptions`，证明 Pydantic request parse 不应触发 FS。并发两个 PATCH，断言文件始终能完整 `Settings.load()`；在 flush 后、replace 前及 replace 后、directory fsync 前注入崩溃，验证旧/新文件与 revision 恢复。阻塞 header reconnect、live monitor reconfigure 和 `Application.restart()`，用 barrier 证明 route 返回 operation header 后 apply 仍可继续。

再覆盖 apply 正在运行时同 section 第二次 PATCH：第二次只提升 `desired_revision`，同 lane worker 完成第一版后必须重读并应用第二版，不能提前 succeeded。启动时发现 `desired_revision > applied_revision` 必须补 apply。填满 2+8 admission 后下一项不得进入 executor，shutdown 必须明确 drain 全部 running+queued future。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/setting/test_file_work.py tests/setting/test_settings_persistence.py tests/control/test_operations.py tests/test_application_live_status.py tests/web/test_settings_routes.py tests/web/test_application_routes.py tests/web/test_validation_routes.py -k 'directory or atomic or revision or apply or restart or concurrent or shutdown' -q
```

Expected: FAIL，因为 model validator 直接访问 FS，dump truncate 目标文件且无 mutation lock，apply/restart 仍在请求内 await。

- [ ] **Step 2: 从 request model 移除真实 FS 探测**

`OutputSettings._validate_dir`/`LoggingSettings._validate_dir` 只做字符串规范化，不执行 `isdir/access`；默认 factory 仅在应用初始化创建默认目录。提取同步 `validate_directory_sync()`：`main.py` 在事件循环启动前用它校验 `Settings.load()` 的目录，`SettingsManager.change_*` 和 `validation/dir` 则通过 `SettingsFileWorkCoordinator` 调用同一函数，并保持 ENOTDIR/EACCES/0 response code。coordinator saturation 映射固定 503 + `Retry-After: 1`。

- [ ] **Step 3: copy-on-write + temp/fsync/replace**

async mutation lock 内以 deep copy 计算 diff并确定下一 revision；等待 file worker 时 event loop 仍可推进，但下一 mutation 不得越过当前版本顺序。worker unit 在目标同目录创建 0600 temp、序列化并写 TOML、flush、`os.fsync(file)`、`os.replace`、支持的平台 fsync directory，任何失败都在 worker 内 cleanup。持久化成功后才把 candidate section 写回 live settings并提交 desired revision；失败保留旧内存与可完整加载的旧/新原子文件。一个 PATCH 恰好一次 dump，no-op 零 dump；global/task PATCH 共用同一 lock。

- [ ] **Step 4: apply/restart 进入 section-keyed operation**

持久化完成后，header reconnect、live-monitor reconfigure、mode restart、task header restart 提交 Task 6 journal；target key 为 `settings:<section>` 或 `task-settings:<room>:<section>`。journal 为每个 key 保存单调 `desired_revision/applied_revision`；同 key 已 running 时更新 desired revision并返回同一非终态 operation。worker 每次 apply 后以 generation CAS 写 applied revision，然后重读，直到与 desired 相等才 succeeded。失败只更新当前 attempt，不把 desired settings 回滚；startup 扫描所有 revision gap 并恢复。`POST /app/restart` 使用 `application:restart` key 同样遵循 revision 追赶并返回 202。`main.py` 将 `X-BLREC-Operation-ID` 加入 CORS exposed headers。

- [ ] **Step 5: 完成 coordinator 启停并验证恢复**

`main.py` startup 在接收请求前创建 coordinator、打开 journal、恢复 revision gap；shutdown 先停止 settings mutation admission，再停止 settings-apply lane，随后 drain 所有已 admitted running+queued file future，最后关闭 executor/journal。不能仅 cancel asyncio wrapper，也不能让 cleanup 在 executor 关闭后丢失。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/setting/test_file_work.py tests/setting/test_settings_persistence.py tests/control/test_operations.py tests/test_application_live_status.py tests/web/test_settings_routes.py tests/web/test_application_routes.py tests/web/test_validation_routes.py -q
black --check src/blrec/setting/file_work.py src/blrec/setting/models.py src/blrec/setting/setting_manager.py src/blrec/control/operations.py src/blrec/application.py src/blrec/web/routers/settings.py src/blrec/web/routers/application.py src/blrec/web/routers/validation.py src/blrec/web/main.py
flake8 src/blrec/setting/file_work.py src/blrec/setting/models.py src/blrec/setting/setting_manager.py src/blrec/control/operations.py src/blrec/application.py src/blrec/web/routers/settings.py src/blrec/web/routers/application.py src/blrec/web/routers/validation.py src/blrec/web/main.py
mypy src/blrec/setting/file_work.py src/blrec/setting/models.py src/blrec/setting/setting_manager.py src/blrec/web/routers/settings.py
```

Expected: PASS；全部 FS 生命周期 off-loop、每 PATCH 一次 atomic dump、同 section revision 不丢、replace crash 与 startup gap 可恢复、shutdown 无遗留 file future；D100/C100/heartbeat 数值另写 benchmark。

- [ ] **Step 6: Commit**

```bash
git add src/blrec/setting/file_work.py src/blrec/setting/models.py src/blrec/setting/setting_manager.py src/blrec/control/operations.py src/blrec/application.py src/blrec/web/routers/settings.py src/blrec/web/routers/application.py src/blrec/web/routers/validation.py src/blrec/web/main.py tests/setting/test_file_work.py tests/setting/test_settings_persistence.py tests/control/test_operations.py tests/test_application_live_status.py tests/web/test_settings_routes.py tests/web/test_application_routes.py tests/web/test_validation_routes.py
git commit -m "perf: persist settings before background apply"
```

### Task 9: WM-09 上传/场次单事务批处理

**覆盖：** I-065/I-068 的非删除分支、I-069；删除分支使用 Task 3。

**Files:**
- Create: `src/blrec/bili_upload/migrations/0029_initial.sql`
- Modify: `src/blrec/bili_upload/database.py`
- Modify: `src/blrec/bili_upload/task_actions.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/control/operations.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `tests/bili_upload/test_task_actions.py`
- Modify: `tests/bili_upload/test_account_runtime.py`
- Modify: `tests/bili_upload/test_database.py`
- Modify: `tests/control/test_operations.py`
- Modify: `tests/web/test_recording_sessions_routes.py`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.model.ts`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.service.ts`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.service.spec.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`

**接口：** 显式 `run_job_batch/run_session_batch` 在一次 `database.write` 内处理最多 100 项并保留原有逐项 `accepted/message`。I-069 “重试全部失败任务”改为 durable `upload-retry` operation：admission 事务用 set-based membership 快照冻结提交瞬间的全部 eligible job ID，后台每 quantum 消费最多 100 个 frozen item；页面关闭不影响继续执行，状态通过 I-107 查询。

- [ ] **Step 1: 写 database-call、partial、101 rows 与 worker 生命周期失败测试**

种 58 项混合 valid/rejected jobs，统计显式 batch 的 `database.write` 恰好一次；中间一项触发 fence，断言该 SAVEPOINT rollback 而前后项成功。分别种 101/201 个 retryable failed jobs，提交 I-069 后执行 2/3 个 durable quantum，断言最终全部处理且每 quantum 一次 transaction/一次 wake。operation 创建后，让一个**未入选的旧低 ID** 新变为 failed，同时让一个 frozen item 改成不再 eligible；前者不得进入本 operation，后者必须得到稳定 rejected 终态，证明集合不漂移。第一个 quantum 后关闭前端订阅并重建 runtime，operation 必须从持久 membership 继续。unknown outcome/lease fence 逐项记 rejected，不盲重试。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_task_actions.py tests/bili_upload/test_account_runtime.py tests/web/test_recording_sessions_routes.py -k 'batch or retryable or savepoint or wakeup' -q
```

Expected: FAIL，因为 route 当前逐项调用 manager/runner，retry-all 无持久 operation/quantum，每项各有 transaction/worker 生命周期。

- [ ] **Step 2: 把单项状态机提取为 connection-scoped helper**

将 pause/resume/retry/repair/skip/repost/set-intent 的校验和写入提取为接收 `sqlite3.Connection` 的私有 helper；单项 public API 仍以一次 `database.write` 调用该 helper。batch 在一个 outer write 内对每个 ID 执行 `SAVEPOINT item_<ordinal>`，捕获 `UploadTaskActionRejected` 后 rollback/release 该项并继续。不得用宽 UPDATE 绕过 unknown-outcome、active lease、审核状态或 ownership fence。

- [ ] **Step 3: 持久化 retry-all 快照并以 100 项 quantum 跑完**

Migration 29 新增 `upload_retry_batches(operation_id,state,total_items,created_at,updated_at)` 与 `upload_retry_batch_items(operation_id,job_id,state,error_code)`，并建立 `(operation_id,state,job_id)` 索引。Migration 28 已由转码修复恢复状态占用。route 先生成 operation ID，在**同一个 upload DB transaction** 写 batch，并用一次 set-based INSERT 冻结提交瞬间的完整 membership：

```sql
INSERT INTO upload_retry_batch_items(operation_id,job_id,state,error_code)
SELECT ?,job.id,'queued',NULL
FROM upload_jobs job
JOIN bili_accounts account ON account.id=job.account_id
WHERE job.state='paused'
  AND account.state='active'
  AND job.operator_paused=0
  AND job.submit_state NOT IN ('in_flight','unknown_outcome')
  AND job.repair_state NOT IN ('queued','checking','reuploading','editing')
  AND NOT EXISTS(
    SELECT 1 FROM upload_parts part
    WHERE part.job_id=job.id
      AND part.upload_state IN ('completing','unknown_outcome')
  );
```

`total_items` 取本次 insert 数量。不得用 `max_job_id` 或 worker 时动态 predicate 代替快照。随后提交 Task 6 journal；若进程在两库提交之间崩溃，startup scanner 将 orphan accepted batch 补入 journal，相同未完成 batch 的重复请求返回同一 operation ID。

worker 每次在一个 write 中只读取该 operation 的 `state='queued' ORDER BY job_id LIMIT 100` membership，用 SAVEPOINT 逐项重新校验安全 fence并持久化 succeeded/rejected，再在 transaction 后至多 wake 一次。提交后新增的失败任务不在 membership 中；frozen job 后续状态改变则得到 `state_changed` 或更具体稳定 rejected code。crash 后从 durable items 继续，terminal item 不会再次被选；直到无 queued item 才将 batch/journal operation terminal。显式 batch 继续保持 1--100 与唯一 ID validator；delete action 只写 Task 3 requested state。

- [ ] **Step 4: 前端轮询累计进度并验证契约**

`retryFailedJobs()` 接受 202 operation admission；组件复用 `ControlOperationService` 显示 processed/total/succeeded/rejected，关闭页面只停止轮询，重新打开可按当前 operation ID 恢复。终态刷新上传任务列表。测试覆盖 101/201 条、中途页面关闭与 runtime 重启。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_database.py tests/bili_upload/test_task_actions.py tests/bili_upload/test_account_runtime.py tests/control/test_operations.py tests/web/test_recording_sessions_routes.py -q
(cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/shared/recording-session.service.spec.ts' --include='src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts')
black --check src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/recording_sessions.py
flake8 src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/recording_sessions.py
mypy src/blrec/bili_upload/task_actions.py src/blrec/web/routers/recording_sessions.py
```

Expected: PASS；显式 request/response <=100、retry-all 对提交时全部失败记录逐 quantum 处理、每 quantum 一次 write/commit 和至多一次 wake、101/201 与重启恢复通过、partial result 与所有 side-effect fence 不变；58 项 2 秒另写 benchmark。

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/migrations/0029_initial.sql src/blrec/bili_upload/database.py src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/runtime.py src/blrec/control/operations.py src/blrec/web/routers/recording_sessions.py tests/bili_upload/test_database.py tests/bili_upload/test_task_actions.py tests/bili_upload/test_account_runtime.py tests/control/test_operations.py tests/web/test_recording_sessions_routes.py webapp/src/app/upload-tasks/shared/recording-session.model.ts webapp/src/app/upload-tasks/shared/recording-session.service.ts webapp/src/app/upload-tasks/shared/recording-session.service.spec.ts webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts
git commit -m "perf: batch upload task mutations"
```

### Task 10: WM-10 统一完成媒体 response、Range 与条件缓存

**覆盖：** I-072、I-095、I-096；活动快照继续使用 Task 4。

**Files:**
- Create: `src/blrec/web/media_response.py`
- Modify: `src/blrec/bili_upload/recording_content.py`
- Modify: `src/blrec/bili_upload/highlights.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `src/blrec/web/routers/highlights.py`
- Modify: `src/blrec/web/main.py`
- Modify: `tests/bili_upload/test_highlights.py`
- Modify: `tests/bili_upload/test_recording_content.py`
- Create: `tests/web/test_media_response.py`
- Modify: `tests/web/test_recording_sessions_routes.py`
- Modify: `tests/web/test_highlights_routes.py`
- Modify: `tests/web/test_request_performance_middleware.py`

**接口：** `open_media_resource()` 在 worker 中完成 realpath/open/fstat 一次并返回 `OpenedMediaResource`；`build_media_response(request, resource, range_header, if_none_match, if_range, download_name)` 统一 200/206/304/416、close 与流指标。

- [ ] **Step 1: 写 open/stat thread、条件请求和断开关闭失败测试**

对 completed recording/ready clip 分别覆盖完整 GET、prefix/suffix Range、非法多 range、416、匹配/不匹配 `If-None-Match`、匹配/不匹配 `If-Range`。patch `open/Path.stat/os.stat` 记录 event-loop thread 并断言请求路径为零调用。客户端读取首 chunk 后断开，文件必须 close 且 metric 只含 route/status/first_byte_ms/bytes/range/reason，不含 path/token。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/web/test_media_response.py tests/web/test_recording_sessions_routes.py tests/web/test_highlights_routes.py tests/bili_upload/test_highlights.py tests/web/test_request_performance_middleware.py -k 'media or range or etag or first_byte or disconnect' -q
```

Expected: FAIL，因为两个 route 在响应前同步 open/stat，clip 先 full hydrate 后重复 stat，完成媒体仍统一 no-store 且无 ETag/If-Range。

- [ ] **Step 2: 增加 lightweight clip media resource**

`HighlightService.clip_media_resource(clip_id)` 只查询 clip id/state/name/output path/persisted size，不加载 sources、upload progress 或完整 projection，也不自行 stat。仅 `state='ready'` 可返回。`RecordingContentReader.media_descriptor(part_id)` 只查询 final/source 候选、artifact state、content hints 与远端状态，不做 stat；stream route 把候选交给 worker opener，按既有 final-first/source-fallback 顺序只 open/fstat 被选中的 regular file。I-071 仍使用 Task 4 的 `media()` access 路径。

- [ ] **Step 3: worker open/fstat 一次并生成 validator**

worker 以 `O_RDONLY` 打开并对 fd `fstat`，校验 regular file 与预期 root/identity。完成 artifact 的 strong ETag 是不可变 artifact key、`st_dev/st_ino/st_size/st_mtime_ns` 的 SHA-256 quoted digest；header 不得暴露 inode/device/path 原值。文件由不同 inode/revision 替换时产生新 ETag。活动 recording snapshot 不生成 ETag。304 在 close fd 后返回零 body，并保留 `ETag` 与 `Cache-Control`。

- [ ] **Step 4: 统一 Range/cache 与首字节 metrics**

helper 复用现有 `parse_byte_range` 语义并按 HTTP precedence 执行：先判断 `If-None-Match`，匹配直接 304 且忽略 Range；否则只有 strong `If-Range` 匹配才 206，不匹配则忽略 Range 回完整 200。completed recording/clip 返回 `Cache-Control: private, max-age=3600`；active snapshot 固定 `no-store` 且绝不 304。stream wrapper 在第一次 yield 记 first-byte，在正常结束/取消/异常 finally close fd 并发一条 `media_stream` audit。download 仍以 clip name 生成 UTF-8 `Content-Disposition`。

- [ ] **Step 5: 验证 T150、Range 回归和 CORS headers**

`main.py` 的 CORS `expose_headers` 增加 `ETag`、`Cache-Control`、`Content-Disposition`。Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/web/test_media_response.py tests/web/test_recording_sessions_routes.py tests/web/test_highlights_routes.py tests/bili_upload/test_recording_content.py tests/bili_upload/test_highlights.py tests/web/test_request_performance_middleware.py -q
black --check src/blrec/web/media_response.py src/blrec/bili_upload/recording_content.py src/blrec/bili_upload/highlights.py src/blrec/web/routers/recording_sessions.py src/blrec/web/routers/highlights.py
flake8 src/blrec/web/media_response.py src/blrec/bili_upload/recording_content.py src/blrec/bili_upload/highlights.py src/blrec/web/routers/recording_sessions.py src/blrec/web/routers/highlights.py
mypy src/blrec/web/media_response.py src/blrec/bili_upload/recording_content.py src/blrec/bili_upload/highlights.py
```

Expected: PASS；每个 clip access 一次 lightweight DB read 与一次 worker open/fstat、304 零 body且保留 validator/cache headers、ETag 不泄漏文件 identity、Range/token/download 全部回归，活动媒体永不 cache；T150 数值另写 benchmark。

- [ ] **Step 6: Commit**

```bash
git add src/blrec/web/media_response.py src/blrec/bili_upload/recording_content.py src/blrec/bili_upload/highlights.py src/blrec/web/routers/recording_sessions.py src/blrec/web/routers/highlights.py src/blrec/web/main.py tests/bili_upload/test_recording_content.py tests/bili_upload/test_highlights.py tests/web/test_media_response.py tests/web/test_recording_sessions_routes.py tests/web/test_highlights_routes.py tests/web/test_request_performance_middleware.py
git commit -m "perf: unify conditional media responses"
```

### Task 11: WM-11 有界顺序弹幕 cursor

**覆盖：** I-073。

**Files:**
- Modify: `src/blrec/bili_upload/recording_content.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `tests/bili_upload/test_recording_content.py`
- Modify: `tests/web/test_recording_sessions_routes.py`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.service.ts`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.service.spec.ts`
- Modify: `webapp/src/app/upload-tasks/part-video-dialog/part-video-dialog.component.ts`
- Modify: `webapp/src/app/upload-tasks/part-video-dialog/part-video-dialog.component.spec.ts`

**接口：** 保留 integer cursor，但只允许 `cursor=0` 创建新 stream，或消费当前 `(part_id,st_dev,st_ino)` handle 的准确 next cursor。相同 inode 的 append-only 增长继续读取；只有 inode 替换、size 收缩或错误 cursor 才抛 `RecordingContentCursorStale` 并映射 409。每 handle 一个串行锁。

- [ ] **Step 1: 写 cursor=100,000、第三文件 eviction 与文件变化测试**

instrument `_iter_danmaku` 的 `next()` 次数。直接请求 cursor=100,000 必须在固定步数内 409，不能推进十万次。顺序读两页内容/next cursor 必须正确；第一页后向同 inode XML append 新 `<d>`，第二页继续得到新增内容。size 收缩或 `os.replace` 到新 inode 才 409，普通 mtime/size 增长不得 stale。两个并发消费者必须由 handle lock 串行且不重复 index。打开第三个文件驱逐第一个后，后端返回 409；前端自动从 cursor 0 重建、按 `DanmakuLine.index` 去重，并恢复活动弹幕与滚动位置。删除/关闭时 reader 必须 close。保留 DOCTYPE/XXE、invalid XML、limit 1/500 和 `limit+1` 测试。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_recording_content.py tests/web/test_recording_sessions_routes.py -k 'danmaku or cursor' -q
```

Expected: FAIL，因为 cache miss 当前执行 `for range(cursor): next(iterator)`，工作量随任意 cursor 线性增加。

- [ ] **Step 2: 只允许顺序 continuation 并显式限制 cache**

stream identity 使用 `(part_id,st_dev,st_ino)`；handle 保存 fd、增量安全 XML pull reader、observed_size、exact next cursor、last-access monotonic、一个 pending item和串行锁。每页先对 fd `fstat`：size >= observed_size 视作 append-only，只把新增 bytes feed 给同一个 reader，mtime 变化不失效；活动文件暂时 EOF/未闭合根节点视为“等待追加”，只有 finalized 文件才在 EOF 调 parser close 做完整校验。size 收缩或路径当前 inode 与 fd 不同才 stale。`cursor=0` 关闭旧 handle 后从头开始；`cursor>0` 只有与 handle.next_cursor 完全一致才读取，否则立即 stale。cache 最多 2 个 handle、TTL 10 分钟、pending 文本合计 256 KiB；驱逐当前未加锁的最旧 handle。所有 reader/fd 在 eviction/error/reader.close 时 close；DOCTYPE/ENTITY 前缀和 no-network/entity-disabled 约束保持不变。

- [ ] **Step 3: route 映射和 D100/heartbeat 验证**

新增固定 409 detail `弹幕分页状态已失效，请从第一页重新加载`，不回显 path/cursor。`PartVideoDialogComponent` 捕获该特定 409 后最多自动恢复一次：从 cursor 0 重新分页到当前播放位置，按 index 覆盖/去重，恢复 `activeDanmakuIndex`、follow 状态与滚动锚点；内部 eviction 不显示用户错误。Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_recording_content.py tests/web/test_recording_sessions_routes.py -q
black --check src/blrec/bili_upload/recording_content.py src/blrec/web/routers/recording_sessions.py tests/bili_upload/test_recording_content.py
flake8 src/blrec/bili_upload/recording_content.py src/blrec/web/routers/recording_sessions.py
mypy src/blrec/bili_upload/recording_content.py
(cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/shared/recording-session.service.spec.ts' --include='src/app/upload-tasks/part-video-dialog/part-video-dialog.component.spec.ts' && npx eslint src/app/upload-tasks/shared/recording-session.service.ts src/app/upload-tasks/shared/recording-session.service.spec.ts src/app/upload-tasks/part-video-dialog/part-video-dialog.component.ts src/app/upload-tasks/part-video-dialog/part-video-dialog.component.spec.ts)
```

Expected: PASS；任一 page parser work 有固定上界、同 inode append-only 连续、并发串行、第三文件 eviction 前端自动恢复、limit <=500、安全 parser 与顺序内容均不回归；D100/heartbeat 数值另写 benchmark。

- [ ] **Step 4: Commit**

```bash
git add src/blrec/bili_upload/recording_content.py src/blrec/web/routers/recording_sessions.py tests/bili_upload/test_recording_content.py tests/web/test_recording_sessions_routes.py webapp/src/app/upload-tasks/shared/recording-session.service.ts webapp/src/app/upload-tasks/shared/recording-session.service.spec.ts webapp/src/app/upload-tasks/part-video-dialog/part-video-dialog.component.ts webapp/src/app/upload-tasks/part-video-dialog/part-video-dialog.component.spec.ts
git commit -m "perf: bound sequential danmaku pagination"
```

### Task 12: WM-12 有界封面 validation/hash/store/cleanup

**覆盖：** I-081。

**Files:**
- Modify: `src/blrec/bili_upload/covers.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/web/routers/upload_covers.py`
- Modify: `tests/bili_upload/test_covers.py`
- Modify: `tests/web/test_upload_covers_routes.py`

**接口：** `CoverWorkCoordinator` 提供 2 个活动 job、8 个等待 job；满载抛 `CoverWorkSaturated(retry_after=1)`。PNG/JPEG 格式与尺寸校验、SHA-256、content-addressed store、DB metadata commit/复用和失败 cleanup 按 digest 单飞。

- [ ] **Step 1: 写 heartbeat、overload、同内容和孤儿恢复失败测试**

分别阻塞 PNG/JPEG scan/hash/store，发 11 个请求并跑 heartbeat：第 11 个不得进入 executor并返回 503，active<=2、waiting<=8。并发相同内容只执行一次 digest-keyed 文件+DB commit 链。注入文件已写后 DB insert 失败，同时让第二个同 digest consumer 等待，断言 cleanup 不会删除第二个将复用的文件；模拟崩溃留下同 hash 文件后重试应验证并完成 metadata；在 hash 路径预置不同内容时不得覆盖。shutdown 时 2 running+8 queued 全部得到明确完成/拒绝结果。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_covers.py tests/web/test_upload_covers_routes.py -k 'worker or heartbeat or overload or orphan or duplicate' -q
```

Expected: FAIL，因为 image scan/hash/cleanup 当前在 event loop，工作 admission 无上限，digest 锁未覆盖 DB commit/cleanup，且 PNG/JPEG/shutdown 回归未完整覆盖。

- [ ] **Step 2: 实现 2+8 coordinator 和 digest-keyed store**

coordinator 在提交 executor 前以锁保护 `active+waiting<=10`，不使用无界默认 executor queue。worker 保留现有 PNG+JPEG markers、尺寸和格式校验并计算 SHA-256；取得 digest 后按 digest single-flight 完成 path 校验、`_store_file`、DB metadata insert/复用。已存在文件必须重新计算/比较内容 hash；一致则复用，不一致抛固定 `InvalidCover` 且不覆盖。DB 调用仍通过原有单线程 executor，但 digest single-flight 直到 DB transaction 明确提交后才释放。

- [ ] **Step 3: 定义 DB 失败与 shutdown 行为**

若本次创建文件但 DB insert 明确失败，只能在同一 digest 锁内确认 DB 无引用且无在途 consumer 后 unlink；若进程在两者之间崩溃，下次同 digest add 复用并补 metadata。runtime close 停止接收新 job，追踪并 drain 全部已 admission 的 2 running+8 queued future，再关闭 executor；或在执行前以固定取消结果完成 queued，不能只等待两个 active 后遗留 waiter。route 保留流式读取超过 2 MiB 立即 413；worker saturation 映射 503 + `Retry-After: 1`，不记作 invalid image。

- [ ] **Step 4: 验证限制、幂等与格式检查**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_covers.py tests/web/test_upload_covers_routes.py tests/bili_upload/test_account_runtime.py -q
black --check src/blrec/bili_upload/covers.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/upload_covers.py tests/bili_upload/test_covers.py tests/web/test_upload_covers_routes.py
flake8 src/blrec/bili_upload/covers.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/upload_covers.py
mypy src/blrec/bili_upload/covers.py src/blrec/web/routers/upload_covers.py
```

Expected: PASS；PNG/JPEG 与 payload <=2 MiB 回归、active<=2、waiting<=8、digest 单飞覆盖 DB commit、失败 cleanup 不误删复用文件、shutdown 无遗留 future；C100/heartbeat 数值另写 benchmark。

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/covers.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/upload_covers.py tests/bili_upload/test_covers.py tests/web/test_upload_covers_routes.py
git commit -m "perf: bound upload cover file work"
```

## 最终整体验证与审计回填

以下步骤是 12 个任务后的 completion gate，不创建第 13 个实现任务。

- [ ] **逐 ID 核对 36/36 coverage**

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import re
from pathlib import Path
from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

from blrec.web.main import api

frozen = set('''
I-002 I-003 I-006 I-009 I-011 I-022 I-023 I-024 I-025 I-026 I-027
I-028 I-029 I-030 I-031 I-032 I-034 I-036 I-039 I-041 I-043 I-044
I-055 I-065 I-068 I-069 I-071 I-072 I-073 I-081 I-089 I-090 I-095
I-096 I-097 I-102
'''.split())
assert len(frozen) == 36, sorted(frozen)

ledger_text = Path('docs/performance/request-audit.md').read_text()
plan_text = Path(
    'docs/superpowers/plans/2026-07-20-write-media-request-performance.md'
).read_text()
coverage_table = plan_text.split('## 36/36 请求覆盖', 1)[1].split(
    '表中去重后', 1
)[0]
covered = set(re.findall(r'I-\d{3}', coverage_table))
assert covered == frozen, (sorted(frozen - covered), sorted(covered - frozen))

ledger_entries = [
    (match.group(1), match.group(2), match.group(3))
    for line in ledger_text.splitlines()
    for match in [re.match(r'\| (I-\d{3}) \| ([A-Z]+) \| `([^`]+)` \|', line)]
    if match
]
ledger_ids = [entry[0] for entry in ledger_entries]
assert len(ledger_ids) == len(set(ledger_ids)) == 107, ledger_ids
ledger_by_id = {item_id: (method, path) for item_id, method, path in ledger_entries}
ledger_routes = {(method, path) for _item_id, method, path in ledger_entries}
actual_routes = set()
for route in api.routes:
    if isinstance(route, APIRoute) and route.path.startswith('/api/'):
        actual_routes.update(
            (method, route.path)
            for method in route.methods
            if method not in {'HEAD', 'OPTIONS'}
        )
    elif isinstance(route, WebSocketRoute) and route.path.startswith('/ws/'):
        actual_routes.add(('WS', route.path))

assert len(ledger_routes) == 107, len(ledger_routes)
assert len(actual_routes) == 107, len(actual_routes)
assert ledger_routes == actual_routes, (
    sorted(actual_routes - ledger_routes),
    sorted(ledger_routes - actual_routes),
)
assert ledger_by_id['I-106'] == (
    'GET',
    '/api/v1/highlights/inspections/{operation_id}',
), ledger_by_id['I-106']
assert ledger_by_id['I-107'] == (
    'GET',
    '/api/v1/control-operations/{operation_id}',
), ledger_by_id['I-107']
assert '**唯一整项无需修改**' in plan_text and 'I-055' in plan_text
print('Write/media coverage: 36/36; route ledger: 107/107')
PY
```

- [ ] **运行后端聚焦回归与事件循环 harness**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/web/test_auth_store.py tests/web/test_auth_routes.py \
  tests/web/test_websockets_auth.py tests/web/test_websockets_streams.py \
  tests/control/test_operations.py tests/task/test_control_reconciler.py \
  tests/test_application_task_controls.py tests/task/test_task_manager_cleanup.py \
  tests/web/test_tasks_routes.py tests/web/test_settings_routes.py \
  tests/web/test_application_routes.py tests/web/test_validation_routes.py \
  tests/bili_upload/test_database.py tests/bili_upload/test_deletion_worker.py \
  tests/bili_upload/test_task_actions.py tests/bili_upload/test_account_runtime.py \
  tests/bili_upload/test_media_index.py tests/bili_upload/test_upos.py \
  tests/bili_upload/test_collection_publish.py \
  tests/bili_upload/test_recording_content.py tests/bili_upload/test_active_media.py \
  tests/bili_upload/test_highlight_cut.py tests/bili_upload/test_highlights.py \
  tests/bili_upload/test_highlight_worker.py tests/bili_upload/test_covers.py \
  tests/web/test_recording_sessions_routes.py tests/web/test_highlights_routes.py \
  tests/web/test_media_response.py tests/web/test_upload_covers_routes.py \
  tests/web/test_browser_extension_routes.py tests/web/test_request_performance_middleware.py
(cd browser-extension && npm test && npm run typecheck && npm run build)
```

- [ ] **运行前端聚焦测试、lint 与生产构建**

```bash
(cd webapp && npm test -- --watch=false --browsers=ChromeHeadless \
  --include='src/app/core/services/control-operation.service.spec.ts' \
  --include='src/app/tasks/shared/services/task.service.spec.ts' \
  --include='src/app/tasks/shared/services/task-manager.service.spec.ts' \
  --include='src/app/upload-tasks/shared/recording-session.service.spec.ts' \
  --include='src/app/upload-tasks/shared/highlight.service.spec.ts' \
  --include='src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts' \
  --include='src/app/upload-tasks/part-video-dialog/part-video-dialog.component.spec.ts')
(cd webapp && npx eslint \
  src/app/core/services/control-operation.service.ts \
  src/app/core/services/control-operation.service.spec.ts \
  src/app/tasks/shared/task.model.ts \
  src/app/tasks/shared/services/task.service.ts \
  src/app/tasks/shared/services/task.service.spec.ts \
  src/app/tasks/shared/services/task-manager.service.ts \
  src/app/tasks/shared/services/task-manager.service.spec.ts \
  src/app/tasks/add-task-dialog/add-task-dialog.component.ts \
  src/app/tasks/add-task-dialog/add-task-dialog.component.spec.ts \
  src/app/tasks/task-item/task-item.component.ts \
  src/app/tasks/task-item/task-item.component.spec.ts \
  src/app/upload-tasks/shared/recording-session.model.ts \
  src/app/upload-tasks/shared/recording-session.service.ts \
  src/app/upload-tasks/shared/recording-session.service.spec.ts \
  src/app/upload-tasks/shared/highlight.model.ts \
  src/app/upload-tasks/shared/highlight.service.ts \
  src/app/upload-tasks/shared/highlight.service.spec.ts \
  src/app/upload-tasks/highlight-editor/highlight-editor.component.ts \
  src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts \
  src/app/upload-tasks/recording-sessions/recording-sessions.component.ts \
  src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts \
  src/app/upload-tasks/part-video-dialog/part-video-dialog.component.ts \
  src/app/upload-tasks/part-video-dialog/part-video-dialog.component.spec.ts \
  && npm run build)
```

全量 `npx ng lint` 另行运行并记录基线，但不把与本计划无关的 5 个既有错误当作阻断：`page-not-found` 空 lifecycle、`info-panel` native output、3 个 task-detail 空 lifecycle。若错误数或文件集合增加则失败；本计划改动文件必须由上述 targeted ESLint 全绿。

- [ ] **运行整仓后端回归和静态检查**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
black --check src tests
isort --check-only src tests
flake8 src tests
mypy src/blrec
.venv/bin/python -m build
```

GitHub Actions 的既有 Python 3.8/3.10/3.11 matrix 必须全绿；本地 diff 额外检查不得引入 `asyncio.timeout`、`TaskGroup`、`except*` 或 PEP 604 union，Black 继续使用仓库 `py38` target。

- [ ] **只用 fixture 写性能证据并回填台账**

创建 `docs/performance/write-media-benchmark.md`，记录每个 WM 任务的测试命令、fixture 规模、p50/p95、heartbeat、active/waiting 峰值、DB write 数、probe 数、首字节和恢复结果。将冻结 36 条基线中的 35 条 gap disposition 更新为具体 commit/test evidence，I-055 继续 Keep；I-104/I-105 保持现有 Hot-read 含义，I-106 记录 inspection status，I-107 记录 control-operation status，并把台账头部更新为实施后 107 条。不得记录本机/NAS 路径、账号或请求值。

允许的上线验证仅为：部署后单次读取健康/operation 状态、打开一个既有媒体并发出一个 Range 请求、提交一个 no-op task desired-state 操作。禁止在 NAS 执行并发密码、1,000 WebSocket、批量删除、批量 FFprobe、批量剪辑或媒体吞吐压测。

- [ ] **提交最终证据**

```bash
git add docs/performance/request-audit.md docs/performance/write-media-benchmark.md
git commit -m "docs: record write media performance evidence"
```

## 完成定义

- 36 条基线 Write/media 请求全部有实现测试或 I-055 的保留证据；不存在第二条整项 Keep。
- 四个最高风险放大器均有硬上界：Argon2 1+4、WebSocket 1 sender+128、删除 worker 1/quantum 128、高光 probe 2+8/单 source/30 秒。
- 所有长控制操作先持久化 intent 并快速确认；崩溃后继续或明确 failed，不依赖 Starlette 临时 BackgroundTasks。
- 完成媒体保留 Range 并新增正确条件缓存；活动媒体保持 no-store；所有响应前 FS 工作离开 event loop。
- 显式批量 mutation 不超过 100 项；retry-all durable operation 以 quantum 100 处理提交时全部失败记录，每 quantum 一次 transaction/至多一次 wake；58 个纯本地修改 benchmark 小于 2 秒。
- 所有聚焦测试、整仓回归、静态检查、Angular targeted lint/build、浏览器插件 test/typecheck/build、Python 3.8 matrix 和 package build 通过；全量 Angular lint 只允许记录的 5 个既有错误，最终证据不包含 NAS 压测或敏感信息。
