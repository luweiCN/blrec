# 虚荣对局榜前台

这是独立于 BLREC 管理后台的公开静态站点。它单独安装依赖、构建和发布，不会被打进 NAS 上的后台管理页面。

开发与验证：

```bash
npm ci
npm start
npm test
npm run lint
npm run build
```

生产构建位于 `dist/`。部署时将该目录作为一个版本化静态发布上传到阿里云；排行榜数据由 NAS 另行生成并发布到 `/data/`，站点代码发布与每日数据发布互不依赖。
