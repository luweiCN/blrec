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

## NAS 运维

- 群晖地址为 `192.168.50.24`；SSH 用户名和密码只从本机环境变量 `SYNO_ADMIN_USERNAME`、`SYNO_ADMIN_PASSWORD` 读取，禁止输出、记录或写入仓库。
- 本机没有 `sshpass`，使用 `/usr/bin/expect` 启动 SSH，并在密码提示时发送上述环境变量。SSH 连接前先用 `test -n` 确认两个变量存在。
- Container Manager 实际项目目录为 `/volume1/docker/blrec-next/workspace`，Compose 文件为 `compose.yml`，容器名为 `blrec-next`。不要误改项目根目录下的旧 `compose.yaml`。
- 当前容器使用 `host` 网络，管理页面和 API 地址为 `http://192.168.50.24:2234`；NAS 上没有映射 `2233` 端口。
- 群晖非交互 SSH 的 PATH 不含 Docker；使用管理员权限调用 `/usr/local/bin/docker`，sudo 密码同样通过 Expect 从 `SYNO_ADMIN_PASSWORD` 提供。
- 更新前先用容器标签核对 `com.docker.compose.project.config_files` 和 `working_dir`，并确认 `/volume1/docker/blrec-next/config`、`log`、`rec` 三个挂载不变。更新后检查容器健康状态、版本接口和关键日志。
