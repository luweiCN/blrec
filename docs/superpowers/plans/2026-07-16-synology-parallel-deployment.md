# Synology Parallel Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `192.168.50.24` 上部署独立的 `blrec-next`，并保持旧 BLREC 与 Java 上传容器不变。

**Architecture:** 使用固定版本的公开 GHCR 镜像和主机网络，在 `/volume1/docker/blrec-next` 下隔离工作文件与所有运行数据。Compose 内直接保存初始化变量，随机安全码仅在 NAS 内生成和保存；所有远程操作串行执行并在每个变更点验证旧实例未改变。

**Tech Stack:** Synology DSM 7.3.2、Container Manager 24.0.2、Docker Compose v2.20.1、SSH、GHCR。

## Global Constraints

- 项目名和容器名均为 `blrec-next`，镜像固定为 `ghcr.io/luweicn/blrec:3.0.0-beta.1`。
- 管理用户名固定为 `luwei`，初始化安全码使用 `openssl rand -hex 32` 生成且不得输出。
- 工作文件位于 `/volume1/docker/blrec-next/workspace`；数据目录为并列的 `config`、`log`、`rec`。
- 新实例使用 `network_mode: host` 和端口 `2234`，不得声明 `ports`。
- 不停止、重建、重命名或修改 `blrec`、`biliupforjava-blrec` 及 `/volume1/docker/blrec`。
- SSH 密码只从 `SYNO_ADMIN_PASSWORD` 读取，不写入文件、命令参数或日志。

---

### Task 1: 创建隔离目录与 NAS 本地 Compose

**Files:**
- Create on NAS: `/volume1/docker/blrec-next/workspace/compose.yml`
- Create on NAS: `/volume1/docker/blrec-next/config/credential.key`
- Create on NAS: `/volume1/docker/blrec-next/log/`
- Create on NAS: `/volume1/docker/blrec-next/rec/`

**Interfaces:**
- Consumes: NAS SSH credentials from `SYNO_ADMIN_USERNAME` and `SYNO_ADMIN_PASSWORD`.
- Produces: 一个权限受限、无需 `.env` 的 Compose 工作目录。

- [ ] **Step 1: 运行无修改前置断言**

以 `sudo` 在 NAS 上执行：

```sh
set -eu
d=/var/packages/ContainerManager/target/usr/bin/docker
base=/volume1/docker/blrec-next
test -x "$d"
test -x /usr/local/bin/docker-compose
test ! -e "$base"
test "$("$d" inspect --format '{{.State.Running}}' blrec)" = true
test -d /volume1/docker/blrec
! ss -ltn 2>/dev/null | grep -Eq ':(2234)[[:space:]]'
```

Expected: exit 0；旧 `blrec` 正在运行、目标目录不存在且 `2234` 未监听。

- [ ] **Step 2: 创建目录与凭据加密密钥**

```sh
set -eu
base=/volume1/docker/blrec-next
install -d -m 750 -o luwei -g users "$base"
install -d -m 700 -o luwei -g users "$base/workspace" "$base/config"
install -d -m 750 -o luwei -g users "$base/log" "$base/rec"
umask 077
openssl rand -base64 32 > "$base/config/credential.key"
chown luwei:users "$base/config/credential.key"
chmod 600 "$base/config/credential.key"
test -s "$base/config/credential.key"
```

Expected: 四个目录存在，`credential.key` 非空且模式为 `0600`。

- [ ] **Step 3: 在同一受保护 shell 中生成安全码并写入 Compose**

在 NAS 的 root shell 内执行下列完整命令。安全码只进入 shell 变量和文件，
不会成为进程参数或终端输出：

```sh
set -eu
compose=/volume1/docker/blrec-next/workspace/compose.yml
api_key="$(openssl rand -hex 32)"
umask 077
{
  printf '%s\n' \
    'name: blrec-next' \
    '' \
    'services:' \
    '  blrec:' \
    '    image: ghcr.io/luweicn/blrec:3.0.0-beta.1' \
    '    pull_policy: always' \
    '    container_name: blrec-next' \
    '    network_mode: host' \
    '    restart: unless-stopped' \
    '    stop_grace_period: 2m' \
    "    command: ['--port', '2234']" \
    '    environment:' \
    '      TZ: Asia/Shanghai' \
    '      BLREC_ADMIN_USERNAME: "luwei"'
  printf '      BLREC_API_KEY: "%s"\n' "$api_key"
  printf '%s\n' \
    '      BLREC_FORWARDED_ALLOW_IPS: "127.0.0.1"' \
    '      BLREC_CREDENTIAL_KEY_FILE: /cfg/credential.key' \
    '    volumes:' \
    '      - /volume1/docker/blrec-next/config:/cfg' \
    '      - /volume1/docker/blrec-next/log:/log' \
    '      - /volume1/docker/blrec-next/rec:/rec' \
    '    healthcheck:' \
    "      test: ['CMD', 'python', '-c', \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:2234/api/v1/auth/status', timeout=3).read()\"]" \
    '      interval: 30s' \
    '      timeout: 5s' \
    '      start_period: 30s' \
    '      retries: 3'
} > "$compose"
unset api_key
chown luwei:users "$compose"
chmod 600 "$compose"
test -s "$compose"
```

Expected: Compose 文件非空、模式为 `0600`，命令及终端输出均不包含安全码。

### Task 2: 校验、拉取并启动 Compose 项目

**Files:**
- Read on NAS: `/volume1/docker/blrec-next/workspace/compose.yml`

**Interfaces:**
- Consumes: Task 1 生成的 Compose 和凭据密钥。
- Produces: 由 Compose 管理、带 `com.docker.compose.project=blrec-next` 标签的运行容器。

- [ ] **Step 1: 校验展开后的配置契约**

```sh
set -eu
cd /volume1/docker/blrec-next/workspace
c=/usr/local/bin/docker-compose
resolved="$($c -p blrec-next -f compose.yml config)"
test "$(printf '%s\n' "$resolved" | grep -c 'image: ghcr.io/luweicn/blrec:3.0.0-beta.1')" -eq 1
printf '%s\n' "$resolved" | grep -q 'network_mode: host'
printf '%s\n' "$resolved" | grep -q '/volume1/docker/blrec-next/config:/cfg'
printf '%s\n' "$resolved" | grep -q '/volume1/docker/blrec-next/log:/log'
printf '%s\n' "$resolved" | grep -q '/volume1/docker/blrec-next/rec:/rec'
! printf '%s\n' "$resolved" | grep -q '/volume1/docker/blrec/'
unset resolved
```

Expected: exit 0；只解析一个固定镜像，且没有引用旧目录。不得输出 `config` 的完整结果，以免显示安全码。

- [ ] **Step 2: 记录旧实例身份并启动新项目**

```sh
set -eu
d=/var/packages/ContainerManager/target/usr/bin/docker
c=/usr/local/bin/docker-compose
cd /volume1/docker/blrec-next/workspace
old_id="$($d inspect --format '{{.Id}}' blrec)"
old_started="$($d inspect --format '{{.State.StartedAt}}' blrec)"
$c -p blrec-next -f compose.yml pull
$c -p blrec-next -f compose.yml up -d
test "$($d inspect --format '{{.Id}}' blrec)" = "$old_id"
test "$($d inspect --format '{{.State.StartedAt}}' blrec)" = "$old_started"
```

Expected: `blrec-next` 创建并启动；旧 `blrec` 的容器 ID 和启动时间完全不变。

- [ ] **Step 3: 验证 Compose 项目标识**

```sh
set -eu
d=/var/packages/ContainerManager/target/usr/bin/docker
test "$($d inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' blrec-next)" = blrec-next
test "$($d inspect --format '{{.HostConfig.NetworkMode}}' blrec-next)" = host
```

Expected: 项目标识为 `blrec-next`，网络为 `host`。随后在 DSM Container Manager 的“项目”页确认项目可见；若尚未列出，使用“创建项目”选择 `/volume1/docker/blrec-next/workspace` 和现有 `compose.yml`，项目名仍为 `blrec-next`，不得创建第二个容器。

### Task 3: 运行验收与重启验证

**Files:**
- Inspect on NAS: `/volume1/docker/blrec-next/config/`
- Inspect on NAS: `/volume1/docker/blrec-next/log/`
- Inspect on NAS: `/volume1/docker/blrec-next/rec/`

**Interfaces:**
- Consumes: 运行中的 `blrec-next`。
- Produces: 可从 DSM 管理、可重启且不影响旧实例的已验证测试部署。

- [ ] **Step 1: 等待健康接口**

```sh
set -eu
response=
for attempt in $(seq 1 90); do
  response="$(curl -fsS http://127.0.0.1:2234/api/v1/auth/status 2>/dev/null || true)"
  if printf '%s' "$response" | grep -q '"setupRequired":true'; then
    break
  fi
  sleep 1
done
printf '%s' "$response" | grep -q '"setupRequired":true'
unset response
```

Expected: 90 秒内返回首次初始化状态；不输出响应正文。

- [ ] **Step 2: 验证挂载、版本和旧实例不变**

```sh
set -eu
d=/var/packages/ContainerManager/target/usr/bin/docker
test "$($d inspect --format '{{.State.Health.Status}}' blrec-next)" = healthy
test "$($d inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' blrec-next)" = 3.0.0-beta.1
$d inspect --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' blrec-next \
  | grep -q '/volume1/docker/blrec-next/config -> /cfg'
$d inspect --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' blrec-next \
  | grep -q '/volume1/docker/blrec-next/log -> /log'
$d inspect --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' blrec-next \
  | grep -q '/volume1/docker/blrec-next/rec -> /rec'
test "$($d inspect --format '{{.State.Running}}' blrec)" = true
```

Expected: 新容器健康、版本正确、三个挂载均为新目录，旧容器仍在运行。

- [ ] **Step 3: 执行一次安全重启测试**

```sh
set -eu
d=/var/packages/ContainerManager/target/usr/bin/docker
$d restart blrec-next >/dev/null
for attempt in $(seq 1 90); do
  test "$($d inspect --format '{{.State.Health.Status}}' blrec-next)" = healthy && break
  sleep 1
done
test "$($d inspect --format '{{.State.Health.Status}}' blrec-next)" = healthy
curl -fsS http://127.0.0.1:2234/api/v1/auth/status >/dev/null
test "$($d inspect --format '{{.State.Running}}' blrec)" = true
```

Expected: 新容器重启后恢复健康，`2234` 可访问，旧 `blrec` 未受影响。

- [ ] **Step 4: 浏览器与 Container Manager 验收**

访问 `http://192.168.50.24:2234`，确认显示管理员初始化页面；在 Container Manager 中确认 `blrec-next` 位于“项目”页，状态为运行中。初始化安全码只从 NAS 本地 `workspace/compose.yml` 复制，不在聊天或日志中展示。
