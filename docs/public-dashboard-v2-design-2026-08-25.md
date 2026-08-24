# Public Dashboard v2 载荷诊断与最小设计

日期：2026-08-25

## 结论

问题已经复现。首屏慢与 `/v1/matches` 无关；`/v1/dashboard` 把当前页面没有使用的九个赛季完整榜单、九套环境英雄明细，以及 180 份历史完整榜单一次发给浏览器。现有 ETag 正常，但它只能避免“内容完全没变”时的 body；当前历史回填持续推进 revision，因此约每 45～60 秒仍会重新传输整个聚合文档。

本次不修改 `/v1/matches`，不删除 `/v1/dashboard`，不以调高 Nginx gzip 作为解决方案。

## 可重复基线

运行：

```bash
python apps/public-dashboard/api/scripts/measure_dashboard_payload.py \
  https://vg-api.luwei.host/v1/dashboard
```

脚本只输出字段名、大小和计数，不保存或打印业务数据正文。固定结果保存在 `docs/public-dashboard-v2-baseline-2026-08-25.json`。

| 项目 | 原始 | gzip / 线上传输 |
| --- | ---: | ---: |
| `/v1/dashboard` 整包 | 12,302,575 B | 1,801,166 B（线上） |
| `snapshot` | 5,625,195 B | 566,170 B（字段独立 gzip） |
| `trends` | 6,677,357 B | 705,082 B（字段独立 gzip） |
| `snapshot.standings` | 5,623,068 B | 565,647 B |
| `trends.publications` | 6,677,280 B | 705,013 B |

当前整包 identity 请求约 30.85 秒，gzip 请求约 3.59 秒。字段独立 gzip 只用于找占比，不能与整包线上 gzip 直接相加。

## 字段消费表

| 字段 | 实际页面／代码 | 加载时机 |
| --- | --- | --- |
| `snapshot.schemaVersion` | `public-dashboard-data.service.ts` 契约校验 | 初次加载；不直接显示 |
| `snapshot.snapshotId` | DataService 校验当前趋势节点；首页、玩家榜、玩家详情计算趋势 | 初次元数据；趋势请求的 revision 基准 |
| `snapshot.contentRevision` | DataService 判断是否更新 | 初次元数据、SSE revision 对账 |
| `snapshot.publicationDate` | Shell 页脚 `DATA SNAPSHOT` | 初次元数据 |
| `snapshot.generatedAt` | 契约校验；Dashboard 页面未直接读取 | 初次元数据 |
| `snapshot.sourceLastMatchId` | 契约校验；Dashboard 页面未直接读取 | 初次元数据 |
| `snapshot.sourceMatchCount` | 契约校验；首页统计实际由 standings 重算 | 初次元数据 |
| `snapshot.ratingModel` | 契约校验；当前页面未直接读取模型参数 | 初次元数据 |
| `snapshot.currentSeasonKey` | Shell、首页、玩家榜、英雄榜、玩家详情、英雄详情默认赛季 | 初次元数据 |
| `snapshot.seasons` | 所有赛季选择器；玩家／英雄详情赛季历史 | 初次元数据；仅 1.8 KB 原始 |
| `snapshot.standings[season].players` | 首页、玩家榜、玩家详情、英雄熟练度对比 | 首页只取当前赛季；切赛季按需取 |
| `snapshot.standings[season].heroes` | 首页英雄榜、英雄榜、英雄详情 | 首页只取当前赛季；切赛季按需取 |
| `snapshot.standings[season].environmentHeroes` | 英雄榜切到“全阵容环境”；英雄详情协同／克制 | 默认不取；用户切环境或打开相应详情才取 |
| `snapshot.matches` | 首页、比赛页、玩家详情的旧回退路径 | 当前 `/v1/matches` 可用时不需要；v2 不返回 |
| `trends.schemaVersion` | DataService 契约校验 | 请求趋势时 |
| `trends.updatedAt` | 契约校验；页面未直接显示 | 请求趋势时 |
| `trends.publications[].snapshotId` | 找到当前快照在时间线中的位置 | 请求当前趋势时 |
| `trends.publications[].publicationDate` | 趋势图横轴、表格、提示 | 请求当前趋势时 |
| `trends.publications[].sourceLastMatchId` | 契约校验；页面未直接读取 | v2 可省略 |
| `trends.publications[].standings` | 首页前十、玩家榜当前页、玩家详情当前玩家趋势 | 只取当前赛季、模式、可见玩家和时间范围 |
| `PlayerStanding.heroPool` | 多处展示玩家英雄池 | v2 不发送；兼容层从 `heroPools.all` 提供同一引用 |
| `PlayerStanding.heroPools` | 模式英雄池、英雄熟练度 | 随当前赛季 standings 加载 |

当前行为的契约证据：

- `public-dashboard-data.service.spec.ts` 的 “loads the database-backed dashboard API directly” 证明首屏只请求一次完整 `/v1/dashboard`。
- `public-dashboard-shell.component.spec.ts` 的 “documents the legacy v1 contract...” 证明 `dashboard` SSE 事件会调用同一聚合文档刷新路径。

## 最小资源边界

真实 UI 的玩家／英雄详情页会展示跨赛季历史，仅有四个建议接口会迫使前端重新下载九个完整榜单。因此保留建议的四个边界，并增加两个紧凑历史接口；它们返回的只是单个对象跨赛季的摘要，不返回完整榜单。

### `GET /v2/dashboard/summary`

返回：

- schema、快照日期、当前赛季、赛季列表、rating model 版本；
- `playersById` 的静态资料（57 个唯一玩家，只保存一份）；
- 资源 revision manifest：summary、各赛季 standings、各赛季 environment、趋势 revision、matches revision、live rooms revision。

不返回 standings、trends、environment、matches。

### `GET /v2/standings?seasonId=...`

只返回一个赛季的 `players` 与 `heroes`。玩家只保留 `heroPools`；前端临时适配 `heroPool = heroPools.all`，不复制数组。

模式切换仍在浏览器使用同一赛季对象完成，因为一个玩家的四个 mode 汇总很小，避免每点一次 mode 再请求。

### `GET /v2/trends?seasonId=...&mode=...&playerIds=...&from=...&to=...`

只返回所选玩家的紧凑序列：公共日期数组 + 每位玩家的 `[dateIndex, rank, ratingScore]`。首页请求前十，玩家榜请求当前页，玩家详情只请求本人。默认最近 30 天；用户选更长范围时再请求。

### `GET /v2/environment?seasonId=...`

只在英雄页切到环境范围或需要协同／克制时加载。返回该赛季 `environmentHeroes`。

### `GET /v2/players/{playerId}/history?mode=...`

返回该玩家每赛季的 performance 与 rank，替代为了一个玩家下载九个完整玩家榜。

### `GET /v2/heroes/{heroId}/history?mode=...&scope=...`

返回该英雄每赛季的 performance 与 rank，替代为了一个英雄下载九个完整英雄榜。

## revision 与 ETag

- 每个规范化查询键独立生成稳定 JSON 和强 ETag：`sha256(resource + canonical_query + resource_revision)`。
- 查询参数先规范化：season/mode 单值；playerIds 去重并升序；日期转 ISO；字段与数组稳定排序。
- 公共响应使用 `Cache-Control: public, no-cache`；owner 响应继续 `private, no-store` 和 `Vary: Authorization`。
- `If-None-Match` 命中返回 304、空 body。
- summary manifest 本身很小；`resync` 只重新取 manifest，再比较已加载资源 revision。

## SSE 事件

保持 `/v1/events` 地址和旧事件兼容，同时让 data 携带：

```json
{"resource":"standings","seasonId":"2026-summer","revision":"..."}
```

资源包括 `summary`、`standings`、`trends`、`environment`、`matches`、`live_rooms`。前端只在资源当前可见且 revision 变化时重新请求。未打开环境页时，environment 事件只更新 manifest，不发环境请求。

`resync` 先取 summary manifest，禁止直接回退 `/v1/dashboard`。

## 灰度与回滚

- 新增 build-time feature flag `useDashboardV2`；初期 v1 逻辑和 `/v1/dashboard` 原样保留。
- v2 可用时默认灰度打开；API 异常不静默混用半套数据，显示错误并可通过一次配置发布切回 v1。
- 回滚只切 feature flag，不删 v2 表或接口；旧前端继续可用。
- 线上记录每个资源的 status、raw/gzip bytes、duration、304 命中和 SSE 后请求原因，不记录响应正文。

## 实现验收预算

- 首屏：summary + 当前赛季 standings + 当前可见趋势，合计 `<500 KB gzip`，目标 `<250 KB`。
- 单响应 `<500 KB gzip`。
- payload budget fixture 在 CI 中对 raw 与 gzip 双门槛失败。
- v1/v2 同 season/mode 的玩家顺序、分数、英雄榜和趋势点逐字段对比。
