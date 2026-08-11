# 虚荣对局榜前台

这是独立于 BLREC 管理后台的公开静态站点。它单独安装依赖、构建和发布，不会被打进 NAS 上的后台管理页面。

首先在仓库根目录安装共享核心和 Dashboard Publisher，再从 SQLite 生成只读静态
快照：

```bash
python -m pip install -e .
python -m pip install -e 'apps/public-dashboard/publisher[avatars]'
blrec-dashboard-export \
  --database /path/to/blrec.sqlite3 \
  --output apps/public-dashboard/public-data
```

`public-data/manifest.json`、`public-data/snapshots/` 和
`public-data/avatars/` 是包含真实榜单及玩家头像的本地生成产物，已被 Git
忽略。导出器使用 SQLite 只读事务，不会修改正在更新的业务数据库；JSON
生成完成后，会按玩家绑定的直播间从 B 站公开接口同步头像并压缩为 256px
JPEG。该导出仅用于本地预览或应急恢复；临时离线导出时可以追加
`--skip-player-avatars`。

开发与验证：

```bash
npm ci
npm start
npm test
npm run lint
npm run build
```

生产构建位于 `dist/`。本地 `public-data/` 有快照时，构建可用于完整预览；
GitHub 的生产发布会拒绝上传其中的 `/data/`，只更新页面和静态资源。线上榜单、
趋势和对局由 `https://vg-api.luwei.host/v1` 提供；完整对局采用分页接口，首页
响应不再携带全部对局。原 `/data/manifest.json`、`trends.json` 和最后一份静态
快照继续保留为 API 故障时的只读回退，不再随日常数据变化更新。玩家头像、
结算图与访问统计仍由各自独立链路维护。

生产环境已将页面、API 与数据同步拆成独立链路。GitHub Actions 分别发布页面
和 ECS API；NAS worker 每秒读取持久变更版本号，变化后合并 2 秒并只向 API 增量写入，
并把结算图写入 OSS；工程化配置、权限边界和恢复流程见
[`DEPLOYMENT.md`](DEPLOYMENT.md)。

正式静态站点、Publisher Python 包和 Publisher 镜像只由 GitHub Actions 构建。
Publisher 镜像为 `ghcr.io/luweicn/blrec-dashboard-publisher`，不继承或安装 BLREC
Server 镜像。

站点 `favicon.ico` 使用《虚荣》官方网站提供的多尺寸品牌图标，来源为
`https://www.vainglorygame.com/wp-content/themes/vainglory/images/favicon.ico`。

`vg.luwei.host` 的 HTTPS 使用现有 Nginx UI 签发的 `*.luwei.host` 通配符
证书。`deploy/aliyun/` 中的服务器定时任务会在证书续期后自动、幂等地同步
到阿里云 CDN，HTTP 请求由 CDN 使用 301 跳转到 HTTPS；站点发布和 NAS
数据发布都不接触证书私钥。
