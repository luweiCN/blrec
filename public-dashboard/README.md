# 虚荣对局榜前台

这是独立于 BLREC 管理后台的公开静态站点。它单独安装依赖、构建和发布，不会被打进 NAS 上的后台管理页面。

首先从 SQLite 生成只读静态快照：

```bash
PYTHONPATH=../src python -m blrec.vainglory.dashboard_snapshot \
  --database /path/to/blrec.sqlite3 \
  --output public-data
```

`public-data/manifest.json` 和 `public-data/snapshots/` 是包含真实榜单的本地生成产物，已被 Git 忽略。导出器使用 SQLite 只读事务，不会修改正在更新的业务数据库。

开发与验证：

```bash
npm ci
npm start
npm test
npm run lint
npm run build
```

生产构建位于 `dist/`，其中已包含当次构建使用的 `/data/` 快照。部署时将该目录上传到阿里云 OSS；日常数据发布只替换 `/data/manifest.json` 并新增不可变快照，无需重新发布站点代码。
