# Operational Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让现有六个通知渠道可按事件路由新增的账号、网络、录制、上传和容量状态变化。

**Architecture:** `OperationalNotificationCenter` 接收领域状态，SQLite 状态表去重后交给 `NotificationDispatcher`。设置模型保存每个事件的渠道与格式；现有 provider 只负责发送。业务组件通过轻量 reporter 解耦接线。

**Tech Stack:** Python、Pydantic、SQLite、aiohttp、RxPy、Angular/ng-zorro、pytest、Jasmine。

### Task 1: 设置模型和旧设置映射

**Files:**
- Modify: `src/blrec/setting/models.py`
- Modify: `src/blrec/setting/typing.py`
- Modify: `webapp/src/app/settings/shared/setting.model.ts`
- Test: `tests/setting/test_settings.py`

- [ ] 写失败测试：事件代码白名单、渠道/格式兼容、未知值拒绝、旧四类设置映射且不重复通知。
- [ ] 增加运行通知路由模型和默认路由，保留现有渠道凭据/模板。
- [ ] 运行设置测试。

### Task 2: 状态去重与分发器

**Files:**
- Create: `src/blrec/bili_upload/migrations/0016_initial.sql`
- Create: `src/blrec/notification/operational.py`
- Modify: `src/blrec/notification/providers.py`
- Test: `tests/notification/test_operational.py`

- [ ] 写失败测试：首次基线静默、异常一次、重复静默、恢复一次、重启去重、并行发送和单渠道隔离。
- [ ] 实现持久状态、事件标题/正文和按路由分发；通知发送失败只写日志。
- [ ] 运行 `pytest -q tests/notification/test_operational.py`。

### Task 3: 领域接线

**Files:**
- Modify: `src/blrec/bili_upload/accounts.py`
- Modify: `src/blrec/networking/manager.py`
- Modify: `src/blrec/core/recorder.py`
- Modify: `src/blrec/bili_upload/upload.py`
- Modify: `src/blrec/bili_upload/review.py`
- Modify: `src/blrec/bili_upload/collection_publish.py`
- Modify: `src/blrec/bili_upload/comments.py`
- Modify: `src/blrec/bili_upload/danmaku_publish.py`
- Modify: `src/blrec/bili_upload/retention.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Test: `tests/notification/test_operational_integrations.py`

- [ ] 写失败测试覆盖账号失效/恢复、网络失败/切换/恢复、录制/上传失败恢复、审核拒绝、合集/评论/弹幕最终失败和容量预警。
- [ ] 注入统一 reporter；瞬时重试不报告最终失败，网络接线不新增 B 站轮询。
- [ ] 运行集成测试。

### Task 4: 设置页通知页签和路由矩阵

**Files:**
- Modify: `webapp/src/app/settings/settings.component.{ts,html,scss,spec.ts}`
- Modify: `webapp/src/app/settings/notification-settings/notification-settings.component.{ts,html,scss,spec.ts}`
- Modify: `webapp/src/app/settings/settings.module.ts`

- [ ] 写失败测试：系统/通知页签、六渠道入口、事件多选渠道、格式选择、未配置渠道禁用和保存回显。
- [ ] 实现紧凑路由矩阵，移除系统设置底部重复“通知”区块。
- [ ] 运行 Angular 聚焦测试和完整测试。

### Task 5: 验收清单与全量验证

**Files:**
- Create: `docs/operations/release-acceptance-checklist.md`

- [ ] 记录尚需真实环境验证：自定义封面、合集创建/加入、定时发布、真实评论/弹幕、长期凭据续期、群晖双网络、3 天 3～5 房间灰度和 58 房间全量。
- [ ] 给每项写明前置、操作、预期、证据和失败回滚，不把未验证项写成已完成。
- [ ] 运行完整 pytest、Angular test/lint/build、Black/isort/Flake8/mypy，并重启唯一服务做浏览器冒烟。
