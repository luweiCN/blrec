# BLREC 工具

Chromium 浏览器插件。在 B 站直播页顶部提供以下操作：

- 未收录：`收录`、`收录并投稿`
- 正在录制：`添加高光`
- 已收录但未录制：不显示按钮

高光是独立书签，可以在 BLREC 的上传任务页面进入剪辑；重复点击会保存多个高光点。

## 安装发布包

1. 从 BLREC Release 下载 `blrec-browser-extension-<版本>.zip` 并解压。
2. 打开 `chrome://extensions`，启用右上角“开发者模式”。
3. 点击“加载已解压的扩展程序”，选择解压目录；该目录下应直接包含 `manifest.json`。
4. 打开扩展的“选项”，填写 `http://<NAS-IP>:2233` 和 BLREC 管理员用户名，然后点击“连接”。
5. 打开 `https://live.bilibili.com/<房间号>`。

连接时不需要管理员密码或 API Key。首次连接会请求访问所填 BLREC 地址的权限；配对成功后，受限令牌保存在浏览器本地。

## 撤销授权

进入 BLREC 的“设置 → 浏览器插件授权”，找到对应授权并点击“撤销”。撤销后插件只能重新连接，不能继续查询房间或添加高光。

用户名配对只适合个人设备和可信内网。若 BLREC 可从公网访问，请先在入口配置 HTTPS、访问控制或 VPN。

## 开发构建

```bash
cd browser-extension
npm ci
npm test
npm run typecheck
npm run build
```

然后在 `chrome://extensions` 加载 `browser-extension/dist`。构建目录不会提交到 Git。
