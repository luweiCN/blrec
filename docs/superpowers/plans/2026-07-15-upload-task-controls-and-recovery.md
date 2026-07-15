# Upload Task Controls and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 增加安全的暂停/继续/修改任务和任意状态持久化删除，并完善重启恢复及任务详情。

**Architecture:** Migration 14 增加管理员暂停和场次删除状态。所有 worker 领取任务时统一检查终止/暂停标记。任务修改只在零远端副作用时事务完成。删除执行器采用可重入阶段，启动时恢复。

**Tech Stack:** Python、SQLite WAL 租约、FastAPI、Angular/ng-zorro、pytest、Jasmine。

### Task 1: 模式和能力计算

**Files:**
- Create: `src/blrec/bili_upload/migrations/0014_initial.sql`
- Modify: `src/blrec/bili_upload/database.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Test: `tests/bili_upload/test_database.py`
- Test: `tests/bili_upload/test_journal.py`

- [ ] 写失败迁移测试：`operator_paused`、`deletion_state/error/requested_at`、旧数据默认值和索引。
- [ ] 写失败能力测试：暂停、继续、修改和所有状态删除由后端返回，未开始上传才可修改。
- [ ] 实现迁移、查询映射和单一主状态，运行聚焦测试。

### Task 2: 暂停、继续与任务修改

**Files:**
- Modify: `src/blrec/bili_upload/task_actions.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/bili_upload/upload.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Test: `tests/bili_upload/test_task_actions.py`
- Test: `tests/web/test_recording_sessions_routes.py`

- [ ] 写失败测试：暂停先持久化、活动分片安全停止、继续保留确认分片、重启保持暂停、结果未知禁止继续。
- [ ] 写失败测试：零远端副作用可改账号/策略；活动租约、已上传分片或投稿中拒绝；换账号清理不兼容合集。
- [ ] 增加 `pause_upload`、`resume_upload`、`update_job` 场次动作和请求模式，复用账号/策略校验。
- [ ] 运行聚焦测试。

### Task 3: 任意状态可恢复删除

**Files:**
- Modify: `src/blrec/bili_upload/task_actions.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/bili_upload/upload.py`
- Modify: `src/blrec/bili_upload/review.py`
- Modify: `src/blrec/bili_upload/collection_publish.py`
- Modify: `src/blrec/bili_upload/comments.py`
- Modify: `src/blrec/bili_upload/danmaku_publish.py`
- Modify: `src/blrec/core/recorder.py`
- Test: `tests/bili_upload/test_task_actions.py`
- Test: `tests/bili_upload/test_upload.py`
- Test: `tests/core/test_recorder_event_order.py`

- [ ] 写失败测试：录制中、上传中、审核中和完成状态均先落终止标记；worker 不再发新请求；不存在文件幂等成功；不调用 B 站删稿。
- [ ] 写失败测试：文件删除或进程中断保留状态，重启自动续跑，失败可再次删除。
- [ ] 实现删除执行器和各 worker 终止检查；活动录制调用现有场次取消器并防止同场重建。
- [ ] 运行聚焦测试。

### Task 4: 详情字段与失败可见性

**Files:**
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.model.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.{ts,html,scss,spec.ts}`

- [ ] 写失败测试：合集状态/错误、定时发布时间/状态、评论和弹幕最终错误出现在 API 与详情。
- [ ] 实现字段映射、状态标签和简洁错误展示。
- [ ] 运行后端路由及 Angular 组件测试。

### Task 5: Angular 操作与修改弹窗

**Files:**
- Create: `webapp/src/app/upload-tasks/task-edit-dialog/task-edit-dialog.component.{ts,html,scss,spec.ts}`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.module.ts`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.service.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.{ts,html,scss,spec.ts}`

- [ ] 写失败测试：能力驱动暂停/继续/修改、禁用原因、单项与批量删除、删除风险文案及修改保存。
- [ ] 实现任务编辑弹窗和操作反馈；不增加未确认的人工评论/弹幕/审核/扫描按钮。
- [ ] 运行完整 Angular 测试、lint 和 build。

### Task 6: 重启验收

- [ ] 造一个上传中、一个管理员暂停和一个删除中 fixture，重启后确认分别续跑、保持暂停和继续删除。
- [ ] 确认没有重复 AID/BVID、没有 B 站删稿请求、已确认分片未重传。
