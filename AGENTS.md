# Repository Guidelines（仓库指南）

## 项目结构与模块组织

Python 后端采用 `src` 布局，代码位于 `src/blrec/`。其中 `cli/` 提供命令行入口，`web/` 实现 FastAPI 接口，`bili/` 负责 B 站交互，`flv/` 与 `hls/` 处理录制格式，任务、设置和通知逻辑分别放在对应子包中。模板及随包发布的静态资源位于 `src/blrec/data/`。

Angular 前端位于 `webapp/src/app/`，图片和图标存放在 `webapp/src/assets/`，测试以 `*.spec.ts` 形式与被测代码就近放置。生产构建会写入 `src/blrec/data/webapp/`；其中带哈希的文件应视为生成产物。Python 打包与质量工具配置集中在 `setup.cfg`、`pyproject.toml`、`.flake8` 和 `mypy.ini`。

## 构建、测试与开发命令

- `python3 -m venv .venv && source .venv/bin/activate`：创建并激活 Python 虚拟环境。
- `pip install -e '.[dev]'`：以可编辑模式安装后端及开发工具。
- `blrec`：启动应用，默认界面为 `http://localhost:2233`。
- `black --check src && isort --check-only src && flake8 src && mypy src/blrec`：运行后端格式、导入、静态检查及类型检查。
- `python -m build`：在 `dist/` 中生成 wheel 和源码包。
- `cd webapp && npm ci`：按锁文件安装前端依赖。
- 在 `webapp/` 中运行 `npm start` 启动开发服务器，运行 `npm test -- --watch=false --browsers=ChromeHeadless` 执行一次无头测试，运行 `npx ng lint` 检查代码，运行 `npm run build` 生成生产包。

## 编码风格与命名约定

Python 使用四空格缩进、类型注解和 88 字符行宽，并遵循 Black、isort、Flake8 与 mypy 配置。沿用现有单引号风格；模块和函数使用 `snake_case`，类使用 `PascalCase`。Angular 文件使用两空格缩进和单引号，文件名采用 kebab-case（如 `task-item.component.ts`）；组件选择器使用 `app-kebab-case`，指令选择器使用 `appCamelCase`。

## 测试指南

后端测试位于 `tests/`，使用 Pytest，文件命名为 `test_*.py`；运行 `.venv/bin/python -m pytest -q`。前端使用 Jasmine 与 Karma，测试以 `*.spec.ts` 与源码就近放置。新增行为必须覆盖正常路径及关键异常路径；仓库未设置硬性覆盖率门槛，但提交前应运行相关测试和整仓回归。

## 提交与拉取请求规范

提交主题遵循现有的简洁前缀：`feat:`、`fix:`、`perf:`、`chore:` 或 `release:`，每个提交只处理一个明确变更。拉取请求应说明变更行为与动机、关联议题，并列出验证命令；可见的界面改动需附截图。禁止提交凭据、API 密钥、本地设置、日志、录制文件、虚拟环境或依赖目录。

## 网络接入约束

- 新增对外网络功能时，凡是大流量上传下载、携带账号凭据的请求、可能触发平台风控的操作或高频轮询，都必须定义明确的 `NetworkPurpose`，接入 `NetworkRouteManager` 和网络管理页面；禁止绕过选路直接创建裸 `aiohttp`、`requests` 或下载器连接。
- 固定线路必须同时绑定用户所选网卡的源地址和 DNS 解析，并在整次上传、下载、直播场次或认证会话内保持出口稳定；只绑定传输连接、仍用系统默认线路解析 DNS 不算完成网络切换。轮换线路仅用于允许轮换的匿名读取或轮询，并按请求批次、连接或场次维持粘性，不能在同一任务中途换出口。
- 未选择网卡时允许使用系统默认路由，但必须解析并记录实际默认网卡，流量不得因 `interface=None` 被丢弃。所有出站任务都要记录用途、选中线路、源地址、选路原因和上下行流量。
- 新增网络用途必须同时提供旧配置迁移策略、前端用途说明以及固定、轮换、系统默认和重启恢复场景的验证；不得让已有部署因为新增空配置而静默切换到另一条宽带。

## NAS 运维

- 群晖地址为 `192.168.50.24`；SSH 用户名和密码只从本机环境变量 `SYNO_ADMIN_USERNAME`、`SYNO_ADMIN_PASSWORD` 读取，禁止输出、记录或写入仓库。
- 本机没有 `sshpass`，使用 `/usr/bin/expect` 启动 SSH，并在密码提示时发送上述环境变量。SSH 连接前先用 `test -n` 确认两个变量存在。
- Container Manager 实际项目目录为 `/volume1/docker/blrec-next/workspace`，Compose 文件为 `compose.yml`，容器名为 `blrec-next`。不要误改项目根目录下的旧 `compose.yaml`。
- 当前容器使用 `host` 网络，管理页面和 API 地址为 `http://192.168.50.24:2234`；NAS 上没有映射 `2233` 端口。
- 群晖非交互 SSH 的 PATH 不含 Docker；使用管理员权限调用 `/usr/local/bin/docker`，sudo 密码同样通过 Expect 从 `SYNO_ADMIN_PASSWORD` 提供。
- 任何重建容器、切换镜像、执行数据库迁移或可能批量重算数据的部署，必须先在运行中容器内执行 `scripts/backup_blrec_database.py --label <版本或提交号>`，并确认备份文件非空且 `PRAGMA quick_check` 为 `ok`；备份失败必须终止部署，禁止继续更新 Compose 或数据库。部署脚本必须把备份作为强制前置步骤，不得依赖人工记忆。
- 更新前先用容器标签核对 `com.docker.compose.project.config_files` 和 `working_dir`，并确认 `/volume1/docker/blrec-next/config`、`log`、`rec`、`clips` 四个挂载不变。更新后检查容器健康状态、版本接口和关键日志。
