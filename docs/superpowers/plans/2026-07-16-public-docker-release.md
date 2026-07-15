# Public Docker Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 发布可在群晖直接安装的 `v3.0.0-beta.1` 公开 GHCR 单容器镜像，并建立测试、构建、升级与回滚链路。

**Architecture:** Dockerfile 通过 Node、Python wheel、Python runtime 三阶段生成一个运行镜像；Git 标签先调用可复用测试工作流，全部通过后才用 Buildx 发布 AMD64/ARM64 manifest。群晖 Compose 固定版本拉取该镜像，持久化配置、日志与录像目录。

**Tech Stack:** Python 3.8～3.11、Angular 15、Node 18、Docker Buildx、GitHub Actions、GHCR、Docker Compose、Synology Container Manager。

## Global Constraints

- 首版 Git 标签为 `v3.0.0-beta.1`，程序和镜像版本为 `3.0.0-beta.1`。
- 镜像固定为 `ghcr.io/luweicn/blrec`，发布 `3.0.0-beta.1` 与 `beta`，不得发布 `latest`。
- 最终只运行一个 BLREC 镜像和一个容器，同时支持 `linux/amd64`、`linux/arm64`。
- Compose 保持 `network_mode: host`、`/cfg`、`/log`、`/rec`，不得添加端口映射。
- 不向仓库、Release、镜像层或日志写入真实管理员密码、API Key、Cookie、Token 或凭据加密密钥。
- 删除旧 Docker Hub/旧 GHCR 自动发布；PyPI 与 Windows portable 只保留手动触发。
- 发布前必须通过后端、前端、Docker 冒烟、Compose 解析和凭据扫描。
- 不使用 worktree；公共标签和镜像不得在本地验证完成前推送。

---

### Task 1: 版本与发布说明契约

**Files:**
- Create: `tests/release/test_version_metadata.py`
- Modify: `src/blrec/__init__.py`
- Create: `docs/releases/3.0.0-beta.1.md`

**Interfaces:**
- Produces: `blrec.__version__ == '3.0.0-beta.1'`；发布工作流使用的固定说明文件。

- [ ] **Step 1: 写版本失败测试**

```python
from pathlib import Path

import blrec


ROOT = Path(__file__).resolve().parents[2]


def test_release_version_matches_first_public_beta() -> None:
    assert blrec.__version__ == '3.0.0-beta.1'


def test_release_notes_describe_beta_scope_without_claiming_validation() -> None:
    notes = (ROOT / 'docs/releases/3.0.0-beta.1.md').read_text(encoding='utf8')
    assert '# BLREC 3.0.0-beta.1' in notes
    assert '公开测试版' in notes
    assert '尚未完成真实环境验收' in notes
    assert 'latest' not in notes.lower()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest -q tests/release/test_version_metadata.py`

Expected: FAIL，当前版本仍为 `2.0.0-beta.5`，且发布说明不存在。

- [ ] **Step 3: 更新程序版本并写发布说明**

`src/blrec/__init__.py`：

```python
__version__ = '3.0.0-beta.1'
```

`docs/releases/3.0.0-beta.1.md` 必须包含以下正文：

```markdown
# BLREC 3.0.0-beta.1

这是内置录制、投稿账号、上传任务、弹幕回灌、容量管理、多网络和通知功能的首个公开测试版。

本版本已通过自动化测试和 Docker 冒烟，但尚未完成真实环境验收。生产部署前请备份 `/cfg`，先以 3～5 个房间灰度运行。

## 主要变化

- 批量查询直播状态并按房间录制视频与弹幕。
- 内置扫码账号、上传投稿、审核跟踪、自动评论和弹幕回灌。
- 支持合集、自定义封面、定时发布及录像保留策略。
- 支持群晖多网卡分工、故障切换和运行异常通知。
- 增加单管理员会话认证、初始化恢复和登录限速。

## 安装

下载本 Release 附带的 `compose.synology.yml` 与 `synology.env.example`，按照仓库中的群晖部署文档创建目录、密钥并启动项目。

## 升级与回滚

升级前备份整个 `/cfg`。如果需要回滚，必须同时恢复升级前 `/cfg` 与上一固定镜像版本；不要只回退镜像。
```

- [ ] **Step 4: 运行版本测试**

Run: `.venv/bin/python -m pytest -q tests/release/test_version_metadata.py`

Expected: `2 passed`。

- [ ] **Step 5: 提交版本元数据**

```bash
git add src/blrec/__init__.py docs/releases/3.0.0-beta.1.md tests/release/test_version_metadata.py
git commit -m "chore: prepare 3.0.0 beta release"
```

### Task 2: 单镜像多阶段 Docker 构建与冒烟

**Files:**
- Create: `tests/release/test_docker_image_contract.py`
- Modify: `Dockerfile`
- Modify: `.dockerignore`
- Create: `scripts/docker-smoke.sh`

**Interfaces:**
- Consumes: Task 1 的版本字符串和现有 `MANIFEST.in` package data。
- Produces: `blrec:release-test` 单运行镜像；`scripts/docker-smoke.sh <image>`。

- [ ] **Step 1: 写 Docker 契约失败测试**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_builds_frontend_wheel_and_runtime_separately() -> None:
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf8')
    assert 'AS webapp-builder' in dockerfile
    assert 'AS wheel-builder' in dockerfile
    assert 'AS runtime' in dockerfile
    assert 'npm ci' in dockerfile
    assert 'npm run build' in dockerfile
    assert 'pip3 install --no-cache-dir -e .' not in dockerfile
    assert 'HEALTHCHECK' in dockerfile
    assert '/api/v1/auth/status' in dockerfile


def test_docker_context_excludes_local_and_generated_state() -> None:
    ignored = (ROOT / '.dockerignore').read_text(encoding='utf8')
    for value in ('.git', '.venv', 'webapp/node_modules', 'src/blrec/data/webapp'):
        assert value in ignored


def test_smoke_script_uses_ephemeral_credentials_and_cleans_up() -> None:
    script = (ROOT / 'scripts/docker-smoke.sh').read_text(encoding='utf8')
    assert 'mktemp -d' in script
    assert 'trap cleanup EXIT' in script
    assert 'BLREC_CREDENTIAL_KEY_FILE=/cfg/credential.key' in script
    assert '/api/v1/auth/status' in script
```

- [ ] **Step 2: 运行测试确认旧 Dockerfile 不符合发布契约**

Run: `.venv/bin/python -m pytest -q tests/release/test_docker_image_contract.py`

Expected: FAIL，缺少三个 stage、健康检查和冒烟脚本。

- [ ] **Step 3: 改为三个构建阶段和一个运行镜像**

`Dockerfile` 使用以下完整结构；OCI 参数放在最终 stage，前端产物在 wheel 构建前覆盖旧产物：

```dockerfile
# syntax=docker/dockerfile:1

FROM --platform=$BUILDPLATFORM node:18-bookworm-slim AS webapp-builder
WORKDIR /build
COPY webapp/package.json webapp/package-lock.json ./webapp/
RUN cd webapp && npm ci
COPY webapp ./webapp
RUN cd webapp && npm run build

FROM python:3.11-slim-bookworm AS wheel-builder
WORKDIR /build
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential python3-dev && \
    rm -rf /var/lib/apt/lists/*
COPY pyproject.toml setup.py setup.cfg MANIFEST.in README.md LICENSE ./
COPY src ./src
COPY --from=webapp-builder /build/src/blrec/data/webapp ./src/blrec/data/webapp
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.11-slim-bookworm AS runtime
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/luweiCN/blrec" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.description="Bilibili live recording and publishing service"
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*
COPY --from=wheel-builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels blrec && \
    rm -rf /wheels
ENV BLREC_DEFAULT_SETTINGS_FILE=/cfg/settings.toml \
    BLREC_DEFAULT_LOG_DIR=/log \
    BLREC_DEFAULT_OUT_DIR=/rec \
    TZ=Asia/Shanghai
VOLUME ["/cfg", "/log", "/rec"]
EXPOSE 2233
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:2233/api/v1/auth/status', timeout=3).read()"]
ENTRYPOINT ["blrec", "--host", "0.0.0.0", "--no-progress"]
CMD []
```

`.dockerignore` 至少包含：

```text
.git
.venv
**/__pycache__
**/.mypy_cache
**/.pytest_cache
webapp/node_modules
webapp/.angular
src/blrec/data/webapp
dist
build
*.log
```

- [ ] **Step 4: 创建可重复冒烟脚本**

`scripts/docker-smoke.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

image="${1:?usage: scripts/docker-smoke.sh IMAGE}"
root="$(mktemp -d)"
container="blrec-smoke-${RANDOM}-${RANDOM}"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf "$root"
}
trap cleanup EXIT

mkdir -p "$root/cfg" "$root/log" "$root/rec"
openssl rand -base64 32 > "$root/cfg/credential.key"
chmod 600 "$root/cfg/credential.key"

docker run -d --name "$container" \
  -p 127.0.0.1::2233 \
  -e BLREC_ADMIN_USERNAME=admin \
  -e BLREC_API_KEY=smoke-test-initialization-key \
  -e BLREC_CREDENTIAL_KEY_FILE=/cfg/credential.key \
  -v "$root/cfg:/cfg" \
  -v "$root/log:/log" \
  -v "$root/rec:/rec" \
  "$image" >/dev/null

port="$(docker port "$container" 2233/tcp | head -1 | awk -F: '{print $NF}')"
for _ in $(seq 1 90); do
  response="$(curl -fsS "http://127.0.0.1:${port}/api/v1/auth/status" 2>/dev/null || true)"
  if [[ "$response" == *'"setupRequired":true'* ]]; then
    docker inspect --format '{{.State.Status}}' "$container" | grep -qx running
    exit 0
  fi
  sleep 1
done

docker logs "$container"
exit 1
```

Run: `chmod +x scripts/docker-smoke.sh`

- [ ] **Step 5: 验证契约、构建和启动**

Run: `.venv/bin/python -m pytest -q tests/release/test_docker_image_contract.py`

Expected: `3 passed`。

Run: `docker build --build-arg VERSION=3.0.0-beta.1 --build-arg REVISION="$(git rev-parse HEAD)" -t blrec:release-test .`

Expected: image build succeeds。

Run: `scripts/docker-smoke.sh blrec:release-test`

Expected: exit 0；临时容器和目录均被清理。

- [ ] **Step 6: 提交 Docker 构建改造**

```bash
git add Dockerfile .dockerignore scripts/docker-smoke.sh tests/release/test_docker_image_contract.py
git commit -m "build: create production Docker image"
```

### Task 3: 群晖 Compose、环境示例和回滚说明

**Files:**
- Create: `tests/release/test_synology_release_contract.py`
- Modify: `compose.synology.yml`
- Create: `synology.env.example`
- Modify: `docs/operations/synology-multi-network.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 2 的单镜像端口、volume 和环境变量。
- Produces: 可由 Container Manager 项目或 `docker compose` 使用的单服务配置。

- [ ] **Step 1: 写 Compose 发布契约失败测试**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_synology_compose_pulls_one_pinned_public_image() -> None:
    compose = (ROOT / 'compose.synology.yml').read_text(encoding='utf8')
    assert 'build:' not in compose
    assert 'ghcr.io/luweicn/blrec:${BLREC_IMAGE_TAG:-3.0.0-beta.1}' in compose
    assert 'network_mode: host' in compose
    assert 'ports:' not in compose
    assert 'stop_grace_period: 2m' in compose
    for path in ('/cfg', '/log', '/rec'):
        assert path in compose


def test_environment_example_contains_no_credential() -> None:
    example = (ROOT / 'synology.env.example').read_text(encoding='utf8')
    assert 'BLREC_IMAGE_TAG=3.0.0-beta.1' in example
    assert 'BLREC_ADMIN_USERNAME=admin' in example
    assert 'BLREC_API_KEY=\n' in example
    assert 'BLREC_CREDENTIAL_KEY=' not in example


def test_synology_documentation_has_install_upgrade_and_rollback() -> None:
    document = (ROOT / 'docs/operations/synology-multi-network.md').read_text(
        encoding='utf8'
    )
    for heading in ('## 首次安装', '## 升级', '## 回滚', '## 日志与验收'):
        assert heading in document
    assert 'openssl rand -hex 32' in document
    assert 'openssl rand -base64 32' in document
```

- [ ] **Step 2: 运行测试确认本地构建 Compose 不符合发布契约**

Run: `.venv/bin/python -m pytest -q tests/release/test_synology_release_contract.py`

Expected: FAIL，Compose 仍包含 `build` 和 `blrec-local:latest`。

- [ ] **Step 3: 改为固定公开镜像的单服务 Compose**

`compose.synology.yml`：

```yaml
services:
  blrec:
    image: ghcr.io/luweicn/blrec:${BLREC_IMAGE_TAG:-3.0.0-beta.1}
    pull_policy: always
    container_name: blrec
    network_mode: host
    restart: unless-stopped
    stop_grace_period: 2m
    environment:
      TZ: ${TZ:-Asia/Shanghai}
      BLREC_ADMIN_USERNAME: ${BLREC_ADMIN_USERNAME:?请设置管理员用户名}
      BLREC_API_KEY: ${BLREC_API_KEY:?请设置初始化安全码}
      BLREC_FORWARDED_ALLOW_IPS: ${BLREC_FORWARDED_ALLOW_IPS:-127.0.0.1}
      BLREC_CREDENTIAL_KEY_FILE: /cfg/credential.key
    volumes:
      - ${BLREC_CONFIG_DIR:-/volume1/docker/blrec/config}:/cfg
      - ${BLREC_LOG_DIR:-/volume1/docker/blrec/log}:/log
      - ${BLREC_RECORDING_DIR:-/volume1/video/blrec}:/rec
```

`synology.env.example`：

```dotenv
BLREC_IMAGE_TAG=3.0.0-beta.1
TZ=Asia/Shanghai
BLREC_ADMIN_USERNAME=admin
BLREC_API_KEY=
BLREC_FORWARDED_ALLOW_IPS=127.0.0.1
BLREC_CONFIG_DIR=/volume1/docker/blrec/config
BLREC_LOG_DIR=/volume1/docker/blrec/log
BLREC_RECORDING_DIR=/volume1/video/blrec
```

- [ ] **Step 4: 完成群晖安装、升级、回滚文档**

在 `docs/operations/synology-multi-network.md` 使用以下可执行命令，并明确 Container Manager 项目导入相同 Compose：

```bash
mkdir -p /volume1/docker/blrec/config /volume1/docker/blrec/log /volume1/video/blrec
openssl rand -base64 32 > /volume1/docker/blrec/config/credential.key
chmod 600 /volume1/docker/blrec/config/credential.key
openssl rand -hex 32
cp synology.env.example .env
docker compose --env-file .env -f compose.synology.yml pull
docker compose --env-file .env -f compose.synology.yml up -d
```

升级章节必须先备份 `/cfg` 对应宿主目录，再修改 `BLREC_IMAGE_TAG`；回滚章节必须同时恢复旧 `/cfg` 和旧镜像。README 的 Docker 快速入口链接到这份文档和公开镜像。

- [ ] **Step 5: 验证 Compose 和文档**

Run: `.venv/bin/python -m pytest -q tests/release/test_synology_release_contract.py`

Expected: `3 passed`。

Run:

```bash
BLREC_ADMIN_USERNAME=admin \
BLREC_API_KEY=compose-contract-only-key \
docker compose --env-file synology.env.example -f compose.synology.yml config >/dev/null
```

Expected: exit 0，输出只有一个 service 且没有 `ports`。

- [ ] **Step 6: 提交群晖发布配置**

```bash
git add compose.synology.yml synology.env.example README.md docs/operations/synology-multi-network.md tests/release/test_synology_release_contract.py
git commit -m "docs: add Synology container deployment"
```

### Task 4: 唯一的标签发布工作流

**Files:**
- Create: `tests/release/test_github_release_workflows.py`
- Modify: `.github/workflows/test.yml`
- Create: `.github/workflows/release.yml`
- Delete: `.github/workflows/docker-hub.yml`
- Delete: `.github/workflows/ghcr.yml`
- Modify: `.github/workflows/pypi.yml`
- Modify: `.github/workflows/portable.yml`

**Interfaces:**
- Consumes: Task 1 发布说明、Task 2 Dockerfile/冒烟脚本、Task 3 Release 附件。
- Produces: tag `v*.*.*` → tests → `ghcr.io/luweicn/blrec:<version>,beta` → GitHub Pre-release。

- [ ] **Step 1: 写工作流失败契约测试**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / '.github/workflows'


def test_test_workflow_is_reusable_and_covers_runtime_python() -> None:
    workflow = (WORKFLOWS / 'test.yml').read_text(encoding='utf8')
    assert 'workflow_call:' in workflow
    assert "'3.11'" in workflow
    assert 'scripts/docker-smoke.sh blrec:release-test' in workflow


def test_release_workflow_has_test_gate_and_exact_image_contract() -> None:
    workflow = (WORKFLOWS / 'release.yml').read_text(encoding='utf8')
    assert "tags: ['v*.*.*']" in workflow
    assert 'uses: ./.github/workflows/test.yml' in workflow
    assert 'needs: quality' in workflow
    assert 'packages: write' in workflow
    assert 'linux/amd64,linux/arm64' in workflow
    assert 'ghcr.io/luweicn/blrec' in workflow
    assert ':beta' in workflow
    assert ':latest' not in workflow
    assert 'gh release create' in workflow


def test_legacy_automatic_publishers_cannot_run_for_tag() -> None:
    assert not (WORKFLOWS / 'docker-hub.yml').exists()
    assert not (WORKFLOWS / 'ghcr.yml').exists()
    for name in ('pypi.yml', 'portable.yml'):
        workflow = (WORKFLOWS / name).read_text(encoding='utf8')
        assert 'workflow_dispatch:' in workflow
        assert 'tags:' not in workflow
```

- [ ] **Step 2: 运行测试确认旧工作流冲突**

Run: `.venv/bin/python -m pytest -q tests/release/test_github_release_workflows.py`

Expected: FAIL，缺少 `release.yml`，旧发布器仍响应标签。

- [ ] **Step 3: 使测试工作流可复用并增加容器冒烟**

`.github/workflows/test.yml` 的触发器改为：

```yaml
on:
  push:
  pull_request:
  workflow_call:
```

Python matrix 改为：

```yaml
python-version: ['3.8', '3.10', '3.11']
```

Docker job 构建命令改为：

```yaml
- name: Build Docker image
  run: docker build --build-arg VERSION=3.0.0-beta.1 --build-arg REVISION=${{ github.sha }} -t blrec:release-test .

- name: Smoke test Docker image
  run: scripts/docker-smoke.sh blrec:release-test
```

- [ ] **Step 4: 创建唯一 GHCR 标签发布工作流**

`.github/workflows/release.yml`：

```yaml
name: Publish Docker release

on:
  push:
    tags: ['v*.*.*']

permissions:
  contents: read

jobs:
  quality:
    uses: ./.github/workflows/test.yml

  publish:
    needs: quality
    runs-on: ubuntu-latest
    permissions:
      contents: write
      packages: write
    env:
      IMAGE: ghcr.io/luweicn/blrec
    steps:
      - name: Checkout
        uses: actions/checkout@v6

      - name: Validate version
        id: version
        shell: bash
        run: |
          version="${GITHUB_REF_NAME#v}"
          package_version="$(python -c "import sys; sys.path.insert(0, 'src'); import blrec; print(blrec.__version__)")"
          test "$version" = "$package_version"
          echo "value=$version" >> "$GITHUB_OUTPUT"

      - name: Set up QEMU
        uses: docker/setup-qemu-action@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v4

      - name: Log in to GHCR
        uses: docker/login-action@v4
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push image
        uses: docker/build-push-action@v7
        with:
          context: .
          file: Dockerfile
          platforms: linux/amd64,linux/arm64
          push: true
          build-args: |
            VERSION=${{ steps.version.outputs.value }}
            REVISION=${{ github.sha }}
          tags: |
            ${{ env.IMAGE }}:${{ steps.version.outputs.value }}
            ${{ env.IMAGE }}:beta
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Verify manifest platforms
        shell: bash
        run: |
          docker buildx imagetools inspect --raw "$IMAGE:${{ steps.version.outputs.value }}" > manifest.json
          python - <<'PY'
          import json
          manifest = json.load(open('manifest.json', encoding='utf8'))
          platforms = {
              (item.get('platform') or {}).get('os', '') + '/' +
              (item.get('platform') or {}).get('architecture', '')
              for item in manifest.get('manifests', [])
          }
          assert {'linux/amd64', 'linux/arm64'} <= platforms, platforms
          PY

      - name: Create GitHub pre-release
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh release create "$GITHUB_REF_NAME" \
            compose.synology.yml synology.env.example \
            --verify-tag \
            --prerelease \
            --title "BLREC ${{ steps.version.outputs.value }}" \
            --notes-file "docs/releases/${{ steps.version.outputs.value }}.md"
```

- [ ] **Step 5: 消除旧标签发布器**

删除 `.github/workflows/docker-hub.yml` 和 `.github/workflows/ghcr.yml`。将 `.github/workflows/pypi.yml` 与 `.github/workflows/portable.yml` 的 `on` 块精确改为：

```yaml
on:
  workflow_dispatch:
```

- [ ] **Step 6: 验证工作流契约与 YAML 格式**

Run: `.venv/bin/python -m pytest -q tests/release/test_github_release_workflows.py`

Expected: `3 passed`。

Run: `git diff --check -- .github tests/release`

Expected: exit 0。

- [ ] **Step 7: 提交发布自动化**

```bash
git add .github/workflows tests/release/test_github_release_workflows.py
git commit -m "ci: publish multi-architecture GHCR image"
```

### Task 5: 完整发布门禁与候选提交审计

**Files:**
- Modify generated assets: `src/blrec/data/webapp/`
- Review: all tracked and untracked release candidate files

**Interfaces:**
- Consumes: Tasks 1～4 全部产物。
- Produces: 无凭据、可构建、可安装的干净发布候选提交。

- [ ] **Step 1: 重新生成前端静态产物**

Run: `cd webapp && npm ci && npm run build`

Expected: exit 0；`src/blrec/data/webapp/index.html` 引用本次新 hash 资源。

- [ ] **Step 2: 运行完整后端门禁**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m black --check src tests
.venv/bin/python -m isort --check-only src tests
.venv/bin/python -m flake8 src tests
.venv/bin/python -m mypy src/blrec
```

Expected: 全部 exit 0。

- [ ] **Step 3: 运行完整前端门禁**

```bash
cd webapp
npm test -- --watch=false --browsers=ChromeHeadless
npm run build
```

Expected: 全部 exit 0；只允许已经记录的体积预算和 CommonJS 警告。

- [ ] **Step 4: 运行 Docker 与 Compose 门禁**

```bash
docker build --build-arg VERSION=3.0.0-beta.1 --build-arg REVISION="$(git rev-parse HEAD)" -t blrec:release-test .
scripts/docker-smoke.sh blrec:release-test
BLREC_ADMIN_USERNAME=admin BLREC_API_KEY=compose-contract-only-key \
  docker compose --env-file synology.env.example -f compose.synology.yml config >/dev/null
```

Expected: 全部 exit 0。

- [ ] **Step 5: 审计工作区和凭据**

Run: `git status --short`

逐项确认所有源代码、迁移、测试、Web 产物和发布文件属于 `v3.0.0-beta.1`；不得把 `.venv`、`node_modules`、本地数据库、录像、日志或真实设置加入版本库。

Run:

```bash
git diff --check
git grep -n -I -E 'SESSDATA=|bili_jct=|BEGIN (RSA |EC )?PRIVATE KEY' -- . ':!tests' || true
git grep -n -I -E 'access_token[=: ]' -- . ':!tests' || true
```

Expected: `git diff --check` exit 0；凭据扫描无真实值。

- [ ] **Step 6: 提交剩余发布候选功能**

只在逐文件审计后暂存属于当前产品版本的剩余改动：

```bash
git add README.md docs src tests webapp compose.synology.yml synology.env.example Dockerfile .dockerignore scripts setup.cfg pyproject.toml MANIFEST.in
git status --short
git diff --cached --check
git commit -m "feat: prepare integrated BLREC beta"
```

Expected: 提交成功；`AGENTS.md`、本地运行目录和任何不属于产品的文件不被意外加入。

### Task 6: 推送公开测试版并验证匿名拉取

**Files:**
- No file changes expected.

**Interfaces:**
- Consumes: Task 5 的干净发布候选提交和 GitHub 仓库写权限。
- Produces: GitHub tag/Pre-release 与公开 GHCR 多架构镜像。

- [ ] **Step 1: 确认候选提交位于默认分支历史**

```bash
git fetch origin
git status --short
git branch -vv
git log --oneline --decorate -12
```

Expected: 工作区干净；候选提交可安全合并到远端默认分支。若远端默认分支有新提交，先普通合并并重新运行 Task 5，不得强推。

- [ ] **Step 2: 合并并推送默认分支**

以远端默认分支实际名称为准；当前仓库预期为 `master`：

```bash
git checkout master
git merge --no-ff feature/batch-live-monitor
git push origin master
```

Expected: 无冲突、无 force push；默认分支 CI 通过。

- [ ] **Step 3: 创建不可变版本标签**

```bash
test "$(python -c "import sys; sys.path.insert(0, 'src'); import blrec; print(blrec.__version__)")" = "3.0.0-beta.1"
git tag -a v3.0.0-beta.1 -m "BLREC 3.0.0-beta.1"
git push origin v3.0.0-beta.1
```

Expected: tag push 成功并只触发新的 Docker Release 工作流和普通测试工作流；PyPI、Windows、Docker Hub 任务不运行。

- [ ] **Step 4: 监控发布工作流**

```bash
gh run list --workflow release.yml --limit 1
gh run watch "$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
```

Expected: workflow exit 0；不得在失败时手动推送部分标签。

- [ ] **Step 5: 验证 Release 与多架构镜像**

```bash
gh release view v3.0.0-beta.1
docker buildx imagetools inspect ghcr.io/luweicn/blrec:3.0.0-beta.1
docker buildx imagetools inspect ghcr.io/luweicn/blrec:beta
```

Expected: Release 为 Pre-release；两个镜像标签指向同一 manifest，包含 `linux/amd64` 与 `linux/arm64`。

- [ ] **Step 6: 将首次 GHCR 包改为 Public 并验证匿名访问**

在 GitHub `luweiCN` 账号的 Packages → `blrec` → Package settings → Change visibility 中选择 Public。完成后退出 GHCR 登录状态或使用未登录环境执行：

```bash
docker logout ghcr.io || true
docker buildx imagetools inspect ghcr.io/luweicn/blrec:3.0.0-beta.1
```

Expected: 无凭据也能读取 manifest。若仍返回 unauthorized，不得宣称公开发布完成。

- [ ] **Step 7: 记录发布结果**

在发布交接中记录 GitHub Release 链接、固定镜像标签、manifest digest、匿名拉取结果、群晖安装入口，以及仍待真实环境验证的项目；不得把自动化测试描述为线上验收。
