# Upload Settings and Task List UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use test-driven-development for each behavior change and verification-before-completion before reporting success.

**Goal:** 修复投稿分区选择与保存交互，并把上传任务改成可分页表格和详情抽屉。

**Architecture:** 保留现有投稿规则、录制场次和上传任务领域模型；后端只增加子分区校验和分页元数据，前端在现有 Angular 组件内调整交互与布局，不引入新的状态层。

**Tech Stack:** Python 3、FastAPI、SQLite、pytest、Angular、TypeScript、NG-ZORRO、Jasmine/Karma。

## Global Constraints

- 不使用 worktree，不启动重复的前后端实例。
- 只修改投稿设置、分区校验、上传任务分页与展示相关代码。
- 每项行为先写失败测试，再实现最小改动。

## Task 1: 后端约束与分页

- [ ] 在 `tests/web/test_room_upload_policies_routes.py` 增加一级分区拒绝、二级分区允许的接口测试并确认失败。
- [ ] 在 `tests/bili_upload/test_journal.py` 和 `tests/web/test_recording_sessions_routes.py` 增加 `offset`、`total` 测试并确认失败。
- [ ] 修改 `src/blrec/web/routers/room_upload_policies.py`、`src/blrec/bili_upload/journal.py`、`src/blrec/web/routers/recording_sessions.py`，使测试通过。

## Task 2: 投稿设置弹窗

- [ ] 在 `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.spec.ts` 增加叶子分区回填、可点击保存后的字段错误、正向开关及依赖联动测试并确认失败。
- [ ] 修改对应的 `.ts`、`.html`、`.scss` 和模型映射，完成三组清晰布局、叶子分区选取、正向开关及字段级校验。
- [ ] 运行该组件及服务的聚焦测试。

## Task 3: 上传任务表格与详情抽屉

- [ ] 在 `recording-sessions.component.spec.ts` 和服务测试中增加分页请求、表格展示、详情抽屉测试并确认失败。
- [ ] 修改 `recording-sessions`、`upload-tasks` 组件及模块：每场直播一行、每页 20 条、20/50/100 可选、右侧详情抽屉、标题右侧刷新。
- [ ] 运行前端完整测试、后端完整测试、相关 lint 与生产构建；重启现有后端并在本机页面冒烟验证。
