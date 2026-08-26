# PVE 内网站长页面

站长页面是 Public Dashboard 的独立内网构建，只监听 PVE 管理网地址的 `8790` 端口。
公开构建没有编辑入口；内网构建通过同源 `/internal-api/` 调用 BLREC 既有的对局纠错接口。

浏览器不保存、接收或提交管理密钥。PVE Nginx 从权限为 `0600` 的
`/etc/blrec-dashboard-admin/admin.env` 读取既有 BLREC API key 和 Dashboard owner
token：前者只在向 NAS BLREC 管理 API 反代时注入，后者只在同机读取完整 Dashboard
数据时注入。两个值都不会进入静态文件或浏览器。对局修改继续由 BLREC 负责人工覆盖、
Vision Lab 纠错样本和 Dashboard revision 更新。

首次部署前：

1. 参考 `admin.env.example` 创建 `/etc/blrec-dashboard-admin/admin.env`；
2. 确认站点只监听 `192.168.50.0/24` 管理网，不开放公网或 WireGuard 公网转发；
3. 为 PVE self-hosted runner 仅授权非交互执行本目录的 `install-release.sh`。

每个 GitHub Actions release 都是不可变目录。安装脚本会先验证 Nginx 配置，再原子切换
静态文件和站点配置；首页或 BLREC 英雄接口检查失败时恢复上一 release。
