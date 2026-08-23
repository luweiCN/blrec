# Dashboard 图片 Publisher

该独立 worker 只处理无法从 PostgreSQL 直接取得的结算图片和回放可见性核验：

1. 使用只读账号从 `core` 读取结算图片引用和 source revision；
2. 将新增或变化的图片压缩为 WebP 并上传 OSS，再写入图片 URL、尺寸和 SHA-256；
3. 网络或 API 失败时保留图片 outbox，重启后用相同幂等键继续重试。

Publisher 不复制玩家或对局，不计算榜单、评分或趋势，也不生成静态 JSON 快照。
Dashboard API 直接读取 `core`；Publisher 的本地 JSON 只保存图片签名、水位和失败
outbox，不是网页数据源。

容器只复制共享网络选路模块，固定线路会同时绑定源地址与 DNS，并记录用途、网卡、
源地址和流量。该镜像不继承 BLREC Server 镜像，也不包含 FFmpeg、管理端、分析模型
或推理依赖。NAS 安装、备份、验证与回滚见 `../deploy/nas/README.md`。
