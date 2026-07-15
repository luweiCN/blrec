# Creation Statements and Unattended Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对齐 B 站当前创作声明和参考项目模板，并让异常中断的录制完全自动恢复或排除，不再等待人工确认。

**Architecture:** 扩展现有投稿元数据缓存和房间策略快照，改用 B 站当前 Web 投稿接口。新增独立的录制文件探测器，日志桥在启动和后处理失败时自动选择可用文件，上传协调器只冻结可用分段。

**Tech Stack:** Python 3、SQLite、FastAPI、pytest、ffprobe、Angular、TypeScript、NG-ZORRO、Jasmine/Karma。

## Global Constraints

- 不使用 worktree，不派子代理，不启动重复服务实例。
- 不新增 AI 功能，不自动切换投稿账号。
- 录制异常不得进入需要用户确认的状态。
- 损坏或缺失分段不得阻塞同场其他可用分段。
- 每项行为先写失败测试并确认预期失败，再写最小实现。

---

### Task 1: 录制文件探测与自动恢复

**Files:**
- Create: `src/blrec/bili_upload/artifact_recovery.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Test: `tests/bili_upload/test_artifact_recovery.py`
- Test: `tests/bili_upload/test_journal.py`

**Interfaces:**
- Produces: `RecoveredArtifact(path: str, size_bytes: int, duration_seconds: Optional[int])`
- Produces: `probe_recording_artifact(path: str) -> Optional[RecoveredArtifact]`
- Produces: `RecordingJournalBridge.finalize_cancelled_sessions(grace_seconds: int = 600) -> None`

- [ ] **Step 1: 写探测器失败测试**

```python
def test_probe_accepts_file_with_video_stream(tmp_path, monkeypatch):
    path = tmp_path / 'part.flv'
    path.write_bytes(b'video')
    monkeypatch.setattr(subprocess, 'run', successful_ffprobe(duration='12.8'))
    assert probe_recording_artifact(str(path)) == RecoveredArtifact(
        path=str(path), size_bytes=5, duration_seconds=13
    )
```

- [ ] **Step 2: 运行 `pytest tests/bili_upload/test_artifact_recovery.py -q`，确认因模块不存在而失败。**

- [ ] **Step 3: 实现无 shell 的限时 `ffprobe` 调用。**

```python
command = (
    'ffprobe', '-v', 'error', '-read_intervals', '%+#1',
    '-select_streams', 'v:0', '-show_entries',
    'stream=codec_type:format=duration', '-of', 'json', path,
)
result = subprocess.run(command, capture_output=True, check=False, timeout=15)
```

- [ ] **Step 4: 把现有重启测试改为期望有效文件转为 `ready`、`final_path` 回退源文件；补充无效文件转为 `failed`、旧 `manual_review` 被消化、成品优先和后处理失败回退测试。**

- [ ] **Step 5: 在 `RecordingJournalBridge` 注入探测函数，启动时在数据库事务外完成探测，再原子更新分段大小、时长、结束时间、最终路径及诊断信息。**

- [ ] **Step 6: 运行 `pytest tests/bili_upload/test_artifact_recovery.py tests/bili_upload/test_journal.py -q`，确认全部通过。**

### Task 2: 自动归集与仅上传可用分段

**Files:**
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `src/blrec/bili_upload/runtime.py`
- Modify: `src/blrec/bili_upload/upload.py`
- Test: `tests/bili_upload/test_journal.py`
- Test: `tests/bili_upload/test_upload.py`

**Interfaces:**
- Consumes: `RecordingJournalBridge.finalize_cancelled_sessions()`
- Produces: 上传任务仅复制 `artifact_state='ready'` 的录制分段。

- [ ] **Step 1: 写失败测试：取消场次在 600 秒内可被同一直播复用，超过 600 秒后有可用分段则关闭、全部坏分段则跳过。**
- [ ] **Step 2: 写失败测试：场次含一个 `ready` 和一个 `failed` 分段时仍创建上传任务，任务只含可用分段。**
- [ ] **Step 3: 写失败测试：上传任务已经冻结后同一 `live_start_time` 再次录制会创建续录场次，而不是修改旧任务。**
- [ ] **Step 4: 运行两个聚焦测试文件，确认分别因现有 `manual_review` 和“所有分段必须 ready”条件失败。**
- [ ] **Step 5: 实现取消场次定时归集，并在上传循环每次创建任务前调用。**

```python
await journal.finalize_cancelled_sessions()
await coordinator.create_ready_jobs()
```

- [ ] **Step 6: 修改候选冻结逻辑，只比较和复制可用分段；没有可用分段时不创建任务。**
- [ ] **Step 7: 修改场次选择逻辑，优先复用同直播仍处于 `open/cancelled` 的场次，已冻结则生成带 continuation 后缀的新键。**
- [ ] **Step 8: 运行 `pytest tests/bili_upload/test_journal.py tests/bili_upload/test_upload.py -q`。**

### Task 3: 动态创作声明与策略迁移

**Files:**
- Create: `src/blrec/bili_upload/migrations/0008_initial.sql`
- Modify: `src/blrec/bili_upload/categories.py`
- Modify: `src/blrec/bili_upload/policies.py`
- Modify: `src/blrec/web/routers/room_upload_policies.py`
- Test: `tests/bili_upload/test_database.py`
- Test: `tests/bili_upload/test_categories.py`
- Test: `tests/bili_upload/test_policies.py`
- Test: `tests/web/test_room_upload_policies_routes.py`

**Interfaces:**
- Produces: `UploadCreationStatement(id: int, content: str)`
- Produces: `creation_statement_id: int` and `original_authorization: bool` on policy request/view.

- [ ] **Step 1: 写失败迁移测试，断言旧自制/转载规则分别映射为 `-1/-2`，并允许派生的 `copyright=3`。**
- [ ] **Step 2: 写失败目录测试，断言 `neutral_mark.tips` 和 `mark_list` 被缓存并返回，旧格式缓存自动失效。**
- [ ] **Step 3: 写失败策略及路由测试，断言声明 ID 必须来自当前账号目录、转载必须有来源、转载不能勾选原创授权。**
- [ ] **Step 4: 运行四个聚焦测试文件并确认预期失败。**
- [ ] **Step 5: 用表重建迁移保留原字段并增加两个新字段，旧数据按原语义复制。**
- [ ] **Step 6: 扩展目录缓存 JSON 为格式 2，并规范化 B 站返回的动态声明列表。**
- [ ] **Step 7: 更新策略命令、视图和 API；`copyright` 与 `no_reprint` 只由声明组合派生，不接受互相冲突的前端值。**
- [ ] **Step 8: 运行四个聚焦测试文件，确认通过。**

### Task 4: 模板渲染与当前 Web 投稿协议

**Files:**
- Modify: `src/blrec/bili_upload/signing.py`
- Modify: `src/blrec/bili_upload/protocol.py`
- Modify: `src/blrec/bili_upload/upload.py`
- Test: `tests/bili_upload/test_protocol_matrix.py`
- Test: `tests/bili_upload/test_upload.py`

**Interfaces:**
- Consumes: 策略快照中的 `creation_statement_id` 和 `original_authorization`。
- Produces: `/x/vu/web/add/v3` JSON 投稿请求。

- [ ] **Step 1: 写失败协议测试，断言投稿使用 Web Cookie、CSRF、`/x/vu/web/add/v3`，且不再携带 TV access token 签名。**
- [ ] **Step 2: 写失败负载测试，断言标签经过 Liquid 渲染，转载/原创授权/无授权分别生成 `copyright=2/1/3` 和正确的 `creation_statement`。**
- [ ] **Step 3: 运行两个聚焦测试文件并确认预期失败。**
- [ ] **Step 4: 把协议矩阵的 `submit_archive` 改为 Web JSON 请求，正文和查询均带 CSRF，Referer 指向创作中心。**
- [ ] **Step 5: 快照格式升为 3，渲染标签模板，并按声明组合构造投稿负载；新投稿使用 `recreate=0`。**
- [ ] **Step 6: 运行 `pytest tests/bili_upload/test_protocol_matrix.py tests/bili_upload/test_upload.py -q`。**

### Task 5: Angular 创作声明与上传任务收尾

**Files:**
- Modify: `webapp/src/app/tasks/upload-policy-dialog/room-upload-policy.model.ts`
- Modify: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.ts`
- Modify: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.html`
- Modify: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.scss`
- Modify: `webapp/src/app/tasks/upload-policy-dialog/upload-policy-dialog.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.html`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.scss`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`

**Interfaces:**
- Consumes: API 返回的动态声明列表和提示。
- Produces: 与 B 站一致的声明选择、参考项目默认模板和无需人工确认的任务展示。

- [ ] **Step 1: 写失败组件测试，断言新规则默认参考模板、默认转载及直播间来源模板。**
- [ ] **Step 2: 写失败组件测试，断言转载显示来源并禁用原创授权，其他声明允许独立勾选授权。**
- [ ] **Step 3: 写失败任务列表测试，断言关闭抽屉后刷新不重开、路径只显示 basename 且完整路径进入 tooltip、页面不再出现“需要确认”。**
- [ ] **Step 4: 运行两个组件测试文件并确认预期失败。**
- [ ] **Step 5: 替换旧稿件类型和禁止转载控件，接入动态列表；保持现有三个清晰表单区块。**
- [ ] **Step 6: 使用独立 `nz-pagination` 页脚，清除关闭时的选中场次，并为录制、成品、弹幕路径添加省略显示和完整路径提示。**
- [ ] **Step 7: 运行聚焦 Angular 测试。**

### Task 6: 全量验证与本机数据恢复

**Files:**
- Modify: `docs/superpowers/plans/2026-07-15-creation-statements-and-unattended-recovery.md`

- [ ] **Step 1: 运行 `pytest -q`，要求全部通过。**
- [ ] **Step 2: 在 `webapp/` 运行 `npm test -- --watch=false --browsers=ChromeHeadless`、`npx ng lint` 和 `npm run build`。**
- [ ] **Step 3: 运行 Black、isort、Flake8 和 mypy 的相关检查，修复本次改动产生的问题。**
- [ ] **Step 4: 重启唯一的本机后端实例，确认迁移成功且前端仍使用现有 4200 端口实例。**
- [ ] **Step 5: 只读核对历史 P11：状态已由 `manual_review` 自动变为 `ready`，最终路径回退到原 FLV，文件大小和时长已写入。**
- [ ] **Step 6: 在本机页面保存一次动态创作声明，刷新后值保持一致；刷新上传任务不会打开抽屉。**
