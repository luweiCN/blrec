# BLREC 出站请求只读审计

最初代码审计基线：`85d6585`；计划修订基线使用当前 105 路由 ledger，其中
`I-104` 为 recording detail、`I-105` 为 marker counts。本报告只读核对
`docs/performance/request-audit.md` 中 18 个 Outbound operation group，以及
disposition 含 `Outbound` 的 20 个入站触发点；未修改 production/test 代码。该证据现已纳入
版本控制，后续新增入站 ID 从 `I-106` 开始。

这里的“预算”同时指请求预算、时限预算和改动面预算。所有建议都遵守以下硬约束：

- 房间状态轮询保持当前 30–60 秒区间，默认 30 秒；不缩短。
- 开播后的 stream availability probe 保持当前 1 秒 cadence；不缩短。
- QR 上游 poll 保持当前 1 秒 cadence 和 180 秒 TTL；不缩短。
- 审核查询保持每账号 900 秒；不缩短。
- 弹幕回灌保持每账号至少 25 秒一条；不增加并行发送线。
- UPOS chunk concurrency 保持默认 2、上限 3；不提高。
- 所有节省来自复用、single-flight、去掉重复探测、绝对 deadline 和安全退避，绝不来自更高请求频率。

## 结论

当前最值得先修的是两个远端结果安全缺口，而不是缓存：

1. **UPOS completion 的未知结果会自动再次 completion。**
   `BiliProtocolClient.complete_upload()` 明确以 `idempotent=False` 执行，但
   `UposUploader._complete()` 捕获 `RemoteOutcomeUnknown` 后把 part 改回
   `uploading`，下一次 `upload_part()` 会再次调用 completion。现有
   `test_unknown_complete_result_is_deferred_and_completed_on_retry` 还固定断言
   `complete_calls == 2`。这与审计表要求的非幂等 unknown-outcome fence 冲突。
2. **弹幕发送的未知结果和进程中断会自动重发。**
   `DanmakuPublisher._retry_uncertain()` 把未知结果改回 `prepared`，
   `recover_interrupted()` 又把所有 `in_flight/unknown_outcome` 改回 `prepared`；现有测试
   明确断言第二次 `post_danmaku`。这可能产生重复弹幕，且直接增加风控暴露。

其次是四类请求放大：

- `Live.init()` 和 `Live.update_info()` 会对同一个 `getInfoByRoom` 响应做两次上游请求；
  外层 60 秒、WebApi 20 秒、BaseApi 5 秒 retry 叠加，UI 没有可证明的 10 秒逻辑 deadline。
- 一次开播可能先由 `LiveMonitor` probe play info，再由 fMP4 debounce probe 两次，最后由
  `StreamURLResolver` 再取一次 URL；复用 URL 时还会先做一个未显式关闭的 stream GET，
  随后真实 recorder 又立即 GET 同一流。
- 合集列表没有 TTL/single-flight；创建合集后后端先 list 一次，前端成功回调又 list 一次，
  而合集创建和加入分集均未进入现有 per-account write gate。
- Notification/Webhook 都会为每个事件创建 detached task 和新 ClientSession，缺少明确请求
  timeout；重试窗口分别可达 300 秒和 180 秒，事件风暴会积累任务和连接。

另有两处审计表已经落后于当前代码，实施时不能按旧 Finding 误改：

- I-057 的浏览器 `GET /qr-sessions/{id}` **不会**请求 B 站；`AccountManager.status()` 只读
  内存/SQLite。上游 poll 是 `create_qr()` 创建的每 session 一个后台任务。真实缺口是同一
  manager subject 可重复创建多个 active session，从而启动多个 1 秒 poller。
- `ReviewWatcher.run_once()` 已把 waiting jobs 按 account 分组，并由
  `test_waiting_jobs_are_grouped_into_one_read_per_account` 固定。剩余重复来自 Review 与投稿
  unknown reconciliation 各自读取同一 archive pages，而不是同一 Review 批次内按 job 重读。

## 20 个入站触发点到出站组的映射

| 入站 ID | 入口 | 涉及的出站组 | 当前同步等待点 |
| --- | --- | --- | --- |
| I-011 | `POST /tasks/actions` | Room detail、Play info、Recording transfer | 最多 100 个 action 串行；`start/update/recorder_enable` 会进入远端路径。 |
| I-018/I-019 | `POST /tasks/info`、`/{room_id}/info` | Room detail | 等待每房间详情；全量版本串行。 |
| I-022/I-023 | `POST /tasks/start`、`/{room_id}/start` | Room detail、Play info、Recording transfer、Danmaku WS | `start_task()` 先 refresh，再启动 monitor/recorder；当前直播时可同步进入取流。 |
| I-026/I-027 | `POST /tasks/recorder/enable`、`/{room_id}/recorder/enable` | Play info、Recording transfer | 当前直播时 `Recorder._do_start()` 会直接 `_start_recording()`。 |
| I-030 | `POST /tasks/{room_id}` | Room detail、Play info、Danmaku WS | `ensure_room_id()`、`RecordTask.setup()->Live.init()` 及可选 monitor/recorder 均在请求内。 |
| I-036 | `PATCH /settings/tasks/{room_id}` | Danmaku WS | header/cookie 改变时请求内 `restart_danmaku_client()`。 |
| I-042 | `POST /validation/cookie` | B 站导航验证 | 新 session，等待嵌套 WebApi retry。 |
| I-045 | `GET /update/version/latest` | Update check | 每次导航到 About 都可能直连 PyPI。 |
| I-056/I-057 | QR create/status | QR/account | create 等一次上游请求；status 当前已是纯本地。 |
| I-059 | account refresh | QR/account | 先等待 write gate，再顺序执行 OAuth/nav/可能 refresh；没有整个操作的 deadline。 |
| I-076/I-078/I-099 | category catalog/政策/高光上传任务 | Categories | fresh cache 为本地；miss/force 才等待 B 站。 |
| I-083 | collection list | Collections | 每个 UI consumer 都直接等待 B 站。 |
| I-084 | collection create | Covers、Collections | cover resolve/upload、create、post-create list 全部在 UI 请求内。 |
| I-102 | browser extension collect | Room detail、Play info、Categories | add 后紧接 start/recorder enable，可能重复刚完成的详情和 play work。 |

`start/recorder enable/header reconnect` 的 C100/accepted 边界属于既有 Write/media operation
层的职责；Outbound 实施不应另造一套不可恢复的 `BackgroundTasks`。本报告只给远端 work
规定复用和 deadline，并要求由该 operation 层承载长动作。

## 18 个 Outbound operation group 的当前状态

| Group | 当前调用链 | 缓存 / single-flight / 并发 / retry / timeout | 审计结论 |
| --- | --- | --- | --- |
| Room status | `Application._setup_live_status_monitor` → `LiveStatusCoordinator.poll_once` → `_resolve_uid_mappings` / `_poll_uids` → `BatchStatusClient.fetch`，missing/stale 才 `_confirm` | interval 30–60 秒；batch ≤29；`_poll_lock`；batch 串行；同 loader 的 room-id mapping 一次批量读取；fallback 按 room single-flight、600 秒 cooldown；breaker/canary；共享匿名 session total 10 秒 | **保留。** 当前 58-room integration 已断言每轮最多 2 个 batch。不得提高 cadence 或 fallback 频率。 |
| Room detail | task/extension → `RecordTask.update_info` / `setup` → `Live.update_info` / `init` → `get_room_info` + `get_user_info` | 两个 projection 都先调同一 `WebApi.get_info_by_room`；无 same-room single-flight；`RecordTaskManager.add_task` 还把整个 setup 放进 60 秒 retry，内层 `Live` 60 秒、`WebApi` 20 秒、`BaseApi` 5 秒，单 request total 10 秒；web→app→legacy fallback | **需改。** 成功路径每 logical refresh 至少 2 个重叠 B 请求，失败时可重复整个 task setup，且 UI deadline 不成立。 |
| Play info | `LiveMonitor._check_if_stream_available` → `Live.get_live_streams`; fMP4 时 `StreamRecorder._wait_fmp4_stream`; 最后 `StreamURLResolver._solve` | availability 每 1 秒一次；fMP4 要连续两次成功；三段之间不传递 parsed streams/selected URL；resolver 只在自身保存 URL | **需改复用，不改 cadence/debounce。** 成功 resolution 应在 monitor→recorder→resolver 间交接。 |
| Recording transfer | selected URL → `StreamURLResolver._can_resue_url` → `StreamFetcher` 或 `PlaylistFetcher` / `SegmentFetcher` | 共用 recorder 的 `requests.Session`；但 reuse 先额外 `GET(stream=True, timeout=3)`，未显式 close，随后真实 transfer 再 GET；FLV read 3 秒，playlist 3 秒且 8 秒 retry window，segment 5 秒且 60 秒 retry/integrity check | **去掉额外 probe GET；保留传输语义。** HLS init-section 两次一致性读取及 segment 长 retry 是完整性逻辑，不是可删的重复请求。 |
| Danmaku WebSocket | `DanmakuConnection.start` → authenticated `DanmakuClient.start` → anonymous fallback；断线走 `DanmakuClient._retry` | connect/auth 各 5 秒；connect retry window 30 秒；认证总尝试 ≤6、同 credential ≤2；heartbeat/receive timeout 已有；默认 reconnect 60 次、线性 delay；batch mode 不运行 legacy 10 分钟 status poll | **保留。** 只补连接/认证/fallback 指标；不缩短 heartbeat/reconnect/status cadence。header change 的同步 UI 等待交给 operation 层。 |
| UPOS | `UploadCoordinator._process`（持 account gate）→ `UposUploader.upload_part` → preupload/init → chunks → completion | transport session 按 `(purpose, source)` 池化，request total 30 秒；preupload admission 1/min 自适应到 5/min，rate-limit cooldown 最长 15 分钟；chunk 默认并发 2、上限 3、最多 3 次，但 transport failure 立即循环；completion non-idempotent | **P0 安全缺口。** completion unknown 会被再次调用；chunk retry 未抖动且未利用 `Retry-After`。池化、admission、chunk 并发上限保留。 |
| Submission | `UploadCoordinator._process` → `submit_archive`; unknown 时 `_reconcile_unknown_submission` → `_find_remote_submission` | account gate 覆盖 upload+submit；request total 30 秒；DefinitelyNotSent 指数退避 ≤300 秒；rate limit 60–900 秒；unknown 只 list/view reconciliation，不盲目 resubmit | **保留 unknown fence。** `_find_remote_submission` 最多 20 list pages，再对所有同标题候选串行 view，缺整个 reconcile cycle deadline/候选上限。 |
| Review | runtime broad loop → `ReviewWatcher.run_once` → per-account `_load_archives` → per-approved-job `archive_view` | 已按 account 合并；900 秒 `_next_poll_at`；list page size 50、最多 20 页；detail 串行；无 archive page 共享缓存和 absolute cycle deadline | **部分已完成。** 保留 900 秒与单账号顺序读取；与 submission reconciliation 共享短期只读 snapshot，并给 cycle 加 deadline。不得增加审核频率。 |
| Comments | broad loop → `CommentPublisher.run_once`（一次 claim）→ account gate → add/pin 或 list/detail reconciliation | WBI key 600 秒 cache + lock；DefinitelyNotSent 指数退避 ≤300 秒；text unknown 先 read reconciliation，pin unknown 直接人工；runtime 一次 comment 后等待 5 秒；request total 30 秒 | **保留。** 当前 unknown fence、每轮一项和 5 秒 action delay 都正确；本阶段不拆成更高并发 worker。 |
| Danmaku posting | broad loop → `DanmakuPublisher.run_once` → per-account breaker/gate → `post_danmaku` | interval `max(25, config)`；per-account fairness、rate/daily breaker；但 RemoteOutcomeUnknown 和 startup `in_flight/unknown_outcome` 都自动回 `prepared` | **P0 安全缺口。** 未知结果必须停在 unknown/paused，不能自动重发；DefinitelyNotSent 才可按原 cadence retry。 |
| Collections | dialog/task labels → route → `CollectionManager.list/create`; review approved → `CollectionPublisher.create` → add episode | list 无 cache/lock；create 为 cover upload→create→list，UI success 又 list；create unknown 不盲重试；publisher 中断转 failed/manual；manager/publisher 都未注入 account gate；每 request total 30 秒 | **需改。** per-account 60 秒 TTL/single-flight；create 后只允许一次 catalog refresh；所有写进入 gate 且 UI gate wait 有界。 |
| Categories | policies/highlight/extension → `UploadCategoryCatalog.list` → `archive_pre` | SQLite 24 小时 cache，credential-version scoped；per-account lock 和 double-check；失败返回 stale；正常 miss single-flight；并发 `force_refresh=True` waiter 会依次再刷 | **核心保留。** TTL、stale、credential scope 均正确；补 normal/forced 并发 request-count guard，不能缩短 TTL。 |
| Covers | custom asset → `CoverResolver.remote_url`; live cover → `live_url`; legacy record cover → `CoverDownloader._save_cover` | custom `(asset, account)` 持久 cache + lock；live transient 无 coalescing；remote download 每次新 session、total 30 秒、2 MiB/trusted HTTPS/no redirect；legacy 每个视频分段先 `update_room_info` 再 GET，下载后才 SHA1 dedup，GET retry 3 次 | **需改复用。** 保留 custom cache/安全限制；legacy 用现有 ROOM_CHANGE metadata，并按 broadcast+URL coalesce，避免每分段详情+相同封面 GET。 |
| QR/account | `create_qr` → one background `_poll` per session；`status` local；refresh → `AccountWriteGate` → identity/renewal | poll 1 秒、TTL 180 秒；单 session 只有一个 poller；同 subject 无 active-session cap/create single-flight；protocol request total 30 秒；refresh unknown fence 正确；upload 可长时间持有同 gate | **修正旧审计并收口。** status 保留纯本地；同 subject 最多一个 nonterminal session/poller；UI refresh 不可无限等 upload gate，且整个 renewal 要有 deadline。 |
| Notifications | EventCenter/ExceptionCenter → `MessageNotifier`; operational scan → `OperationalNotificationCenter._dispatch` | legacy 每 event detached task，retry window 300 秒；各 HTTP provider 每消息新 ClientSession、无 timeout；SMTP 无 timeout；Pushplus 默认 HTTP；operational dispatch 直接 gather，并延迟 upload loop 下一轮 | **需改。** 有界队列、池化 client、HTTPS、请求/交付 deadline、shutdown drain；保留 operational state-transition 去重。 |
| Webhook | event/exception → 每个匹配 webhook → `WebHookEmitter._send_request` | 最多 50 webhook；每 delivery detached task；每 attempt 新 ClientSession；无 timeout；所有异常 retry，window 180 秒 | **需改。** 有界 delivery worker、共享 session、只重试 transient、显式 close/drain。 |
| Network probe | explicit `POST /network/probe` → `NetworkRouteManager.probe` → 每 interface `_probe_interface` | 单 interface 一次 B 站 zone HTTPS GET；all-interface 用 gather；ClientTimeout total 8 秒；结果缓存；realtime 只读缓存，不自动 probe | **保留。** 用户显式动作才触发；不增加自动探测或 cadence。 |
| Update check | About → `UpdateService` → route → `get_latest_version_string` → `PypiApi` | 每次新 ClientSession；单 request timeout 10 秒，retry window 5 秒；无 TTL、single-flight、stale fallback | **需改。** application-lifetime client + 30 分钟 cache/single-flight + stale-if-error。 |

## 会让 UI 等待或扩大 B 站请求的路径

| 路径 | 最坏问题 | 处理边界 |
| --- | --- | --- |
| task info/start/add 与 browser collect | 重叠 room/anchor request、嵌套 retry；add 后立即 start 又 refresh；batch 串行 | O-03/O-04 先去重并限 10 秒；长 lifecycle 由 Write/media operation 返回 accepted。 |
| recorder enable/start（当前直播） | UI 内进入 fMP4 wait、重复 play resolution 和额外 stream GET | O-05 交接成功 resolution；长 recorder start 由 operation 层承载。 |
| task header/cookie PATCH | UI 内等待 WS stop/start，单次 connect 自身可 retry 30 秒 | 不改 WS cadence；只把 reconnect 移到可观察 operation。 |
| cookie validation | 新 session + WebApi 嵌套 retry，没有 one-operation deadline | O-15，不缓存 cookie。 |
| collection list/create | 多 consumer 重复 list；create 后后端/前端连续两次 list；可能与上传账号写并发 | O-08/O-09。 |
| QR create | 每次 create 都可新增 1 秒上游 poller | O-11，同 subject 复用 active session；status 继续纯本地。 |
| account refresh | UI 可能等待被整个 UPOS job 持有的 account gate，然后还要做多次 30 秒请求 | O-09，gate acquisition 快速返回 busy，renewal 加 absolute deadline；不削弱 gate。 |
| update/latest | 每次 About navigation 直连 PyPI | O-14。 |
| network probe | 显式动作最多等待 8 秒 | 保留；这是用户要求的 probe，不后台放大。 |

## 最小独立实施任务

任务按安全性和依赖排序。每项都可单独 review/回滚；“改动预算”是建议的最大 production/test
文件面，超过时应重新拆分。

### O-01（P0）冻结 UPOS completion 未知结果

- **Production：**
  `src/blrec/bili_upload/upos.py:UposUploader.upload_part,_complete`。unknown 时持久化
  `upload_state='unknown_outcome'`，不得在入口归一化回 `uploading`；沿用现有 task action 对
  unknown part 的 retry 拒绝。`DefinitelyNotSent` 仍可安全回到 prepared/uploading。
- **Tests：**
  修改 `tests/bili_upload/test_upos.py:test_unknown_complete_result_is_deferred_and_completed_on_retry`
  为“未知后不会再次 complete”，断言两轮 `complete_calls == 1` 和 part 保持 unknown；保留/
  加强 `tests/bili_upload/test_task_actions.py:test_retry_refuses_unknown_remote_outcomes`。
- **请求预算：** 每个 UPOS session 的 completion 最多 1 次，除非 transport 明确
  `DefinitelyNotSent`；`RemoteOutcomeUnknown` 自动重试次数必须为 0。
- **改动预算：** 1 个 production 文件 + 2 个既有 test 文件；无需 migration。

### O-02（P0）冻结弹幕 unknown/in-flight，禁止自动重发

- **Production：**
  `src/blrec/bili_upload/danmaku_publish.py:recover_interrupted,_process,_retry_uncertain`
  （将 uncertain 路径改为持久 `unknown_outcome` 并暂停 branch）；
  `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.html` 显示已有
  `danmakuUnknown/unknownDanmakuItems`，不提供未经确认的一键自动重发。
- **Tests：**
  改写 `tests/bili_upload/test_danmaku_publish.py:test_unknown_outcome_is_requeued_automatically`、
  `test_crash_interrupted_in_flight_item_is_requeued`、
  `test_startup_recovery_requeues_every_interrupted_item`，断言零次后续 post；更新
  `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts` 的 unknown
  展示断言。
- **请求预算：** unknown/in-flight 自动 post 次数 0；只有 `DefinitelyNotSent` 可按原
  ≥25 秒 interval、最多现有 5 次 safe retry；不得新增并行发送线。
- **改动预算：** 2 个 production 文件 + 2 个 test 文件。

### O-03（P1）把 room/anchor 刷新合成一个响应并设逻辑 deadline

- **Production：**
  `src/blrec/bili/live.py:Live.init,update_info,update_room_info,update_user_info,get_room_info`、
  `src/blrec/bili/live.py:Live.get_user_info,_get_room_info_via_api`。新增一个内部 composite refresh：一次
  `getInfoByRoom` payload 同时构建 `RoomInfo` 与 `UserInfo`；同 `Live` 实例 concurrent
  refresh single-flight。移除/绕开 60→20→5 秒叠加的外层 retry，并以一个 10 秒
  absolute timeout 包住整个 logical refresh。web→app→legacy fallback 只在失败后进入。
- **Tests：**
  新增 `tests/bili/test_live_info_refresh.py`：成功路径一次 upstream call 同时更新两个
  projection；10 个 concurrent caller 仍一次；web 失败才 app fallback；logical timeout
  ≤10 秒；取消不吞掉。更新受影响的 `tests/task/test_live_connection_controller.py`。
- **请求预算：** success 为 1 个 room-detail request/logical refresh；同 room 同时在途为 1；
  fallback 总请求数 ≤3 且总 wall time ≤10 秒。这里不引入通用长 TTL。
- **改动预算：** 1–2 个 production 文件 + 2 个 test 文件。

### O-04（P1）复用 task 初始化快照并给批量房间动作小并发

- **Production：**
  `src/blrec/task/task_manager.py:RecordTaskManager.add_task,start_task,start_all_tasks`、
  `src/blrec/task/task_manager.py:RecordTaskManager.update_all_task_infos`；
  `src/blrec/task/task.py:RecordTask.setup,update_info`；
  `src/blrec/application.py:add_task,start_task,start_all_tasks,update_all_task_infos`；
  `src/blrec/web/routers/tasks.py:run_task_batch_action,update_all_task_infos,start_all_tasks`；
  `src/blrec/web/routers/browser_extension.py:collect_room`。让同一
  add→start/collect operation 显式传递刚完成的初始化 revision，而不是再次 refresh；只对
  room-disjoint 的 refresh/start 使用固定上限 2 的 bounded concurrency，其他 action 继续串行；
  移除 `RecordTaskManager.add_task` 对整个 setup 的 60 秒重放，远端 retry 只留在 O-03 的
  10 秒 composite budget 内；结果仍按 room 稳定排序，settings 仍只批量持久化一次。
- **Tests：**
  新增 `tests/task/test_task_manager_outbound.py` 断言 add→start 每 room 只有一次 composite
  detail、batch max in-flight=2、单 room 失败不重复其他 room；更新
  `tests/web/test_tasks_routes.py` 与 `tests/web/test_browser_extension_routes.py` 的 request-count
  和 partial-result 断言。
- **请求预算：** 每 room 每 logical action ≤1 composite detail；add→立即 start 仍为 1；
  detail 并发 ≤2；short-id normalization 仍允许 1 次独立 `room_init`，但不得重复。
  task setup 失败不得自动重放整个 operation。不得通过缩短 Live status interval 来“加速”。
- **改动预算：** 4 个 production 文件 + 3 个 test 文件。accepted/operation 状态复用
  Write/media 层，不在本任务另建 detached task。

### O-05（P1）在 stream probe、fMP4 debounce 与 recorder 间交接 resolution

- **Production：**
  `src/blrec/bili/live.py:get_play_infos,get_live_streams,get_live_stream_url`；
  `src/blrec/bili/live_monitor.py:_check_if_stream_available`；
  `src/blrec/core/stream_recorder.py:_do_start,_wait_fmp4_stream`；
  `src/blrec/core/operators/stream_url_resolver.py:_solve,_can_resue_url`。交接 parsed streams/
  selected URL 和参数 identity；保留 fMP4 连续两次成功的 debounce，但第二次成功 URL 直接给
  resolver。删除 `_can_resue_url` 的预验证 GET；真实 `StreamFetcher/PlaylistFetcher` GET
  即验证，失败仍走现有 resolver rotate/retry。
- **Tests：**
  扩充 `tests/bili/test_live_stream_url.py`；新增
  `tests/core/test_stream_request_reuse.py`，以 fake play API/requests session 断言请求数、参数
  不匹配不复用、真实 transfer 失败后才重新 resolve、response 无泄漏。
- **请求预算：** availability/fMP4 等待在首次得到目标格式前，每 tick 仍至多 1 次 play-info；
  monitor response 若已含目标 fMP4，必须计作第一次成功；首次目标格式成功后只再允许 1 次
  debounce confirmation；resolver 额外 play-info=0，validation stream GET=0。1 秒 cadence 不变。
- **改动预算：** 4 个 production 文件 + 2 个 test 文件。

### O-06（P1）统一 B 站协议 timeout/backoff 元数据，不改变业务 cadence

- **Production：**
  `src/blrec/bili_upload/protocol.py:AiohttpProtocolTransport.__init__,send`、
  `src/blrec/bili_upload/protocol.py:BiliProtocolClient._execute`；
  `src/blrec/bili_upload/errors.py:BiliApiError`；
  `src/blrec/bili_upload/upos.py:_upload_chunk,_complete`；
  `src/blrec/bili_upload/upload.py:UploadCoordinator._process`。保留 total 30 秒，同时显式配置
  connect/sock_connect/sock_read；把 HTTP `Retry-After` 安全解析到 `BiliApiError`（设上限）；
  idempotent chunk 的最多 3 次 transport retry 加 jitter，不立即 burst。UPOS/submission 的
  server backoff 用持久 defer/release，不在持 gate worker 内长 sleep。
- **Tests：**
  `tests/bili_upload/test_protocol_matrix.py` 覆盖 timeout/headers-sent taxonomy/Retry-After；
  `tests/bili_upload/test_upos.py` 覆盖 jitter/defer 且 max attempts 不变；
  `tests/bili_upload/test_upload.py` 覆盖 submission 采用 server backoff 但不盲重投。
- **请求预算：** 单 request total 30 秒、connect ≤5 秒；chunk 总 attempts ≤3；
  `Retry-After` 采纳范围 1–900 秒；completion/submission/danmaku 等非幂等 unknown 仍为 0 次盲重试。
- **改动预算：** 4 个 production 文件 + 3 个 test 文件。

### O-07（P1）共享 archive 只读 snapshot，并限制 reconcile cycle

- **Production：**
  新增 `src/blrec/bili_upload/archive_reads.py`（按 account、credential version、query/page
  single-flight 的只读 snapshot）；接入
  `src/blrec/bili_upload/review.py:ReviewWatcher._load_archives,_process_job`、
  `src/blrec/bili_upload/upload.py:UploadCoordinator._find_remote_submission` 和
  `src/blrec/bili_upload/runtime.py`。保持同账号 detail 顺序执行，不把拆 worker 变成额外并发线。
- **Tests：**
  保留 `tests/bili_upload/test_review.py:test_waiting_jobs_are_grouped_into_one_read_per_account`；
  新增同 cycle Review+reconciliation 复用、60 秒 timeout、重复 page 中止测试；
  `tests/bili_upload/test_upload.py` 增加同标题候选上限和“超限后仍不 resubmit”。
- **请求预算：** 相同 account/query/page 30 秒内最多 1 次；Review cadence 仍 900 秒；每次 list
  ≤20 页（现有上限），archive detail 候选 ≤10 且并发 1；整个 account read cycle ≤60 秒。
- **改动预算：** 4 个 production 文件 + 2 个既有 test 文件（可加 1 个 focused 新 test）。

### O-08（P1）给合集列表加短 TTL/single-flight，消除创建后的双 list

- **Production：**
  `src/blrec/bili_upload/collections.py:CollectionManager.list,create` 增加 per-account cache/
  in-flight task；`src/blrec/web/routers/bili_collections.py:list_bili_collections` 增加明确
  `forceRefresh`；
  `webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.service.ts:collections` 和
  `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.ts:loadCollections,createCollection`
  让手动刷新才 bypass cache，创建成功直接 merge result，不再立即再 GET；task-list labels
  共用后端 cache。
- **Tests：**
  `tests/bili_upload/test_collections.py` 覆盖 20 个 concurrent list 只有 1 次、TTL、stale-on-error、
  create 后 cache invalidation/refresh；`tests/web/test_bili_collections_routes.py` 覆盖 force 参数；
  `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.spec.ts` 与
  `webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.service.spec.ts` 断言 create 后没有
  第二次 list。
- **请求预算：** fresh TTL 60 秒、stale-if-error 最长 15 分钟；同账号最多 1 个在途 list；
  create 流程 cover upload ≤1、create ≤1、post-create list 总计 ≤1（后端和前端合计）。
- **改动预算：** 4 个 production 文件 + 4 个 focused test 文件。

### O-09（P1）把合集写纳入 account gate，并让 UI gate wait 有界

- **Production：**
  `src/blrec/bili_upload/accounts.py:_PerAccountGate,AccountWriteGate`、
  `src/blrec/bili_upload/accounts.py:AccountManager.check_account_renewal` 增加可选 admission
  timeout 与明确 `AccountWriteBusy`；
  UI admission 同时覆盖 `_auth_failure_lock` 与 per-account gate 的等待，不能只给后者计时；
  `src/blrec/bili_upload/collections.py:CollectionManager.create`、
  `src/blrec/bili_upload/collection_publish.py:CollectionPublisher.create` 注入/使用同一 gate；
  `src/blrec/bili_upload/runtime.py` 传入 gate；
  `src/blrec/web/routers/bili_accounts.py:refresh_account`、
  `src/blrec/web/routers/bili_collections.py:create_bili_collection` 将 UI busy 映射为
  409/可重试响应。
- **Tests：**
  `tests/bili_upload/test_accounts.py` 覆盖 upload 持 gate 时 UI 250ms 内返回 busy、worker 仍可等待、
  credential version 重检不变；`tests/bili_upload/test_collections.py` /
  `tests/bili_upload/test_collection_publish.py` 覆盖同账号写 concurrency=1 和 unknown 不重试；
  `tests/web/test_bili_accounts_routes.py` 与 `tests/web/test_bili_collections_routes.py` 覆盖 409。
- **请求预算：** 每账号 remote write concurrency=1；UI gate wait ≤250ms；collection create 和
  account renewal 各自 absolute operation deadline ≤60 秒；upload 的现有 gate/fence 不削弱。
- **改动预算：** 6 个 production 文件 + 5 个 focused test 文件。若超过，先拆“gate primitive”
  与“collection consumers”两个连续 commit，不混入协议改动。

### O-10（P2）复用 live metadata/封面下载并池化 downloader

- **Production：**
  `src/blrec/core/cover_downloader.py:_save_cover,_fetch_cover` 不再每完成一个分段都主动
  `Live.update_room_info()`，使用 `LiveMonitor` 在 LIVE/ROOM_CHANGE 已维护的 metadata；按
  broadcast identity + cover URL single-flight/dedup，在下载前判重；若进入 broadcast 时没有
  可用 metadata，最多做一次 O-03 composite fallback，而不是每 part 刷新；
  `src/blrec/bili_upload/covers.py:CoverResolver.live_url,_download` 复用生命周期 client，并对同一
  live source/account coalesce。custom asset 的现有持久 cache 原样保留。
- **Tests：**
  新增 `tests/core/test_cover_downloader.py`：同 broadcast 同 URL 多 part 只有 1 GET/0 额外详情，
  ROOM_CHANGE 新 URL 才再 GET；扩充 `tests/bili_upload/test_covers.py` 的 concurrent live URL、
  2 MiB、trusted HTTPS/no redirect 和 close lifecycle。
- **请求预算：** 每 broadcast+URL cover GET ≤1；cover saver fallback room-detail≤1/broadcast；
  同 live source 同账号 upload 在途=1；download total ≤30 秒、size ≤2 MiB。
- **改动预算：** 2 个 production 文件 + 2 个 test 文件。

### O-11（P2）限制同一管理主体的 active QR session

- **Production：**
  `src/blrec/bili_upload/accounts.py:AccountManager.create_qr,status,_poll,cancel` 增加
  manager-subject keyed create lock/single-flight；已有 nonterminal session 时返回同一 view，
  只有 cancel/terminal 后才能创建新 QR。不要让 browser GET 驱动 upstream poll。
- **Tests：**
  在 `tests/bili_upload/test_accounts.py` 保留
  `test_one_poller_expires_after_180_seconds`，新增 20 个 concurrent create 只有一次 create_qr、
  一个 runtime task、max concurrent poller=1；
  `tests/web/test_bili_accounts_routes.py` 固定 repeated status 不增加 protocol calls。
- **请求预算：** 每 manager subject nonterminal session≤1、poller≤1、create upstream in-flight≤1；
  poll interval 1 秒和 TTL 180 秒原样保留。
- **改动预算：** 1 个 production 文件 + 2 个 test 文件。

### O-12（P1）把 Notification 改为有界、可关闭的 dispatcher

- **Production：**
  `src/blrec/notification/providers.py` 为 HTTP providers 注入共享 session/timeout，SMTP 设置
  timeout，Pushplus 改 HTTPS；`src/blrec/notification/notifiers.py:MessageNotifier._send_message`
  改为 enqueue；`src/blrec/notification/operational.py:OperationalNotificationCenter._dispatch`
  也 enqueue 而不阻塞 upload loop；`src/blrec/application.py:_setup_notifiers`、
  `src/blrec/application.py:_destroy_notifiers,_exit` 管理 start/drain/close。保留 operational
  state-transition suppression。
- **Tests：**
  新增 `tests/notification/test_providers.py`、`tests/notification/test_notifiers.py`；扩充
  `tests/notification/test_operational.py`，覆盖 queue 满、同 channel 顺序、shutdown、timeout、
  4xx 不重试、5xx/transport 有界 retry、HTTPS URL。
- **请求预算：** queue capacity 100；每 channel concurrency=1；request total ≤10 秒；每 delivery
  ≤3 attempts 且总 deadline ≤60 秒；非 transient 4xx attempts=1。队列满要合并同 key 或明确
  drop+metric，不能创建旁路 detached task：operational item 按 `(event, object_key, channel)`
  latest-wins；无法合并的 legacy item 拒绝 newest，并增加可观测 drop counter。
- **改动预算：** 4 个 production 文件 + 3 个 test 文件。

### O-13（P1）把 Webhook 改为有界 delivery worker

- **Production：**
  `src/blrec/webhook/webhook_emitter.py:WebHookEmitter._send_request,_send_request_async,_post`
  改为共享 session + bounded queue，按 URL 保序；
  `src/blrec/application.py:_setup_webhooks,_destroy_webhooks,_exit` 显式 start/drain/close。
- **Tests：**
  新增 `tests/webhook/test_webhook_emitter.py`，覆盖 50 webhook/event storm 的 queue 上限、
  per-URL concurrency、10 秒 timeout、4xx no retry、5xx/transport max3+jitter、disable/exit 后无
  pending task 和 session leak；补 `tests/web/test_main_lifecycle.py` teardown 断言。
- **请求预算：** queue capacity 100；global concurrency≤4、同 URL≤1；request total≤10 秒；
  delivery deadline≤60 秒、attempts≤3；queue 满时拒绝 newest 并记录 URL-redacted drop counter，
  保留已排队项顺序；不再使用 180 秒无差别 retry window。
- **改动预算：** 2 个 production 文件 + 2 个 test 文件。

### O-14（P2）缓存 Update check

- **Production：**
  `src/blrec/update/helpers.py:get_project_metadata,get_latest_version_string` 收口为 application-lifetime
  client/cache；`src/blrec/update/api.py:PypiApi._get` 接受剩余 deadline；
  `src/blrec/web/routers/update.py:get_latest_version` 使用该实例；
  `src/blrec/application.py` 管理 session lifecycle。不要缓存不同 project/version 为同一 key。
- **Tests：**
  新增 `tests/update/test_helpers.py` 覆盖 concurrent single-flight、TTL、stale、404、close；
  `tests/web/test_update_routes.py` 覆盖 error 返回 stale/空值而不是长时间挂起。
- **请求预算：** 每 project 30 分钟最多 1 次 PyPI request；同 key in-flight=1；错误可返回最长
  24 小时 stale；logical total deadline≤10 秒。该 TTL 在 15–60 分钟审计预算内。
- **改动预算：** 4 个 production 文件 + 2 个 test 文件。

### O-15（P2）池化 cookie validation，但绝不缓存凭据

- **Production：**
  `src/blrec/bili/helpers.py:get_nav` 接受 application-owned anonymous `bili_api` client
  （`DummyCookieJar`，cookie 只放显式 request header），不再每请求创建 ClientSession；
  `src/blrec/web/routers/validation.py:validate_cookie` 注入该 client，并以单一 absolute deadline
  包住 logical validation。cookie 不进入共享 jar、cache、metric label 或日志。
- **Tests：**
  新增 `tests/bili/test_helpers.py` 与 `tests/web/test_validation_routes.py`，断言 session 复用、
  成功一次 upstream、deadline、取消传播、cookie 不出现在异常/日志；若保留 GET retry，只允许
  在 10 秒 deadline 内。
- **请求预算：** success 1 次 nav request；同 validation total≤10 秒；并发由共享 connector
  上限控制；cache TTL=0。
- **改动预算：** 2 个 production 文件 + 2 个 test 文件。

## 明确保留、不应“优化”掉的行为

1. `LiveStatusCoordinator` 的 30–60 秒区间、batch size 29、串行 batch、breaker/canary、
   one-stale-confirmation 和 600 秒 fallback cooldown；
   `tests/bili/test_live_status_coordinator.py`、`tests/integration/test_batch_live_monitor.py`、
   `tests/task/test_live_connection_controller.py` 已是 request-count guard。
2. `_resolve_uid_mappings()` 按 loader 汇总所有 unresolved room IDs，再一次调用
   `fetch_uid_mappings(requested_room_ids)`；这是正确的批量房间信息复用。
3. Danmaku WebSocket 的 5 秒 handshake、30 秒 connect retry、credential 尝试上限、匿名
   fallback、heartbeat 和现有 reconnect cadence。只补指标，不加速。
4. HLS init-section 的“两次相同才接受”、segment size/CRC 校验、route rotation 与长传输 retry；
   这些是数据完整性保障。只删除 recorder 前的额外 validation GET。
5. UPOS transport session pooling、preupload admission 1→5/min、最长 15 分钟 cooldown、chunk
   默认并发 2/上限 3、file identity 校验和固定 upload route。
6. Submission/edit 的 `DefinitelyNotSent` 与 `RemoteOutcomeUnknown` 分类、投稿远端 reconciliation、
   edit/transcode repair 的人工确认 fence；现有 `test_upload.py` / `test_task_actions.py` 已证明
   不盲目重投。
7. Review 的 per-account grouping 和 900 秒 cadence；Comments 的 WBI 10 分钟 cache、一次一项、
   5 秒 action delay、unknown comment reconciliation、unknown pin 人工暂停。
8. 弹幕的 ≥25 秒 interval、per-account breaker、公平性、频率过快/日限额退避；O-02 只修
   unknown 重发，不动频率。
9. Categories 的 24 小时 credential-scoped cache、per-account lock、stale fallback；
   Network probe 的显式触发、每 interface 一次 GET 和 8 秒 timeout。
10. Cover custom asset 的 `(asset_id, account_id)` 持久 remote URL cache，以及 live cover 的
    trusted HTTPS、no redirect、2 MiB 上限。
11. QR status 的纯本地读取、单 session 一个 poller、1 秒 cadence、180 秒 TTL；account refresh
    的 write gate 和 unknown-outcome fence。
12. Operational notification 的状态变化去重；队列化不能把同一 unhealthy state 每次 scan
    都重新发一遍。

## 建议实施顺序与总体验收

先独立落 O-01、O-02，消除会重复非幂等写的 P0 风险；随后 O-03→O-05 降低录制控制链的
B 站请求数和 UI 等待；O-06 提供统一 backoff 元数据；O-07→O-11 收口 archive/合集/账号/
封面；O-12/O-13 解决无界任务；最后 O-14/O-15 处理低频 UI 外部调用。

整阶段的总体验收不是“请求更快更多”，而是以下不变量：

- 正常一次 room refresh 的 `getInfoByRoom` 从 2 降为 1；同房间并发 refresh 仍为 1。
- 一次成功开播 resolution 不被 monitor、fMP4 wait、resolver 重复获取；无预验证 stream GET。
- UPOS completion unknown 和 danmaku post unknown 的后续自动写请求都为 0。
- collection list、archive pages、update metadata 的 concurrent caller 各自 single-flight。
- 所有 UI-bound/notification/webhook 外部操作有 absolute deadline；所有 session/task 可在退出时
  被 close/drain。
- 房间状态、stream probe、QR、审核、评论、弹幕的现有 cadence 均不缩短，UPOS chunk 并发不提高。
