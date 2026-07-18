# 分 P 增量预上传与时间轴修正 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 已封口分 P 在直播结束前完成可恢复的预上传但不投稿，并修正高光时间轴轨道、悬浮参考线及浮层裁切问题。

**Architecture:** 在现有 `upload_jobs` 上增加 `preupload_finalized` 生命周期标记，开放场次和已结束场次共用同一个上传任务及 UPOS 分块记录。协调器负责创建、追加分 P、取消和最终冻结配置；上传执行器只允许已冻结任务调用投稿接口。前端从该标记和内部上传状态派生用户可读的预上传阶段，时间轴则统一轨道几何和 Pointer Events，并把浮层交给全局 overlay。

**Tech Stack:** Python 3、SQLite、FastAPI/Pydantic、pytest、Angular 15、TypeScript 4.9、Angular CDK、ng-zorro、Jasmine/Karma。

## Global Constraints

- 不新增上传任务状态枚举；既有历史任务迁移后必须保持正式任务语义。
- 开放场次绝不调用 `submit_archive`；直播结束后只提交一次包含全部分 P 的稿件。
- 同账号的已确认远端文件可复用；最终账号变化时必须清空远端引用并重传。
- 不改变 `room_id + live_start_time` 的场次识别规则。
- 不允许在录制中的不稳定尾部创建裁剪边界。
- 不使用 worktree；只修改本需求直接涉及的文件。

---

### Task 1: 持久化预上传生命周期

**Files:**
- Create: `src/blrec/bili_upload/migrations/0022_initial.sql`
- Modify: `tests/bili_upload/test_database.py`
- Modify: `src/blrec/bili_upload/journal.py`

**Interfaces:**
- Produces: `upload_jobs.preupload_finalized INTEGER NOT NULL DEFAULT 1`
- Produces: `UploadJobProgress.preupload_finalized: bool`

- [ ] **Step 1: 写迁移失败测试**

在 `test_database.py` 的上传任务列断言中加入 `preupload_finalized`，并新增断言验证旧式插入得到值 `1`。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest -q tests/bili_upload/test_database.py`

Expected: FAIL，提示 `preupload_finalized` 不存在。

- [ ] **Step 3: 添加最小迁移和领域字段**

```sql
ALTER TABLE upload_jobs
ADD COLUMN preupload_finalized INTEGER NOT NULL DEFAULT 1 CHECK (
    preupload_finalized IN (0,1)
);
```

在 `UploadJobProgress`、任务查询和构造函数中读取为 `bool`。

- [ ] **Step 4: 运行数据库测试**

Run: `pytest -q tests/bili_upload/test_database.py`

Expected: PASS。

### Task 2: 创建、追加和最终冻结预上传任务

**Files:**
- Modify: `tests/bili_upload/test_upload.py`
- Modify: `src/blrec/bili_upload/upload.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/bili_upload/session_submission.py`
- Modify: `src/blrec/bili_upload/task_actions.py`
- Modify: `tests/bili_upload/test_session_submission.py`
- Modify: `tests/bili_upload/test_task_actions.py`

**Interfaces:**
- Produces: `UploadCoordinator.sync_live_sessions() -> List[int]`
- Produces: `UploadCoordinator.prepare_waiting_jobs() -> List[int]`，可向既有任务幂等追加稳定分 P
- Consumes: `upload_jobs.preupload_finalized`

- [ ] **Step 1: 写开放场次预上传失败测试**

新增测试：开放场次 P1 为稳定 `ready`、P2 仍录制时，`sync_live_sessions()` 创建 `preupload_finalized=0` 的可见任务，`prepare_waiting_jobs()` 只写入 P1；执行上传后 P1 为 `confirmed`，`submit_archive` 调用次数仍为 0。

- [ ] **Step 2: 运行目标测试确认失败**

Run: `pytest -q tests/bili_upload/test_upload.py -k preupload`

Expected: FAIL，提示 `sync_live_sessions` 尚不存在或开放场次未创建任务。

- [ ] **Step 3: 实现幂等同步和追加**

`sync_live_sessions()` 扫描 `source_kind='live'` 且未删除的开放/结束场次：开放场次按当前投稿决策创建临时快照任务；既有临时任务追加新的稳定 `recording_parts`；结束场次重新解析最新配置并原子设置 `preupload_finalized=1`。`prepare_waiting_jobs()` 不再要求 `session.state='closed'`，且使用 `UNIQUE(job_id,part_index)` 幂等追加而不是要求任务没有任何分 P。

`SessionSubmissionManager` 仅在任务仍为 `preupload_finalized=0` 时允许修改本场配置；设为不上传后由协调器取消临时任务。`skip_upload()` 对临时任务允许删除已确认的预上传状态，但不删除 `recording_parts` 或本地录像；恢复上传时清理本场 suppression。

- [ ] **Step 4: 将运行循环切到统一同步入口**

在 `runtime.py` 中每轮依次调用 `sync_live_sessions()`、`prepare_waiting_jobs()`、`run_once()`，保留 `resolve_finished_sessions()` 作为兼容入口并让其委托最终同步逻辑。

- [ ] **Step 5: 覆盖新增分 P、下播和配置变化**

新增测试验证：P2 封口后仅追加/上传 P2；下播时用最新标题且只投稿一次；关闭本场上传取消临时任务但不删除录像；账号变化清空 `upload_parts.remote_filename`、`upload_session_json` 与分块并重传；非账号设置变化保留已确认文件。

- [ ] **Step 6: 运行上传测试**

Run: `pytest -q tests/bili_upload/test_upload.py`

Expected: PASS。

### Task 3: 上传执行器只在最终冻结后投稿

**Files:**
- Modify: `tests/bili_upload/test_upload.py`
- Modify: `src/blrec/bili_upload/upload.py`

**Interfaces:**
- Consumes: `_Job.preupload_finalized: bool`
- Produces: 预上传完成后任务回到 `waiting_artifacts` 并释放租约；最终任务继续进入 `submitting`

- [ ] **Step 1: 写防提前投稿和重启恢复测试**

测试临时任务所有当前分 P 上传完成后状态为 `waiting_artifacts`、远端文件名保留、无投稿；重建 coordinator 后同一任务不重传 P1，新增 P2 后只传 P2。

- [ ] **Step 2: 运行目标测试确认失败**

Run: `pytest -q tests/bili_upload/test_upload.py -k 'preupload and (submit or restart)'`

Expected: FAIL，当前 `_process()` 会调用投稿接口。

- [ ] **Step 3: 实现最终化闸门**

扩展 `_load_job()` 读取标记；上传完当前分 P 后再次从数据库读取标记。若仍为假，设置 `state='waiting_artifacts'`、`upload_completed_at=NULL` 并释放租约，同时写入 `upload_preupload_waiting_for_part` 审计事件；只有标记为真才构造 payload 和投稿。

- [ ] **Step 4: 运行上传与 UPOS 回归测试**

Run: `pytest -q tests/bili_upload/test_upload.py tests/bili_upload/test_upos.py`

Expected: PASS。

### Task 4: API 与上传列表展示预上传阶段

**Files:**
- Modify: `tests/bili_upload/test_journal.py`
- Modify: `tests/web/test_recording_sessions.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.model.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.html`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`

**Interfaces:**
- Produces: `UploadJobProgressResponse.preuploadFinalized: boolean`
- Produces: `UploadJobProgressResponse.displayState: 'standard' | 'preuploading' | 'preuploaded_waiting' | 'preupload_paused'`
- Produces: SSE `upload_progress.jobs[].displayState`

- [ ] **Step 1: 写 API 和组件失败测试**

分别断言开放临时任务派生出 `preuploading`、全部当前分 P 确认后派生出 `preuploaded_waiting`、暂停后派生出 `preupload_paused`，并在列表显示“录制中 · 正在预上传”“录制中 · 已预上传，等待新分 P”“录制中 · 预上传已暂停”。

- [ ] **Step 2: 运行目标测试确认失败**

Run: `pytest -q tests/bili_upload/test_journal.py tests/web/test_recording_sessions.py`

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts'`

Expected: FAIL，响应和类型尚无派生阶段。

- [ ] **Step 3: 实现单一派生函数和前端文案**

后端依据 `preupload_finalized`、`job.state` 和分 P 是否全部 `confirmed` 计算 `display_state`，HTTP 与 SSE 共用该值；前端优先展示该字段，正式任务仍沿用现有状态映射。进度只使用当前已发现分 P 的字节数。

- [ ] **Step 4: 运行 API 与组件测试**

重复 Step 2 命令。

Expected: PASS。

### Task 5: 时间轴几何、浮层和 Pointer Events

**Files:**
- Modify: `webapp/src/app/upload-tasks/upload-tasks.module.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.ts`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.html`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.scss`
- Modify: `webapp/src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts`

**Interfaces:**
- Produces: `handleTimelinePointerMove(event: PointerEvent, track: HTMLElement): void`
- Produces: CDK connected overlay 锚点与 `nz-tooltip` 高光说明

- [ ] **Step 1: 写时间轴 DOM 与事件失败测试**

断言主轨和蓝框共用 `.primary-lane` 几何、绿色范围位于 `.created-lane`、操作面板渲染在 `.cdk-overlay-container`、高光按钮配置 `nz-tooltip`，并模拟 pointerdown/drag/pointerup 后继续 pointermove 仍更新 `hoverTimeMs`，pointerleave 才清空。

- [ ] **Step 2: 运行组件测试确认失败**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts'`

Expected: FAIL，当前浮层属于工作台子树且 hover 仍依赖 `mousemove`。

- [ ] **Step 3: 统一轨道布局**

使用 CSS 变量定义主轨/蓝框相同的 `top` 和 `height`，新增明确的第三条已创建片段轨道并让绿色框填满该轨；提高悬浮虚线层级到范围之上、边界手柄之下。

- [ ] **Step 4: 将两类说明移到全局 overlay**

导入 `OverlayModule`，时间轴操作面板改为 `cdkConnectedOverlay` 并提供上下/左右避让位置；高光按钮使用 `[nzTooltipTitle]`，删除 `.marker-pin:hover::after`。全屏时 overlay 仍位于可见的全局容器。

- [ ] **Step 5: 统一 Pointer Events**

模板只监听 `(pointermove)` 与 `(pointerleave)`；`handleTimelinePointerMove()` 总是先计算稳定区悬浮时间，再在当前 pointer 被捕获时更新播放头。结束拖动后不清除 hover，实际离开轨道才清除。

- [ ] **Step 6: 运行时间轴测试、lint 和构建**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/upload-tasks/highlight-editor/highlight-editor.component.spec.ts'`

Run: `cd webapp && npx ng lint && npm run build`

Expected: 全部 PASS。

### Task 6: 整体回归与发布前验证

**Files:**
- Modify: `docs/superpowers/plans/2026-07-18-incremental-part-upload-and-timeline-polish.md`（勾选执行项）

- [ ] **Step 1: 运行后端完整测试**

Run: `pytest -q`

Expected: PASS。

- [ ] **Step 2: 运行后端静态检查**

Run: `black --check src tests && isort --check-only src tests && flake8 src tests && mypy src/blrec`

Expected: PASS；若仓库既有检查不覆盖 tests，则按配置缩小为 `src` 并记录实际命令。

- [ ] **Step 3: 运行前端完整测试和生产构建**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless && npm run build`

Expected: PASS。

- [ ] **Step 4: 检查变更范围**

Run: `git status --short && git diff --check && git diff --stat`

Expected: 无空白错误，变更仅涉及本计划列出的代码、测试、迁移和文档。
