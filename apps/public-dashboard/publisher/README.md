# Dashboard Publisher

该包从 BLREC SQLite 只读导出排行榜快照，并通过既有网络选路层发布到 OSS。
Publisher 与静态站点同属 Public Dashboard 产品，但使用独立容器部署；镜像不继承
BLREC Server 镜像，也不包含 FFmpeg、管理端、分析模型或推理依赖。

容器只复制共享的网络选路模块，以确保固定线路同时绑定源地址与 DNS，并继续记录
用途、网卡、源地址和流量。NAS 安装、备份、验证与回滚见 `../deploy/nas/README.md`。
