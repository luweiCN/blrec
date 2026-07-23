# Task Plan: Media Library

## Goal

在隔离 worktree 中交付一个可验证的媒体库：片段按来源录制场次聚合且可从两个管理入口重命名；录制场次可收藏为永久直播、保留多分 P 与 B 站投稿历史；外部直播和片段可上传、重命名、打标签、预览、剪辑、投稿和删除；永久媒体不参与录像容量与自动清理。

## Current Phase

Phase 8

## Phases

### Phase 1: Requirements & Existing-System Discovery

- [x] 创建独立 worktree 和分支
- [x] 梳理录制场次、分 P、片段、上传任务、投稿历史和保留策略
- [x] 记录用户补充的收藏项、多分 P、重命名与标签要求
- [x] 核对文件所有权、运行时目录和删除状态机的全部接入点
- **Status:** complete

### Phase 2: Domain & Storage Design

- [x] 定义媒体库条目、永久直播、外部导入、标签和投稿历史的领域边界
- [x] 用录制中、上传中、跨文件系统移动、重复收藏和部分导入场景验证状态机
- [x] 确定迁移、索引、查询分页、REST API 和前端信息架构
- [x] 更新 `CONTEXT.md`，并仅为不可逆的存储边界决策添加 ADR
- **Status:** complete

### Phase 3: Backend Foundation (TDD)

- [x] 先写数据库迁移与约束测试
- [x] 实现媒体库查询、重命名、标签与投稿历史读取
- [x] 实现收藏文件迁移及路径引用同步
- [x] 实现外部多分 P/单片段上传与媒体探测
- **Status:** complete

### Phase 4: Backend Integration (TDD)

- [x] 从容量统计、按事件清理与低空间回收中排除媒体库条目
- [x] 接入预览、剪辑、再次投稿和本地删除
- [x] 将已生成片段查询改为按来源场次分页聚合，外部片段在媒体库独立管理
- [x] 补齐审计日志、冲突响应与崩溃恢复
- **Status:** complete

### Phase 5: Frontend

- [x] 使用适用的 TypeScript/Angular 技能并读取仓库前端约束
- [x] 新增“媒体库/直播收藏”列表与详情操作
- [x] 录像列表增加收藏入口及清晰的处理中/失败反馈
- [x] 片段管理改为按来源场次聚合并增加上传入口
- [x] 外部上传支持多文件排序、元数据、重命名和标签
- **Status:** complete

### Phase 6: Verification & Delivery

- [x] 运行相关后端测试、整仓 Pytest 与静态检查
- [x] 运行前端单测、lint 与生产构建
- [x] 验证旧数据库迁移、已有投稿历史和 Docker/NAS 挂载兼容性
- [x] 审查最小化 diff，更新规划文件并交付 worktree/分支与验证结果
- **Status:** complete

### Phase 7: Sibling Permanent Storage & Manual-Only Deletion

- [x] 将永久媒体根从 `<recording-root>/favorites/` 改为与录制根同级的 `favorites/`
- [x] 扩展媒体读取与本地删除的可信路径边界，并保持所有关联路径原子更新/崩溃恢复
- [x] 验证收藏直播与外部上传内容不参与容量统计、低空间回收或投稿保留删除
- [x] 更新设计、部署挂载说明与测试，完成全量回归
- **Status:** complete

### Phase 8: Generated Clip Rename

- [x] 为系统生成片段增加名称校验、持久化、审计和 PATCH API
- [x] 在独立片段管理页增加重命名对话框
- [x] 在剪辑页底部“已创建片段”列表增加行内重命名
- [x] 验证重命名不改变视频/XML 路径、已有上传任务或远端稿件
- [x] 完成相关及整仓回归、静态检查和生产构建
- **Status:** complete

## Key Questions

1. 收藏请求遇到录制中或存在活跃上传 lease 时，是拒绝、排队，还是先复制后切换？当前倾向：录制中拒绝；短时活跃操作返回冲突，稳定后重试。
2. 用户所说“标识”是否就是可多选标签？当前按标签实现，除非后续澄清。
3. 外部直播缺少房间号/主播信息时，哪些字段必填？当前倾向：名称必填，其余可选，并使用内部导入键而不是伪造 B 站场次键。
4. 再次投稿是否需要并行保留多个活跃任务？现有“重新投稿”会归档旧稿件并复用单一任务；优先保留该机制并把归档历史展示出来，避免改写整个上传状态机。
5. 收藏文件根目录如何持久化？按最新要求使用录制根同级的 `favorites/`；容器需独立持久化挂载，移动必须兼容跨文件系统。

## Decisions Made

| Decision | Rationale |
|----------|-----------|
| worktree `/Users/luwei/code/ai/blrec-media-library`，分支 `feat/media-library` | 隔离原工作区已有未提交改动 |
| 收藏项以“一场直播”为单位，分 P 是其子项 | 与用户的浏览、预览、剪辑和再次投稿心智模型一致 |
| 重命名首先是展示元数据，不重命名物理文件 | 避免破坏上传任务、媒体索引和播放中的路径引用 |
| 投稿历史由当前任务与 `upload_job_archives` 共同构成 | 现有机制已经安全保留旧 aid/bvid，无需先将上传任务关系改成一对多 |
| 永久媒体必须同时从候选删除、容量统计和低空间回收中排除 | 仅设置 `retention_mode=never` 仍会被现有容量统计计入，不满足需求 |
| 永久文件保存在与录制根同级的 `<recording-root>/../favorites/` | 用户明确要求收藏使用独立同级目录；必须同步扩展可信根和部署挂载 |

## Errors Encountered

| Error | Attempt | Resolution |
|-------|---------|------------|
| 数据库技能展示名不是实际目录名 | 1 | 改用 `/Users/luwei/.agents/skills/database-design-expert/` |
| `find .. -name AGENTS.md` 扫描范围过大并长时间运行 | 1 | 中断后仅使用当前仓库根 `AGENTS.md` |
| 两个测试文件名猜测错误 | 1 | 用 `rg --files tests` 找到实际的 `*_routes.py` 文件 |
| 合并检查命令中的 `rg upload_job_archives` 无匹配而以 1 退出 | 1 | 将“历史稿件尚未暴露到 API/UI”记录为设计输入，不重复搜索同一路径 |
| 新 worktree 没有 `node_modules` | 1 | 链接原工作区已安装依赖，该路径受 `.gitignore` 排除 |
| development build 覆盖了打包静态资源 | 1 | 验证后精确恢复跟踪文件并删除本次生成的未跟踪文件 |

## Notes

- 任何数据库实现先写失败测试；迁移必须保留旧数据并验证索引查询。
- 不提交构建后的哈希静态资源，除非仓库现有发布流程明确要求。
- 所有上传文件名都只能作为显示信息，服务端路径必须由系统生成。
