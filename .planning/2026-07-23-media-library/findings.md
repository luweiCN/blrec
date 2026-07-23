# Findings & Decisions

## Requirements

- 片段管理不再平铺，按来源直播场次聚合；一场直播可包含多个片段。
- 收藏一场直播后，在独立收藏列表创建一项，永久保存其多个分 P。
- 收藏文件移动到独立目录，不参与录像容量统计、按策略清理或低空间回收。
- 收藏项可预览、剪辑、再次投稿、删除、重命名并添加标签。
- 收藏项保留原录制场次及其 B 站投稿关联，至少展示 aid/bvid 和历史稿件。
- 外部直播可一次上传多个有序分 P；外部片段也可上传并集中保存。
- 外部上传内容与系统收藏内容具有相同的重命名、标签、预览、剪辑、投稿和删除能力。
- 用户进一步明确：永久媒体目录必须与 `rec` 同级，而不是位于 `rec` 内。
- 收藏直播和外部上传内容与片段一致，只允许显式手动删除；投稿成功、审核通过、保存天数、容量超限和低空间回收均不得删除。
- 系统生成片段必须可重命名，且剪辑页底部“已创建片段”和独立片段管理页两个入口都要提供该操作。

## Research Findings

- `recording_sessions` 已是“场次”聚合根，`recording_parts` 通过 `session_id` 和 `part_index` 表示多个分 P。
- `highlight_clips.source_session_id` 已关联来源录制场次，片段汇总查询也已有主播、直播标题和房间号；现有前端却按单条片段分页平铺。
- 片段当前保存在录制目录同级的 `clips/`，Docker/NAS 将其独立挂载为 `/clips`；它们本来就不参与录像保留策略。
- `upload_jobs.session_id` 具有唯一约束。现有 `repost_as_new` 会先写入 `upload_job_archives` 保存旧 aid/bvid，再复用原任务重新投稿。因此“再次投稿并保留历史”无需立即改成并行一对多任务。
- `RetentionManager` 的事件候选、容量候选、实时磁盘用量与持久化用量都按 `recording_sessions.source_kind='live'` 查询。只使用 `retention_mode='never'` 不能满足“不参与容量统计”。
- 本地删除工作器分别校验录制根和片段根的文件所有权。增加永久直播根后，删除、媒体读取和路径迁移都必须显式认识该根，不能绕过路径边界。
- 永久目录改为录制根同级的 `favorites/` 后，现有媒体读取和本地删除的单一录制根校验不再足够；必须只为媒体库成员增加精确的第二可信根。
- 媒体读取、上传任务、剪辑时间线和弹幕读取已经围绕 `recording_sessions`/`recording_parts` 工作。复用这组聚合可显著减少新功能分叉。
- `recording_sessions.source_kind` 目前 CHECK 为 `live|highlight`。SQLite 不能直接扩展 CHECK；为“外部导入”重建这个被大量外键引用的核心表风险较高。
- 可用的兼容方案是新增一对一媒体库条目记录 `origin=recording|upload`，而完整直播仍使用现有 session/parts；`source_kind='live'` 继续表示“整场、多分 P 的源媒体”，来源由媒体库条目区分。
- 收藏时若文件路径改变，需要同步 `recording_parts`、`upload_parts`、可能的 `cover_path` 以及正在引用源分 P 的剪辑安全条件。活跃 lease 下直接移动会产生竞态。
- 片段按来源场次聚合时不能先按片段分页再在浏览器分组，否则同一场次会跨页拆分；后端应按场次/组分页，再返回组内片段。
- `upload_job_archives` 目前没有任何管理 API 或 Angular 视图读取；媒体库详情必须把当前任务与归档行合并成按时间排序的投稿历史。
- 现有封面上传路由会把整个请求读入内存，只适合 2 MiB 图片。外部视频上传必须分块流式写入受控临时文件，不能复用该实现。
- 同级永久目录可能位于独立 bind mount。收藏实现必须明确处理同文件系统原子移动与跨文件系统移动的差异，不能假定 `os.replace` 永远成功。
- 目录位置变化不需要新增数据库列：现有 `storage_path`、`source_path`、`final_path` 和持久化移动计划已经保存绝对目标路径。需要改变的是运行时根目录推导、可信根校验和部署挂载。
- 路径切换仍应沿用“文件操作计划持久化 + 每个文件完成后事务内更新引用”的模式；跨文件系统复制必须保留源文件直到目标文件落盘并同步完成，才能满足崩溃恢复。
- 第二可信根只能是运行时推导出的精确 `favorites` 根，不能把整个录制根父目录加入白名单，否则会把配置、日志或其他 sibling 目录意外暴露给预览和删除接口。
- `RecordingMediaDescriptor` 已携带单个 `expected_root`；无需扩大成多根。查询媒体库 `storage_key` 后，普通录像使用 `rec`，媒体库条目使用精确的 `favorites/<storage-key>` 即可。
- `LocalDeletionWorker` 的 session 快照已包含媒体库分 P 和移动计划路径，但当前统一按 `rec` 校验；应为媒体库会话追加精确条目目录，并把跨文件系统复制临时文件纳入手动删除快照。
- 运行时已经集中创建内容读取器、删除工作器和媒体库服务，适合只推导一次同级 `favorites` 根并显式注入三个组件。
- 现有数据库路径更新仍使用参数化值；动态表名/列名仅来自代码内固定白名单。目录修正不引入新的用户可控 SQL 或 schema migration。
- `SpaceReclaimer` 只扫描录制根；永久目录移到同级后天然不在其扫描范围，但托管保留查询仍必须按媒体库成员排除。
- 媒体库当前复用投稿策略对话框且仍展示“本地录像”自动删除模式。虽然后端查询会排除媒体库会话，但这与“不要有这种策略”不一致；媒体库入口应隐藏该区块并强制提交 `retentionMode='never'`、`retentionDays=0`。
- 删除工作器已把 `media_library_items.state='moving'` 视为 blocker，因此跨文件系统复制可移出 SQLite 写事务：先持久化 moving，再在线程中复制，手动删除会等待移动结束，最后用短事务切换所有路径。
- 投稿策略对话框已有 `deferredSave` 的强制 `never/0` 先例；新增语义明确的 `manualDeletionOnly` 输入即可复用同一请求构造逻辑，不必另建媒体库专用对话框。
- 自动删除配置不在 `recording_sessions` 独立列中，而来自房间策略或场次 `upload_override_json`。因此永久语义应在 `SessionSubmissionManager.save_override` 对媒体库会话强制归一化为 `never/0`，同时继续保留 RetentionManager 的成员排除作为最终安全边界。
- 外部导入的 `staging_path` 位于 `favorites/<key>/incoming/` 且仍保留视频扩展名，可以沿用精确条目根的后缀校验；收藏跨挂载临时文件也应采用保留原扩展名的受控隐藏文件名。
- 删除工作器已有媒体库导入文件、上传临时文件和 failed move 目标的回归测试；将这些夹具改为 `rec`/`favorites` 同级并加入 `.move-<id>-<name>` 文件，可直接验证精确第二根和失败复制残留的手动清理。
- 投稿策略组件的请求构造和保留天数校验都只判断 `deferredSave`；`manualDeletionOnly` 必须同时控制模板可见性、校验跳过和请求 `never/0`，否则隐藏字段中的旧值仍可能阻止保存。
- 仓库的群晖 Compose 当前只挂载 `/cfg`、`/log`、`/rec`、`/clips`。同级目录若不新增 `/favorites` 持久化挂载会落入容器可写层，重建容器后丢失；Compose、环境变量示例、Docker VOLUME 和部署文档必须一起更新。
- 现有 ADR 的核心结论仍写“录制根内、避免独立挂载”，与最终需求相反；应修订为同级独立根，并记录跨文件系统复制/恢复这一实际代价。
- 路径列审计确认已覆盖 event、recording/upload part、repair path、cover 及媒体库计划；但跨文件系统复制会改变 inode，`upload_parts.file_identity` 与修复 identity 也可能失效。最终路径事务必须清空受影响的身份快照，使再次投稿/修复按新路径重新探测。
- UPOS 不接受缺失的 `file_identity`，因此不能简单清空。每个视频目标落盘后应按新路径生成 `FileIdentity` JSON，并在路径切换事务中写回所有命中该旧路径的 `upload_parts.file_identity`；若命中 `repair_original_path`，同步刷新 `repair_original_identity`。XML/封面无需身份快照。
- 媒体响应最终会用 `expected_root` 逐级 `O_NOFOLLOW` 打开文件；内容读取器仍需保证媒体库条目根解析后位于精确 `/favorites` 根内，防止条目目录被替换成指向外部的符号链接后把可信根一起带出边界。
- 外部导入未填房间号时，对外保持 `roomId=0`；数据库内使用保留的正整数，以满足现有 `highlight_clips.room_id > 0` 约束并保持剪辑可用。
- 系统生成片段名称已独立存于 `highlight_clips.name`，无需迁移或重命名文件；预览、下载和两个列表都会从同一字段读取，因此一个 PATCH 接口即可保持入口一致。

## Working Technical Design

### Aggregate shape

- `media_library_items`：一项对应一场永久直播或一个直接导入片段；持有 `session_id`、`kind`、`origin`、展示名称、备注、迁移状态、稳定存储键和时间戳。
- `media_library_tags` 与 `media_library_item_tags`：规范化标签及多对多关系。
- 视频、弹幕与上传所需的规范文件关系继续由 `recording_parts` 持有；手动导入会创建内部 session/run/parts。
- 生成的高光继续由 `highlight_clips` 持有，片段页查询以来源 session 聚合；直接导入的 `kind=clip` 条目与生成高光统一展示在媒体库“片段”页签中，但不伪造剪辑来源关系。
- 收藏录像常为 FLV，外部导入录像常为 MP4。媒体库原先把两者都直接绑定到原生 `<video>`，导致收藏 FLV 授权和 Range 响应正常却无法解码；预览必须按格式复用现有 FLV 播放适配。
- 剪辑编辑器要求明确的 `partId`，因此媒体库剪辑入口必须放在具体分 P 行内，不能从整场直播只传 `sessionId`。

### File layout

- 规划根目录：录制根同级的 `favorites/`；容器部署需要独立持久化挂载。
- 每项使用服务端生成、不可由上传文件名控制的稳定目录：`favorites/<storage-key>/`。
- 分 P 使用有序系统文件名，原始文件名仅作为展示元数据；弹幕与封面同目录保存。
- 收藏流程先在数据库中建立保护状态，确保保留策略立即排除，再执行可恢复移动，最后原子更新所有路径并标记 ready。

### Behaviour boundaries

- 录制中场次不能收藏；处理中的文件不能移动。
- 存在活跃上传/修复 lease 时返回可重试冲突，不静默打断远端副作用。
- 收藏重复请求必须幂等：已有条目直接返回；失败状态允许重试迁移。
- 媒体库删除删除本地条目和归属文件，但不删除 B 站稿件；历史稿件信息在确认删除前展示在破坏性提示中。
- 媒体库条目和系统生成片段的重命名都只改展示元数据，不改物理路径、已创建上传任务或远端稿件；标签变更独立、可重复提交。

## Issues Encountered

| Issue | Resolution |
|-------|------------|
| 原工作区有与本需求无关的未提交改动 | 新 worktree 从当前 HEAD 建分支，不复制也不触碰这些改动 |
| 现有 `.planning/.active_plan` 指向别的任务 | 使用技能脚本创建隔离计划 `2026-07-23-media-library` 并切换当前 worktree 指针 |
| 投稿归档没有读路径 | 新增只读投稿历史查询，而不是改变上传状态机的唯一任务约束 |
| 独立永久挂载会让大文件移动跨 mount | 最新要求仍采用同级独立目录；实现流式复制、目标原子落盘、源文件延后删除及重启恢复 |

## Resources

- `CONTEXT.md`：现有领域词汇
- `src/blrec/bili_upload/migrations/0001_initial.sql`、`0005_initial.sql`、`0010_initial.sql`、`0018_initial.sql`、`0026_initial.sql`
- `src/blrec/bili_upload/journal.py`：录制 session/part 查询模型
- `src/blrec/bili_upload/highlights.py`：片段保存、列表和投稿 session
- `src/blrec/bili_upload/retention.py`：容量统计与自动清理
- `src/blrec/bili_upload/deletion_worker.py`：文件归属与删除状态机
- `src/blrec/bili_upload/task_actions.py`：重新投稿和历史稿件归档
- `webapp/src/app/upload-tasks/clip-library/`：当前片段平铺页面

## Visual/Browser Findings

- 未启动带真实录像数据的交互式浏览器；Angular 模板由严格生产构建验证，组件行为由 ChromeHeadless 全量测试验证。
