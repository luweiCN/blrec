# BLREC 内置 B 站投稿、弹幕回灌与自动评论设计

## 1. 背景与目标

BLREC 当前负责直播监控、录制和弹幕 XML 落盘，投稿依赖外部工具或 Webhook。本设计在同一仓库和 Docker 镜像内增加纯 Python 投稿子系统，使一场直播的多个最终视频文件组成一个可恢复的多 P 投稿；稿件审核通过后，系统再执行 SC/上舰索引评论和原生视频弹幕回灌。

目标是：

- 扫码登录并按正确协议刷新账号凭据；
- 持久化录制会话、最终制品、上传分块和后处理状态；
- 在网络中断或容器重启后安全恢复；
- 将所有通过过滤的弹幕排队回灌，不设置软件数量上限；
- 提供账号、房间投稿策略和任务管理页面；
- 新增功能故障时不影响 BLREC 的核心录制。

58 个房间的批量开播状态查询与“仅直播房间建立弹幕 WSS”属于独立子系统，由同目录的 `2026-07-12-batch-live-monitor-design.md` 规定，并在投稿灰度前完成。两者共享匿名读取优先、登录凭据最小使用的边界，但不共享任务状态机。

## 2. 范围与方案选择

首期包含账号管理、多 P 投稿、审核同步、弹幕回灌、固定模板自动评论、管理 API/UI、自动化测试和 NAS 灰度。不包含 AI、云剪辑、高能片段、微信推送、自动删除或移动录像、账号池分摊、代理或身份轮换，以及验证码自动处理。

评估过外部 Webhook、Java 边车和纯 Python 内置三种方案。Webhook 会分散账号、任务与管理状态；Java 边车需要第二套运行时和数据库；因此选择纯 Python 内置实现，默认单镜像、单容器、单进程，不引入 Redis、Celery 或外部数据库。

协议研究固定参考：

- `mwxmmy/biliupforjava@a366e4f1f86bfd1c69a9b6cc66c372d2f6da7e1e`：业务行为参考，Apache-2.0；
- `biliup/biliup@18c5bf086e943e07e9d88a905d2e5d407d6305bb`：登录、刷新、UPOS 和投稿的主要协议基线，MIT；
- `BACNext/bilibili-API-collect-backup@cfc5fddcc8a94b74d91970bb5b4eaeb349addc47`：参数与错误码交叉校验。

这些均非官方稳定契约。实现需保留必要许可证与来源声明，并以脱敏合同测试和线上 canary 验证协议是否仍有效。

## 3. 组件边界

- `CredentialStore`：加密、版本化并原子替换账号凭据包；
- `AccountManager`：TV 扫码、状态校验、刷新和账号暂停；
- `BiliProtocolClient`：按端点白名单装配认证、签名、Cookie 与设备标识；
- `DurableUploadJournal`：先于通知总线持久化录制和后处理事件；
- `UploadCoordinator`：建立会话、分 P 和投稿任务；
- `UploadWorker`：预上传、UPOS 分块、完成及投稿提交；
- `ReviewWatcher`：低频同步审核状态和分 P CID；
- `CommentPublisher`：生成、发布并置顶 SC/上舰索引；
- `DanmakuImporter`：流式解析 XML 并批量写入回灌队列；
- `DanmakuPublisher`：公平调度、限速、发送和熔断；
- `AccountWriteGate`：串行化同账号的刷新、投稿提交、评论、置顶和弹幕写操作。

现有 RxPY `EventCenter` 继续服务 WebSocket、Webhook 和 UI 通知，但不是投稿任务的事实来源，也不承担崩溃恢复。

## 4. 管理面与凭据安全

当前 BLREC 只有配置 `BLREC_API_KEY` 时才启用 API 认证，而 Docker 默认监听 `0.0.0.0`。因此，只要启用账号模块或任一 B 站写功能，就必须同时配置：

- `BLREC_API_KEY`：保护账号、投稿策略及所有任务变更路由；
- `BLREC_CREDENTIAL_KEY_FILE` 或 `BLREC_CREDENTIAL_KEY`：加密账号凭据。

缺少任一项时，录制和匿名读取仍可启动，但账号及写功能必须 fail closed；相关路由返回明确错误，不能退化为匿名管理。二维码短期会话必须绑定已认证管理请求，不能代替管理身份。

凭据使用带认证加密的版本化 envelope，记录 `key_id` 和随机 nonce。读取端允许同时配置当前密钥和旧密钥，轮换时先验证新密文再切换；密钥缺失或错误时账号进入只读暂停，系统不得覆盖原密文。数据库、WAL 与 SHM 权限为 `0600`，父目录为 `0700`。统一 HTTP 客户端不记录完整 URL query、表单、Cookie 或原始上游响应；未知响应默认不落盘。

## 5. 首期协议矩阵

每个操作首期只允许一种认证模式，禁止在失败时随意切换 Web/APP 身份。实现不得复用 BLREC 当前另一组 APP signer 处理 TV token。

| 操作 | 首期协议与端点 | 认证及签名 |
| --- | --- | --- |
| TV 二维码创建/轮询 | `/x/passport-tv-login/qrcode/auth_code`、`.../poll` | BiliTV APPKEY 族签名；确认前不带用户会话 |
| token 状态/刷新 | `/x/passport-login/oauth2/info`、`.../refresh_token` | TV `access_key`/`refresh_token` 与其签发 BiliTV APPKEY 族 |
| UPOS 预上传 | `member.bilibili.com/preupload` | 当前版本 Cookie jar；只读取服务端返回的 UPOS 参数 |
| UPOS 分片/完成 | 服务端返回的 UPOS URL | 仅使用该上传会话的 `X-Upos-Auth` 与 `upload_id` |
| 最终投稿 | `/x/vu/app/add` | TV `access_key` 与 BiliTV APPKEY 族签名 |
| 审核列表 | `/x/web/archives` | 当前版本 Web Cookie jar |
| 评论/置顶 | `/x/v2/reply/add`、`.../top` | Web Cookie、同版本 `bili_jct`/CSRF |
| 视频弹幕 | `/x/v2/dm/post` | Web Cookie、CSRF，并按固定 API 基线添加 WBI；发送前合同探测 |

APP BUVID/Device-ID 与 Web `buvid3`、`buvid4`、`b_nut` 分开存储，记录来源、协议作用域和客户端版本，不能互相替代或跨账号共享。Cookie 按标准 jar 保存 domain、path、expires、secure 与 httpOnly；CSRF 是同一凭据版本内 `bili_jct` 的派生值，不能单独更新。

协议漂移若使矩阵中的模式失效，系统暂停对应能力并要求更新适配器；不得自动轮换端点、签名族或身份尝试“撞通”。

## 6. SQLite 与任务协议

BLREC 当前没有业务关系数据库。首期新增 `/cfg/blrec.sqlite3`，可通过配置改到其他本地路径，但必须位于支持 POSIX/本地文件锁的文件系统；不支持 NFS/SMB 共享卷，也不支持两个容器或多 Uvicorn worker 同时执行任务。数据库启用 WAL、`foreign_keys=ON`、`busy_timeout`、显式迁移和单写 DB actor。

主要表为：

- `event_journal`：录制/后处理事件、稳定关联键和消费状态；
- `bili_accounts`：UID、加密凭据版本、APP/Web 设备字段、状态与暂停原因；
- `qr_sessions`：管理主体、auth code 哈希、状态和过期时间；
- `room_upload_policies`：账号映射、投稿元数据、评论/回灌开关及过滤规则；
- `recording_sessions`：`broadcast_session_key`、状态和录制起止时间；
- `recording_runs`：一次进程内录制运行及取消/完成状态；
- `upload_jobs`：策略快照、AID/BVID、审核和聚合状态；
- `upload_parts`：`part_index`、源/最终路径、XML、文件身份、远端 filename 与 CID；
- `upload_chunks`：分块号、大小、ETag/结果、次数和上传会话；
- `comment_items`、`danmaku_items`：请求指纹、内容、目标、状态与错误。

最低约束包括：

- `UNIQUE(upload_jobs.session_id)`；
- `UNIQUE(upload_parts(job_id, part_index))`；
- `UNIQUE(upload_chunks(part_id, chunk_no))`；
- `UNIQUE(comment_items(job_id, ordinal))`；
- `UNIQUE(danmaku_items(part_id, xml_identity, original_index))`；
- 全部外键、状态 `CHECK`、非负计数及领取索引 `(state, next_attempt_at, priority, id)`。

账号存在历史任务时只能禁用/归档，不能物理删除。时间统一存 UTC epoch。最终视频身份由规范路径、大小、mtime 与首尾固定块摘要组成，并在预上传、恢复和完成前复核；无需额外读取整个大文件计算 SHA-256。

任务领取字段至少包含 `lease_owner`、单调递增的 `lease_generation`、`lease_until`、`attempt` 和 `next_attempt_at`。使用 `BEGIN IMMEDIATE` 与条件更新原子领取，在半个 TTL 前续租；所有结果更新带 generation fencing。过期租约只能自动恢复纯本地步骤和可证明幂等的上传分块。

## 7. 可靠的录制会话与最终制品

当前 `EventCenter` 不持久，现有事件也没有共同 session ID。新增轻量 Recorder/Postprocessor listener，在返回录制回调前先幂等写入 `event_journal`；成功后再发送现有通知。数据库和 listener 必须在加载录制任务之前启动。

关联规则如下：

- `broadcast_session_key` 优先使用 `room_id + live_start_time`，缺失时生成并持久化替代键；
- 每次 `RecordingStartedEvent` 创建 `recording_run_id`；同一直播中应用重启可通过开放会话对账继续绑定；
- `VideoFileCreatedEvent` 立即分配不可变 `part_index`，分 P 顺序不得使用异步完成顺序；
- 视频、XML 和后处理完成事件均通过 run、路径关系和 part 绑定；
- `RecordingFinishedEvent` 表示正常闭合，`RecordingCancelledEvent` 表示中断，不能直接当成可投稿完成；未在同一直播中恢复的中断会话只能由管理页人工结束或跳过；
- 启动时扫描开放会话、journal 和相关文件，无法唯一归属的文件进入人工确认，不自动拼接。

`VideoFileCompletedEvent` 只表示原始录制文件关闭。只有收到该 part 的 `VideoPostprocessingCompletedEvent`/`PostprocessingCompletedEvent` 或明确后处理失败终态，才确定 `final_path`。这样可避免与原地元数据注入并发，也不会在 remux 删除源文件后继续引用旧路径。30 秒大小/mtime 稳定检查仅是最终制品的额外保护。

会话必须同时满足“录制已闭合”和“所有已创建 part 达到后处理终态”才可创建投稿。XML 可以稍后完成；缺失不阻止视频上传，但对应后处理明确标记等待或跳过。

## 8. 账号与扫码刷新

二维码状态为 `created → pending → scanned → confirmed`，终态为 `expired/cancelled/failed`。每个 auth code 只有一个服务端 poller，最长不超过上游 180 秒，并明确处理未扫码、已扫码未确认和过期状态。

确认后将以下内容作为一个不可拆分、版本化凭据包保存：

- `access_token`、`refresh_token`、`mid`、`issued_at`、`expires_at`、签名族；
- 完整 Cookie jar 及派生 CSRF；
- APP 和 Web 各自的设备标识。

入库前校验 token `mid`、Cookie `DedeUserID` 与账号查询 UID 一致。评论和回灌前还要确认稿件 owner 与账号一致；写操作禁止回落到现有全局浏览器 Cookie。

现有全局浏览器 Cookie 设置继续兼容旧读取链路，但不会自动迁移、拼接或覆盖结构化凭据包。

每天一次健康检查、距过期不足 72 小时刷新是本地运维策略，不是 B 站协议保证。刷新采用固定 `biliup` 基线的 OAuth info 与 refresh 端点；网络层可有限重试一次刷新事务，但远端结果未知时进入 `refresh_unknown`，不自动重复。只有新凭据校验通过后，才在一个数据库事务中原子替换整个凭据包。

## 9. 投稿与审核状态流

主流程为：

`持久录制事件 → 最终制品就绪 → 创建投稿任务 → 分 P 上传 → 提交投稿 → 等待审核`

首期全局一次只上传一个稿件，分块并发使用固定小值，避免占满 NAS 磁盘和上行带宽。每个分块结果、上传会话和完成响应均持久化；服务端上传会话失效时只重建受影响的 part。

创建任务时保存房间策略快照。启用自动评论时，投稿必须保持评论区开放，并确保该房间录制设置会保存 SC 和上舰；启用回灌时必须保持弹幕开放。冲突配置直接拒绝创建任务；旧 XML 缺少 SC/上舰时显示“源文件无此数据”，不伪造内容。首期不自动删除或移动录像。

`ReviewWatcher` 每 15 分钟按账号读取近期审核列表。写入 CID 前校验账号 owner、AID/BVID、远端 filename、分 P 数量和顺序；不能只按返回数组位置匹配。审核拒绝保存原因并停止后处理。审核通过后并列创建评论和回灌两个独立子工作流；`AccountWriteGate` 优先尝试评论，但评论失败不会阻塞回灌。

## 10. 自动评论

自动评论只整理 SC 和上舰信息，不包含 AI 或宣传文案：

```text
SC 和上舰列表
1#00:12:34  用户A发送了30元留言：……
2#00:03:05  用户B开通了舰长
```

内容按分 P 和时间排序，达到 1000 字符阈值时拆分：第一段为根评论，其余为楼中楼回复，并单独尝试置顶根评论。没有 SC/上舰时标记“无内容，已跳过”。置顶失败不得重发评论。

评论请求在发送前保存请求指纹并进入 `in_flight`。若响应明确成功则保存 RPID；若超时发生在可能已送达之后，则进入 `unknown_outcome`，先查询近期评论并按 owner、稿件、父级和完整内容对账。无法唯一匹配时要求人工确认，禁止自动重发。验证码 `12015`、评论区关闭和权限错误分别进入人工暂停或永久失败，不自动处理验证码。

## 11. 弹幕回灌

回灌向每个 CID 写入原生视频弹幕，不把文字压制进视频。普通弹幕尽量保留播放进度、模式、字号和颜色；SC/上舰转换为包含用户名、金额或舰长信息的顶部文字弹幕，但不伪装为付费 SC。

默认过滤：抽奖触发、已识别系统消息、房间黑名单，以及 XML 中同一原始事件的重复记录。不得按文本全局去重。为支持新录制的抽奖、用户等级和粉丝勋章过滤，XML writer 以向后兼容的可选属性保存这些原始字段；旧 XML 缺少字段时不据此丢弃。用户等级和粉丝勋章过滤默认关闭。

所有通过过滤的弹幕都进入持久队列：

- 不设置软件每日数量上限；
- 不设置每个分 P 数量上限；
- 每次读取和批量入库 500 条只为控制内存；
- 同账号在不同稿件间公平轮转，单个大直播不能永久饿死其他房间；
- 每个稿件内优先 SC/上舰，再按原始时间处理普通弹幕。

25 秒是参考 Java 行为得到的初始最小间隔，约 144 条/小时/账号，不是安全额度保证。灰度期间只能根据真实响应向更慢方向调整；不以自动增加速度解决积压。同一视频只使用投稿账号，不使用账号池分摊。

例如 10,000 条弹幕按 25 秒需要约 69 小时。页面必须展示每账号的新增速率、完成速率、净积压和预计完成时间。若数据库或磁盘达到水位，只暂停新的 XML 导入与写入子任务，不得阻塞录制或删除原 XML。

弹幕接口没有已知客户端幂等键。明确成功时保存 dmid；结果未知时标记 `unknown_outcome`，因缺少可靠远端逐条对账能力，必须人工决定“视为成功”或“重试并接受重复风险”，系统不能自动重发。

## 12. 错误、熔断与外部写一致性

非幂等外部写统一使用：

`prepared → in_flight → confirmed | unknown_outcome | failed_permanent`

- 投稿提交结果未知：先查询近期稿件并匹配账号、远端 filename、分 P 和任务指纹；不唯一则人工确认；
- 评论结果未知：按第 10 节远端对账；
- 弹幕结果未知：不自动重试；
- 置顶结果未知：查询根评论置顶状态后再决定；
- 过期租约不能把 `in_flight` 直接恢复为 `prepared`。

错误策略：

- 网络连接失败且可确认请求未发送：指数退避后有限重试；
- `5xx` 或连接中断导致结果未知：进入 `unknown_outcome`；
- token 失效：暂停写操作并执行固定刷新流程；
- `36703`：只熔断弹幕发送桶，逐级延长等待；连续出现账号级异常时再升级为账号熔断；
- `36704`：该条不消耗，重新同步审核和 CID；
- `36715`：仅能确认“当日操作超过上限”，重置时区和时刻未知；至少暂停 24 小时后只做一次低频探测，或等待人工恢复，不能假定北京时间零点必然解除；
- 评论验证码或风控挑战：暂停并提示人工，不自动过验证码；
- 内容、时间、权限或稿件拒绝：记录永久错误，不盲目重试。

同账号的刷新、投稿 finalize/submit、评论、置顶和弹幕共享 `AccountWriteGate`、熔断状态与凭据版本。系统不切换账号、代理、token、签名族或设备身份规避限制。

提供三个独立紧急开关：停止自动投稿、停止自动评论、停止弹幕回灌。关闭任何写功能都不停止录制。

## 13. 应用生命周期与运行边界

启动顺序：

`打开 DB → 权限/迁移/完整性检查/单进程锁 → 启动 journal listener → 对账开放会话和未知结果 → 启动 worker/watcher → 加载录制任务`

关闭或应用内重启顺序：

`停止领取新任务和管理变更 → 保持 journal/DB 开启 → 停止录制并等待后处理事件入库 → 在分块检查点停止 worker → flush/checkpoint → 关闭 DB`

同步 SQLite、XML `iterparse`、文件摘要和加密工作放入有界 executor 或单写 DB actor，兼容 Python 3.8，不使用 `asyncio.to_thread`、`TaskGroup` 等较新特性。上传流式读取固定分块，不把整个录像载入内存。

## 14. 管理页面

Angular 前端增加：

- 账号管理：扫码、状态、凭据有效期、暂停原因和重新登录；
- 房间投稿配置：账号、元数据、评论/弹幕开关与过滤；
- 上传任务中心：会话、分 P、上传/审核、评论、回灌、未知结果和错误；
- 任务详情：暂停、继续、跳过、远端对账，以及对 `unknown_outcome` 的人工裁决。

页面明确提示：回灌弹幕会长期出现在投稿视频中，发送者归因于投稿账号，而不是原直播观众账号。敏感凭据永不返回前端。

## 15. 测试策略

后端新增 `pytest` 与异步测试支持。假服务和固定脱敏 fixture 覆盖：

- 协议矩阵、TV 二维码有界状态机、原子刷新和 UID 一致性；
- 管理路由在缺少 API Key 时 fail closed；
- 事件先持久化、会话跨重启关联、创建顺序和 cancelled 流程；
- 最终制品等待 remux/元数据注入完成，源文件删除后仍选择正确路径；
- SQLite 迁移、租约 fencing、分块恢复、文件身份和异常重启；
- 投稿/评论/弹幕“远端成功但响应丢失”的未知结果；
- XML 普通弹幕、SC、上舰、过滤、无数量裁剪和跨稿件公平调度；
- `36703`、`36704`、`36715`、验证码、token 过期及 `5xx`；
- 紧急开关关闭写入时录制不受影响。

Angular 使用 Jasmine/Karma 测试表单、状态、人工裁决和凭据不泄漏。CI 至少覆盖 Python 3.8 与当前 Docker Python，运行格式、静态、类型、迁移和异常恢复测试。Docker 冒烟覆盖新旧数据卷、错误密钥、非本地锁文件系统拒绝、正常关闭和强制终止恢复。

自动化测试不执行真实写操作。进入首批 3～5 个房间前，先在这批房间所用账号上执行一次受控协议 canary：一个短稿件、一个评论和一条弹幕；它不是额外的长期单房间灰度阶段。

## 16. 实施里程碑

该子系统按可独立验证的里程碑实施：

1. 安全管理面、SQLite、journal、迁移和测试骨架；
2. 扫码、凭据包、刷新与协议合同；
3. 最终制品聚合、UPOS、多 P 投稿和审核；
4. 自动评论及其远端对账；
5. 弹幕解析、公平队列、回灌与熔断；
6. 管理 UI、Docker 验证和线上灰度。

配套批量直播监控规格先于第 6 阶段完成。每个里程碑必须保持原有录制测试通过，不能用未完成的下一阶段掩盖失败。

## 17. 线上灰度与全量门槛

协议 canary 通过后，显式启用首批 3～5 个房间，至少连续运行 3 天，且每个房间至少完成一场完整直播。检查：

- 不漏录、不损坏、不漏 P，后处理最终文件选择正确；
- 不发生可自动避免的重复投稿或评论；未知结果进入人工状态；
- 评论、弹幕对应正确账号、BVID/CID 和播放时间；
- 网络中断、容器重启和紧急开关后安全恢复；
- 上传不造成其他房间漏录或 NAS 资源异常；
- 无持续性账号风控，异常均按预期暂停；
- 每账号回灌完成速率和净积压可测。

扩到全部房间前，除无严重故障外，还要求灰度期没有持续增长且无法消化的回灌积压。如果新增速率长期高于实测完成速率，则保留全部弹幕和现有限速，推迟全量回灌，而不是丢弃弹幕、提高请求速度或增加账号池。录制和投稿可按其独立指标决定是否扩容。

## 18. 完成标准

1. 配置管理认证和加密密钥后，纯 Python 单镜像可升级现有 Docker 数据卷；
2. 持久 journal、稳定会话键和最终制品边界确保不漏 P；
3. 扫码账号跨重启可用，刷新使用正确签名族并原子替换；
4. 投稿、多 P、审核、评论和回灌均有明确状态与错误语义；
5. 远端结果未知时不自动重复非幂等写，页面可对账或人工裁决；
6. 自动评论只包含 SC/上舰索引，不接入 AI；
7. 所有通过过滤的弹幕均入队，不因软件数量上限丢弃；
8. 自动化、Docker 冒烟、协议 canary 和 3～5 房间灰度达到本设计门槛。
