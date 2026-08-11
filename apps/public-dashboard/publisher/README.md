# Dashboard Publisher

该包从 BLREC SQLite 只读提取规范化玩家与对局，并通过既有网络选路层同步到
排行榜 API；结算图压缩后仍发布到 OSS。
Publisher 与静态站点同属 Public Dashboard 产品，但使用独立容器部署；镜像不继承
BLREC Server 镜像，也不包含 FFmpeg、管理端、分析模型或推理依赖。

容器只复制共享的网络选路模块，以确保固定线路同时绑定源地址与 DNS，并继续记录
用途、网卡、源地址和流量。NAS 安装、备份、验证与回滚见 `../deploy/nas/README.md`。

生产 Compose 默认设置 `DASHBOARD_STATIC_JSON_ENABLED=false`，因此不再更新旧的
manifest、快照和 `trends.json`。服务端在同一个写事务中重算榜单物化视图与当天
趋势；旧静态文件留作 API 不可用时的只读回退。本地预览与应急恢复仍可启用静态
JSON 导出能力。
