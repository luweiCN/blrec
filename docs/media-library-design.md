# 媒体库设计

## 目标

媒体库负责长期保存完整直播和外部片段。完整直播以场次为聚合根，包含有序分 P、弹幕、封面、当前上传任务及投稿历史；片段管理按来源场次聚合。媒体库内容不参与录像容量统计、事件保留策略或低空间回收，只允许显式手动删除。

本设计不支持并行创建多个活跃投稿任务。再次投稿沿用现有 `repost_as_new`：旧 aid/bvid 进入投稿历史，原任务重置后重新排队。

## 数据关系

```text
recording_sessions 1 ── 0..1 media_library_items
        │                         │
        ├── * recording_parts     ├── * media_library_parts
        ├── 0..1 upload_jobs      ├── * tags（经关联表）
        ├── * upload_job_archives └── * file_moves
        └── * highlight_clips（作为来源场次）
```

`media_library_items.kind` 为 `broadcast` 或 `clip`，`origin` 为 `recording` 或 `upload`。系统收藏创建 `broadcast/recording`；外部多分 P 创建 `broadcast/upload`；外部单片段创建 `clip/upload`。

## Schema

### `media_library_items`

- `session_id`：唯一外键，删除场次时级联删除条目。
- `storage_key`：服务端生成的稳定随机键，唯一且不接受用户输入。
- `display_name`、`note`：只影响展示，不改变物理路径。
- `state`：`uploading | moving | ready | failed`。
- `error`：仅失败状态使用的可重试诊断。
- `created_at`、`updated_at`：列表排序和审计时间。

### `media_library_parts`

- `(item_id, part_index)` 唯一，表达用户选择的分 P 顺序。
- `recording_part_id` 在收藏时立即存在，在外部上传完成探测后补齐。
- `original_filename` 仅用于展示；`storage_path` 与可选 `staging_path` 均由服务端生成。
- `expected_size`、`received_size` 和 `state` 用于流式上传校验与恢复。

### 标签

`media_library_tags` 保存规范化名称，`media_library_item_tags` 表达多对多关系。名称去除首尾空白，长度 1–40，同一名称按 SQLite `NOCASE` 唯一。

### `media_library_file_moves`

收藏前先持久化每个旧路径到目标路径的计划。同一文件系统使用原子重命名；跨文件系统则在目标目录流式复制到受控隐藏文件，完成同步和原子落盘后才删除源文件。文件操作在 SQLite 写事务外执行，随后用短事务更新全部路径引用。恢复逻辑覆盖“源和目标并存”“仅目标存在”及复制临时文件残留。

## 文件布局

```text
<recording-root>/../favorites/<storage-key>/
├── cover.<ext>
├── part-0001.<ext>
├── part-0001.xml
├── part-0002.<ext>
└── incoming/
    └── part-0002.<ext>   # 尚未完成的外部上传
```

文件名、扩展名和目标目录均由服务端白名单生成。上传提供的原始文件名不会进入路径。收藏后的录像、上传任务和剪辑源都更新为目标路径；展示重命名不触碰这些路径。

## 状态与冲突

### 收藏

1. 仅接受已结束、未删除且至少有一个可用视频分 P 的场次。
2. 若存在录制/后处理、媒体索引或带 lease 的上传/修复/片段任务，返回 409，用户稍后重试。
3. 创建 `moving` 条目和文件计划后，保留策略立即排除该场。
4. 逐文件重命名或跨挂载复制，并同步 `recording_parts`、`upload_parts`、封面、事件和剪辑来源缓存。
5. 全部完成后转为 `ready`；失败转为 `failed`，已移动文件仍受永久保护，可重试恢复。
6. 重复收藏返回原条目，不创建副本。

### 外部上传

1. 创建导入草稿，声明类型、展示名称、可选来源信息及有序文件名/大小。
2. 每个分 P 通过独立 PUT 请求流式写入 `incoming/`，大小不能超过声明值。
3. 全部分 P 上传后显式完成；服务端逐个运行 ffprobe，拒绝空文件或无视频流文件。
4. 探测成功后创建 `recording_parts`，按累计时长设置时间线并将条目标记为 `ready`。
5. 中断的 `uploading` 分 P 可覆盖重试；删除草稿会删除已写入文件。

未填写 B 站房间号时，API 保持 `roomId=0` 的“未知来源”语义；内部 session 使用保留的正整数房间号，以满足现有剪辑模型的正数约束，因此外部直播仍可正常剪辑。

## API

- `GET /api/v1/media-library`：按类型和关键词分页，每项包含当前与历史稿件。
- `GET /api/v1/media-library/{id}`：条目、分 P、标签和投稿历史。
- `POST /api/v1/media-library/favorites/{sessionId}`：收藏现有场次。
- `POST /api/v1/media-library/imports`：创建外部直播或片段草稿。
- `PUT /api/v1/media-library/{id}/parts/{index}/content`：流式上传单个分 P。
- `POST /api/v1/media-library/{id}/complete`：探测并完成导入。
- `PATCH /api/v1/media-library/{id}`：修改展示名称、备注或标签。
- `DELETE /api/v1/media-library/{id}`：排队删除本地条目及归属文件，不删 B 站稿件。
- `GET /api/v1/highlights/clips/groups`：按来源场次分页返回片段组。
- `PATCH /api/v1/highlights/clips/{id}`：修改系统生成片段的展示名称，不改物理文件或已有稿件。

预览、弹幕、剪辑、投稿设置和再次投稿继续使用已有 session/part/job API，媒体库响应提供对应 ID。收藏录像可能仍为 FLV，因此媒体库预览必须复用录像播放器的 FLV/MSE 适配，不能把签名地址直接交给原生 `<video>`。

媒体库投稿设置不显示本地自动删除选项，前后端均把保留模式固定为 `never/0`。即使存在旧房间策略，保留工作器仍按媒体库成员关系排除该场次。

## 页面层级

- “媒体库”是统一入口，包含“直播收藏”和“片段”两个页签，不再在主导航中单列片段管理。
- “直播收藏”：一行/卡片代表一场永久直播，展开后显示分 P、投稿历史和操作；预览和剪辑都位于具体分 P 上。
- “片段”：同一页签内分为直播剪辑片段和外部导入片段。直播剪辑片段按来源场次聚合，外部导入片段使用媒体库条目管理；两类都支持各自已有的预览、重命名、投稿和删除操作。
- “剪辑”：底部“已创建片段”列表同样提供重命名入口，并与片段管理页共用名称和接口。
- 录像列表：已收藏显示永久标识，未收藏的稳定场次显示“收藏”入口。
- 上传对话框：完整直播允许多选文件并调整分 P 顺序；片段限制为单文件。上传完成前展示逐分 P 进度与失败重试。

## 部署目录

- 本机运行：若录像根为 `<data>/rec`，永久根固定为 `<data>/favorites`。
- 容器运行：`/rec` 与 `/favorites` 是同级独立持久化卷；群晖默认宿主目录分别为 `.../rec` 和 `.../favorites`。
- `/favorites` 不能只存在于容器可写层。升级前应创建宿主目录并保持原有 `/cfg`、`/log`、`/rec`、`/clips` 挂载不变。

## 查询与索引

- 媒体库列表索引：`(kind, created_at DESC, id DESC)`，直接覆盖按类型分页的排序。
- 分 P：`(item_id, part_index)` 主键和 `recording_part_id` 部分唯一索引。
- 标签关联：主键 `(item_id, tag_id)`，另建 `(tag_id, item_id)`。
- 片段组：以“来源场次；无来源时为独立片段”表达式开头、再按创建时间排序的部分索引，直接覆盖分组和组内读取。
- 片段分页先选择来源组，再读取组内全部片段，避免同一场直播跨页拆分。

## 验收场景

- 同一直播有三个分 P、五个片段：收藏列表只有一项且含三个分 P；片段页只有一个组且含五项。
- 已投稿直播收藏后：详情同时显示当前 bvid 与重新投稿前的历史 bvid。
- 收藏容量 20 GiB：录像容量状态不增加，事件清理和低空间回收均不选择其分 P。
- 收藏中进程退出：重启后从文件计划恢复，不丢失路径，不产生第二个条目。
- `/rec` 与 `/favorites` 为不同挂载：收藏通过流式复制完成，复制中断后可重试，源文件在目标完整落盘前不删除。
- 上传三分 P 时第二个断线：第一、三个结果保留，第二个可覆盖重试，完成前不能预览或投稿。
- 文件名为路径穿越或 SQL 字符串：物理路径仍由 storage key/part index 生成，标签与搜索全部参数化。
- 删除媒体库项：本地文件和数据库关系删除，远端 B 站稿件保持不变。
- 在剪辑页或片段管理页重命名系统生成片段：两处立即读取同一新名称，视频/XML 路径和已有 B 站稿件不变。
- 收藏的 FLV 与外部导入的 MP4 均可在媒体库预览；FLV 走现有播放器适配，MP4 保持原生播放。
- 多分 P 直播只在每个具体分 P 上显示剪辑入口，链接包含对应 `recordingPartId`。
