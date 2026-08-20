# Vision Worker 本地标注控制面

Mac Worker 同时运行完整 Vision Lab 控制面和模型任务 Worker。浏览器访问
`http://Worker地址:8801`；除图片字节外，所有 API 都在本机执行并直接连接远程
PostgreSQL。

需要两个 LaunchAgent：

1. `com.luwei.blrec-vision-database-tunnel`：把本机 `15433` 转发到
   数据库主机的 `127.0.0.1:5432`；模板固定使用受信任的主机地址和独立密钥，
   部署前必须先核对并安装 known_hosts，禁止关闭主机指纹校验；
2. `com.luwei.blrec-vision-worker`：启动 wheel 中的 `blrec-vision-worker`。

仓库内两个 plist 都是部署模板，先替换 `__VISION_USER__`、
`__VISION_RELEASE_DIR__` 和 `__VISION_LOG_DIR__`，再安装到
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

`database.url` 和 `worker.token` 必须为 `600`。数据库 URL 使用本机隧道地址
`127.0.0.1:15433`，不要写 NAS 的 `127.0.0.1:15432`，也不要把密码直接写入
LaunchAgent plist。

切换顺序：先启动隧道并验证 15433 可连接，再启动 Worker；本地控制面启动完成
后 Worker 才会注册和领取任务。回滚时恢复上一版 Worker plist/wheel，并把
`VISION_LAB_SERVER_URL` 改回旧控制面地址。

`model_prefill` Worker 不依赖标注页触发任务：它空闲时会从共享 PostgreSQL 领取
下一张未预标候选，从 NAS 按帧读取原图，完成核心分类及适用的英雄级联后再领取
下一张。原图只保留当前任务期间，模型权重可以缓存；不会在本机同步整套候选图片。
暂停 Worker 后不会继续创建或领取新的预打标任务。
