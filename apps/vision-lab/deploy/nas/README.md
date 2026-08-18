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
VISION_LAB_IMAGE_TAG=vision-lab-v0.3.11
VISION_LAB_DATABASE_URL=postgresql://...
VISION_LAB_DATABASE_SCHEMA=vision_lab
```

`VISION_LAB_DATABASE_URL` 应连到现有受管的正式 PostgreSQL 通道；不要让
Vision Lab 绕过既有固定线路直连外网数据库。首次切换前先停止写入、
备份 `lab.db` 与正式库，再运行：

```bash
python -m labeler.migrate_sqlite_to_postgres \
  --sqlite /data/lab.db \
  --schema vision_lab \
  --report /data/postgres-migration-report.json
```

脚本只允许写入空 schema，并会逐表校验行数和内容摘要；校验不通过时整个
数据事务回滚。

不要在这个 Compose 项目中增加训练进程。计算节点使用 `blrec-vision-worker` 命令连接
NAS 上的 Vision Lab API，并按自身能力领取任务。
