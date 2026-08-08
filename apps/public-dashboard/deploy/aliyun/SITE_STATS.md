# 公开站点访问统计

静态站点通过 `/analytics/pixel.svg` 记录匿名页面浏览与可见页心跳。阿里云
CDN 将这些请求投递到 SLS；服务器每五分钟执行一次
`publish_site_stats.py`，只把聚合后的四个数字写入 OSS：今日访客、今日浏览、
近五分钟活跃和累计浏览。

这条链路不读取 SQLite，也不调用排行榜快照导出器。SLS 原始日志保留七天，
按日汇总历史保存在 OSS 的 `data/site-stats-history.json`，因此累计浏览不会在
七天后清零。每次执行都会重算最近七个自然日；短期故障或服务器重启后可
自动补齐，且计数回退时会停止发布而不是覆盖累计数据。

## RAM 最小权限

把 `site-stats-sls-read-policy.json` 附加到站点发布 RAM 用户。策略只允许
执行 `log:GetLogStoreLogs`，且资源被限定为北京区域 `vainglory` Project 下的
`vainglory-dashboard` Logstore。浏览器和公开 JSON 中都不得出现 AccessKey。

## 服务器安装

安装目录固定为 `/opt/blrec-dashboard-site-stats`，配置文件为
`/etc/blrec-dashboard/site-stats.env`，权限必须是 `0600`：

```bash
python3 -m venv /opt/blrec-dashboard-site-stats/venv
/opt/blrec-dashboard-site-stats/venv/bin/pip install \
  -r /opt/blrec-dashboard-site-stats/site-stats-requirements.txt
install -m 0600 site-stats.env /etc/blrec-dashboard/site-stats.env
systemctl daemon-reload
systemctl enable --now blrec-dashboard-site-stats.timer
systemctl start blrec-dashboard-site-stats.service
systemctl status blrec-dashboard-site-stats.service
systemctl list-timers blrec-dashboard-site-stats.timer
```

定时器使用 `Persistent=true`，错过计划时间后会在服务器恢复时补跑。服务
成功时只记录聚合数字，不记录 AccessKey、访客标识或 SLS 原始日志。
