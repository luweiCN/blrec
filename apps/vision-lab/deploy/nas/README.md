# Vision Lab NAS 图片与候选索引服务

NAS 不再运行交互式标注控制面。这个 Compose 项目只负责：

- 从 NAS 本地路径读取原图和缩略图；
- 为模型 Worker 提供受 token 保护的图片下载；
- 为迁移前生成的数据集清单、模型产物和模型包提供受 token 保护的只读下载；
- 接收 BLREC Server 刚产出的候选元数据并立即写入本机 Vision 控制机数据库；
- 保存控制机定时上传的可验证 PostgreSQL 备份；
- 仅在人工开启对账时扫描 NAS 候选目录，补回漏写素材。

标注页面、筛选、统计、保存、模型预填和训练任务控制面全部运行在 Vision
Worker 本机，直接连接本机 PostgreSQL。普通标注 API 不经过 NAS。

持久化目录：

- `/volume1/docker/blrec-next/vision-lab/data`：历史数据集和模型文件，只读回溯使用；
- `/volume1/docker/blrec-next/vision-data/candidates`：Analysis Worker 产出的候选素材；
- `/volume1/docker/blrec-next/vision-models`：已发布模型包。

`.env` 至少需要配置：

```dotenv
VISION_LAB_WORKER_TOKEN=独立随机令牌
VISION_LAB_IMAGE_TAG=vision-lab-v0.3.49
VISION_LAB_DATABASE_URL=postgresql://vision:密码@127.0.0.1:15434/blrec_vision
VISION_LAB_DATABASE_SCHEMA=vision_lab
VISION_LAB_CANDIDATE_RECONCILIATION_ENABLED=0
```

正常链路由 BLREC Server 调用本机回环地址
`http://127.0.0.1:8800/api/training-candidates/ingest`，不再周期遍历全部 JSON。
BLREC Server 的环境文件需要同时配置：

```dotenv
BLREC_VISION_LAB_INGEST_URL=http://127.0.0.1:8800/api/training-candidates/ingest
BLREC_VISION_LAB_INGEST_TOKEN=与上方 VISION_LAB_WORKER_TOKEN 相同的令牌
```

该入口在代码中强制限定为 loopback HTTP，不会把图片或标注发送到局域网外。
首次上线或需要对账时，在 NAS Vision Lab 容器中执行：

```bash
blrec-vision-backfill-material-index --apply
```

命令会把候选目录中的历史 sidecar 幂等补入数据库，再重建素材检索索引和
增量统计；中断后可直接重跑。若只需重建数据库内已有素材的索引，可增加
`--skip-candidate-import`。

Compose 必须用 `entrypoint` 把镜像入口覆盖为 `blrec-vision-media`，不能只写
`command`。否则镜像默认的 `blrec-vision-lab` 仍会启动，NAS 会重新暴露完整
标注 Server，保存请求又可能被错误地接回 NAS。

数据库 URL 使用 NAS 自己的 `127.0.0.1:15434`。这个端口由 Vision 控制机主动
建立的反向 SSH 隧道提供，最终连接控制机本地 `127.0.0.1:5432`；因此控制机 IP
变化不会影响 NAS，PostgreSQL 也不需要监听局域网。

## 上线顺序与回退

1. 先发布并启动新版 Vision NAS 图片服务，让 PostgreSQL schema 迁移完成；
2. 执行 `blrec-vision-backfill-material-index --apply`，确认最后一条输出中
   `index.indexed` 与 `index.total` 一致；
3. 再给 BLREC Server 配置回环增量地址和同一个 Worker token，并发布 Server；
4. 新增一张候选后，确认素材建议和筛选列表无需目录扫描即可出现该素材；
5. 最后保持 `VISION_LAB_CANDIDATE_RECONCILIATION_ENABLED=0`。

需要回退时，先移除 BLREC Server 的 `BLREC_VISION_LAB_INGEST_URL`。候选图片和
sidecar 始终已经落盘，因此不会丢素材；旧版 Vision 可继续读取原表，新增的索引表
可以保留，之后重新上线并再次执行回填即可。
