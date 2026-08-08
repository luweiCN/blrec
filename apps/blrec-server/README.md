# BLREC Server

BLREC 的生产服务端交付物，包含 Python 服务端和编译后的外部管理端。该镜像只负责录制、管理、投稿、任务协调和远程分析任务交接，不包含视觉模型、推理依赖或 Analysis Worker 命令。

## 构建与验证

服务端镜像禁止在开发电脑上构建。提交或 PR 触发
`.github/workflows/test-server.yml`，由 GitHub Runner 构建镜像、检查模型依赖边界
并运行启动冒烟测试。推送 `server-v<版本>` 标签后，
`.github/workflows/release-server.yml` 独立发布多架构镜像：

```bash
gh workflow run test-server.yml --ref <远端分支>
git tag server-v<版本>
git push origin server-v<版本>
```

生产环境使用远程 Analysis Worker，并通过 `BLREC_ANALYSIS_WORKER_TOKEN` 建立受限任务通道。录像、配置、日志、收藏、片段和结算截图必须继续使用独立持久化挂载。
