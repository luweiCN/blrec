# Vision Lab NAS 图片与候选索引服务

NAS 不再运行交互式标注控制面。这个 Compose 项目只负责：

- 从 NAS 本地路径读取原图和缩略图；
- 为模型 Worker 提供受 token 保护的图片下载；
- 为迁移前生成的数据集清单、模型产物和模型包提供受 token 保护的只读下载；
- 扫描 NAS 候选目录并把新素材索引写入共享 PostgreSQL。

标注页面、筛选、统计、保存、模型预填和训练任务控制面全部运行在 Vision
Worker 本机，直接连接共享 PostgreSQL。普通标注 API 不经过 NAS。

持久化目录：

- `/volume1/docker/blrec-next/vision-lab/data`：历史数据集和模型文件，只读回溯使用；
- `/volume1/docker/blrec-next/vision-data/candidates`：Analysis Worker 产出的候选素材；
- `/volume1/docker/blrec-next/vision-models`：已发布模型包。

`.env` 至少需要配置：

```dotenv
VISION_LAB_WORKER_TOKEN=独立随机令牌
VISION_LAB_IMAGE_TAG=vision-lab-v0.3.14
VISION_LAB_DATABASE_URL=postgresql://...
VISION_LAB_DATABASE_SCHEMA=vision_lab
```

容器入口固定为 `blrec-vision-media`。不要改回 `blrec-vision-lab`，否则 NAS
会重新暴露完整标注 Server，保存请求又可能被错误地接回 NAS。

数据库 URL 继续使用 NAS 自己的固定 PostgreSQL 隧道。Mac Worker 有独立 SSH
隧道，两者不会互相转发流量。
