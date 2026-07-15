# 群晖双网络部署

此部署使用主机网络模式，让容器直接看到群晖的物理网卡、内网 IP 和网关。管理页面会监听 `0.0.0.0:2233`，因此两个内网均可通过各自的 NAS IP 访问。应用只为连接绑定源 IP，不会修改群晖路由表。

本文中的 `compose.synology.yml` 只运行一个 blrec 服务，并从公开镜像 `ghcr.io/luweicn/blrec` 拉取固定版本。Container Manager“项目”导入的也是同一份 Compose；不要另建容器配置。

## 前置设置

1. 在 DSM“控制面板 → 网络 → 常规”中启用多网关，并确认两个内网均能访问 NAS。
2. 安装 Container Manager，或确认 SSH 终端中的 `docker compose version` 可正常执行。
3. 将 `compose.synology.yml` 和 `synology.env.example` 放在同一个工作目录。

不需要额外执行 `docker network create`，也不要在项目中配置 `ports`：`network_mode: host` 已直接使用群晖网络栈。

## 首次安装

先创建 `/cfg`、`/log` 和 `/rec` 对应的宿主目录，并在配置目录生成 Base64 编码的 32 字节凭据密钥：

```bash
mkdir -p /volume1/docker/blrec/config /volume1/docker/blrec/log /volume1/video/blrec
openssl rand -base64 32 > /volume1/docker/blrec/config/credential.key
chmod 600 /volume1/docker/blrec/config/credential.key
openssl rand -hex 32
cp synology.env.example .env
```

把最后一条 `openssl rand -hex 32` 的输出填入 `.env` 的 `BLREC_API_KEY`，并按需修改管理员用户名和三个宿主目录。API Key 只用于首次创建管理员和密码恢复，不要提交 `.env`；原始凭据密钥只保存在 `/cfg/credential.key`，不要写入环境变量。

通过 SSH 启动：

```bash
docker compose --env-file .env -f compose.synology.yml pull
docker compose --env-file .env -f compose.synology.yml up -d
```

也可以在 Container Manager 的“项目”中导入包含上述两个文件的目录。项目必须使用同一份 `compose.synology.yml`，并把 `.env` 中的值作为项目环境变量；不要改成 `build`，也不要添加端口映射。

`BLREC_FORWARDED_ALLOW_IPS` 默认只信任 `127.0.0.1`。只有通过群晖反向代理访问，并且确认代理连接来源地址后，才把该地址加入此变量；不要设置为 `*`，否则客户端可伪造来源 IP 绕过登录限速。

## 升级

先停止服务并备份 `/cfg` 对应的宿主目录及当前 `.env`，确认备份成功后再修改 `BLREC_IMAGE_TAG`。下面的命令以示例中的目录为准；如果修改过 `BLREC_CONFIG_DIR`，请同步替换 `config_dir`：

```bash
backup_id="$(date +%Y%m%d-%H%M%S)"
config_dir=/volume1/docker/blrec/config
docker compose --env-file .env -f compose.synology.yml stop
cp -a "$config_dir" "${config_dir}.backup-${backup_id}"
cp .env ".env.backup-${backup_id}"
echo "$backup_id"
```

记录终端输出的 `backup_id`，然后编辑 `.env`，把 `BLREC_IMAGE_TAG` 改成要升级的固定版本，再部署：

```bash
docker compose --env-file .env -f compose.synology.yml pull
docker compose --env-file .env -f compose.synology.yml up -d
```

Container Manager 的操作顺序相同：先停止项目并通过 File Station 备份配置目录，再备份项目环境、修改 `BLREC_IMAGE_TAG`，最后重新构建项目。不要只使用 `latest`；固定标签才能执行可重复的回滚。

## 回滚

回滚必须成对恢复升级前的 `/cfg` 和镜像标签。把下方 `backup_id` 改成升级时记录的值；如果自定义过配置目录，也要修改 `config_dir`。当前配置会另存为 `.failed-*`，便于排查：

```bash
backup_id=20260716-120000
config_dir=/volume1/docker/blrec/config
failed_id="$(date +%Y%m%d-%H%M%S)"
docker compose --env-file .env -f compose.synology.yml down
mv "$config_dir" "${config_dir}.failed-${failed_id}"
cp -a "${config_dir}.backup-${backup_id}" "$config_dir"
cp ".env.backup-${backup_id}" .env
docker compose --env-file .env -f compose.synology.yml pull
docker compose --env-file .env -f compose.synology.yml up -d
```

恢复的 `.env` 会重新选中旧 `BLREC_IMAGE_TAG`，恢复的配置目录同时带回旧设置、状态和 `credential.key`。在 Container Manager 中也必须同时还原配置目录和项目环境中的旧标签，然后重新构建项目。

## 日志与验收

先确认 Compose 只解析出一个服务，再检查容器状态和日志：

```bash
docker compose --env-file .env -f compose.synology.yml config --services
docker compose --env-file .env -f compose.synology.yml ps
docker compose --env-file .env -f compose.synology.yml logs --tail=200 blrec
```

启动后分别访问 `http://<NAS-网络1-IP>:2233` 和 `http://<NAS-网络2-IP>:2233`。首次访问时用 `.env` 中的管理员用户名和 API Key 创建管理员密码。进入“网络管理”，执行“检测全部线路”，确认两块网卡的网关、公网出口 IP 和连通性，再分别为房间状态轮询、弹幕 WebSocket、录像下载、视频上传及其他 B 站请求设置主线路与备用线路。

主线路连续出现两次传输失败后才会切换备用线路，冷却后再尝试主线路。已有 WebSocket 和录像连接不会被强制中断，会在自然重连时应用新设置。
