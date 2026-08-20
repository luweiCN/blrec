# 虚荣对局榜前台

这是独立于 BLREC 管理后台的公开静态站点。页面代码由 GitHub Actions 构建并发布，
但榜单、趋势、直播状态和对局数据不是静态文件：浏览器统一从
`https://vg-api.luwei.host/v1` 读取。

生产数据链路如下：

1. BLREC 和 Analysis Worker 直接写移动云 PostgreSQL 的 `core` schema。
2. NAS Publisher 使用只读账号比较玩家、对局内容哈希，只把变化的记录通过带幂等键的
   API 批次发送；source revision 变化但内容未变化时只发送空的快进批次。
3. API 在短生命周期子进程中把增量写入 `public` schema，并在同一事务发布 public/owner
   榜单；页面随后通过 API 获取榜单和分页对局，SSE 通知页面刷新。
4. Publisher 还处理仍位于 NAS 文件系统中的结算图片：上传 OSS 后，把图片 URL、尺寸和
   摘要写入独立的资产表。

接口响应仍然使用 JSON 作为 HTTP 传输格式，但生产链路不生成或读取
`manifest.json`、榜单快照文件或 `trends.json`。PostgreSQL 作为持久缓存层保存
可变的对局查询索引和同 revision 的 public/owner 榜单字节；API 进程只保留当前响应字节，
列表请求最多读取一页数据。历史趋势和结算图片元数据仍以结构化表保存。

开发与验证：

```bash
npm ci
npm start
npm test
npm run lint
npm run build
```

生产构建位于 `dist/`。GitHub 的页面发布拒绝上传真实的 `/data/` 榜单文件，只更新
页面与静态资源。完整发布结构、权限边界和恢复流程见
[`DEPLOYMENT.md`](DEPLOYMENT.md)。

正式页面、Dashboard API wheel、Publisher wheel 和 Publisher 镜像只由 GitHub
Actions 构建；本机不构建正式制品或容器镜像。

站点 `favicon.ico` 使用《虚荣》官方网站提供的多尺寸品牌图标，来源为
`https://www.vainglorygame.com/wp-content/themes/vainglory/images/favicon.ico`。

`vg.luwei.host` 的 HTTPS 使用现有 Nginx UI 签发的 `*.luwei.host` 通配符证书。
`deploy/aliyun/` 中的服务器定时任务会在证书续期后自动、幂等地同步到阿里云 CDN；
站点发布和 NAS 图片发布都不接触证书私钥。
