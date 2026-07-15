# Remove Upload Feature Switches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除四个不可见的投稿总开关，让账号、任务、上传、评论和弹幕仅由实际账号及任务策略驱动。

**Architecture:** `BiliAccountRuntime` 始终初始化数据库和后台循环；没有账号时循环空闲。上传任务是否创建来自场次意图，评论和弹幕是否执行来自任务策略快照。旧 TOML 多余键由 Pydantic 忽略并在下次保存时消失。

**Tech Stack:** Python 3.8、Pydantic、asyncio、pytest、Angular/TypeScript。

### Task 1: 后端模型与运行时

**Files:**
- Modify: `src/blrec/setting/models.py`
- Modify: `src/blrec/bili_upload/models.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Test: `tests/bili_upload/test_account_runtime.py`
- Test: `tests/bili_upload/test_upload.py`

- [ ] 写失败测试：没有四个字段也能启动账号子系统，空账号保持空闲，策略关闭时不执行评论/弹幕。
- [ ] 运行聚焦测试并确认因现有开关判断失败。
- [ ] 删除四个字段及所有运行时条件；API Key 不再参与子系统启动，仅保留凭据加密密钥的真实配置校验。
- [ ] 运行聚焦测试并确认通过。

### Task 2: 前端类型和旧配置兼容

**Files:**
- Modify: `webapp/src/app/settings/shared/setting.model.ts`
- Test: `tests/bili_upload/test_account_runtime.py`

- [ ] 增加旧 TOML 包含四个键时仍可加载且 dump 后不再写出的测试。
- [ ] 删除前端模型中的四个字段，确认没有页面引用。
- [ ] 运行 `pytest -q tests/bili_upload/test_account_runtime.py` 和相关 Angular 模型测试。

### Task 3: 本机配置与验证

- [ ] 保存一次 `/Users/luwei/.blrec-dev/settings.toml`，确认旧键被清理。
- [ ] 重启唯一后端实例，确认账号接口不再返回“未启用”。
- [ ] 运行完整后端和前端测试。
