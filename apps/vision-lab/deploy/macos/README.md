# Vision Worker 本地标注控制面

Mac Worker 同时运行完整 Vision Lab 控制面和模型任务 Worker。浏览器访问
`http://Worker地址:8801`；除图片字节外，所有 API 都在本机执行并直接连接本机
独立 PostgreSQL。

稳定运行需要三个 LaunchAgent：

1. `com.luwei.blrec-vision-worker`：启动页面与模型预填 Worker；
2. `com.luwei.blrec-vision-nas-database-tunnel`：由控制机主动连接 NAS，把 NAS
   的 `127.0.0.1:15434` 反向转发到控制机本地 PostgreSQL；
3. `com.luwei.blrec-vision-backup`：每 6 小时验证并上传一次 PostgreSQL 备份。

旧的 `com.luwei.blrec-vision-database-tunnel` 只在从移动云迁移时临时读取源库，
切换到本机库后不再常驻。

仓库内 plist 都是部署模板，先替换 `__VISION_USER__`、
`__VISION_RELEASE_DIR__`、`__VISION_LOG_DIR__` 和 `__NAS_SSH_USER__`，再安装到
`~/Library/LaunchAgents/`。Worker 必须配置：

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

`database.url` 和 `worker.token` 必须为 `600`。数据库 URL 使用本机 PostgreSQL
地址 `127.0.0.1:5432/blrec_vision`，不要写 NAS 的反向端口，也不要把密码直接
写入 LaunchAgent plist。NAS 容器使用 `127.0.0.1:15434/blrec_vision`。

切换顺序：先启动本机 PostgreSQL并验证数据，再启动 Worker，然后启动 NAS 反向
隧道并从 NAS 容器验证 15434 可连接。更换控制机时必须先停旧机反向隧道，恢复
最近一次 NAS 备份并核对数量后，才能启动新机隧道；同一 NAS 端口不能同时被两台
机器占用。

`model_prefill` Worker 不依赖标注页触发任务：它空闲时会从共享 PostgreSQL 领取
下一张未预标候选，从 NAS 按帧读取原图，完成核心分类及适用的英雄级联后再领取
下一张。原图只保留当前任务期间，模型权重可以缓存；不会在本机同步整套候选图片。
暂停 Worker 后不会继续创建或领取新的预打标任务。
