# 虚荣对局榜前台

这是独立于 BLREC 管理后台的公开静态站点。它单独安装依赖、构建和发布，不会被打进 NAS 上的后台管理页面。

首先从 SQLite 生成只读静态快照：

```bash
PYTHONPATH=../src python -m blrec.vainglory.dashboard_snapshot \
  --database /path/to/blrec.sqlite3 \
  --output public-data
```

`public-data/manifest.json`、`public-data/snapshots/` 和
`public-data/avatars/` 是包含真实榜单及玩家头像的本地生成产物，已被 Git
忽略。导出器使用 SQLite 只读事务，不会修改正在更新的业务数据库；JSON
生成完成后，会按玩家绑定的直播间从 B 站公开接口同步头像并压缩为 256px
JPEG。临时离线导出时可以追加 `--skip-player-avatars`。

开发与验证：

```bash
npm ci
npm start
npm test
npm run lint
npm run build
```

生产构建位于 `dist/`。本地 `public-data/` 有快照时，构建可用于完整预览；
GitHub 的生产发布会拒绝上传其中的 `/data/`，只更新页面和静态资源。日常数据
发布只替换 `/data/manifest.json` 并新增不可变快照，现有玩家头像与访问统计由
各自独立链路维护，无需重新发布站点代码。

生产环境已将页面和数据拆成独立发布链路。GitHub Actions 只发布页面文件，
NAS worker 每天生成并发布排行榜 JSON；工程化配置、权限边界和恢复流程见
[`DEPLOYMENT.md`](DEPLOYMENT.md)。

站点 `favicon.ico` 使用《虚荣》官方网站提供的多尺寸品牌图标，来源为
`https://www.vainglorygame.com/wp-content/themes/vainglory/images/favicon.ico`。

`vg.luwei.host` 的 HTTPS 使用现有 Nginx UI 签发的 `*.luwei.host` 通配符
证书。`deploy/aliyun/` 中的服务器定时任务会在证书续期后自动、幂等地同步
到阿里云 CDN，HTTP 请求由 CDN 使用 301 跳转到 HTTPS；站点发布和 NAS
数据发布都不接触证书私钥。
