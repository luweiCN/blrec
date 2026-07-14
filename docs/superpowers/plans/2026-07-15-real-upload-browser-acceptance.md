# 真实投稿与浏览器验收实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为上传任务文件增加完整路径复制操作，并用隔离短录播跑通一次真实 B 站投稿，最终通过无头浏览器核对本地状态和创作中心字段。

**Architecture:** 复用现有 `UploadCoordinator`、`UposUploader`、投稿接口和 `ReviewWatcher`，只创建一个临时验收场次与不可变任务快照。浏览器使用独立会话，凭据只从本机加密存储临时注入，验收结束后清理。

**Tech Stack:** Python 3.8、FastAPI、SQLite、pytest；Angular 15、Jasmine/Karma、NG-ZORRO；agent-browser。

## 约束

- 直接在当前工作目录操作；不用 worktree，不派子代理。
- 真实稿件固定使用主账号，任务绑定后不回退到其他账号。
- 仅自己可见；不发动态、不发评论、不回灌弹幕。
- 开启自动上传前必须断言只有一个候选任务；任何断言失败都保持开关关闭。
- 不输出 Cookie、API key 或加密密钥；浏览器临时凭据不进入 Git。

### Task 1：上传文件路径复制

**Files:**
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.{ts,html,scss,spec.ts}`
- Modify: `webapp/src/app/upload-tasks/upload-tasks.module.ts`
- Modify: `webapp/src/app/icons-provider.module.ts`

- [ ] 先写失败测试：成品、录制、弹幕路径按钮存在；成功复制原始完整路径并提示“已复制完整路径”；失败提示“复制失败，请重试”。
- [ ] 运行聚焦 Karma 测试，确认新增断言先失败。
- [ ] 注入 Angular CDK `Clipboard` 和消息服务，在省略文件名右侧增加 `CopyOutline` 文本按钮、tooltip 与 `aria-label`。
- [ ] 运行聚焦测试与前端 lint，确认交互和样式通过。

### Task 2：自动化回归与投稿预检

- [ ] 运行上传、UPOS、审核对账相关 pytest；运行完整前端测试和生产构建。
- [ ] 查询数据库，确认自动上传、自动评论、弹幕回灌关闭，且无既有规则和任务。
- [ ] 检查主账号仍可用；若凭据失效则停止，不切换账号。

### Task 3：通过本地页面保存验收规则

- [ ] 用独立无头浏览器打开 `http://127.0.0.1:4200`，为房间 `22907214` 保存设计文档中的标题、简介、分 P、标签、分区和声明设置。
- [ ] 刷新页面核对回填，并从数据库逐字段验证规则；留存无敏感信息截图到 `/tmp/blrec-upload-acceptance/`。

### Task 4：创建唯一验收任务

- [ ] 复制 17 秒 FLV 到 `/tmp/blrec-upload-acceptance/`，创建独立 closed 会话、finished run 和 ready part。
- [ ] 等待生产协调器创建任务，随后立即禁用房间规则。
- [ ] 断言数据库只有这个新任务，候选场次为零，全局自动上传仍关闭；失败则停止。

### Task 5：真实投稿与状态流转

- [ ] 开启全局自动上传，监控分片、提交、AID/BVID 和审核/CID 入库；拿到投稿结果后立即再次关闭自动上传。
- [ ] 若暴露代码缺口，先添加可重复的失败测试，再做最小修复并从原任务恢复，不创建第二稿。
- [ ] 在本地上传任务页核对上传、投稿和审核状态；点击复制按钮验证 toast 与抽屉不受影响。

### Task 6：创作中心验收与收尾

- [ ] 将主账号 Web Cookie 安全注入独立浏览器会话，按 BVID 在创作中心核对标题、简介、分 P、标签、分区、创作声明、仅自己可见和不发动态。
- [ ] 确认没有额外任务、动态、评论或弹幕回灌；保持房间规则和全局自动上传关闭。
- [ ] 清理临时 Cookie 和浏览器会话，保留验收稿件与无敏感截图；运行最终测试并记录 AID/BVID/CID。
