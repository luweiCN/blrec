# Request Performance Audit

Audited baseline: `57361f7` (2026-07-20); hot-read evidence is current through
`2c7b933`. The inventory is generated from the registered FastAPI routes, then
checked against each router handler. It contains exactly 107 inbound endpoints:
105 HTTP endpoints and 2 WebSocket endpoints.

This ledger records only normalized route templates and repository-relative source
evidence. It intentionally contains no credentials, request/query values, runtime
filesystem locations, or concrete account identifiers.

## Legend and budgets

- IO classes: `R` database read, `W` database or configuration write, `F` filesystem
  IO, `P` subprocess/system-command work, `X` external network IO, and `S` a streaming
  or long-lived connection. `—` means the handler adds no business IO beyond memory.
- The IO column describes handler-specific work. Protected routes also perform the
  common authentication-store read. That cross-cutting cost is represented by the
  auth rows and the 60-second activity-write harness instead of being repeated on
  every row.
- `M25`: warm in-memory GET p95 below 25 ms.
- `D100`: ordinary local-database GET p95 below 100 ms.
- `L150`: 20-row list p95 below 150 ms, constant query count, and zero per-row file
  `stat` calls.
- `T150`: media first byte p95 below 150 ms; sustained streams are not judged by total
  connection time.
- `C100`: one local control mutation acknowledges within 100 ms. Pure local batches
  additionally target 58 changes in under 2 seconds.
- `EXT`: the operation must have an explicit connect/read/total deadline, bounded
  retry/concurrency, and must not increase the established upstream request rate.
- `STR`: handshake/first event is measured separately; duration, events/bytes,
  backlog, and disconnect reason are connection metrics.

Disposition values refer to the completed foundation (`Foundation`), the independent
hot-read plan (`Hot read`), later write/media work (`Write/media`), later outbound work
(`Outbound`), or evidence-backed retention of current behavior (`Keep`). Destructive
operations are verified with fakes and focused manager tests, not bulk-tested against
the NAS.

## Inbound endpoints

### `auth` router (9)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-001 | GET | `/api/v1/auth/status` | auth | R | D100 | `src/blrec/web/routers/auth.py:auth_status` -> `authenticate_session`, `is_initialized` | Session validation remains per request; repeated activity persistence was the shared write hotspot. | Foundation: activity writes are limited to once per 60 seconds. |
| I-002 | POST | `/api/v1/auth/setup` | auth | R,W | C100 except password hash | `src/blrec/web/routers/auth.py:setup` -> `verify_bootstrap_attempt`, `initialize` | Password hashing is synchronous in an async handler. | Write/media: move hash work to a bounded worker without weakening rate limits. |
| I-003 | POST | `/api/v1/auth/login` | auth | R,W | C100 except password hash | `src/blrec/web/routers/auth.py:login` -> `AdminAuthStore.login` | Password verification can block the event loop; failure throttling must remain. | Write/media: offload the hash and keep the existing security fences. |
| I-004 | GET | `/api/v1/auth/session` | auth | — | M25 | `src/blrec/web/routers/auth.py:session` -> request-state credentials | Handler is a memory projection; middleware authentication supplies the store read. | Foundation/Keep: throttled activity write plus request metrics. |
| I-005 | POST | `/api/v1/auth/logout` | auth | W | C100 | `src/blrec/web/routers/auth.py:logout` -> `AdminAuthStore.logout` | Single fenced revocation write; no hot-loop behavior. | Keep; cover with auth regression tests. |
| I-006 | POST | `/api/v1/auth/change-password` | auth | R,W | C100 except password hashes | `src/blrec/web/routers/auth.py:change_password` -> `AdminAuthStore.change_password` | Verify/hash and session revocation are synchronous but security-critical. | Write/media: bounded hash worker; preserve global revocation. |
| I-007 | GET | `/api/v1/auth/extensions` | auth | R | D100 | `src/blrec/web/routers/auth.py:list_extension_tokens` | Bounded local list; no confirmed endpoint-specific hotspot. | Keep; observe database calls. |
| I-008 | DELETE | `/api/v1/auth/extensions/{token_id}` | auth | W | C100 | `src/blrec/web/routers/auth.py:revoke_extension_token` | Single local revocation write. | Keep; destructive-path test only. |
| I-009 | POST | `/api/v1/auth/recover` | auth | R,W | C100 except password hash | `src/blrec/web/routers/auth.py:recover` -> bootstrap verification, password reset | Password hashing is synchronous; reset must still revoke all sessions. | Write/media: bounded hash worker with revocation regression tests. |

### `tasks` router (23)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-010 | GET | `/api/v1/tasks/data` | tasks | — | M25 | `src/blrec/web/routers/tasks.py:get_task_data` -> in-memory task iterator | In-memory projection is already bounded by pagination. | Keep; request metrics guard p95. |
| I-011 | POST | `/api/v1/tasks/actions` | tasks | W,F,X | C100 local; EXT remote | `src/blrec/web/routers/tasks.py:run_task_batch_action` -> per-task application calls | Up to 100 actions execute serially and mix local, file, and remote work. | Write/media and Outbound: batch once, use bounded concurrency only where safe. |
| I-012 | GET | `/api/v1/tasks/{room_id}/data` | tasks | — | M25 | `src/blrec/web/routers/tasks.py:get_one_task_data` -> task memory | No endpoint-specific IO hotspot. | Keep. |
| I-013 | GET | `/api/v1/tasks/{room_id}/param` | tasks | — | M25 | `src/blrec/web/routers/tasks.py:get_task_param` -> task memory | No endpoint-specific IO hotspot. | Keep. |
| I-014 | GET | `/api/v1/tasks/{room_id}/metadata` | tasks | — | M25 | `src/blrec/web/routers/tasks.py:get_task_metadata` -> task memory | No endpoint-specific IO hotspot. | Keep. |
| I-015 | GET | `/api/v1/tasks/{room_id}/profile` | tasks | — | M25 | `src/blrec/web/routers/tasks.py:get_task_stream_profile` -> task memory | No endpoint-specific IO hotspot. | Keep. |
| I-016 | GET | `/api/v1/tasks/{room_id}/videos` | tasks | — | M25 | `src/blrec/web/routers/tasks.py:get_task_video_file_details` -> cached task details | Handler reads the recorder's in-memory detail collection. | Keep; do not add per-request file scans. |
| I-017 | GET | `/api/v1/tasks/{room_id}/danmakus` | tasks | — | M25 | `src/blrec/web/routers/tasks.py:get_task_danmaku_file_details` -> cached task details | Handler reads the recorder's in-memory detail collection. | Keep; do not add per-request file scans. |
| I-018 | POST | `/api/v1/tasks/info` | tasks | X | EXT | `src/blrec/web/routers/tasks.py:update_all_task_infos` -> serial room refresh | Refreshes every task serially and can duplicate room-detail calls. | Outbound: share room detail and use a small bounded batch. |
| I-019 | POST | `/api/v1/tasks/{room_id}/info` | tasks | X | EXT | `src/blrec/web/routers/tasks.py:update_task_info` -> room refresh | One room refresh can fetch overlapping room/anchor data. | Outbound: one shared room-detail response per refresh. |
| I-020 | GET | `/api/v1/tasks/{room_id}/cut` | tasks | — | M25 | `src/blrec/web/routers/tasks.py:can_cut_stream` -> recorder state | Pure state check. | Keep. |
| I-021 | POST | `/api/v1/tasks/{room_id}/cut` | tasks | F | C100 | `src/blrec/web/routers/tasks.py:cut_stream` -> recorder cut trigger | Handler triggers rotation and does not wait for post-processing. | Keep; verify acknowledgement and downstream event. |
| I-022 | POST | `/api/v1/tasks/start` | tasks | W,F,X | C100 local; EXT remote | `src/blrec/web/routers/tasks.py:start_all_tasks` -> serial task starts and settings dump | Serial starts repeat remote detail work and persist settings after task work. | Write/media and Outbound: batch persistence; reuse fetched room data. |
| I-023 | POST | `/api/v1/tasks/{room_id}/start` | tasks | W,F,X | C100 local; EXT remote | `src/blrec/web/routers/tasks.py:start_task` -> refresh, monitor/recorder enable, settings dump | Synchronous control path waits for remote refresh. | Write/media: return accepted for long work; Outbound shares detail/play info. |
| I-024 | POST | `/api/v1/tasks/stop` | tasks | W,F | C100 ack; local batch <2 s | `src/blrec/web/routers/tasks.py:stop_all_tasks` -> serial stops plus settings dump | Foreground mode can wait on every recorder; background mode already exists. | Write/media: one batch lifecycle and one settings persistence. |
| I-025 | POST | `/api/v1/tasks/{room_id}/stop` | tasks | W,F | C100 ack | `src/blrec/web/routers/tasks.py:stop_task` -> recorder stop plus settings dump | Foreground request can wait for file finalization. | Write/media: preserve optional background acknowledgement. |
| I-026 | POST | `/api/v1/tasks/recorder/enable` | tasks | W,X | C100 ack; EXT remote | `src/blrec/web/routers/tasks.py:enable_all_task_recorders` -> serial enable plus dump | Batch lifecycle is serial and may start remote stream work. | Write/media: batch once; Outbound: reuse play resolution. |
| I-027 | POST | `/api/v1/tasks/{room_id}/recorder/enable` | tasks | W,X | C100 ack; EXT remote | `src/blrec/web/routers/tasks.py:enable_task_recorder` -> recorder enable plus dump | Can synchronously enter stream setup. | Write/media/Outbound: accepted control boundary and shared play result. |
| I-028 | POST | `/api/v1/tasks/recorder/disable` | tasks | W,F | C100 ack; local batch <2 s | `src/blrec/web/routers/tasks.py:disable_all_task_recorders` -> serial disable plus dump | Repeats lifecycle work for the batch. | Write/media: one bounded batch and one persistence. |
| I-029 | POST | `/api/v1/tasks/{room_id}/recorder/disable` | tasks | W,F | C100 ack | `src/blrec/web/routers/tasks.py:disable_task_recorder` -> recorder disable plus dump | May wait for finalization when foregrounded. | Write/media: retain background option and operation state. |
| I-030 | POST | `/api/v1/tasks/{room_id}` | tasks | W,X | EXT | `src/blrec/web/routers/tasks.py:add_task` -> room normalization, task setup, settings dump | Room normalization/setup performs remote calls and retries up to the task budget. | Outbound: reuse room data; Write/media: recoverable operation boundary. |
| I-031 | DELETE | `/api/v1/tasks` | tasks | W,F | C100 ack; local batch <2 s | `src/blrec/web/routers/tasks.py:remove_all_tasks` -> serial destruction and settings dump | Destructive serial batch can block while recorders close. | Write/media: bounded operation with one settings persistence. |
| I-032 | DELETE | `/api/v1/tasks/{room_id}` | tasks | W,F | C100 ack | `src/blrec/web/routers/tasks.py:remove_task` -> task destruction and settings dump | Destructive work can include recorder/file shutdown. | Write/media: controlled asynchronous operation; no NAS bulk test. |

### `settings` router (4)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-033 | GET | `/api/v1/settings` | settings | — | M25 | `src/blrec/web/routers/settings.py:get_settings` -> in-memory settings model | Memory projection only. | Keep. |
| I-034 | PATCH | `/api/v1/settings` | settings | W,F,X | C100 local; EXT applied services | `src/blrec/web/routers/settings.py:change_settings` -> apply sections, one dump, optional restart | One dump is good; applying sections may restart network clients or the app. | Write/media: preserve one dump and move long lifecycle work behind an operation. |
| I-035 | GET | `/api/v1/settings/tasks/{room_id}` | settings | — | M25 | `src/blrec/web/routers/settings.py:get_task_options` -> in-memory settings | Linear task lookup is small at current scale. | Keep; measure before indexing in memory. |
| I-036 | PATCH | `/api/v1/settings/tasks/{room_id}` | settings | W,F,X | C100 local; EXT reconnect | `src/blrec/web/routers/settings.py:change_task_options` -> apply options, one dump | Header changes can reconnect remote clients inside the request. | Write/media/Outbound: retain one dump; bound reconnect work. |

### `application` router (4)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-037 | GET | `/api/v1/app/status` | application | — | M25 | `src/blrec/web/routers/application.py:get_app_status` -> in-memory status | No endpoint-specific hotspot. | Keep. |
| I-038 | GET | `/api/v1/app/info` | application | — | M25 | `src/blrec/web/routers/application.py:get_app_info` -> in-memory info | No endpoint-specific hotspot. | Keep. |
| I-039 | POST | `/api/v1/app/restart` | application | W,F,X | C100 ack | `src/blrec/web/routers/application.py:restart_app` -> full application restart | Request waits for a broad lifecycle transition. | Write/media: return recoverable operation state. |
| I-040 | POST | `/api/v1/app/exit` | application | — | C100 | `src/blrec/web/routers/application.py:exit_app` -> process signal | Immediate control signal; no repeated IO. | Keep; single controlled verification only. |

### `validation` router (2)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-041 | POST | `/api/v1/validation/dir` | validation | F | D100 | `src/blrec/web/routers/validation.py:validate_dir` -> directory/access checks | Synchronous filesystem checks run on the event loop and may hit NAS latency. | Write/media: move validation to a bounded worker. |
| I-042 | POST | `/api/v1/validation/cookie` | validation | X | EXT | `src/blrec/web/routers/validation.py:validate_cookie` -> Bilibili navigation request | One external validation request; no cache at this boundary. | Outbound: pooled session and explicit deadline. |

### `websockets` router (2)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-043 | WS | `/ws/v1/events` | websockets | R,S | STR | `src/blrec/web/routers/websockets.py:receive_events` -> auth plus event subscription | Long-lived stream has auth but no dedicated handshake/backlog/disconnect metrics. | Write/media: add WebSocket-specific connection metrics; preserve semantics. |
| I-044 | WS | `/ws/v1/exceptions` | websockets | R,S | STR | `src/blrec/web/routers/websockets.py:receive_exception` -> auth plus exception subscription | Same observability gap as the event socket. | Write/media: add stream metrics without logging exception payloads. |

### `update` router (1)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-045 | GET | `/api/v1/update/version/latest` | update | X | EXT | `src/blrec/web/routers/update.py:get_latest_version` -> package-index metadata | A new client is created per call; there is no 15-60 minute cache or stale fallback. | Outbound: pooled client, TTL cache, single-flight, stale-if-error. |

### `live_status` router (2)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-046 | GET | `/api/v1/live-status` | live_status | — | M25 | `src/blrec/web/routers/live_status.py:get_live_status` -> coordinator metrics | Snapshot is in memory. | Keep. |
| I-047 | POST | `/api/v1/live-status/resume` | live_status | — | C100 | `src/blrec/web/routers/live_status.py:resume_live_status` -> coordinator state | Local breaker/resume signal only. | Keep; do not trigger extra polling. |

### `network` router (3)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-048 | GET | `/api/v1/network/interfaces` | network | P | D100 | `src/blrec/web/routers/network.py:get_interfaces` -> TTL refresh then cached snapshot | Discovery used to run repeatedly during realtime sampling. | Foundation: 10-second cache and worker-thread refresh; realtime invokes zero subprocesses. |
| I-049 | PATCH | `/api/v1/network/interfaces/{interface_name}` | network | W,F,P | C100 local plus one forced refresh | `src/blrec/web/routers/network.py:update_interface` -> persistence and forced refresh | One explicit post-write refresh is intentional. | Foundation/Keep: cached reads; focused route test. |
| I-050 | POST | `/api/v1/network/probe` | network | P,X | EXT (8-second total probe deadline) | `src/blrec/web/routers/network.py:probe_networks` -> cached discovery and bounded probe | Explicit route boundary is allowed to refresh/probe; realtime is isolated from it. | Foundation/Keep: no polling increase. |

### `realtime` router (1)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-051 | GET | `/api/v1/realtime` | realtime | R,S | STR | `src/blrec/web/routers/realtime.py:get_realtime`; `src/blrec/web/realtime.py:RealtimeSampler.sample_once` | Formerly computed all providers every second and replayed an initial redundant reload. | Foundation: topic-aware single SSE; zero uninterested providers; later resync preserved. |

### `bili_accounts` router (8)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-052 | GET | `/api/v1/bili-accounts` | bili_accounts | R | D100 | `src/blrec/web/routers/bili_accounts.py:list_accounts` -> account manager list | Bounded local list; no confirmed hotspot. | Keep; observe query count. |
| I-053 | PUT | `/api/v1/bili-accounts/{account_id}/primary` | bili_accounts | R,W | C100 | `src/blrec/web/routers/bili_accounts.py:select_primary_account` -> primary-account transaction | Local fenced mutation. | Keep. |
| I-054 | GET | `/api/v1/bili-accounts/{account_id}/relationships` | bili_accounts | R | D100 | `src/blrec/web/routers/bili_accounts.py:account_relationships` -> policy/job relationships | Multiple relationship classes are assembled for one account; query shape needs scale guard. | Hot read: aggregate relationship queries and add a fixed query-budget test. |
| I-055 | POST | `/api/v1/bili-accounts/{account_id}/removal` | bili_accounts | R,W | C100 local batch | `src/blrec/web/routers/bili_accounts.py:remove_account` -> relationship validation/reassignment | Potentially rewrites policies and jobs; destructive and cardinality-sensitive. | Write/media: one transaction and fake-backed destructive tests. |
| I-056 | POST | `/api/v1/bili-accounts/qr-sessions` | bili_accounts | R,W,X | EXT | `src/blrec/web/routers/bili_accounts.py:create_qr_session` -> remote QR creation plus journal | Remote call is synchronous to request but protected by protocol outcome rules. | Outbound: retain account gate and explicit total deadline. |
| I-057 | GET | `/api/v1/bili-accounts/qr-sessions/{session_id}` | bili_accounts | R,W,X | EXT | `src/blrec/web/routers/bili_accounts.py:get_qr_session` -> remote QR poll plus state update | Browser polling directly drives upstream polling. | Outbound: keep current cadence; deduplicate concurrent polls. |
| I-058 | DELETE | `/api/v1/bili-accounts/qr-sessions/{session_id}` | bili_accounts | W | C100 | `src/blrec/web/routers/bili_accounts.py:cancel_qr_session` -> local session cancellation | Local cancellation is bounded. | Keep. |
| I-059 | POST | `/api/v1/bili-accounts/{account_id}/refresh` | bili_accounts | R,W,X | EXT | `src/blrec/web/routers/bili_accounts.py:refresh_account` -> credential renewal | Non-idempotent renewal has explicit unknown-outcome handling. | Outbound: retain the account write gate and outcome fence. |

### `recording_sessions` router (15)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-060 | GET | `/api/v1/recording-sessions` | recording_sessions | R | L150 | `src/blrec/web/routers/recording_sessions.py:list_recording_sessions` -> `count_sessions`, `list_session_summaries` | A 20-row page now uses count plus one page-first aggregate query and performs no list-time path checks; child scans are index-bounded to selected IDs. | Hot read implemented: deterministic two-call/zero-file budget, summary/detail split, SQLite 3.22/current query-plan evidence; NAS p95 is pending. |
| I-061 | GET | `/api/v1/recording-sessions/{session_id}/submission-settings` | recording_sessions | R | D100 | `src/blrec/web/routers/recording_sessions.py:get_session_submission_settings` -> session submission manager | Detail-only policy projection; appropriate outside the list. | Keep; fixed query-budget test. |
| I-062 | PUT | `/api/v1/recording-sessions/{session_id}/submission-settings` | recording_sessions | R,W | C100 | `src/blrec/web/routers/recording_sessions.py:save_session_submission_settings` -> override transaction | Local validated override; no confirmed hotspot. | Keep; transaction regression tests. |
| I-063 | DELETE | `/api/v1/recording-sessions/{session_id}/submission-settings` | recording_sessions | R,W | C100 | `src/blrec/web/routers/recording_sessions.py:clear_session_submission_settings` -> clear override | Local validated mutation. | Keep. |
| I-064 | PATCH | `/api/v1/recording-sessions/{session_id}/submission-decision` | recording_sessions | R,W | C100 | `src/blrec/web/routers/recording_sessions.py:set_session_submission_decision` -> decision transaction | Local validated mutation. | Keep. |
| I-065 | POST | `/api/v1/recording-sessions/upload-jobs/actions` | recording_sessions | R,W,F | C100 ack; local batch <2 s | `src/blrec/web/routers/recording_sessions.py:run_upload_job_actions` -> serial action manager | Up to 100 actions run serially; delete/repair branches may touch files or worker lifecycle. | Write/media: one transaction where possible and bounded background operations otherwise. |
| I-066 | GET | `/api/v1/recording-sessions/upload-jobs/{job_id}/settings` | recording_sessions | R | D100 | `src/blrec/web/routers/recording_sessions.py:get_upload_task_settings` -> task settings detail | Detail-only read; no list amplification. | Keep. |
| I-067 | PUT | `/api/v1/recording-sessions/upload-jobs/{job_id}/settings` | recording_sessions | R,W | C100 | `src/blrec/web/routers/recording_sessions.py:update_upload_task_settings` -> update then detail read | Performs a post-write read; acceptable for one job. | Keep; assert bounded database calls. |
| I-068 | POST | `/api/v1/recording-sessions/actions` | recording_sessions | R,W,F | C100 ack; local batch <2 s | `src/blrec/web/routers/recording_sessions.py:run_recording_session_actions` -> serial session action runner | Up to 100 actions are serial and can delete/backfill local artifacts. | Write/media: group local mutations and bound file work. |
| I-069 | POST | `/api/v1/recording-sessions/upload-jobs/retry-failed` | recording_sessions | R,W | C100 ack | `src/blrec/web/routers/recording_sessions.py:retry_all_failed_upload_jobs` -> list then serial retries | Cardinality-dependent serial mutation. | Write/media: one batch selection/transaction; one worker wakeup. |
| I-070 | GET | `/api/v1/recording-sessions/upload-jobs/retry-failed-preview` | recording_sessions | R | L150 | `src/blrec/web/routers/recording_sessions.py:preview_retryable_failed_upload_jobs` -> `UploadTaskActionManager.retryable_failed_jobs` | The preview is a single joined scalar projection and never hydrates upload parts, chunks, danmaku items, or local paths. | Hot read implemented: one database query; NAS p95 is pending. Retry mutation is exclusively tracked by I-069. |
| I-071 | POST | `/api/v1/recording-sessions/parts/{part_id}/media-access` | recording_sessions | R,F | T150 | `src/blrec/web/routers/recording_sessions.py:create_recording_media_access` -> media lookup and FLV snapshot | Active FLV snapshot/index work can touch the file in the request path. | Write/media: bounded worker, first-byte metrics, snapshot budget. |
| I-072 | GET | `/api/v1/recording-sessions/parts/{part_id}/media` | recording_sessions | R,F,S | T150 | `src/blrec/web/routers/recording_sessions.py:stream_recording_media` -> range-aware file stream | Range exists, but completed media still uses `no-store` and lacks ETag/conditional requests. | Write/media: completed-file ETag/cache; active snapshots remain `no-store`. |
| I-073 | GET | `/api/v1/recording-sessions/parts/{part_id}/danmaku` | recording_sessions | R,F | D100 | `src/blrec/web/routers/recording_sessions.py:list_recording_danmaku` -> paged XML read | File parse is detail-only but must not block the event loop. | Write/media: bounded file worker and page latency test. |
| I-104 | GET | `/api/v1/recording-sessions/{session_id}` | recording_sessions | R,F | D100 | `src/blrec/web/routers/recording_sessions.py:get_recording_session` -> `get_session`, full upload detail | Complete parts, paths, unknown danmaku items, and submission verification are loaded only after the drawer opens; missing IDs return 404. | Hot read implemented: on-demand detail preserves the full contract and is never called by the list pipeline. Per-part availability checks remain intentionally detail-only; NAS p95 is pending. |

### `recording_retention` router (1)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-074 | GET | `/api/v1/recording-retention/status` | recording_retention | R | D100 | `src/blrec/web/routers/recording_retention.py:get_retention_status` -> persisted managed-size aggregate | Status uses one aggregate query and no recording-path IO. Capacity cleanup still measures the real filesystem, including active rows with unknown persisted size. | Hot read implemented: one-call/zero-file status budget without weakening destructive cleanup; NAS p95 is pending. |

### `room_upload_policies` router (5)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-075 | GET | `/api/v1/room-upload-policies` | room_upload_policies | R | L150 | `src/blrec/web/routers/room_upload_policies.py:list_room_upload_policies` -> joined policy/account projection | Primary and fixed accounts are resolved by one query for 1, 20, and 100 policies; missing, paused, and archived-account semantics are retained. | Hot read implemented: one database call with no policy-to-account N+1; NAS p95 is pending. |
| I-076 | GET | `/api/v1/room-upload-policies/categories` | room_upload_policies | R,X | D100 cached; EXT forced | `src/blrec/web/routers/room_upload_policies.py:list_upload_categories` -> category catalog | Existing 24-hour cache and per-account single-flight are appropriate. | Keep; Outbound only verifies deadline/stale behavior. |
| I-077 | GET | `/api/v1/room-upload-policies/{room_id}` | room_upload_policies | R | D100 | `src/blrec/web/routers/room_upload_policies.py:get_room_upload_policy` -> one resolved policy | One detail read; no list amplification. | Keep. |
| I-078 | PUT | `/api/v1/room-upload-policies/{room_id}` | room_upload_policies | R,W,X | C100 cached; EXT refresh | `src/blrec/web/routers/room_upload_policies.py:upsert_room_upload_policy` -> cached category validation and upsert | Cache miss may call upstream, but local mutation remains one validated write. | Keep cache; Outbound verifies explicit remote deadline. |
| I-079 | DELETE | `/api/v1/room-upload-policies/{room_id}` | room_upload_policies | W | C100 | `src/blrec/web/routers/room_upload_policies.py:delete_room_upload_policy` -> one delete | Single local destructive write. | Keep; focused test only. |

### `upload_covers` router (3)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-080 | GET | `/api/v1/upload-covers` | upload_covers | R | D100 | `src/blrec/web/routers/upload_covers.py:list_upload_covers` -> cover library list | Bounded metadata list. | Hot read frontend: lazy visible covers; backend query stays bounded. |
| I-081 | POST | `/api/v1/upload-covers` | upload_covers | R,W,F | C100 for bounded payload | `src/blrec/web/routers/upload_covers.py:add_upload_cover` -> capped body, validation, file/database write | Payload is capped; image validation/file write must stay off the event loop. | Write/media: worker-bound file work and atomic persistence. |
| I-082 | GET | `/api/v1/upload-covers/{asset_id}/content` | upload_covers | R,F,S | T150 | `src/blrec/web/routers/upload_covers.py:read_upload_cover` -> file response | Private one-hour caching already exists. | Keep; frontend coalesces identical visible requests. |

### `bili_collections` router (2)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-083 | GET | `/api/v1/bili-collections` | bili_collections | R,X | D100 cached; EXT refresh | `src/blrec/web/routers/bili_collections.py:list_bili_collections` -> remote collection list | No 30-60 second TTL or single-flight; repeated dialogs can duplicate calls. | Outbound: short TTL, per-account single-flight, stale-on-error policy. |
| I-084 | POST | `/api/v1/bili-collections` | bili_collections | R,W,F,X | EXT | `src/blrec/web/routers/bili_collections.py:create_bili_collection` -> cover resolution/upload and remote create | Multi-step non-idempotent remote operation must preserve unknown-outcome safety. | Outbound: account gate, absolute deadline, no blind retry. |

### `highlights` router (17)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-085 | POST | `/api/v1/highlights` | highlights | R,W | C100 | `src/blrec/web/routers/highlights.py:create_marker` -> marker insert | Single local marker write. | Keep. |
| I-086 | PATCH | `/api/v1/highlights/{marker_id}` | highlights | R,W | C100 | `src/blrec/web/routers/highlights.py:update_marker` -> marker update | Single local marker mutation. | Keep. |
| I-087 | DELETE | `/api/v1/highlights/{marker_id}` | highlights | W | C100 | `src/blrec/web/routers/highlights.py:delete_marker` -> marker delete | Single local destructive mutation. | Keep; focused test only. |
| I-088 | GET | `/api/v1/highlights/sessions/{session_id}/timeline` | highlights | R,F | D100 | `src/blrec/web/routers/highlights.py:get_timeline`; `src/blrec/bili_upload/highlights.py:timeline`, `_available_path` | The full filesystem-aware timeline remains necessary for the editor, but recording rows/details no longer request it just to display counts. | Hot read complete at the call boundary: timeline is editor-only; I-105 supplies the zero-file count projection. |
| I-089 | POST | `/api/v1/highlights/sessions/{session_id}/clips/inspect` | highlights | R,W | T150 handshake; bounded probe | `src/blrec/web/routers/highlights.py:inspect_clip` -> `HighlightService.submit_clip_inspection` | The request admits a durable operation and returns 202; at most two workers probe while eight operations wait, under one absolute 30-second FFprobe deadline. | Write/media WM-05: bounded durable admission with fingerprint/range single-flight reuse. |
| I-090 | POST | `/api/v1/highlights/sessions/{session_id}/clips` | highlights | R,W,F | C100 ack | `src/blrec/web/routers/highlights.py:create_clip` -> token validation and atomic clip enqueue | A ready one-use inspection token is consumed in the same transaction as the idempotent clip insert; stale fingerprints return the existing background inspection operation instead of probing in the request. | Write/media WM-05: response-loss-safe create and worker reuse of persisted inspection. |
| I-091 | GET | `/api/v1/highlights/sessions/{session_id}/clips` | highlights | R,F | D100 | `src/blrec/web/routers/highlights.py:list_clips` -> full clip projections | Full projections include source/upload/file-derived fields. | Hot read: counts/summary for rows; full list only when opened. |
| I-092 | GET | `/api/v1/highlights/clips` | highlights | R,F | L150 | `src/blrec/web/routers/highlights.py:list_all_clips` -> count plus page-first clip summary | Both 20- and 100-row pages use exactly two database calls and zero `getsize`/`stat`/`lstat`; size is persisted on create/recovery and unknown legacy size stays `null`. | Hot read implemented: selected-page chunk aggregation, migration-24 size lifecycle, at-most-100 startup backfill, unchanged full detail paths/sources, and proven partial index; completion remains pending until warm NAS p95 is below 150 ms. |
| I-093 | GET | `/api/v1/highlights/clips/{clip_id}` | highlights | R,F | D100 | `src/blrec/web/routers/highlights.py:get_clip` -> full clip detail | Detail route is the correct place for full projection. | Keep; bound file work. |
| I-094 | POST | `/api/v1/highlights/clips/{clip_id}/retry` | highlights | R,W | C100 | `src/blrec/web/routers/highlights.py:retry_clip` -> state transition | Local queue-state transition only. | Keep; worker performs expensive work later. |
| I-095 | POST | `/api/v1/highlights/clips/{clip_id}/media-access` | highlights | R,F | T150 | `src/blrec/web/routers/highlights.py:create_clip_media_access` -> path lookup and synchronous `stat` | Synchronous file `stat` occurs in the async request path. | Write/media: move path validation/stat off-loop. |
| I-096 | GET | `/api/v1/highlights/clips/{clip_id}/media` | highlights | R,F,S | T150 | `src/blrec/web/routers/highlights.py:stream_clip_media` -> range-aware file stream | Range exists, but immutable completed clip still uses `no-store` and synchronous open/stat. | Write/media: ETag/private cache and worker-bound validation. |
| I-097 | DELETE | `/api/v1/highlights/clips/{clip_id}` | highlights | R,W,F | C100 ack | `src/blrec/web/routers/highlights.py:delete_clip` -> upload guard and local deletion | Destructive file/database work can outlive a control request. | Write/media: recoverable background operation; no bulk NAS test. |
| I-098 | POST | `/api/v1/highlights/clips/{clip_id}/upload-session` | highlights | R,W | C100 | `src/blrec/web/routers/highlights.py:prepare_upload_session` -> ensure local session | Local idempotent session creation. | Keep; assert bounded queries. |
| I-099 | POST | `/api/v1/highlights/clips/{clip_id}/upload-task` | highlights | R,W,X | C100 cached; EXT catalog refresh | `src/blrec/web/routers/highlights.py:create_upload_task`; `src/blrec/bili_upload/runtime.py:create_highlight_upload_task` -> catalog validation then local job creation | The media upload remains worker-owned, but a category-catalog cache miss can refresh Bilibili data before the local job is created. | Outbound: preserve the category TTL/single-flight, explicit remote deadline and stale behavior without increasing request cadence; keep the cached local path within C100. |
| I-105 | GET | `/api/v1/highlights/sessions/{session_id}/marker-counts` | highlights | R | D100 | `src/blrec/web/routers/highlights.py:get_marker_counts` -> `HighlightService.marker_counts` | Persisted marker links and legacy time-boundary mapping are counted with at most two queries, including zero-count parts, without selecting paths or touching files. | Hot read implemented: lightweight detail-row counts with editor-compatible boundary semantics; NAS p95 is pending. |
| I-106 | GET | `/api/v1/highlights/inspections/{operation_id}` | highlights | R,W | D100 | `src/blrec/web/routers/highlights.py:get_clip_inspection` -> durable inspection status/token claim | Reads accepted/running/terminal state; a succeeded result exchanges the claim header for a deterministic one-use token in a short transaction without persisting either plaintext secret. | Write/media WM-05: safe polling and response-loss recovery; no local path, probe diagnostic, secret, or token hash is returned. |

### `browser_extension` router (4)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-100 | POST | `/api/v1/browser-extension/pair` | browser_extension | R,W | C100 | `src/blrec/web/routers/browser_extension.py:pair` -> extension-token issue/rate limit | One local token write; pairing rate limit is required. | Keep. |
| I-101 | GET | `/api/v1/browser-extension/rooms/{room_id}` | browser_extension | — | M25 | `src/blrec/web/routers/browser_extension.py:room_status` -> in-memory task state | Business handler is memory-only; extension authentication write is throttled. | Foundation/Keep: zero repeated activity writes inside 60 seconds. |
| I-102 | POST | `/api/v1/browser-extension/rooms/{room_id}/collect` | browser_extension | R,W,X | EXT | `src/blrec/web/routers/browser_extension.py:collect_room` -> add/start task and optional policy/category work | Combines room normalization, task lifecycle, and optional cached upstream category validation. | Write/media and Outbound: recoverable operation, shared room data, fixed request cadence. |
| I-103 | POST | `/api/v1/browser-extension/rooms/{room_id}/highlights` | browser_extension | R,W | C100 | `src/blrec/web/routers/browser_extension.py:create_highlight` -> marker insert | Single local marker write after throttled extension auth. | Keep. |

### `control_operations` router (1)

| ID | Method | Normalized path | Router | IO | Budget | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I-107 | GET | `/api/v1/control-operations/{operation_id}` | control_operations | R | D100 | `src/blrec/web/routers/control_operations.py:get_control_operation` -> dedicated control journal | Reads one durable operation and its bounded per-item steps without running lifecycle work. | Write/media WM-06: status-only polling for accepted task controls; I-106 remains reserved for WM-05. |

Machine count:

```bash
test "$(rg -c '^\| I-[0-9]{3} \|' docs/performance/request-audit.md)" = 107
```

## Outbound operation groups

These are operation groups, not upstream URLs. Budgets apply to the whole logical
operation and preserve the existing polling/upload/danmaku cadence.

| Group | Operations and IO | Budget/policy | Evidence | Finding | Disposition |
| --- | --- | --- | --- | --- | --- |
| Room status | Batched live status plus missing-room fallback (`X`) | Existing polling interval; one batch per cycle; bounded fallback; no frequency increase | `src/blrec/bili/batch_status_client.py:BatchStatusClient.fetch`; `src/blrec/bili/live_status_coordinator.py` | Batching, breaker, and fallback cooldown already exist; fallback counts remain observable. | Keep and regression-test request counts. |
| Room detail | Room/anchor/detail refresh (`X`) | 10-second client total timeout; one shared response per logical refresh | `src/blrec/bili/live.py:get_room_info`, `_get_room_info_via_api`; `src/blrec/bili/anonymous_room_client.py` | Room and anchor projections can trigger overlapping detail calls. | Outbound plan: request-scope single-flight and batch reuse. |
| Play info | Play URL/profile resolution (`X`) | 10-second client total timeout; one resolution reused by probe and recorder | `src/blrec/bili/live.py:get_play_infos`, `get_live_stream_url`; `src/blrec/core/operators/stream_url_resolver.py` | Resolver caches the chosen URL, but validation/probe and recorder can repeat work. | Outbound plan: share parsed play info and close validation responses. |
| Recording transfer | FLV stream, HLS playlist, and segments (`X,S`) | Sustained throughput metric; established retry/route policy; no extra probe GET | `src/blrec/core/operators/stream_fetcher.py`; `src/blrec/hls/operators/playlist_fetcher.py`; `src/blrec/hls/operators/segment_fetcher.py` | Long-lived and segmented transfers have distinct retry semantics; extra validation can consume sockets. | Outbound plan: pooled connections, explicit deadlines, no duplicate stream probe. |
| Danmaku WebSocket | Authenticated/anonymous connection and reconnect (`X,S`) | Connection handshake/uptime/backoff metrics; unchanged reconnect cadence | `src/blrec/bili/danmaku_client.py`; `src/blrec/bili/danmaku_connection.py` | Fallback behavior exists and must not become more aggressive. | Keep semantics; add connection metrics in outbound work. |
| UPOS | Preupload, init, chunks, completion (`X,S`) | 30-second request total timeout; fixed upload route; account gate; idempotent chunks only | `src/blrec/bili_upload/protocol.py:AiohttpProtocolTransport`, `preupload`, `upload_chunk`, `complete_upload` | Transport pooling and unknown-outcome fences exist; completion is non-idempotent. | Keep safety; outbound plan unifies retry budgets and honors server backoff. |
| Submission | Submit and edit archive (`X`) | 30-second total timeout; account write gate; no blind retry | `src/blrec/bili_upload/protocol.py:submit_archive`, `edit_archive`; `src/blrec/bili_upload/upload.py` | Non-idempotent outcome protection exists and is required. | Keep fence; separate worker/deadline in outbound plan. |
| Review | Archive list, preflight, and archive detail (`X`) | Shared per-account list; small detail concurrency; absolute cycle deadline | `src/blrec/bili_upload/protocol.py:list_archives`, `archive_pre`, `archive_view`; `src/blrec/bili_upload/review.py` | Review work is currently coupled to a broad worker loop and can repeat account lists. | Outbound plan: account-shared list and bounded detail fan-out. |
| Comments | Reply list/detail, add, and pin (`X`) | Reads may retry within deadline; writes use account gate and unknown-outcome fence | `src/blrec/bili_upload/protocol.py:list_replies`, `reply_detail`, `add_reply`, `top_reply` | WBI keys already cache for 10 minutes; branch work is coupled to the upload loop. | Outbound plan: dedicated worker and unified retry budget. |
| Danmaku posting | Backfill post (`X`) | Existing send cadence; account gate; no parallel-line increase | `src/blrec/bili_upload/protocol.py:post_danmaku`; `src/blrec/bili_upload/danmaku_publish.py` | Prepared/unknown outcome distinction is already explicit. | Keep safety; separate bounded worker without raising frequency. |
| Collections | List, create, add episode (`X`) | List TTL 30-60 seconds plus single-flight; writes no blind retry | `src/blrec/bili_upload/protocol.py:list_collections`, `create_collection`, `add_collection_episode`; `src/blrec/bili_upload/collections.py` | Collection list has no short cache/single-flight at the manager boundary. | Outbound plan: TTL/single-flight; preserve non-idempotent fences. |
| Categories | Archive preflight/category and creation-statement catalog (`R,W,X`) | Existing 24-hour cache; per-account single-flight; explicit remote deadline | `src/blrec/bili_upload/categories.py:UploadCategoryCatalog`; `src/blrec/bili_upload/protocol.py:archive_pre` | Cache and stale fallback already exist. | Keep; verify refresh count and stale behavior. |
| Covers | Cover download, remote upload, and collection-cover resolution (`F,X`) | 30-second remote fetch cap; bounded size; coalesced identical work | `src/blrec/bili_upload/covers.py`; `src/blrec/bili_upload/protocol.py:upload_cover`; `src/blrec/core/cover_downloader.py` | Some paths already lock/cache; connection reuse and all-source deadlines are inconsistent. | Outbound plan: shared clients/timeouts; frontend plan lazy-loads visible covers. |
| QR/account | QR create/poll, account info, credential refresh (`R,W,X`) | 30-second protocol timeout; current poll cadence; account write gate | `src/blrec/bili_upload/protocol.py:create_qr`, `poll_qr`, `oauth_info`, `refresh_token`; `src/blrec/bili_upload/accounts.py` | Concurrent browser status requests can duplicate a poll; refresh is non-idempotent. | Outbound plan: poll single-flight; retain refresh outcome fence. |
| Notifications | SMTP and push providers (`X`) | Explicit connect/read/total timeouts; bounded queue; pooled clients where supported | `src/blrec/notification/providers.py`; `src/blrec/notification/operational.py` | Providers create a client per message; several lack explicit timeout, and one default endpoint is not HTTPS. | Outbound plan: HTTPS, reuse, deadlines, bounded queue. |
| Webhook | Event and exception POST (`X`) | Bounded queue; explicit total deadline; capped retry with jitter | `src/blrec/webhook/webhook_emitter.py:WebHookEmitter` | A detached task and new client are created per delivery; retries can accumulate for 180 seconds. | Outbound plan: shared session and bounded delivery worker. |
| Network probe | Interface-bound reachability probe (`X`) | Existing 8-second total timeout; only explicit user/health boundary triggers it | `src/blrec/networking/manager.py:_probe_interface` | Already bounded and isolated from realtime sampling. | Foundation/Keep; no automatic frequency increase. |
| Update check | Package-index metadata (`X`) | 15-60 minute TTL, single-flight, stale-if-error, explicit deadline | `src/blrec/update/helpers.py`; `src/blrec/update/api.py` | A new client and request are used for each UI call; no cache. | Outbound plan: pooled client and cached stale fallback. |

## Foundation evidence and remaining scope

- Request middleware records normalized route, status, elapsed time, response bytes,
  and instrumented upload-database calls without request values. SSE/media completion
  logging is excluded pending stream-specific metrics.
- Authentication still validates every request; only activity persistence is
  throttled. Expiry refresh, logout, password-reset revocation, CSRF, and rate limits
  remain unchanged.
- Realtime computation is subscriber- and topic-aware. Active upload/highlight
  projections are bounded, and network snapshots consume cached interfaces.
- The confirmed hot-read findings are deliberately not implemented in the foundation:
  recording/upload summaries, policy/account N+1 queries, retention aggregates,
  highlight counts/timeline, list indexes, and Angular row/lazy-loading work belong to
  `docs/superpowers/plans/2026-07-20-hot-read-path-performance.md`.
