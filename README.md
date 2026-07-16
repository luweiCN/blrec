# Bilibili Live Streaming Recorder (blrec)

这是一个前后端分离的 B 站直播录制工具。前端使用了响应式设计，可适应不同的屏幕尺寸；后端是用 Python 写的，可以跨平台运行。

这个工具是自动化的，会自动完成直播的录制, 在出现未处理异常时会发送通知，空间不足能够自动回收空间，还有详细日志记录，因此可以长期无人值守运行在服务器上。

## 屏幕截图

![webapp](https://user-images.githubusercontent.com/33854576/128959800-451d03e7-c9f9-4732-ac90-97fdb6b88972.png)

![terminal](https://user-images.githubusercontent.com/33854576/128959819-70d72937-65da-4c15-b61c-d2da65bf42be.png)

## 功能

- 自动完成直播录制
- 支持浏览器插件一键收录直播间、标记高光并无损裁剪投稿
- 同步保存弹幕
- 自动修复时间戳问题：跳变、反跳等。
- 直播流参数改变自动分割文件，避免出现花屏等问题。
- 流中断自动拼接且支持 **无缝** 拼接，不会因网络中断而使录播文件片段化。
- `flv` 文件添加关键帧等元数据，使定位播放和拖进度条不会卡顿。
- 可选录制的画质
- 可自定义文件保存路径和文件名
- 支持按文件大小或时长分割文件
- 支持转换 `flv` 为 `mp4` 格式（需要安装 `ffmpeg`）
- 硬盘空间检测并支持空间不足自动删除旧录播文件。
- 事件通知（支持邮箱、`ServerChan`、`PushDeer`、`pushplus`、`Telegram`、`Bark` ）
- `Webhook`（可配合 `REST API` 实现录制控制，录制完成后压制、上传等自定义需求）

## 前提条件

    Python 3.8+
    ffmpeg、 ffprobe

## 安装

- 通过 pip 或者 pipx 安装

    `pip install blrec` 或者 `pipx install blrec`

    使用的一些库需要自己编译，Windows 没安装 C / C++ 编译器会安装出错，
    参考 [Python Can't install packages](https://stackoverflow.com/questions/64261546/python-cant-install-packages) 先安装好 [Microsoft C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)。

- 免安装绿色版

    支持 Windows 10+ 或 Windows Server 2016+，下载后解压运行 `run.bat` 或 `run.ps1` 。

    不是官方或最新系统可能需要安装系统更新或缺少的 `C` 或 `C++` 运行时库

    下载

    - Releases: https://github.com/acgnhiki/blrec/releases
    - 网盘: https://gooyie.lanzoui.com/b01om2zte  密码: 2233

## 更新

- 通过 pip 或者 pipx 安装的用以下方式更新

    `pip install blrec --upgrade` 或者 `pipx upgrade blrec`

- 免安装绿色版

    - 下载并解压新版本
    - 确保旧版本已经关闭退出以避免之后出现端口冲突
    - 把旧版本的设置文件 `settings.toml` 复制并覆盖新版本的设置文件
    - 运行新版本的 `run.bat`

## 卸载

- 通过 pip 或者 pipx 安装的用以下方式卸载

    `pip uninstall blrec` 或者 `pipx uninstall blrec`

- 免安装绿色版

    删除解压后的文件夹


## Docker

Docker 测试版使用公开镜像 [`ghcr.io/luweicn/blrec:3.0.0-beta.5`](https://github.com/luweicn/blrec/pkgs/container/blrec)。请仅使用仓库中的 `compose.synology.yml` 部署；首次安装、凭据密钥初始化、升级与回滚步骤见 [群晖双网络部署](docs/operations/synology-multi-network.md)。

## 使用方法

BLREC 浏览器工具的安装、连接和撤销授权步骤见 [插件说明](browser-extension/README.md)。

### 命令行参数用法

`blrec --help`

### 默认参数运行

在命令行终端里执行 `blrec` ，然后浏览器访问 `http://localhost:2233`。

默认设置文件位置：`~/.blrec/settings.toml`

默认日志文件目录： `~/.blrec/logs`

默认录播文件目录: `.`

### 指定设置文件和录播与日志保存位置

`blrec -c path/to/settings.toml -o path/to/records --log-dir path/to/logs`

如果指定的设置文件不存在会自动创建

**命令行参数会覆盖掉设置文件的对应的设置**

### 绑定主机和端口

默认为本地运行，主机和端口绑定为： `localhost:2233`

需要外网访问，把主机绑定到 `0.0.0.0`，端口绑定则按照自己的情况修改。

例如：`blrec --host 0.0.0.0 --port 8000`

### 网络安全

首次打开页面时需要输入部署时配置的管理员用户名、初始化安全码（API Key）并创建管理员密码。此后日常登录只使用用户名和密码；网页通过 HttpOnly 会话 Cookie 和 CSRF 校验认证，不会保存或继续发送 API Key。

通过不可信网络访问时，应在反向代理或程序入口配置 **HTTPS**：

例如：`blrec --key-file path/to/key-file --cert-file path/to/cert-file`

`BLREC_ADMIN_USERNAME` 指定唯一管理员用户名，区分大小写。`api key` 仅用于首次创建管理员或忘记密码后的恢复验证；设置完成后，普通页面和 API 请求不再携带它。未配置 API Key 时，只允许从本机回环地址完成首次设置和恢复。

### 关于 api-key

api key 可以使用数字和字母，长度限制为最短 8 最长 80。

管理员登录与初始化/恢复失败会分别按客户端限速，五分钟内失败五次会暂停十五分钟；两个计数互不影响。修改或恢复密码会撤销全部已有会话。API Key 不应与管理员密码相同，也不应提交到仓库。

## 作为 ASGI 应用运行

    uvicorn blrec.web:app

或者

    hypercorn blrec.web:app

作为 ASGI 应用运行，参数通过环境变量指定。

- `BLREC_CONFIG` 指定设置文件
- `BLREC_OUT_DIR` 指定录播存放位置
- `BLREC_LOG_DIR` 指定日志存放位置
- `BLREC_ADMIN_USERNAME` 指定管理员用户名（未设置时为 `admin`）
- `BLREC_API_KEY` 指定仅供初始化和密码恢复使用的安全码
- `BLREC_FORWARDED_ALLOW_IPS` 指定可信反向代理地址（默认只信任 `127.0.0.1`）；没有反向代理时不要扩大范围

### bash

    BLREC_CONFIG=path/to/settings.toml BLREC_OUT_DIR=path/to/dir BLREC_ADMIN_USERNAME=owner BLREC_API_KEY=******** uvicorn blrec.web:app --host 0.0.0.0 --port 8000 --forwarded-allow-ips 127.0.0.1

### cmd

    set BLREC_CONFIG=D:\\path\\to\\config.toml & set BLREC_OUT_DIR=D:\\path\\to\\dir & set BLREC_ADMIN_USERNAME=owner & set BLREC_API_KEY=******** uvicorn blrec.web:app --host 0.0.0.0 --port 8000 --forwarded-allow-ips 127.0.0.1

## Webhook

程序在运行过程中会触发一些事件，如果是支持 `webhook` 的事件，就会给所设置的 `webhook` 网络地址发送 POST 请求。

关于支持的事件和 `POST` 请求所发送的数据，详见 wiki。

## REST API

后端 `web` 框架用的是 `FastApi` , 要查看自动生成的交互式 `API` 文档，访问 `http://localhost:2233/docs` （默认主机和端口绑定）。

## Progressive Web App（PWA）

前端其实是一个渐进式网络应用，可以通过地址栏右侧的图标安装，然后像原生应用一样从桌面启动运行。

**注意：PWA 要在本地访问或者在 `https` 下才支持。**

---

## 开发

1. 克隆代码

    `git clone https://github.com/acgnhiki/blrec.git`

2. 进入项目目录

    `cd blrec`

3. 创建虚拟环境

    `python3 -m venv .venv`

4. 激活虚拟环境

    `source .venv/bin/activate`

5. 以可编辑方式安装

    `pip install -e .[dev]`

6. 修改代码

    ……

7. 运行 blrec

    `blrec`

8. 退出虚拟环境

    `deactivate`

---

## 常见问题

[FAQ](FAQ.md)

## 更新日志

[CHANGELOG](CHANGELOG.md)

---

## 其它相关工具或项目

| 名称 | 链接 | 简介 |
| --- | --- | --- |
| 录播姬 | [官网](https://rec.danmuji.org/) | 简单易用成熟稳定的 B 站直播录制工具 |
| rclone | [官网](https://rclone.org/) | 可以挂载网盘用于存放录播文件 |
| alist | [官网](https://alist-doc.nn.ci/) | 网盘文件浏览、播放 |
| filebrowser | [官网](https://filebrowser.org/) | 服务器文件管理 |
