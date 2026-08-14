# Dashboard 图片资产 Publisher

该独立 worker 只负责公开榜单无法从 PostgreSQL 直接取得的结算图片：

1. 使用只读账号从 `core` 查询公开对局 ID 和 NAS 本地结算图路径；
2. 将新增或变化的图片压缩为 WebP 并上传 OSS；
3. 通过带幂等键的 API 批次写入图片 URL、尺寸和 SHA-256；
4. 网络或 API 失败时保留本地 outbox，重启后继续重试。

玩家、对局、阵容、榜单和当前趋势不经过 Publisher，也不生成静态 JSON 快照。
Dashboard API 会直接读取 PostgreSQL 业务表并维护自己的内存缓存。Publisher 的本地
JSON 文件仅是图片上传水位和失败重试日志，不是网页数据源，也不参与榜单计算。

容器只复制共享网络选路模块，固定线路会同时绑定源地址与 DNS，并记录用途、网卡、
源地址和流量。该镜像不继承 BLREC Server 镜像，也不包含 FFmpeg、管理端、分析模型
或推理依赖。NAS 安装、备份、验证与回滚见 `../deploy/nas/README.md`。
