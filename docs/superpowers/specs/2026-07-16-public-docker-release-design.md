# BLREC 公开 Docker 发布设计

## 目标

首个对外版本发布为 `v3.0.0-beta.1`，通过 GitHub Actions 自动构建并发布到 GHCR。群晖最终只运行一个 BLREC 容器；镜像同时支持 `linux/amd64` 与 `linux/arm64`，Docker 会按 NAS 架构选择正确变体。

本次发布只建立可重复的构建、安装、升级和回滚链路，不把尚未完成的 B 站、群晖双网络及长期灰度验收描述为已经通过，也不发布 `latest` 标签。

## 发布产物与标签

- 镜像名称：`ghcr.io/luweicn/blrec`。
- Git 标签：`v3.0.0-beta.1`；程序版本和镜像版本为 `3.0.0-beta.1`。
- 固定镜像标签：`3.0.0-beta.1`，发布后不得覆盖。
- 浮动测试标签：`beta`，指向最新测试版。
- `latest` 只在真实灰度通过并发布正式版本后创建。
- GitHub Release 标记为 Pre-release，附带群晖 Compose、环境变量示例和安装说明。

修复已发布版本时递增预发布号，例如发布 `v3.0.0-beta.2`，不得删除并重用 `beta.1` 标签。

## 单镜像多阶段构建

Dockerfile 使用三个构建阶段，但只输出一个运行镜像：

1. Node 构建阶段执行 `npm ci` 和 Angular 生产构建，避免镜像依赖仓库中遗留的静态产物。
2. Python 构建阶段把后端源码、数据库迁移和新前端产物打入 wheel。
3. Python 3.11 slim 运行阶段只安装 wheel、FFmpeg 和必要运行库，不包含 Node、编译器或可编辑安装目录。

最终镜像继续暴露 2233 端口，使用 `/cfg`、`/log`、`/rec` 三个持久目录，并通过 `/api/v1/auth/status` 执行本地健康检查。暂不改为非 root 用户，因为群晖现有录像目录权限不统一；该安全加固不应阻断首版安装。

镜像写入 OCI source、version、revision、license 和 description 标签，使 GHCR 包与 `luweiCN/blrec` 仓库关联。

## GitHub Actions 发布门禁

现有测试工作流增加 `workflow_call` 支持并保持普通 push/PR 检查。发布工作流只响应 `v*.*.*` 标签，并按以下顺序执行：

1. 后端在 Python 3.8、3.10、3.11 上运行 pytest、Black、isort、Flake8；Python 3.8 额外运行 mypy。
2. 前端在 Node 18 上运行 `npm ci`、ChromeHeadless 测试和生产构建。
3. Docker 原生架构构建后启动临时容器，确认健康检查和管理员状态接口可用。
4. 所有门禁通过后，使用 QEMU 与 Buildx 构建 AMD64/ARM64 镜像，通过仓库 `GITHUB_TOKEN` 推送到 GHCR。
5. 检查多架构 manifest 后创建 GitHub Pre-release，并附加部署文件。

工作流明确声明 `contents: write` 和 `packages: write`，不保存个人 GHCR Token。任何门禁失败都阻止镜像推送；镜像已推送但 Release 创建失败时允许重跑同一次工作流，固定标签内容必须保持同一提交摘要。

首次成功推送后，在 GitHub Packages 设置中将 `blrec` 容器包手动改为 Public。公开后群晖可以匿名拉取，后续发布不再需要重复设置。

## 群晖 Compose

`compose.synology.yml` 只声明一个 `blrec` 服务，不再包含本地 `build`：

- 默认镜像为 `ghcr.io/luweicn/blrec:3.0.0-beta.1`，允许通过 `BLREC_IMAGE_TAG` 显式覆盖。
- 使用 `network_mode: host`，不声明 `ports`，保留双网卡与多网关能力。
- 使用 `restart: unless-stopped` 和两分钟优雅停止时间。
- 挂载配置、日志、录像三个宿主目录。
- 必填 `BLREC_ADMIN_USERNAME`、`BLREC_API_KEY`，凭据加密密钥固定读取 `/cfg/credential.key`。
- 可信代理默认只允许 `127.0.0.1`，只有确认群晖反向代理来源地址后才能扩大。

环境变量示例不包含真实值。初始化安全码使用 `openssl rand -hex 32` 生成；凭据密钥使用 `openssl rand -base64 32` 写入文件并设置为 `0600`。该密钥、认证数据库和上传数据库必须随 `/cfg` 一起长期保存。

## 安装、升级与回滚

安装文档同时覆盖 Container Manager“项目”和命令行，但两者使用同一份 Compose：创建三个目录，生成密钥，填写环境变量，启动项目，然后用浏览器完成管理员初始化。

升级时先停止或等待关键任务进入安全状态，备份整个 `/cfg`，将 `BLREC_IMAGE_TAG` 改为目标固定版本，拉取镜像并重新创建容器。启动后核对健康状态、数据库迁移、账号状态、录制任务和上传任务。

回滚时不得只切换旧镜像。若新版本执行过数据库迁移，必须同时恢复升级前 `/cfg` 备份，再使用上一固定镜像标签启动。录像目录 `/rec` 不随普通配置回滚覆盖。

## 发布验收

发布前必须取得以下自动化证据：完整后端和前端测试通过、Docker 冒烟通过、多架构 manifest 同时包含 AMD64/ARM64、Compose 配置可解析、镜像中版本与标签一致、仓库差异不包含凭据。

发布后只声明“公开测试版可安装”。真实投稿设置、定时发布、自定义封面、合集、评论、弹幕、转码修复、重启恢复、容量通知、群晖双网络以及 3～5 房间三天灰度继续按 `docs/operations/release-acceptance-checklist.md` 收集日志和证据；这些项目完成前不发布稳定版或 `latest`。
