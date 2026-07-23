# Progress Log

## Session: 2026-07-23

### Phase 1: Requirements & Existing-System Discovery

- **Status:** complete
- **Started:** 2026-07-23
- Actions taken:
  - 创建 `/Users/luwei/code/ai/blrec-media-library` worktree 和 `feat/media-library` 分支，基于 `f15ceff`。
  - 确认原工作区的未提交改动未进入新 worktree。
  - 阅读 `domain-modeling`、`Database Design Expert` 及其相关参考、`planning-with-files` 技能。
  - 核对 `CONTEXT.md`、数据库迁移、片段服务、保留策略、重新投稿归档、FastAPI 路由和 Angular 路由/页面。
  - 将用户新增的收藏列表、多分 P、稿件关联、重命名和标签要求纳入计划。
  - 核对媒体读取可信根、本地删除路径所有权、运行时 sibling 目录推导和历史稿件缺失的 API/UI 读路径。
- Files created/modified:
  - `.planning/.active_plan`
  - `.planning/2026-07-23-media-library/task_plan.md`
  - `.planning/2026-07-23-media-library/findings.md`
  - `.planning/2026-07-23-media-library/progress.md`

### Phase 2: Domain & Storage Design

- **Status:** complete
- Actions taken:
  - 形成媒体库条目一对一关联现有 session、复用 parts/上传/剪辑能力的工作模型。
  - 明确普通录像根与永久媒体根必须按条目动态选择，不能扩大为任意 sibling 路径。
  - 初版曾因跨 bind mount 风险将永久目录定为 `<recording-root>/favorites/`；Phase 7 按用户修正改为同级目录并补跨文件系统恢复。
  - 更新领域词汇，新增媒体库 ADR 和完整设计文档。
- Files created/modified:
  - `CONTEXT.md`
  - `docs/adr/0001-media-library-reuses-recording-sessions.md`
  - `docs/media-library-design.md`

### Phase 3: Backend Foundation (TDD)

- **Status:** complete
- Actions taken:
  - 先写 migration/约束/索引失败测试，确认旧版本停在 29 且新表不存在。
  - 新增 v30 媒体库迁移：条目、标签、分 P、崩溃可恢复文件移动及查询索引。
  - 新增 `MediaLibrary` 领域服务，覆盖收藏、外部多分 P 导入、上传、完成探测、列表、重命名/标签和投稿历史。
  - 收藏文件放入录制根同级的 `favorites/<storage-key>/`，移动计划落库并同步录像、上传任务和事件路径引用。
  - 增加媒体库在事件保留、容量候选、实时/持久化容量统计中的统一排除条件。
  - 永久根位于录制根扫描范围外；数据库保留管理器另外按媒体库成员排除容量统计和所有自动清理候选。
  - 接入 FastAPI 和运行时生命周期，补齐删除快照、重启恢复和管理审计。
  - 将已生成片段改为“先场次分页、再返回组内全部片段”。
  - 外部导入使用内部保留正房间号，对外仍表示未知来源，确保剪辑数据约束可满足。
- Files created/modified:
  - `src/blrec/bili_upload/migrations/0030_initial.sql`
  - `src/blrec/bili_upload/database.py`
  - `src/blrec/bili_upload/media_library.py`
  - `tests/bili_upload/test_database.py`
  - `tests/bili_upload/test_media_library.py`
  - `src/blrec/bili_upload/retention.py`
  - `src/blrec/disk_space/space_reclaimer.py`
  - `tests/bili_upload/test_retention.py`
  - `tests/disk_space/test_space_reclaimer.py`

### Phase 4: Backend Integration (TDD)

- **Status:** complete
- Actions taken:
  - 媒体库路由返回条目、有序分 P、标签和当前/历史 aid/bvid。
  - 删除使用现有可恢复 session 删除状态机，不影响 B 站远端稿件。
  - 运行时启动恢复中断的收藏移动，并将导入中断分 P 标记为可重试失败。

### Phase 5: Frontend

- **Status:** complete
- Actions taken:
  - 新增媒体库导航与页面，直播收藏和外部片段分类浏览。
  - 实现多文件选择、分 P 顺序调整、逐个流式上传、进度和失败重试。
  - 实现预览、下载、剪辑、投稿设置、重新投稿、历史稿件、重命名/标签和删除入口。
  - 片段页改为可展开的来源场次组，保留原有片段级操作。
  - 录制任务的已结束场次增加“收藏到媒体库”。

### Phase 6: Verification & Delivery

- **Status:** complete
- Actions taken:
  - 为上传与删除、完成探测与删除增加数据库状态仲裁，避免文件写入和删除并发。
  - 删除快照同时覆盖移动计划的源/目标路径，处理“文件已移动但事务未提交”的崩溃窗口。
  - 媒体库列表批量读取分 P、标签和投稿历史，避免按条目产生 N+1 查询。
  - 调整媒体库列表及片段分组索引，并用 SQLite 查询计划验证排序与分组无需临时 B-tree。
  - 补齐 MKV、MOV、WebM 的媒体响应类型，使所有允许导入的常见封装都进入视频预览链路。
  - 完成后端、前端全量回归、静态检查、生产构建和最终 diff 检查。

### Phase 7: Sibling Permanent Storage & Manual-Only Deletion

- **Status:** complete
- **Started:** 2026-07-23
- Actions taken:
  - 将用户修正后的最终规则记录为：`favorites` 与 `rec` 同级，所有永久媒体只允许手动删除。
  - 重新启用数据库设计、SQLite 与文件化规划检查，准备核对路径可信边界、跨文件系统移动和所有自动清理入口。
  - 确认现有数据库路径字段均为绝对路径，不需要再加迁移；运行时、内容读取和删除快照需要显式接入同级永久根。
  - 确认媒体描述可按条目选择精确 `favorites/<storage-key>`，无需信任整个父目录。
  - 确认媒体库投稿界面仍暴露自动删除选项；将增加媒体库专用的“仅手动删除”输入，并在提交请求层强制 `never/0`。
  - 确认删除工作器会等待 moving 状态，跨挂载复制可以在数据库事务外完成而不让 SQLite 长时间持有写锁。
  - 确认保留设置来自投稿策略 JSON；除 UI 隐藏外，场次投稿设置保存层也要对媒体库会话强制 `never/0`。
  - 先写同级根、跨设备中断恢复、四种自动保留模式与投稿设置强制永久的测试；定向运行得到 4 个预期失败，其余新增保留/回收测试通过。
  - 实现同级 `favorites` 根、跨设备流式复制与重复文件恢复；复制在 SQLite 写事务外执行，完成后短事务统一更新引用。
  - 场次投稿读取和保存对媒体库成员统一归一化为 `retention_mode=never`、`retention_days=0`；移除录制根内 `favorites` 的旧回收特判。
  - 开始第二组 TDD：媒体描述按条目根签名、同级永久目录手动删除、失败跨挂载临时文件清理和运行时统一装配。
  - 内容读取器按媒体库 `storage_key` 返回精确条目根；删除工作器只接受 `rec` 和当前条目的 `favorites/<key>`，并清理跨挂载残留临时文件。
  - 运行时统一推导并注入同级 `favorites` 根；第二组 3 个定向测试由 red 转 green。
  - 已定位前端最小改动面：投稿策略组件新增一个输入并覆盖模板、校验、请求三个位置，媒体库只需传入常量。
  - 投稿策略对话框新增 `manualDeletionOnly`：媒体库隐藏自动删除区、跳过隐藏字段校验并强制请求 `never/0`；组件定向测试 `21 SUCCESS`。
  - 核对部署文件后确认必须新增 `/favorites` 挂载；开始同步 ADR、设计文档、群晖 Compose、环境变量示例和镜像卷声明。
  - 已同步 `/favorites` 持久化卷、群晖环境变量与安装/升级说明；ADR 改为记录同级目录、跨挂载复制和仅手动删除。
  - 自审差异时恢复了 `space_reclaimer.py` 的原有 CRLF，且该文件已回到基线实现；同级永久根天然不在旧扫描范围，无需保留嵌套目录特判。
  - 第一轮集成回归通过：后端相关 8 个测试文件 `110 passed`；投稿策略与媒体库前端 `24 SUCCESS`。
  - 全表路径审计发现跨挂载复制除路径外还会使上传文件 identity 失效；在最终静态检查前补充 identity 重置与回归断言。
  - 进一步确认 UPOS 要求 identity 非空，修正方案改为在移动后重算并写回，而不是置空。
  - 收藏完成事务现会按新路径重算 `FileIdentity`，同步刷新上传与修复身份快照；同文件系统和跨设备恢复测试均通过。
  - 变更文件 Flake8、全量 mypy（262 个源文件）及前端定向 ESLint 均通过。
  - 内容读取器补充条目目录符号链接越界防护；精确根与越界拒绝测试均通过。
  - 后端全量回归 `1564 passed, 1 skipped`，前端全量回归 `480 SUCCESS`。
  - Black、isort、Flake8、mypy 全量检查通过；Angular 生产构建、Python sdist/wheel 构建和 Compose 配置解析通过。
  - 最终 `git diff --check` 通过；构建输出均写入临时目录，原工作树保持无改动。

### Phase 8: Generated Clip Rename

- **Status:** complete
- **Started:** 2026-07-23
- Actions taken:
  - 确认 `highlight_clips.name` 是独立展示元数据，无需 schema migration，也不应改物理视频/XML 或远端稿件。
  - 先补服务、路由、共享 Angular 服务及两个页面入口的失败测试，再实现 `PATCH /api/v1/highlights/clips/{id}`。
  - 后端裁剪名称首尾空格、限制 1–200 字符、更新时间并记录 `highlight_clip_renamed` 审计事件。
  - 独立片段管理页新增重命名对话框；剪辑页底部“已创建片段”新增行内重命名、保存与取消。
  - 相关后端测试 `40 passed`，三个前端相关 spec `101 SUCCESS`。
  - 整仓后端 `1567 passed, 1 skipped`，整仓前端 `484 SUCCESS`；后端全量静态检查和变更前端 ESLint 通过。
  - 首次生产构建暴露剪辑页组件样式超过 10 KiB 硬上限 338 字节；复用既有布局样式并移除新增样式后生产构建通过。

## Test Results

| Test | Input | Expected | Actual | Status |
|------|-------|----------|--------|--------|
| worktree 基线 | `git status --short --branch` | 新分支且无产品代码改动 | `## feat/media-library` | pass |
| v30 migration | 两个 migration/schema 定向测试 | 升级到 30，约束与索引存在 | `2 passed` | pass |
| 媒体库领域服务（首轮） | `test_media_library.py` | 收藏、冲突、分批上传均通过 | `3 passed, 1 failed`；测试标签样例超过 40 字符契约 | expected red |
| 媒体库领域服务 | service + schema 定向测试 | 收藏、分批上传、完成探测通过 | `5 passed` | pass |
| 永久容量保护（red） | retention + fallback 定向测试 | 媒体库不进入任何回收链路 | `2 failed` | expected red |
| 永久容量保护 | retention 全文件 + fallback | 原有保留逻辑不回归，永久目录排除 | `12 passed` | pass |
| 后端整合回归 | 11 个相关测试文件 | 收藏/导入/删除/分组/路由/运行时通过 | `168 passed, 6 failed`；6 个旧 migration 断言仍期望 29 | expected red |
| v30 旧库升级 | `test_database.py` | 所有旧版本升级到 30 且保留数据 | `19 passed` | pass |
| Angular 开发构建 | `ng build --configuration development` | 模板与严格类型编译通过 | pass，仅已有 CommonJS 警告 | pass |
| 前端相关测试（首轮） | 7 组相关 spec | 媒体库、片段聚合、收藏入口和路由通过 | `90 passed, 1 failed`；下拉菜单未展开时不渲染文案 | expected red |
| 前端定向 lint | 本次修改的 TS/HTML | 无 lint 问题 | pass | pass |
| 后端完整回归 | `python -m pytest -q` | 无行为回归 | `1555 passed, 1 skipped` | pass |
| 后端静态检查 | Black、isort、Flake8、mypy | 全部通过 | `mypy: 262 source files` | pass |
| 前端完整回归 | Karma + ChromeHeadless | 全部通过 | `479 SUCCESS` | pass |
| 前端整仓 lint | `ng lint` | 区分本次与既有问题 | 本次文件通过；5 个未改旧组件仍有基线错误 | baseline |
| Angular 生产构建 | 临时输出目录 | 构建成功且不改打包静态资源 | pass；仅预算/CommonJS 警告 | pass |
| Python 包构建 | `python -m build --outdir /tmp/...` | sdist/wheel 成功且包含 v30 迁移和新模块 | pass | pass |
| 最终差异检查 | `git diff --check` | 无空白错误、无生成产物 | pass | pass |
| Phase 7 路径/永久策略（red） | 媒体库、保留、场次投稿、低空间定向测试 | 同级根、跨设备恢复、`never/0` | 4 failed, 11 passed；失败均对应尚未实现的新约束 | expected red |
| Phase 7 核心存储/保留（green） | 同上 | 新约束全部通过 | `15 passed` | pass |
| Phase 7 可信根/手动删除 | 内容读取 + 删除工作器定向测试 | 精确条目根、同级文件与 move 临时文件可删除 | `3 passed` | pass |
| Phase 7 前端仅手动删除 | 投稿策略组件 Karma | 不显示自动删除，提交 `never/0` | `21 SUCCESS` | pass |
| Phase 7 集成回归 | 8 个后端测试文件 + 2 个前端 spec | 存储、恢复、删除、保留、装配、部署契约 | `110 passed`; `24 SUCCESS` | pass |
| Phase 7 后端完整回归 | `python -m pytest -q` | 无行为回归 | `1564 passed, 1 skipped` | pass |
| Phase 7 前端完整回归 | Karma + ChromeHeadless | 无行为回归 | `480 SUCCESS` | pass |
| Phase 7 后端静态检查 | Black、isort、Flake8、mypy | 全部通过 | `262 files` | pass |
| Phase 7 Angular 生产构建 | 临时输出目录 | 严格模板与生产优化构建成功 | pass；仅既有预算/CommonJS 警告 | pass |
| Phase 7 Python 包构建 | 临时输出目录 | sdist/wheel 包含新模块和 v30 迁移 | pass | pass |
| Phase 7 Compose 配置 | `docker compose ... config --quiet` | `/favorites` 挂载配置合法 | pass | pass |
| Phase 7 最终差异检查 | `git diff --check` | 无空白错误或生成产物 | pass | pass |
| Phase 8 片段重命名（red） | 服务、路由和两个 Angular 页面 | 两处可重命名且拒绝空名称 | 后端 3 项失败；前端缺少接口/组件状态而编译失败 | expected red |
| Phase 8 相关后端回归 | `test_highlights.py` + routes | 名称持久化、校验、路径不变、API 可用 | `40 passed` | pass |
| Phase 8 相关前端回归 | 共享服务 + 两个页面 spec | 两处入口、校验和刷新均可用 | `101 SUCCESS` | pass |
| Phase 8 后端完整回归 | `python -m pytest -q` | 无行为回归 | `1567 passed, 1 skipped` | pass |
| Phase 8 前端完整回归 | Karma + ChromeHeadless | 无行为回归 | `484 SUCCESS` | pass |
| Phase 8 静态检查 | Black、isort、Flake8、mypy、变更前端 ESLint | 全部通过 | pass | pass |
| Phase 8 Angular 生产构建 | 临时输出目录 | 严格模板和样式硬预算通过 | pass；仅既有 warning 预算/CommonJS 警告 | pass |

## Error Log

| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
| 2026-07-23 | 数据库技能展示名路径不存在 | 1 | 使用实际 slug 目录 `database-design-expert` |
| 2026-07-23 | 跨父目录查找 AGENTS.md 耗时过长 | 1 | 中断后缩小到仓库根约束 |
| 2026-07-23 | 猜测的 web 测试文件路径不存在 | 1 | 通过 `rg --files tests` 获取实际文件名 |
| 2026-07-23 | 合并检查中的 `rg` 因投稿归档无 API/UI 引用而退出 1 | 1 | 记录为缺失功能，不重复相同搜索 |
| 2026-07-23 | worktree 缺少前端依赖导致首次构建无法解析模块 | 1 | 使用被 gitignore 的 `node_modules` 软链接复用原工作区依赖 |
| 2026-07-23 | development build 写入打包目录 | 1 | 验证后恢复跟踪产物并清除本次未跟踪产物 |
| 2026-07-23 | 6 个旧迁移测试仍断言最高版本 29 | 1 | 更新为 30，`test_database.py` 19 项全通过 |

## 5-Question Reboot Check

| Question | Answer |
|----------|--------|
| Where am I? | Phase 8，补齐两个页面的系统生成片段重命名 |
| Where am I going? | 已完成实现与全量验证，等待评审或提交 |
| What's the goal? | 永久媒体库、场次聚合片段、外部上传与完整操作 |
| What have I learned? | 见 `findings.md` |
| What have I done? | 见本文件 Phase 1–8 |
