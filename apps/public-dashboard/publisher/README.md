# Dashboard 增量缓存与图片 Publisher

该独立 worker 负责把 `core` 的变化送入 Dashboard API，并处理无法从 PostgreSQL 直接
取得的结算图片：

1. 使用只读账号从 `core` 读取玩家、对局和 source revision；
2. 通过内容哈希只发送变化的对局、删除项和完整的小型玩家集合；初次引导按最多 500
   场分批，只有最后一批发布 public/owner 榜单；
3. 将新增或变化的图片压缩为 WebP 并上传 OSS，再写入图片 URL、尺寸和 SHA-256；
4. 网络或 API 失败时分别保留缓存和图片 outbox，重启后用相同幂等键继续重试。

Publisher 不计算榜单、评分或趋势，也不生成静态 JSON 快照；这些计算在 API 的短生命
周期 ingest 子进程中完成。Publisher 的本地 JSON 只保存内容哈希、水位和失败 outbox，
不是网页数据源。source revision 变化但玩家/对局未变化时，API 只快进 revision，不重算
榜单。

容器只复制共享网络选路模块，固定线路会同时绑定源地址与 DNS，并记录用途、网卡、
源地址和流量。该镜像不继承 BLREC Server 镜像，也不包含 FFmpeg、管理端、分析模型
或推理依赖。NAS 安装、备份、验证与回滚见 `../deploy/nas/README.md`。
