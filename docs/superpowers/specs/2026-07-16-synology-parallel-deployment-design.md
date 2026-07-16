# 群晖并行部署设计

## 目标与边界

在 `192.168.50.24` 上新增独立的 `blrec-next` 项目，用于验证
`3.0.0-beta.1`。现有 `blrec`、`biliupforjava-blrec` 及其目录保持运行且
不作任何修改；本次不迁移旧配置、数据库或录像。

## 项目与目录

Container Manager 项目名和容器名均为 `blrec-next`。宿主机目录固定为：

```text
/volume1/docker/blrec-next/
├── compose/
│   └── compose.yml
├── config/
├── log/
└── rec/
```

`compose/` 只保存 Compose 及部署相关文件。三个数据目录分别映射为
`/cfg`、`/log` 和 `/rec`，不会挂载或引用旧目录
`/volume1/docker/blrec`。

## 容器配置

- 镜像固定为 `ghcr.io/luweicn/blrec:3.0.0-beta.1`。
- 使用 `network_mode: host`，监听端口改为 `2234`，健康检查同步访问
  `127.0.0.1:2234`，不声明端口映射。
- 管理用户名固定为 `luwei`。
- 初始化安全码在部署时生成 32 字节随机值，直接写入 NAS 本地
  `compose.yml`，不写入仓库、命令输出或会话日志。
- Compose 文件及目录只允许 NAS 管理员读取。凭据加密密钥继续保存为
  `/cfg/credential.key`。

## 部署与验证

通过 SSH 创建目录和 Compose 文件，先执行配置校验与镜像拉取，再将同一
工作目录注册为 Container Manager 的 `blrec-next` 项目并启动。验证内容包括：

1. 旧容器状态及挂载保持不变；
2. 新容器健康且重启策略为 `unless-stopped`；
3. `http://192.168.50.24:2234` 可访问；
4. 配置、日志和录像只写入 `blrec-next` 对应目录；
5. Container Manager 的“项目”页面能够管理该实例。

如果启动失败，只停止并移除 `blrec-next` 项目；保留新目录用于排查，不清理
旧容器或旧录像。
