# 阿里云 CDN 证书自动同步

Nginx UI 继续负责签发和续期 `*.luwei.host` 通配符证书。本目录的
oneshot 服务每小时比较本地证书与 CDN 当前证书；只有叶证书 SHA-256 指纹
变化或 CDN HTTPS 被关闭时才上传，并在上传后等待阿里云返回新状态。

同步脚本会先检查证书覆盖 `vg.luwei.host`、至少还有 14 天有效期，并检查
证书与私钥匹配。私钥只由 root 服务读取并在进程内交给阿里云 SDK，不进入
命令行参数、日志、NAS 或仓库。

RAM 用户只需要附加 [ram-policy.json](ram-policy.json) 中限定到
`vg.luwei.host` 的证书同步和 HTTPS 跳转配置权限。凭据保存在服务器
`/etc/blrec-dashboard/aliyun-cdn.env`，文件权限必须为 `0600`：

```ini
ALIBABA_CLOUD_ACCESS_KEY_ID=...
ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
```

服务器安装位置固定为 `/opt/blrec-dashboard-cdn-sync`。安装 SDK 后，将 service
和 timer 复制到 `/etc/systemd/system/`，执行：

```bash
systemctl daemon-reload
systemctl enable --now blrec-dashboard-cdn-certificate.timer
systemctl start blrec-dashboard-cdn-certificate.service
systemctl status blrec-dashboard-cdn-certificate.service
systemctl list-timers blrec-dashboard-cdn-certificate.timer
```

`Persistent=true` 会在服务器错过计划时间后补跑；远端 CDN 证书本身是幂等
事实源，因此服务失败或服务器重启后都可以安全重试。
