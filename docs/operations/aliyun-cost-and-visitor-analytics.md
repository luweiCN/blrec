# 阿里云成本与访问分析

管理后台新增两个只读页面：

- “云成本”汇总阿里云账号账单、`vg.luwei.host` 的 CDN 用量以及 OSS 计费用量。
- “访问分析”查询 `vainglory/vainglory-dashboard` Logstore，展示访问趋势、页面、来源、设备、浏览器、地域和运营商分布，并支持组合筛选。

两个功能都只从服务端访问阿里云。AccessKey 不会写入前端资源或 API 响应。

## RAM 权限

为 NAS 服务端创建一个只读 RAM 用户，并附加
`apps/public-dashboard/deploy/aliyun/admin-observability-read-policy.json`。策略包含：

- 账单概览与 OSS 计费项查询；
- CDN 域名用量查询；
- 指定 Logstore 的日志查询。RAM 控制台“摘要 Beta”可能把 `log` 同时识别为
  云监控和日志服务，从而提示“未定义有效资源”；以源代码校验和实际 API 验证为准。

不要把站点发布所需的 OSS 写权限加入这个只读用户。若沿用已有 RAM 用户，也应确认它只拥有实际需要的权限。

## NAS 环境变量

在 Container Manager 项目使用的 `.env` 中填写：

```dotenv
BLREC_CLOUD_COST_ALIYUN_ACCESS_KEY_ID=
BLREC_CLOUD_COST_ALIYUN_ACCESS_KEY_SECRET=
BLREC_CLOUD_COST_ALIYUN_CDN_DOMAIN=vg.luwei.host
BLREC_CLOUD_COST_ALIYUN_OSS_BUCKET=luwei-vainglory
BLREC_VISITOR_ANALYTICS_SLS_ENDPOINT=cn-beijing.log.aliyuncs.com
BLREC_VISITOR_ANALYTICS_SLS_PROJECT=vainglory
BLREC_VISITOR_ANALYTICS_SLS_LOGSTORE=vainglory-dashboard
BLREC_VISITOR_ANALYTICS_DOMAIN=vg.luwei.host
```

访问分析默认复用云成本的 AccessKey。如果以后需要拆分凭据，可以另外设置
`BLREC_VISITOR_ANALYTICS_ALIYUN_ACCESS_KEY_ID` 和
`BLREC_VISITOR_ANALYTICS_ALIYUN_ACCESS_KEY_SECRET`。

可选缓存配置：

```dotenv
BLREC_CLOUD_COST_CACHE_SECONDS=600
BLREC_VISITOR_ANALYTICS_CACHE_SECONDS=300
BLREC_VISITOR_ANALYTICS_RETENTION_DAYS=7
```

## 网络选路

网络管理页包含“云成本查询”和“访客日志查询”两个用途。二者携带 RAM 凭据，因此：

- 只允许固定线路；
- DNS 与请求都绑定所选线路；
- 线路故障时停止查询，不会切换出口；
- 未选择网卡时使用并记录系统实际默认网卡。

旧配置升级时，“云成本查询”继承原排行榜发布线路；“访客日志查询”优先继承云成本线路。升级后应在网络管理页确认两项线路再保存。

## 数据口径与隐私

- 云成本页面的 CDN 流量和请求数精确到 `vg.luwei.host`，但 CDN 费用是账号产品级汇总。
- OSS 计费用量与费用来自账号产品级月账单。阿里云账单接口不能把费用精确拆分到单个 Bucket。
- 账单数据相对实际用量通常延迟约 24 小时，当前月账单也可能继续调整；页面会标出生成时间并使用 10 分钟缓存。
- CDN 流量与请求数按小时查询并汇总到当月，阿里云侧通常仍有数小时延迟。
- 公开站点只采集路由类别、来源域名和粗粒度设备类型，不采集搜索词或玩家、英雄 ID。
- 旧版 `pageview`/`heartbeat` 请求保持原格式，用于继续累计公开站点统计；详细维度通过独立 `detail` 事件上报，避免发布先后顺序造成访问量回退或中断。
- 详细分布只会包含新版埋点上线后产生的记录，既有日志不会被猜测或补写页面、来源和设备字段。
- 地域与运营商由 SLS 在服务端根据日志 IP 聚合。管理 API 不返回原始 IP。
- 最近访问中的匿名访客标识由服务端单向哈希，不返回浏览器 UUID。
- 当前 Logstore 原始日志保留 7 天，因此组合筛选也限制在最近 7 天。公开页面原有的累计浏览历史仍由 OSS 汇总文件长期保存，不受原始日志过期影响。

## 验收

1. 先在网络管理页确认两个用途均为“固定线路”。
2. 打开“云成本”，确认本月账单、CDN 用量和 OSS 明细至少有一项返回；若为部分结果，按页面警告补 RAM 权限。
3. 访问一次 `https://vg.luwei.host` 并切换页面，等待日志写入后打开“访问分析”。
4. 分别按页面、省份和设备筛选，确认趋势与最近访问同步变化。
5. 检查浏览器开发者工具，确认管理端响应不含 AccessKey、原始 IP 或完整匿名 UUID。
