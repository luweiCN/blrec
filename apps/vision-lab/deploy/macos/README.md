# Vision Worker 本地标注控制面

Mac Worker 同时运行完整 Vision Lab 控制面和模型任务 Worker。浏览器访问
`http://Worker地址:8801`；除图片字节外，所有 API 都在本机执行并直接连接本机
独立 PostgreSQL。

稳定运行至少需要两个 LaunchAgent：

1. `com.luwei.blrec-vision-worker`：启动页面与模型预填 Worker；
2. `com.luwei.blrec-vision-backup`：每 6 小时验证并上传一次 PostgreSQL 备份。

只有 NAS SSH 明确允许端口转发时，才额外安装
`com.luwei.blrec-vision-nas-database-tunnel`。当前群晖关闭了 SSH 端口转发，生产
环境改为让 PostgreSQL 只监听本机和控制机的固定内网地址，并在 `pg_hba.conf`
中只允许 NAS 的单一 `/32` 地址和 `blrec_vision` 用户访问。不要为了使用反向隧道
放宽群晖的全局 SSH 安全配置。

旧的 `com.luwei.blrec-vision-database-tunnel` 只在从移动云迁移时临时读取源库，
切换到本机库后不再常驻。

仓库内 plist 都是部署模板，先替换 `__VISION_USER__`、
`__VISION_RELEASE_DIR__` 和 `__VISION_LOG_DIR__`；只有使用可选隧道时才需要替换
`__NAS_SSH_USER__`。再把所需 plist 安装到 `~/Library/LaunchAgents/`。Worker
必须配置：

```text
VISION_LAB_SERVER_URL=http://127.0.0.1:8801
VISION_LAB_MEDIA_SERVER_URL=http://192.168.50.24:8800
VISION_LAB_DATABASE_URL_FILE=~/Library/Application Support/BLRECVisionWorker/database.url
VISION_LAB_DATABASE_SCHEMA=vision_lab
VISION_LAB_CONTROL_PLANE_ONLY=1
VISION_LAB_DATA_DIR=~/Library/Application Support/BLRECVisionWorker/control-plane
VISION_LAB_WORKER_TOKEN_FILE=~/Library/Application Support/BLRECVisionWorker/worker.token
VISION_WORKER_UI_HOST=0.0.0.0
VISION_WORKER_UI_PORT=8801
```

`database.url` 和 `worker.token` 必须为 `600`。Worker 的数据库 URL 始终使用
`127.0.0.1:5432/blrec_vision`，也不要把密码直接写入 LaunchAgent plist。NAS
容器根据实际接入方式使用反向端口或控制机固定内网地址。

切换顺序：先启动本机 PostgreSQL 并验证数据，再恢复数据库并逐表核对，随后配置
NAS 数据库地址，最后启动 Worker 和 NAS 图片服务。更换控制机时必须先停旧控制机，
恢复最近一次 NAS 备份并核对数量后，才能把 NAS 切到新机。使用固定内网接入时，
还必须同步更新 PostgreSQL 的监听地址、NAS 数据库 URL 和 `/32` 访问规则。

`model_prefill` Worker 不依赖标注页触发任务：它空闲时会从共享 PostgreSQL 领取
下一张未预标候选，从 NAS 按帧读取原图，完成核心分类及适用的英雄级联后再领取
下一张。原图只保留当前任务期间，模型权重可以缓存；不会在本机同步整套候选图片。
暂停 Worker 后不会继续创建或领取新的预打标任务。
