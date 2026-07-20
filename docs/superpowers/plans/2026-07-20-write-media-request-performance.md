# Write and Media Request Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让全部 36 条 Write/media 入站请求在慢密码散列、事件突发、文件删除、FLV 快照、FFprobe、批量状态修改和媒体读取压力下仍具有明确的 admission、恢复边界与延迟预算，同时不改变 B 站请求频率和既有业务语义。

**Architecture:** 保留 FastAPI、Angular、SQLite、现有录制核心和上传运行时。同步 CPU/文件工作进入各自的有界工作器；需要等待生命周期、删除或重启的控制动作先持久化 intent，再由可恢复 worker 收敛；纯本地批量写入合并为单事务；完成媒体统一使用一个轻量资源与 HTTP response helper。I-055 保持原实现，混合标记请求只处理本地层，远端复用与重试留给 Outbound 计划。

**Tech Stack:** Python 3.9、FastAPI、SQLite、pytest、Argon2、FFmpeg/FFprobe、Angular 15、RxJS、Jasmine/Karma。

---

## 全局约束与预算

- 不使用 git worktree；每个任务独立提交、独立回滚，并按本计划顺序执行。
- 先写能稳定复现问题的失败测试，再做最小实现；不以提高线程数、队列长度或 B 站请求频率掩盖阻塞。
- `C100`：本地控制动作在 100 ms 内确认；纯本地 58 项批量修改在 2 秒内完成。
- `D100`：普通本地数据库读取 p95 小于 100 ms。
- `T150`：媒体 access/首字节 p95 小于 150 ms；持续传输不按连接总时长判慢。
- `STR`：WebSocket 分开记录握手、首事件、持续时长、事件/字节、峰值积压及断开原因。
- event-loop harness：工作线程内的阻塞 fake 运行期间，每 10 ms heartbeat 的额外延迟 p95 小于 25 ms。
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
| 6 | WM-06 task desired-state reconciler | P1：最多 100 个生命周期动作串行占用请求 | 无 |
| 7 | WM-07 membership/control operation | P1：add/remove/collect 无可恢复 operation 边界 | WM-06 |
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
| WM-05 | I-089、I-090 | 16 sources、30 秒绝对期限、检查 token/指纹复用 |
| WM-06 | I-011(state)、I-022、I-023、I-024、I-025、I-026、I-027、I-028、I-029 | desired state 一次持久化，后台合并收敛 |
| WM-07 | I-011(delete)、I-030、I-031、I-032、I-102 | 持久化 operation journal 与单 worker |
| WM-08 | I-034、I-036、I-039、I-041 | 原子设置写入、目录 worker、后台 apply/restart |
| WM-09 | I-065(non-delete)、I-068(non-delete)、I-069 | 单事务/SAVEPOINT、LIMIT 100、一次 wakeup |
| WM-10 | I-072、I-095、I-096 | lightweight resource、Range/ETag/If-Range/cache/首字节指标 |
| WM-11 | I-073 | 顺序 continuation，cache miss 不再 O(cursor) |
| WM-12 | I-081 | 2+8 有界 validation/hash/store/cleanup |
| 保留 | I-055 | **唯一整项无需修改**；只跑既有事务/回滚测试 |

表中去重后为 36 个 ID；I-011、I-065、I-068、I-089、I-090 按动作或共享服务跨任务出现，但每个入口只保持一份最终路由契约。

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

在 `test_auth_routes.py` 同时发出 6 个慢密码请求：1 个 running、4 个 queued，第 6 个在 C100 内返回 503 且 `Retry-After: 1`；过载不得增加 login failure。对 setup/login/change/recover 各跑 heartbeat。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/web/test_auth_store.py tests/web/test_auth_routes.py -k 'password_worker or hash_lock or overload or revoke or rate_limit' -q
```

Expected: FAIL，因为当前 hash/verify 在 async handler 或 `AdminAuthStore._lock` 内运行，且不存在 admission boundary。

- [ ] **Step 2: 拆分 prepare、CPU work 与 CAS commit**

新增不可变 ticket：`LoginPasswordTicket(encoded_hash, admin_exists, username_matches, rate_limit_key, observed_version)` 与 `PasswordChangeTicket(encoded_hash, observed_version)`。prepare 阶段只在锁内读取 hash/version 并检查 rate-limit；worker 阶段做 dummy/real verify、needs-rehash 和新 hash；commit 阶段重新检查 `admin.updated_at/password_hash`，以 `UPDATE ... WHERE password_hash=? AND updated_at=?` 提交。旧值变化时安全失败，不允许用旧密码创建 session。

setup 先在 worker hash，再在一次事务中确认未初始化并创建 session；change/reset 在同一 CAS 事务更新 hash、写 audit、撤销全部 session。login invalid 在 commit 锁内调用现有 `_record_failed_login`；worker saturation 不进入该路径。

- [ ] **Step 3: 接入有界 coordinator 与 503 映射**

`password_work.py` 使用专用 `ThreadPoolExecutor(max_workers=1, thread_name_prefix='blrec-password')` 和锁保护的 `active + waiting <= 5` 计数；拒绝发生在提交 executor 之前。`auth.configure()` 同时接收 coordinator，四个 handler `await` 其工作；`main.py` startup 创建/配置，shutdown 等待活动项后关闭 executor。503 只返回固定 detail 和 `Retry-After`。

- [ ] **Step 4: 验证安全语义与预算**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/web/test_auth_store.py tests/web/test_auth_routes.py -q
black --check src/blrec/web/password_work.py src/blrec/web/auth_store.py src/blrec/web/routers/auth.py tests/web/test_auth_store.py tests/web/test_auth_routes.py
flake8 src/blrec/web/password_work.py src/blrec/web/auth_store.py src/blrec/web/routers/auth.py
mypy src/blrec/web/password_work.py src/blrec/web/auth_store.py src/blrec/web/routers/auth.py
```

Expected: PASS；活动 Argon2 恒为 1、等待不超过 4、拒绝 < C100、heartbeat p95 < 25 ms，既有 rate-limit/revoke/hash 参数全部通过。

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

callback 只调用 `loop.call_soon_threadsafe(enqueue, item)`，因此 Rx 从其他线程发事件时也不直接触碰 asyncio queue；`enqueue` 在 loop 内 `queue.put_nowait()`。队列满时只设置一次 overflow 信号。sender 是唯一 serializer/`send_text` 调用者，逐条保序发送。overflow 关闭码固定为 1013，不能静默丢 event/exception。所有退出统一进入 `finally`：dispose 一次、取消并 await sender、关闭 socket（如尚未关闭）、完成一次终止 future。

- [ ] **Step 3: 添加不含 payload 的 STR 指标**

在 `finally` 用 `audit('websocket_connection', ...)` 写 route、handshake_ms、first_event_ms、duration_ms、events、bytes、peak_backlog、disconnect_reason、disconnect_code；序列化后的正文和 exception 文本都不进入字段。事件/异常两个 route 只提供不同 serializer，复用同一 pump。

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
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `src/blrec/web/routers/highlights.py`
- Modify: `tests/bili_upload/test_database.py`
- Create: `tests/bili_upload/test_deletion_worker.py`
- Modify: `tests/bili_upload/test_task_actions.py`
- Modify: `tests/bili_upload/test_highlights.py`
- Modify: `tests/bili_upload/test_account_runtime.py`
- Modify: `tests/web/test_recording_sessions_routes.py`
- Modify: `tests/web/test_highlights_routes.py`

**接口：** `LocalDeletionWorker.request_session/request_clip` 只持久化 intent 并 `wake()`；worker 并发 1，每次 lease quantum 最多 128 个已去重且通过 ownership guard 的 path。

- [ ] **Step 1: 写请求不等待、129 path 分片与崩溃恢复测试**

用阻塞 upload/highlight worker fake 调 DELETE，断言 HTTP 在 C100 内返回，且 `_stop_upload_worker()`/`_stop_highlight_worker()` 未被调用。用 `tmp_path` 创建 129 个 owned files，第一次 `run_once()` 只处理 128 个并持久化 cursor；关闭并重建 runtime 后处理剩余项。分别注入 unlink 失败、数据库提交前崩溃、提交后重跑、非 owned path 与 recording source。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_deletion_worker.py tests/bili_upload/test_task_actions.py tests/bili_upload/test_highlights.py tests/bili_upload/test_account_runtime.py tests/web/test_recording_sessions_routes.py tests/web/test_highlights_routes.py -k 'delet' -q
```

Expected: FAIL，因为 session/job 删除仍可在请求内继续，clip delete 会等待两个全局 worker，clip 也没有可恢复 deletion state。

- [ ] **Step 2: 增加 migration 26 的持久状态**

新增 `local_deletion_items(id,owner_kind,owner_id,path,state,error)`，以 `(owner_kind,owner_id,path)` 唯一并按 `(state,id)` 索引；它就是持久化 cursor，worker 每次只选下 128 项。`highlight_clips` 增加 `deletion_state`（`none/requested/deleting/failed`）、`deletion_error`、`deletion_requested_at`，并建立 `(deletion_state,deletion_requested_at,id)` 索引。将 database latest version 更新为 26，并让 migration test 同时验证 CHECK、唯一约束、默认值和升级后的既有行。

- [ ] **Step 3: 统一 session/job 与 clip 删除状态机**

`delete_local_task()` 先解析到 session，再复用 `_request_session_deletion()`；不再保留第二套 prepare/delete/finish。请求事务记录 requested、清空旧 error，以 set-based INSERT 把候选路径去重写入 `local_deletion_items`，并保持 unknown-outcome/active lease fence。worker 每次只 `ORDER BY id LIMIT 128` 读取 pending items，ownership 校验后 unlink 并标记完成；全部 item 完成后再删本地 DB children。崩溃发生在 unlink 与 item commit 之间时，重跑发现文件已不存在并视为成功。

clip 删除先拒绝仍绑定上传任务的记录，再设置 `state='cancelled'` 与 deletion requested；worker 只允许删除 dedicated clip root 下的 output video/XML。失败写 `failed + deletion_error`，重启把 `deleting` 恢复为 requested。任何路径都不得删除 source recording 或远端稿件。

- [ ] **Step 4: runtime 和路由只排队，不停全局 worker**

`BiliAccountRuntime` startup 创建并恢复一个 `LocalDeletionWorker`；shutdown 在当前 quantum 边界停止。`run_recording_session_action` 的 delete 分支和 `delete_highlight_clip` 只调用 request/wake；pause/resume/set-upload/set-skip 直接依赖已有 DB lease/state fence 并 wake upload worker，不再停启全局 worker。两个 batch route 保持逐项 accepted/message；highlight DELETE 保持 204 兼容，但 detail/list 可读取 deletion state/error。

- [ ] **Step 5: 验证迁移、恢复、保护和预算**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_database.py tests/bili_upload/test_deletion_worker.py tests/bili_upload/test_task_actions.py tests/bili_upload/test_highlights.py tests/bili_upload/test_account_runtime.py tests/web/test_recording_sessions_routes.py tests/web/test_highlights_routes.py -q
black --check src/blrec/bili_upload/deletion_worker.py src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/highlights.py src/blrec/bili_upload/runtime.py
flake8 src/blrec/bili_upload/deletion_worker.py src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/highlights.py src/blrec/bili_upload/runtime.py
mypy src/blrec/bili_upload/deletion_worker.py src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/highlights.py
```

Expected: PASS；ack < C100、worker 并发 1、quantum <= 128、重启可续传或明确 failed，不等待全局 worker，ownership/远端非删除/lease fence 全部通过。

- [ ] **Step 6: Commit**

```bash
git add src/blrec/bili_upload/deletion_worker.py src/blrec/bili_upload/migrations/0026_initial.sql src/blrec/bili_upload/database.py src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/highlights.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/recording_sessions.py src/blrec/web/routers/highlights.py tests/bili_upload/test_database.py tests/bili_upload/test_deletion_worker.py tests/bili_upload/test_task_actions.py tests/bili_upload/test_highlights.py tests/bili_upload/test_account_runtime.py tests/web/test_recording_sessions_routes.py tests/web/test_highlights_routes.py
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

**接口：** `ActiveMediaService.snapshot(part_id, path, source_size, metadata)` 返回 `FlvMediaSnapshot`；in-flight key 使用不触盘的 `(part_id, abspath, source_size, lastkeyframelocation, lastkeyframetimestamp)`，worker 再解析 realpath 并形成 cache identity。2 个活动 job、8 个等待 job，相同 key single-flight。

- [ ] **Step 1: 写同步 FS/FLV、single-flight 与失效测试**

让 `realpath/open/FlvMediaSnapshot.create` 在 fake 中阻塞并记录线程；并发请求相同 key，断言只构造一次。改变 source size 或两个 O(1) metadata revision 字段后必须重建。构造第三个并发 job 证明 active <=2，第 11 个总 admission 在 T150 内得到 busy。给 journal 增加 `active_part_for_session(session_id)` 的失败契约：一场多个完成 part 和一个活动 part 时只返回最新 `recording/postprocessing` part，完成场次返回 None。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_recording_content.py tests/bili_upload/test_journal.py tests/bili_upload/test_active_media.py tests/web/test_main_active_media.py tests/web/test_recording_sessions_routes.py -k 'snapshot or active_media or active_duration or active_part' -q
```

Expected: FAIL，因为 route/main 当前在 event loop realpath/open/parse，相同 access 重复创建，highlights 遍历整场 parts。

- [ ] **Step 2: 实现有界 ActiveMediaService**

使用专用两线程 executor 和显式 10 项 admission 计数；realpath、FLV header/read、metadata 重写与 keyframe 遍历全部在 worker。in-flight future 按 key 共享；完成结果只在 source size/revision 未变化时复用。service 的 LRU 结果最多 64 项，签发 token 的 `MediaSnapshotStore` 仍独立保持 64 项。

- [ ] **Step 3: 播放和高光共用同一活动快照**

`create_recording_media_access()` await service；busy 映射 503 与固定 `Retry-After: 1`，损坏 FLV 仍回退 frozen snapshot。`RecordingJournalBridge.active_part_for_session()` 用一个 `artifact_state IN ('recording','postprocessing') ORDER BY part_index DESC LIMIT 1` 查询；`web/main.py:_active_highlight_durations` 只对该 part 调用同一 service，不遍历已完成 parts。`_active_recording_metadata` 不再做 event-loop realpath。startup 创建 service，shutdown 调 `await service.close()`，不得遗留 executor thread。

- [ ] **Step 4: 验证 heartbeat、T150 与 active no-store**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_recording_content.py tests/bili_upload/test_journal.py tests/bili_upload/test_active_media.py tests/web/test_main_active_media.py tests/web/test_recording_sessions_routes.py -q
black --check src/blrec/bili_upload/active_media.py src/blrec/bili_upload/journal.py src/blrec/web/main.py src/blrec/web/routers/recording_sessions.py tests/bili_upload/test_active_media.py tests/web/test_main_active_media.py
flake8 src/blrec/bili_upload/active_media.py src/blrec/bili_upload/journal.py src/blrec/web/main.py src/blrec/web/routers/recording_sessions.py
mypy src/blrec/bili_upload/active_media.py src/blrec/bili_upload/journal.py src/blrec/web/routers/recording_sessions.py
```

Expected: PASS；warm access < T150、active <=2、waiting <=8、相同 key 一次构造、heartbeat p95 <25 ms，活动 token/media 仍 `no-store`。

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

**接口：** inspection coordinator 最多 2 个活动、8 个等待；单次最多 16 sources、绝对期限 30 秒。ready response 含一次性 `inspectionToken`；cold request 超过 T150 返回 202 `operationId/retryAfterMs`，由 `GET /api/v1/highlights/inspections/{operation_id}` 轮询。

- [ ] **Step 1: 写 source、总期限、queue 与重复 probe 的失败测试**

在 `test_highlight_cut.py` 构造 16/17 sources；fake 两个 ffprobe 各消耗预算，断言第二个命令只收到 `deadline-now` 的剩余 timeout，整次不超过 30 秒，argv 仍为 list/tuple 且 `shell=False`。在 service/route tests 并发 11 个阻塞 inspect，断言第 11 个快速 503；相同 fingerprint/range single-flight。跑完整 `inspect -> create -> worker`，记录源文件 ffprobe 次数必须为一轮。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_highlight_cut.py tests/bili_upload/test_highlights.py tests/bili_upload/test_highlight_worker.py tests/web/test_highlights_routes.py -k 'deadline or source_limit or inspection or probe_reuse or overload' -q
```

Expected: FAIL，因为 source 无上限、timeout 为每命令预算、create/worker 会重新 probe，也没有 inspection operation/token。

- [ ] **Step 2: 给 clip 持久化已验证 inspection**

Migration 27 在 `highlight_clips` 增加 `inspection_json TEXT` 和 `source_fingerprint_json TEXT`；两列只保存规范化 profile/keyframe range 与 `(part_id, realpath, size, mtime_ns)`，不得保存临时 token。database latest version 更新为 27，迁移测试验证旧 clip 列为 NULL 且仍能由 worker 按原路径处理。

- [ ] **Step 3: 实现 deadline-aware clipper 与 coordinator**

`LosslessClipper.inspect(..., deadline_monotonic)` 在进入每次 subprocess 前计算剩余秒数并取 `min(probe_timeout_seconds, remaining)`；剩余 <=0 立即抛固定 timeout。service 在 worker 内完成 path existence、fingerprint 和 probe，拒绝第 17 个 source。相同 fingerprints/range 共用 future；ready token TTL 120 秒且单次消费，operation 结果 TTL 5 分钟并限制 32 项，避免新的无界内存表。

- [ ] **Step 4: 让 create 和 worker 复用同一轮结果**

`CreateClipRequest` 增加可选 `inspectionToken`。token 有效且 fingerprint 未变时，create 在 C100 内把 inspection/fingerprint 写入 clip 并排队；token 缺失或 stale 时只提交/复用同一 coordinator inspection 并返回 202 operation，不在 create HTTP 内等待 probe。客户端取得 ready token 后重提一次 create。worker 加载持久 inspection：fingerprint 一致则直接使用 profile/keyframe 结果；变化才在同一 16-source/30 秒预算内重验并更新。FFmpeg 产物验证仍独立执行，不计作源文件重复 probe。

- [ ] **Step 5: 前端轮询且旧 range 不能覆盖新选择**

service 将 200 ready、202 pending、503 busy 映射为显式 union。editor 用 `switchMap` 轮询 operation；用户更改 range 或关闭页面即取消，旧 operation 返回不得覆盖新 range。create 必须发送当前 ready token；token stale 时只重新 inspect 一次。

- [ ] **Step 6: 验证端到端预算与回归**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_database.py tests/bili_upload/test_highlight_cut.py tests/bili_upload/test_highlights.py tests/bili_upload/test_highlight_worker.py tests/web/test_highlights_routes.py -q
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/shared/highlight.service.spec.ts' --include='src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts'
cd webapp && npx eslint src/app/upload-tasks/shared/highlight.model.ts src/app/upload-tasks/shared/highlight.service.ts src/app/upload-tasks/shared/highlight.service.spec.ts src/app/upload-tasks/highlight-editor/highlight-editor.component.ts src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts
```

Expected: PASS；handshake < T150、create < C100、active <=2、waiting <=8、sources <=16、absolute deadline <=30 秒，同一未变化链路只 probe 一轮源文件。

- [ ] **Step 7: Commit**

```bash
git add src/blrec/bili_upload/migrations/0027_initial.sql src/blrec/bili_upload/database.py src/blrec/bili_upload/highlight_cut.py src/blrec/bili_upload/highlights.py src/blrec/bili_upload/highlight_worker.py src/blrec/web/routers/highlights.py tests/bili_upload/test_database.py tests/bili_upload/test_highlight_cut.py tests/bili_upload/test_highlights.py tests/bili_upload/test_highlight_worker.py tests/web/test_highlights_routes.py webapp/src/app/upload-tasks/shared/highlight.model.ts webapp/src/app/upload-tasks/shared/highlight.service.ts webapp/src/app/upload-tasks/shared/highlight.service.spec.ts webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.ts webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts
git commit -m "perf: bound and reuse highlight inspections"
```

### Task 6: WM-06 task desired-state reconciler

**覆盖：** I-011 的 start/stop/recorder actions，I-022--I-029。

**Files:**
- Create: `src/blrec/task/control_reconciler.py`
- Modify: `src/blrec/setting/setting_manager.py`
- Modify: `src/blrec/application.py`
- Modify: `src/blrec/task/task_manager.py`
- Modify: `src/blrec/web/routers/tasks.py`
- Create: `tests/task/test_control_reconciler.py`
- Create: `tests/test_application_task_controls.py`
- Modify: `tests/web/test_tasks_routes.py`

**接口：** `TaskControlReconciler.request(room_id, desired_monitor, desired_recorder, force)` 按 room 合并；`SettingsManager.change_task_desired_states(changes)` 做一次 diff、一次 dump，no-op 零 dump。Task 8 再把共用 dump 升级为 temp/fsync/replace 原子写。

- [ ] **Step 1: 写 58 项单 dump、no-op 与慢 lifecycle 的失败测试**

创建 58 个 fake tasks；batch start/stop/recorder enable/disable 时统计 dump 次数和 HTTP elapsed。阻塞 task manager lifecycle，断言 route 仍应在 C100 内 accepted。连续对同 room 发 start-stop-start，断言 pending key 恒为一个且最终按最后 desired state 收敛。相同 desired state 不得 refresh、dump 或排队。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/task/test_control_reconciler.py tests/test_application_task_controls.py tests/web/test_tasks_routes.py -k 'desired or lifecycle or batch or noop or coalesce' -q
```

Expected: FAIL，因为 batch route 当前逐项 await/dump，单项和 all route 等待 lifecycle，BackgroundTasks 不可恢复也不可观测。

- [ ] **Step 2: 增加一次 copy/diff/dump 的 desired-state mutation**

`change_task_desired_states` 在一个 async mutation lock 内验证全部 room，计算 `enable_monitor/enable_recorder` 的实际 diff，只修改变化项并调用一次 `dump_settings()`；无变化直接返回空 tuple。force 只影响本次 stop/disable 收敛，不写成长期设置。保留 all-path 的单 dump，并让 batch route 也走相同入口。

- [ ] **Step 3: 实现 per-room coalescing reconciler**

reconciler 维护最多 100 个合法 room key，不为重复命令追加 queue item。配置持久化成功后才 wake；单 worker 逐 room 读取最新 desired state并调用现有 TaskManager lifecycle。命令在处理中再次变化时，完成后再读最新值继续收敛。stop/disable force 只在尚未开始相反操作时生效。失败写结构化 audit（room/kind/error class，不含远端正文），保留 desired state供下一次 wake/重启恢复。

- [ ] **Step 4: 所有控制路由统一快速确认**

Application 的单项/all 方法先持久化 desired state，再提交 reconciler；route 统一返回 202，body 继续使用 `ResponseMessage`，`data` 只增加 `pendingRoomIds`。旧 `background` body 字段继续接受但不再分叉执行方式。I-011 的 `cut` 和 `refresh` 保持原分支：cut 同步触发，refresh 仍属于 Outbound，不经过 desired-state mutation。

- [ ] **Step 5: 验证业务语义、前端 loading 与预算**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/task/test_control_reconciler.py tests/test_application_task_controls.py tests/task/test_task_manager_cleanup.py tests/web/test_tasks_routes.py -q
black --check src/blrec/task/control_reconciler.py src/blrec/setting/setting_manager.py src/blrec/application.py src/blrec/task/task_manager.py src/blrec/web/routers/tasks.py
flake8 src/blrec/task/control_reconciler.py src/blrec/setting/setting_manager.py src/blrec/application.py src/blrec/task/task_manager.py src/blrec/web/routers/tasks.py
mypy src/blrec/task/control_reconciler.py src/blrec/setting/setting_manager.py src/blrec/application.py
```

Expected: PASS；单项 ack < C100、58 项 <2 秒且一次 dump、no-op 零 dump、同 room 一个 pending key，远端请求频率未增加。

- [ ] **Step 6: Commit**

```bash
git add src/blrec/task/control_reconciler.py src/blrec/setting/setting_manager.py src/blrec/application.py src/blrec/task/task_manager.py src/blrec/web/routers/tasks.py tests/task/test_control_reconciler.py tests/test_application_task_controls.py tests/web/test_tasks_routes.py
git commit -m "perf: reconcile persisted task desired state"
```

### Task 7: WM-07 task membership 与 control operation journal

**覆盖：** I-011 delete、I-030--I-032、I-102。

**Files:**
- Create: `src/blrec/control/__init__.py`
- Create: `src/blrec/control/operations.py`
- Create: `src/blrec/web/routers/control_operations.py`
- Modify: `src/blrec/application.py`
- Modify: `src/blrec/task/task_manager.py`
- Modify: `src/blrec/web/main.py`
- Modify: `src/blrec/web/routers/tasks.py`
- Modify: `src/blrec/web/routers/browser_extension.py`
- Create: `tests/control/test_operations.py`
- Modify: `tests/task/test_task_manager_cleanup.py`
- Modify: `tests/web/test_tasks_routes.py`
- Modify: `tests/web/test_browser_extension_routes.py`
- Modify: `docs/performance/request-audit.md`

**接口：** 独立 `control.sqlite3` journal，operation 状态为 `accepted/running/succeeded/failed`，step 状态为 `pending/succeeded/failed`；`POST` 返回 `operationId/status`，`GET /api/v1/control-operations/{operation_id}` 查询。按 `(kind,target_key)` 去重，单 worker，最多 100 个非终态 operation。

- [ ] **Step 1: 写 dedupe、容量、partial 与重启恢复失败测试**

并发提交两个相同 add/remove/collect，断言返回同一 ID；100 个不同非终态后第 101 个在 C100 内 503。让 worker 在 remove teardown 中断，重建 journal/worker 后必须继续且启动加载不得重新启动待删除 room。browser collect 的 collect 成功、policy 失败应返回 operation `failed`，steps 明确 `collect=succeeded/policy=failed`，而不是让 409 隐藏前一步。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/control/test_operations.py tests/task/test_task_manager_cleanup.py tests/web/test_tasks_routes.py tests/web/test_browser_extension_routes.py -k 'operation or collect or membership or remove' -q
```

Expected: FAIL，因为 add/remove/collect 当前占用请求、没有 durable status/dedupe，后段错误会掩盖已完成步骤。

- [ ] **Step 2: 实现独立持久 journal 和 status route**

SQLite 文件放在 settings 文件同目录，0600、DELETE journal、FULL synchronous、专用单线程 executor。表含 id/kind/target_key/status/steps_json/error_code/created_at/updated_at，partial unique index 约束 accepted/running 的 `(kind,target_key)`。只允许 100 个非终态；每次终态提交后保留最近 1,000 个终态并删除更旧行，防止历史无限增长。error 只保存稳定错误码与截断安全摘要。

status route 只读取该 journal；`main.py` 在 task load 前 open/recover，在 shutdown 最后 close。将新 route 作为 I-104 追加到 request audit，并把总数更新为 104；本计划原始 36 条 Write/media 覆盖数不变。

- [ ] **Step 3: membership worker 先记 intent、后做 teardown/setup**

add/remove/remove-all 先提交 operation；HTTP 不等待 room normalize、retry 或文件 finalization。单 worker 执行现有 TaskManager 方法并逐 step 提交。remove operation 在 task load 阶段即被视为 desired-absent，避免重启后恢复待删除任务；完成 teardown 后才一次移除 settings。add 成功后使用 Task 6 reconciler 收敛 monitor/recorder desired state。每个 logical membership operation 最多一次 settings dump。

- [ ] **Step 4: browser collect 成为幂等组合 operation**

`collect_room` 以原始 room target 去重，steps 固定为 resolve/add/desired-state/policy。每步成功即提交；重启从首个 pending step继续。category/room 远端调用仍使用现有 cadence/retry，不在本任务添加请求。若 policy 失败，status 清楚展示 partial steps；再次提交相同 target 返回现有 operation，不重复 add。

- [ ] **Step 5: 验证恢复、审计和输入兼容**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/control/test_operations.py tests/task/test_task_manager_cleanup.py tests/web/test_tasks_routes.py tests/web/test_browser_extension_routes.py -q
black --check src/blrec/control src/blrec/web/routers/control_operations.py src/blrec/application.py src/blrec/task/task_manager.py src/blrec/web/routers/tasks.py src/blrec/web/routers/browser_extension.py
flake8 src/blrec/control src/blrec/web/routers/control_operations.py src/blrec/application.py src/blrec/task/task_manager.py src/blrec/web/routers/tasks.py src/blrec/web/routers/browser_extension.py
mypy src/blrec/control src/blrec/web/routers/control_operations.py
test "$(rg -c '^\| I-[0-9]{3} \|' docs/performance/request-audit.md)" = 104
```

Expected: PASS；accept < C100、worker 并发 1、非终态 <=100、相同 target 去重、重启继续或明确 failed，collect partial 可查询。

- [ ] **Step 6: Commit**

```bash
git add src/blrec/control src/blrec/web/routers/control_operations.py src/blrec/application.py src/blrec/task/task_manager.py src/blrec/web/main.py src/blrec/web/routers/tasks.py src/blrec/web/routers/browser_extension.py tests/control/test_operations.py tests/task/test_task_manager_cleanup.py tests/web/test_tasks_routes.py tests/web/test_browser_extension_routes.py docs/performance/request-audit.md
git commit -m "perf: persist task control operations"
```

### Task 8: WM-08 原子设置写入与后台 apply

**覆盖：** I-034、I-036、I-039、I-041。

**Files:**
- Modify: `src/blrec/setting/models.py`
- Modify: `src/blrec/setting/setting_manager.py`
- Modify: `src/blrec/application.py`
- Modify: `src/blrec/web/routers/settings.py`
- Modify: `src/blrec/web/routers/application.py`
- Modify: `src/blrec/web/routers/validation.py`
- Modify: `src/blrec/web/main.py`
- Create: `tests/setting/test_settings_persistence.py`
- Modify: `tests/test_application_live_status.py`
- Modify: `tests/web/test_settings_routes.py`
- Create: `tests/web/test_application_routes.py`
- Create: `tests/web/test_validation_routes.py`

**接口：** `SettingsManager.validate_directory(path)` 通过容量 2、等待 8 的 file worker；PATCH 先一次原子持久化 desired settings，再按 section key 提交 Task 7 operation。响应正文保持原 Settings/TaskOptions，若有 apply operation 则加 `X-BLREC-Operation-ID`。

- [ ] **Step 1: 写 request-model FS、原子失败与慢 apply 的失败测试**

patch `os.makedirs/isdir/access` 为阻塞 fake，构造并提交 `SettingsIn(output=...)`/`TaskOptions`，证明 Pydantic request parse 不应触发 FS。并发两个 PATCH，断言文件始终能完整 `Settings.load()`；在 flush 后、replace 前注入错误，旧文件字节完全不变。阻塞 header reconnect、live monitor reconfigure 和 `Application.restart()`，route 仍在 C100 内返回 operation header。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/setting/test_settings_persistence.py tests/test_application_live_status.py tests/web/test_settings_routes.py tests/web/test_application_routes.py tests/web/test_validation_routes.py -k 'directory or atomic or apply or restart or concurrent' -q
```

Expected: FAIL，因为 model validator 直接访问 FS，dump truncate 目标文件且无 mutation lock，apply/restart 仍在请求内 await。

- [ ] **Step 2: 从 request model 移除真实 FS 探测**

`OutputSettings._validate_dir`/`LoggingSettings._validate_dir` 只做字符串规范化，不执行 `isdir/access`；默认 factory 仅在应用初始化创建默认目录。提取同步 `validate_directory_sync()`：`main.py` 在事件循环启动前用它校验 `Settings.load()` 的目录，`SettingsManager.change_*` 和 `validation/dir` 则通过有界 worker 调用同一函数，并保持 ENOTDIR/EACCES/0 response code。

- [ ] **Step 3: copy-on-write + temp/fsync/replace**

mutation lock 内以 deep copy 计算 diff；验证 candidate 后，在目标同目录创建 0600 temp，写 TOML、flush、`os.fsync(file)`、`os.replace`，支持的平台再 fsync directory。持久化成功后才把 candidate section 写回 live settings；失败删除 temp、保留旧内存与旧文件。一个 PATCH 恰好一次 dump，no-op 零 dump；global/task PATCH 共用同一 lock。

- [ ] **Step 4: apply/restart 进入 section-keyed operation**

持久化完成后，header reconnect、live-monitor reconfigure、mode restart、task header restart 提交 Task 7 journal；target key 为 `settings:<section>` 或 `task-settings:<room>:<section>`，重复 section 合并。`POST /app/restart` 只提交 single-flight `application:restart` 并返回 202 operation ID。失败只更新 operation，不把已持久 desired settings 回滚成未知状态。`main.py` 将 `X-BLREC-Operation-ID` 加入 CORS exposed headers。

- [ ] **Step 5: 验证前端兼容、恢复与预算**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/setting/test_settings_persistence.py tests/test_application_live_status.py tests/web/test_settings_routes.py tests/web/test_application_routes.py tests/web/test_validation_routes.py -q
black --check src/blrec/setting/models.py src/blrec/setting/setting_manager.py src/blrec/application.py src/blrec/web/routers/settings.py src/blrec/web/routers/application.py src/blrec/web/routers/validation.py src/blrec/web/main.py
flake8 src/blrec/setting/models.py src/blrec/setting/setting_manager.py src/blrec/application.py src/blrec/web/routers/settings.py src/blrec/web/routers/application.py src/blrec/web/routers/validation.py src/blrec/web/main.py
mypy src/blrec/setting/models.py src/blrec/setting/setting_manager.py src/blrec/web/routers/settings.py
```

Expected: PASS；heartbeat p95 <25 ms，directory read < D100，PATCH/restart ack < C100，每 PATCH 一次 atomic dump，同 section 一个 pending operation。

- [ ] **Step 6: Commit**

```bash
git add src/blrec/setting/models.py src/blrec/setting/setting_manager.py src/blrec/application.py src/blrec/web/routers/settings.py src/blrec/web/routers/application.py src/blrec/web/routers/validation.py src/blrec/web/main.py tests/setting/test_settings_persistence.py tests/test_application_live_status.py tests/web/test_settings_routes.py tests/web/test_application_routes.py tests/web/test_validation_routes.py
git commit -m "perf: persist settings before background apply"
```

### Task 9: WM-09 上传/场次单事务批处理

**覆盖：** I-065/I-068 的非删除分支、I-069；删除分支使用 Task 3。

**Files:**
- Modify: `src/blrec/bili_upload/task_actions.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `tests/bili_upload/test_task_actions.py`
- Modify: `tests/bili_upload/test_account_runtime.py`
- Modify: `tests/web/test_recording_sessions_routes.py`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.model.ts`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.service.spec.ts`

**接口：** `UploadTaskActionManager.run_job_batch(action, job_ids, subject)` 与 `run_session_batch(action, session_ids, subject)` 在一次 `database.write` 内处理最多 100 项；每项一个 SQLite SAVEPOINT，返回原有逐项 `accepted/message`。response 增加 `hasMore: boolean = false`。

- [ ] **Step 1: 写 database-call、partial、101 rows 与 worker 生命周期失败测试**

种 58 项混合 valid/rejected jobs，统计 `database.write` 恰好一次；中间一项触发 fence，断言该 SAVEPOINT rollback 而前后项成功。种 101 个 retryable failed jobs，第一次只选择确定排序的 100 个并返回 `hasMore=true`，audit 也不得含第 101 个。用 runtime fake 断言整批不调用 per-item `_stop_upload_worker/_start_upload_worker`，且只 wake 一次。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_task_actions.py tests/bili_upload/test_account_runtime.py tests/web/test_recording_sessions_routes.py -k 'batch or retryable or savepoint or wakeup' -q
```

Expected: FAIL，因为 route 当前逐项调用 manager/runner，retryable ID 查询无 LIMIT，每项各有 transaction/worker 生命周期。

- [ ] **Step 2: 把单项状态机提取为 connection-scoped helper**

将 pause/resume/retry/repair/skip/repost/set-intent 的校验和写入提取为接收 `sqlite3.Connection` 的私有 helper；单项 public API 仍以一次 `database.write` 调用该 helper。batch 在一个 outer write 内对每个 ID 执行 `SAVEPOINT item_<ordinal>`，捕获 `UploadTaskActionRejected` 后 rollback/release 该项并继续。不得用宽 UPDATE 绕过 unknown-outcome、active lease、审核状态或 ownership fence。

- [ ] **Step 3: LIMIT 100 且一次选择、提交与唤醒**

retry-all 在同一 write 中 `ORDER BY id LIMIT 101`：前 100 个执行，额外一行只用于 `hasMore`，不进入 response/audit。显式 batch 保持 1--100 与唯一 ID validator。runtime 接收整批结果，若任一成功动作需要上传 worker，只调用一次非阻塞 `wake()`；delete action 只写 Task 3 requested state。

- [ ] **Step 4: 验证契约、预算和前端 model**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_task_actions.py tests/bili_upload/test_account_runtime.py tests/web/test_recording_sessions_routes.py -q
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/shared/recording-session.service.spec.ts'
black --check src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/recording_sessions.py
flake8 src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/recording_sessions.py
mypy src/blrec/bili_upload/task_actions.py src/blrec/web/routers/recording_sessions.py
```

Expected: PASS；request/response <=100、58 项 <2 秒、每批一次 write/commit 和一次 wake、partial result 与所有 side-effect fence 不变。

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/task_actions.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/recording_sessions.py tests/bili_upload/test_task_actions.py tests/bili_upload/test_account_runtime.py tests/web/test_recording_sessions_routes.py webapp/src/app/upload-tasks/shared/recording-session.model.ts webapp/src/app/upload-tasks/shared/recording-session.service.spec.ts
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

worker 以 `O_RDONLY` 打开并对 fd `fstat`，校验 regular file 与预期 root/identity。完成 artifact 的 ETag 由不可变 artifact key、`st_dev/st_ino/st_size/st_mtime_ns` 生成 quoted strong validator；文件一旦由不同 inode/revision 替换就产生新 ETag。活动 recording snapshot 不生成 ETag。304 在 close fd 后返回零 body。

- [ ] **Step 4: 统一 Range/cache 与首字节 metrics**

helper 复用现有 `parse_byte_range` 语义：匹配 strong If-Range 才 206，不匹配忽略 Range 回完整 200；If-None-Match 匹配回 304。completed recording/clip 返回 `Cache-Control: private, max-age=3600`；active snapshot 固定 `no-store` 且绝不 304。stream wrapper 在第一次 yield 记 first-byte，在正常结束/取消/异常 finally close fd 并发一条 `media_stream` audit。download 仍以 clip name 生成 UTF-8 `Content-Disposition`。

- [ ] **Step 5: 验证 T150、Range 回归和 CORS headers**

`main.py` 的 CORS `expose_headers` 增加 `ETag`、`Cache-Control`、`Content-Disposition`。Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/web/test_media_response.py tests/web/test_recording_sessions_routes.py tests/web/test_highlights_routes.py tests/bili_upload/test_recording_content.py tests/bili_upload/test_highlights.py tests/web/test_request_performance_middleware.py -q
black --check src/blrec/web/media_response.py src/blrec/bili_upload/recording_content.py src/blrec/bili_upload/highlights.py src/blrec/web/routers/recording_sessions.py src/blrec/web/routers/highlights.py
flake8 src/blrec/web/media_response.py src/blrec/bili_upload/recording_content.py src/blrec/bili_upload/highlights.py src/blrec/web/routers/recording_sessions.py src/blrec/web/routers/highlights.py
mypy src/blrec/web/media_response.py src/blrec/bili_upload/recording_content.py src/blrec/bili_upload/highlights.py
```

Expected: PASS；warm first byte <T150、每个 clip access 一次 lightweight DB read 与一次 worker open/fstat、304 零 body、Range/token/download 全部回归，活动媒体永不 cache。

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

**接口：** 保留 integer cursor，但只允许 `cursor=0` 创建新 stream，或消费当前 `(part_id,path,size,mtime_ns)` 的准确 next cursor；cache miss/stale cursor 抛 `RecordingContentCursorStale` 并映射 409。

- [ ] **Step 1: 写 cursor=100,000、第三文件 eviction 与文件变化测试**

instrument `_iter_danmaku` 的 `next()` 次数。直接请求 cursor=100,000 必须在固定步数内 409，不能推进十万次。顺序读两页内容/next cursor 必须正确；打开第三个文件驱逐第一个后继续第一个旧 cursor 得到 409；同路径 size/mtime 变化也得到 409。保留 DOCTYPE/XXE、invalid XML、limit 1/500 和 `limit+1` 测试。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_recording_content.py tests/web/test_recording_sessions_routes.py -k 'danmaku or cursor' -q
```

Expected: FAIL，因为 cache miss 当前执行 `for range(cursor): next(iterator)`，工作量随任意 cursor 线性增加。

- [ ] **Step 2: 只允许顺序 continuation 并显式限制 cache**

stream key 加 part_id，handle 保存 exact next cursor、last-access monotonic、一个 pending item。`cursor=0` 关闭旧 handle 后从头开始；`cursor>0` 只有与 handle.next_cursor 完全一致才读取，否则立即 stale。cache 保持最多 2 个 handle、TTL 10 分钟、pending 文本合计 256 KiB；超过字节上限时驱逐最旧 handle，当前 page 仍正确返回 next cursor，下一页若已驱逐则 409 重开。所有 iterator 在 eviction/error/EOF/reader.close 时 close。

- [ ] **Step 3: route 映射和 D100/heartbeat 验证**

新增固定 409 detail `弹幕分页状态已失效，请从第一页重新加载`，不回显 path/cursor。Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_recording_content.py tests/web/test_recording_sessions_routes.py -q
black --check src/blrec/bili_upload/recording_content.py src/blrec/web/routers/recording_sessions.py tests/bili_upload/test_recording_content.py
flake8 src/blrec/bili_upload/recording_content.py src/blrec/web/routers/recording_sessions.py
mypy src/blrec/bili_upload/recording_content.py
```

Expected: PASS；任一 page parser work 有固定上界、warm page <D100、heartbeat p95 <25 ms、limit <=500、安全 parser 与顺序内容均不回归。

- [ ] **Step 4: Commit**

```bash
git add src/blrec/bili_upload/recording_content.py src/blrec/web/routers/recording_sessions.py tests/bili_upload/test_recording_content.py tests/web/test_recording_sessions_routes.py
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

**接口：** `CoverWorkCoordinator` 提供 2 个活动 job、8 个等待 job；满载抛 `CoverWorkSaturated(retry_after=1)`。JPEG info、SHA-256、content-addressed store 和失败 cleanup 全在专用 file executor。

- [ ] **Step 1: 写 heartbeat、overload、同内容和孤儿恢复失败测试**

阻塞 JPEG scan/hash/store，发 11 个请求并跑 10 ms heartbeat：第 11 个 C100 内 503，active<=2、waiting<=8。并发相同内容只执行一次 digest-keyed store。注入文件已写后 DB insert 失败：普通失败清理本请求创建的孤儿；模拟崩溃留下同 hash 文件后重试应验证并完成 metadata；在 hash 路径预置不同内容时不得覆盖。

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_covers.py tests/web/test_upload_covers_routes.py -k 'worker or heartbeat or overload or orphan or duplicate' -q
```

Expected: FAIL，因为 image scan/hash/cleanup 当前在 event loop，工作 admission 无上限，DB failure/retry 一致性未覆盖。

- [ ] **Step 2: 实现 2+8 coordinator 和 digest-keyed store**

coordinator 在提交 executor 前以锁保护 `active+waiting<=10`，不使用无界默认 executor queue。worker 验证 JPEG markers、计算 SHA-256；取得 digest 后按 digest single-flight 完成 path 校验与 `_store_file`。已存在文件必须重新计算/比较内容 hash；一致则复用，不一致抛固定 `InvalidCover` 且不覆盖。DB 仍通过原有单线程 executor 插入。

- [ ] **Step 3: 定义 DB 失败与 shutdown 行为**

若本次创建文件但 DB insert 明确失败，在 file worker 中 unlink；若进程在两者之间崩溃，下次同 digest add 复用并补 metadata。runtime close 停止接收新 job、等待活动 2 个 job 完成并关闭 executor。route 保留流式读取超过 2 MiB 立即 413；worker saturation 映射 503 + `Retry-After: 1`，不记作 invalid image。

- [ ] **Step 4: 验证限制、幂等与格式检查**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/bili_upload/test_covers.py tests/web/test_upload_covers_routes.py tests/bili_upload/test_account_runtime.py -q
black --check src/blrec/bili_upload/covers.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/upload_covers.py tests/bili_upload/test_covers.py tests/web/test_upload_covers_routes.py
flake8 src/blrec/bili_upload/covers.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/upload_covers.py
mypy src/blrec/bili_upload/covers.py src/blrec/web/routers/upload_covers.py
```

Expected: PASS；payload <=2 MiB、active<=2、waiting<=8、拒绝/本地提交 <C100、heartbeat p95 <25 ms，失败后 DB/file 可安全重试且不覆盖异内容。

- [ ] **Step 5: Commit**

```bash
git add src/blrec/bili_upload/covers.py src/blrec/bili_upload/runtime.py src/blrec/web/routers/upload_covers.py tests/bili_upload/test_covers.py tests/web/test_upload_covers_routes.py
git commit -m "perf: bound upload cover file work"
```

## 最终整体验证与审计回填

以下步骤是 12 个任务后的 completion gate，不创建第 13 个实现任务。

- [ ] **逐 ID 核对 36/36 coverage**

```bash
python3 - <<'PY'
import re
from pathlib import Path

ledger = Path('docs/performance/request-audit.md').read_text()
plan = Path(
    'docs/superpowers/plans/2026-07-20-write-media-request-performance.md'
).read_text()
ids = {
    match.group(1)
    for line in ledger.splitlines()
    if line.startswith('| I-') and 'Write/media' in line
    for match in [re.match(r'\| (I-\d{3}) \|', line)]
    if match is not None
}
assert len(ids) == 36, sorted(ids)
missing = sorted(item for item in ids if item not in plan)
assert not missing, missing
assert '**唯一整项无需修改**' in plan and 'I-055' in plan
print('Write/media coverage: 36/36')
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
  tests/bili_upload/test_recording_content.py tests/bili_upload/test_active_media.py \
  tests/bili_upload/test_highlight_cut.py tests/bili_upload/test_highlights.py \
  tests/bili_upload/test_highlight_worker.py tests/bili_upload/test_covers.py \
  tests/web/test_recording_sessions_routes.py tests/web/test_highlights_routes.py \
  tests/web/test_media_response.py tests/web/test_upload_covers_routes.py \
  tests/web/test_browser_extension_routes.py tests/web/test_request_performance_middleware.py
```

- [ ] **运行前端聚焦测试、lint 与生产构建**

```bash
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless \
  --include='src/app/upload-tasks/shared/recording-session.service.spec.ts' \
  --include='src/app/upload-tasks/shared/highlight.service.spec.ts' \
  --include='src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts'
cd webapp && npx ng lint
cd webapp && npm run build
```

- [ ] **运行整仓后端回归和静态检查**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
black --check src tests
isort --check-only src tests
flake8 src tests
mypy src/blrec
python -m build
```

- [ ] **只用 fixture 写性能证据并回填台账**

创建 `docs/performance/write-media-benchmark.md`，记录每个 WM 任务的测试命令、fixture 规模、p50/p95、heartbeat、active/waiting 峰值、DB write 数、probe 数、首字节和恢复结果。将 `docs/performance/request-audit.md` 的 35 条 gap disposition 更新为具体 commit/test evidence，I-055 继续 Keep；I-104 保持 control-operation status 的 D100 行。不得记录本机/NAS 路径、账号或请求值。

允许的上线验证仅为：部署后单次读取健康/operation 状态、打开一个既有媒体并发出一个 Range 请求、提交一个 no-op task desired-state 操作。禁止在 NAS 执行并发密码、1,000 WebSocket、批量删除、批量 FFprobe、批量剪辑或媒体吞吐压测。

- [ ] **提交最终证据**

```bash
git add docs/performance/request-audit.md docs/performance/write-media-benchmark.md
git commit -m "docs: record write media performance evidence"
```

## 完成定义

- 36 条基线 Write/media 请求全部有实现测试或 I-055 的保留证据；不存在第二条整项 Keep。
- 四个最高风险放大器均有硬上界：Argon2 1+4、WebSocket 1 sender+128、删除 worker 1/quantum 128、高光 probe 2+8/16 sources/30 秒。
- 所有长控制操作先持久化 intent 并快速确认；崩溃后继续或明确 failed，不依赖 Starlette 临时 BackgroundTasks。
- 完成媒体保留 Range 并新增正确条件缓存；活动媒体保持 no-store；所有响应前 FS 工作离开 event loop。
- 批量 mutation 不超过 100 项、一次 transaction/一次 wake，58 个纯本地修改小于 2 秒。
- 所有聚焦测试、整仓回归、静态检查、Angular 测试/lint/build 和 package build 通过；最终证据不包含 NAS 压测或敏感信息。
