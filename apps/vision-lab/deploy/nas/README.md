# Vision Lab NAS 控制面

这个 Compose 项目只运行网页、轻量 API、标注数据库和任务队列。训练、模型测试、
模型预填与模型包组装由独立 Vision Worker 领取执行，不在 NAS Web 进程中运行。

持久化目录：

- `/volume1/docker/blrec-next/vision-lab/data`：标注数据库、帧、数据集清单、模型记录。
- `/volume1/docker/blrec-next/vision-data/candidates`：Analysis Worker 产出的候选素材。
- `/volume1/docker/blrec-next/vision-models`：发布给分析 Worker 的模型。

`.env` 至少需要配置：

```dotenv
VISION_LAB_WORKER_TOKEN=独立随机令牌
VISION_LAB_IMAGE_TAG=vision-lab-v0.3.2
```

不要在这个 Compose 项目中增加训练进程。计算节点使用 `blrec-vision-worker` 命令连接
NAS 上的 Vision Lab API，并按自身能力领取任务。
