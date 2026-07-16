# Network Routing, Realtime Observability, and Pilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 BLREC 中实现可用的多网卡自动发现与线路策略、单一 SSE 实时通道、应用级流量与上传进度、按网卡上传限速、投稿回读验证和可审计的群晖试运行。

**Architecture:** 网络层拆成“平台发现、策略选择、源地址 DNS、流量计量、上传限速”五个小组件，由现有 aiohttp/requests/UPOS 传输复用。后端以一个有界队列 SSE broker 推送变化，HTTP API 继续负责首屏快照和断线重同步；Angular 只维护一个 EventSource。数据库保存上传确认字节和投稿验证结果，使重启后状态可恢复。

**Tech Stack:** Python 3.8、FastAPI、aiohttp、requests/urllib3、dnspython、SQLite、Loguru、Angular 13、RxJS、ng-zorro、pytest、Jasmine/Karma、Docker/GHCR。

## Global Constraints

- 不使用 git worktree，不修改用户的未跟踪 `AGENTS.md`。
- Linux/群晖读取全部 IPv4 策略路由；`/proc/net/route` 只作为降级路径。
- 视频上传和所有带账号凭据的请求只允许固定线路；匿名房间状态和 HTTP 请求才允许轮换。
- 录像按直播场次粘住线路，弹幕按 WebSocket 连接粘住线路，状态轮询按批次分配线路。
- `docker0` 等无外网默认路由的网桥默认禁用；用户设置必须持久化。
- 每块网卡共享上传限速，`0` 表示不限速；下载永不限速。
- 浏览器只建立一条 `/api/v1/realtime` SSE；心跳 15 秒，慢订阅者触发 `resync`。
- 流量只统计 BLREC 应用有效载荷，不冒充 NAS 全机流量。
- 日志每天轮换，默认保留 60 天；Cookie、token、API Key、密码和完整请求体不得入日志。
- 新增行为先写失败测试，再写最小实现；每个任务完成后运行其聚焦测试。

---

### Task 1: 平台网卡发现与持久化设置

**Files:**
- Create: `src/blrec/networking/platform.py`
- Modify: `src/blrec/networking/manager.py`
- Modify: `src/blrec/setting/models.py`
- Modify: `src/blrec/web/routers/network.py`
- Modify: `setup.cfg`
- Modify: `docker/Dockerfile`
- Test: `tests/networking/test_network_platform.py`
- Test: `tests/networking/test_network_routing.py`
- Test: `tests/web/test_network_routes.py`

**Interfaces:**
- Produces: `discover_interfaces() -> Dict[str, NetworkInterface]`，其中 `NetworkInterface` 包含 `dns_servers`、`enabled`、`upload_limit_bps` 和 `kind`。
- Produces: `NetworkRouteSettings(mode: Literal['fixed', 'round_robin'], interface: Optional[str], failover_enabled: bool)`，Pydantic root validator 负责从旧 `primaryInterface/fallbackInterface` 迁移。
- Produces: `PATCH /api/v1/network/interfaces/{name}`，行内保存 `enabled` 或 `uploadLimitBps`。

- [ ] **Step 1: 写平台发现失败测试**

```python
def test_linux_policy_routes_supply_both_gateways(monkeypatch):
    monkeypatch.setattr(platform, '_run_ip_json', lambda *args: ROUTES_AND_RULES)
    interfaces = platform.discover_interfaces()
    assert interfaces['ovs_eth0'].gateway == '192.168.1.1'
    assert interfaces['ovs_eth1'].gateway == '192.168.50.1'

def test_bridge_without_external_route_defaults_disabled():
    assert discovered['docker0'].enabled is False
```

- [ ] **Step 2: 运行测试并确认因当前只读 `/proc/net/route` 而失败**

Run: `pytest -q tests/networking/test_network_platform.py tests/networking/test_network_routing.py`
Expected: FAIL，缺少 `platform` 模块、新字段和策略路由网关。

- [ ] **Step 3: 实现最小平台发现与设置迁移**

```python
class NetworkRouteSettings(BaseModel):
    mode: Literal['fixed', 'round_robin'] = 'fixed'
    interface: Optional[str] = None
    failover_enabled: bool = True

class NetworkInterfaceSettings(BaseModel):
    enabled: bool = True
    upload_limit_bps: Annotated[int, Field(ge=0)] = 0
```

Linux 首选 `ip -j -4 addr show`、`ip -j -4 route show table all` 和 `ip -j rule show`，从每个源地址命中的路由表找默认网关；命令不可用时回退现有 psutil 与 `/proc/net/route`。Docker 镜像安装 `iproute2`，依赖增加 `dnspython>=2.4,<2.7`。

- [ ] **Step 4: 增加 API 行内设置测试并实现接口**

```python
def test_patch_interface_persists_enable_and_limit(client):
    response = client.patch('/api/v1/network/interfaces/ovs_eth0', json={
        'enabled': False,
        'uploadLimitBps': 1048576,
    })
    assert response.status_code == 200
```

- [ ] **Step 5: 运行聚焦验证并提交**

Run: `pytest -q tests/networking/test_network_platform.py tests/networking/test_network_routing.py tests/web/test_network_routes.py`
Expected: PASS。

Commit: `feat: discover and configure network interfaces`

### Task 2: 源地址 DNS、固定/轮换与业务粘性

**Files:**
- Create: `src/blrec/networking/resolver.py`
- Create: `src/blrec/networking/affinity.py`
- Modify: `src/blrec/networking/manager.py`
- Modify: `src/blrec/networking/aiohttp_session.py`
- Modify: `src/blrec/networking/requests_session.py`
- Modify: `src/blrec/bili/live_status.py`
- Modify: `src/blrec/bili/danmaku_client.py`
- Modify: `src/blrec/task/live.py`
- Modify: `src/blrec/bili_upload/protocol.py`
- Test: `tests/networking/test_source_bound_dns.py`
- Test: `tests/networking/test_network_affinity.py`
- Test: `tests/bili/test_batch_status_client.py`
- Test: `tests/bili/test_danmaku_client.py`

**Interfaces:**
- Consumes: Task 1 的 `NetworkInterface`、`NetworkRouteSettings` 和启用状态。
- Produces: `NetworkRouteManager.select(purpose, affinity_key=None, anonymous=False) -> RouteSelection`。
- Produces: `SourceBoundResolver.resolve(host, port, family, selection) -> List[ResolvedAddress]`。
- Produces: `RouteLease`，直播场次和 WebSocket 重连可复用同一 `RouteSelection`。

- [ ] **Step 1: 写 DNS 与策略失败测试**

```python
async def test_dns_query_binds_interface_source_and_prefers_gateway():
    addresses = await resolver.resolve('api.bilibili.com', 443, AF_INET, selection)
    assert fake_dns.calls == [('192.168.1.1', '192.168.1.24')]
    assert addresses[0].host == 'real-bili-ip'

def test_round_robin_only_rotates_anonymous_requests():
    assert manager.select('room_status', anonymous=True).interface_name == 'eth0'
    assert manager.select('room_status', anonymous=True).interface_name == 'eth1'
    assert manager.select('bili_api', anonymous=False).interface_name == 'eth0'
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest -q tests/networking/test_source_bound_dns.py tests/networking/test_network_affinity.py`
Expected: FAIL，当前解析由系统 DNS 完成且没有轮换/粘性。

- [ ] **Step 3: 实现解析器和路由租约**

```python
@dataclass(frozen=True)
class RouteLease:
    key: str
    selection: RouteSelection

def select(self, purpose, *, affinity_key=None, anonymous=False):
    # fixed 使用配置网卡；round_robin 只在 anonymous=True 时推进稳定游标
    # affinity_key 命中缓存时保持选择；全部线路不可用时抛 NetworkUnavailable
```

DNS 候选顺序为接口网关、系统 nameserver；每次查询传入 `source=selection.source_address`。aiohttp 使用自定义 `AbstractResolver`；requests 的连接类使用解析出的 IP 建连但保留原始 host 进行 TLS SNI/证书校验。

- [ ] **Step 4: 接入批量状态、弹幕、录像和上传**

批量状态每批生成 affinity key；弹幕以 `room:{room_id}:danmaku` 持有连接租约；录像以 `session:{recording_session_id}` 持有整场租约；上传和账号 API 明确传 `anonymous=False`，禁止 round-robin。

- [ ] **Step 5: 验证故障语义**

Run: `pytest -q tests/networking tests/bili/test_batch_status_client.py tests/bili/test_danmaku_client.py tests/task/test_live_connection_controller.py`
Expected: PASS；连接错误连续两次才失效，HTTP 4xx/5xx 业务错误不计入线路健康，上传故障不换线。

Commit: `feat: route traffic with source-bound dns`

### Task 3: 应用流量计量、共享上传限速和可恢复进度

**Files:**
- Create: `src/blrec/networking/traffic.py`
- Create: `src/blrec/networking/rate_limit.py`
- Modify: `src/blrec/networking/aiohttp_session.py`
- Modify: `src/blrec/bili/danmaku_client.py`
- Modify: `src/blrec/bili_upload/upos.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `src/blrec/bili_upload/database.py`
- Create: `src/blrec/bili_upload/migrations/0017_initial.sql`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Test: `tests/networking/test_traffic_meter.py`
- Test: `tests/networking/test_upload_rate_limit.py`
- Test: `tests/bili_upload/test_journal.py`
- Test: `tests/bili_upload/test_upos.py`

**Interfaces:**
- Produces: `TrafficMeter.record(interface, purpose, direction, byte_count)` 与 `snapshot() -> Sequence[TrafficSnapshot]`。
- Produces: `SharedUploadLimiter.stream(interface, body) -> AsyncIterator[bytes]`，每网卡一个令牌桶。
- Produces: `UploadJobProgress.confirmed_bytes`、`total_bytes`、`percent`、`bytes_per_second`、`eta_seconds` 和 `current_part_index`。

- [ ] **Step 1: 写计量和限速失败测试**

```python
def test_meter_reports_only_recorded_application_bytes(fake_clock):
    meter.record('eth0', 'upload', 'up', 1024)
    fake_clock.advance(1)
    assert meter.snapshot()[0].upload_bps == 1024

async def test_concurrent_uploads_share_interface_limit(fake_clock):
    chunks = await drain_two_streams(limit_bps=1024)
    assert fake_clock.elapsed >= 2.0
    assert sum(map(len, chunks)) == 2048
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest -q tests/networking/test_traffic_meter.py tests/networking/test_upload_rate_limit.py tests/bili_upload/test_journal.py tests/bili_upload/test_upos.py`
Expected: FAIL，当前没有应用级计量、共享限速和字节进度字段。

- [ ] **Step 3: 实现计量器和小块令牌桶流**

```python
async def stream(self, interface: str, body: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(body), 64 * 1024):
        piece = body[offset:offset + 64 * 1024]
        await self.acquire(interface, len(piece))
        self._meter.record(interface, 'upload', 'up', len(piece))
        yield piece
```

UPOS 请求继续发送准确 `Content-Length`，但 `data` 改为上述异步流。并发任务共享相同 limiter；限速改动读取最新设置，下一块立即生效。

- [ ] **Step 4: 从 `upload_chunks` 聚合确认字节并持久化速度样本**

迁移 0017 添加投稿验证字段和必要的上传进度采样时间；查询以 `SUM(CASE WHEN state='confirmed' THEN size ELSE 0 END)` 计算重启后确认字节。速度仅由进程内相邻确认样本计算，重启后首个样本前返回 `null`。

- [ ] **Step 5: 运行聚焦验证并提交**

Run: `pytest -q tests/networking/test_traffic_meter.py tests/networking/test_upload_rate_limit.py tests/bili_upload/test_journal.py tests/bili_upload/test_upos.py tests/web/test_recording_sessions_routes.py`
Expected: PASS。

Commit: `feat: expose upload progress and traffic limits`

### Task 4: 单一 SSE 实时通道

**Files:**
- Create: `src/blrec/web/realtime.py`
- Create: `src/blrec/web/routers/realtime.py`
- Modify: `src/blrec/web/main.py`
- Modify: `src/blrec/application.py`
- Modify: `src/blrec/bili_upload/worker.py`
- Test: `tests/web/test_realtime_routes.py`
- Test: `tests/web/test_main_lifecycle.py`

**Interfaces:**
- Consumes: Task 3 的 `TrafficMeter.snapshot()` 和上传进度。
- Produces: `RealtimeBroker.publish(event_type: str, data: Mapping[str, object])`。
- Produces: `GET /api/v1/realtime`，事件类型为 `resync`、`tasks`、`upload_progress`、`network`、`heartbeat`。

- [ ] **Step 1: 写 SSE 响应、心跳和积压恢复失败测试**

```python
async def test_realtime_stream_starts_with_resync_and_required_headers(client):
    response = await client.get('/api/v1/realtime')
    assert response.headers['cache-control'] == 'no-cache'
    assert response.headers['x-accel-buffering'] == 'no'
    assert await response.read_event() == ('resync', {})

async def test_slow_subscriber_receives_resync_after_queue_overflow():
    assert await overflowed.read_event() == ('resync', {})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest -q tests/web/test_realtime_routes.py tests/web/test_main_lifecycle.py`
Expected: FAIL，路由和 broker 尚不存在。

- [ ] **Step 3: 实现有界 broker 与 FastAPI StreamingResponse**

```python
async def event_stream(subscription):
    yield encode('resync', {})
    while True:
        event = await asyncio.wait_for(subscription.get(), timeout=15)
        yield encode(event.type, event.data)
```

队列满时清空旧增量并只放一个 `resync`。响应 media type 为 `text/event-stream`，沿用现有 cookie 管理员认证。

- [ ] **Step 4: 以一秒采样器发布变化，不发布未变化快照**

应用启动时创建 sampler task，比较序列化后的任务和网络快照；上传分片确认和状态变更主动发布 `upload_progress`。应用停止时取消 sampler 并关闭订阅者。

- [ ] **Step 5: 运行聚焦验证并提交**

Run: `pytest -q tests/web/test_realtime_routes.py tests/web/test_main_lifecycle.py tests/web/test_auth_routes.py`
Expected: PASS。

Commit: `feat: stream realtime application events`

### Task 5: Angular 实时状态、网络管理和上传进度界面

**Files:**
- Create: `webapp/src/app/core/services/realtime.service.ts`
- Create: `webapp/src/app/core/services/realtime.service.spec.ts`
- Modify: `webapp/src/app/tasks/tasks.component.ts`
- Modify: `webapp/src/app/tasks/tasks.component.spec.ts`
- Modify: `webapp/src/app/tasks/info-panel/info-panel.component.ts`
- Modify: `webapp/src/app/network/network.model.ts`
- Modify: `webapp/src/app/network/network.service.ts`
- Modify: `webapp/src/app/network/network.component.ts`
- Modify: `webapp/src/app/network/network.component.html`
- Modify: `webapp/src/app/network/network.component.scss`
- Modify: `webapp/src/app/network/network.component.spec.ts`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.model.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.html`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.scss`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.spec.ts`

**Interfaces:**
- Consumes: Task 4 的 SSE event types 和 Task 1/3 的 API 字段。
- Produces: `RealtimeService.events$: Observable<RealtimeEvent>`，全应用共享一个原生 EventSource。

- [ ] **Step 1: 写共享连接和 resync 失败测试**

```typescript
it('shares one EventSource and emits resync', () => {
  service.events$.subscribe(first);
  service.events$.subscribe(second);
  expect(factory.calls.count()).toBe(1);
  source.emit('resync', '{}');
  expect(first).toHaveBeenCalledWith(jasmine.objectContaining({ type: 'resync' }));
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless --include='src/app/core/services/realtime.service.spec.ts'`
Expected: FAIL，服务尚不存在。

- [ ] **Step 3: 实现共享 EventSource 并移除任务一秒 HTTP 轮询**

首屏仍调用现有 HTTP；`resync` 再拉一次快照；`tasks` 仅更新变化项。EventSource 断线交给浏览器重连，service 在最后一个订阅者离开时关闭连接。删除 `setInterval(1000)`/RxJS `interval(1000)`。

- [ ] **Step 4: 完成网络页约定交互**

“网卡与出口”标题右侧放“检测全部线路”；每行显示启用、应用上传/下载速度、累计量、上传限速和独立检测 spinner，启停/限速确认后直接 PATCH。页面顶栏不放保存；“网络分工”标题右侧保留唯一保存按钮，选择固定/轮换、固定线路和自动接管；上传及账号用途禁用轮换选项。

- [ ] **Step 5: 在列表和抽屉展示上传进度**

显示总百分比、确认字节/总字节、当前分 P、速度和 ETA；状态不是上传中时隐藏速度，重启后无样本显示 `—`。

- [ ] **Step 6: 运行前端验证并提交**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless && npx ng lint && npm run build`
Expected: tests、lint 和生产构建全部成功。

Commit: `feat: update tasks and network status over sse`

### Task 6: 投稿远端回读验证与审计日志

**Files:**
- Create: `src/blrec/bili_upload/submission_verifier.py`
- Modify: `src/blrec/bili_upload/review.py`
- Modify: `src/blrec/bili_upload/journal.py`
- Modify: `src/blrec/web/routers/recording_sessions.py`
- Modify: `webapp/src/app/upload-tasks/shared/recording-session.model.ts`
- Modify: `webapp/src/app/upload-tasks/recording-sessions/recording-sessions.component.html`
- Modify: `src/blrec/logging.py`
- Modify: `src/blrec/setting/models.py`
- Test: `tests/bili_upload/test_submission_verifier.py`
- Test: `tests/bili_upload/test_review.py`
- Test: `tests/bili_upload/test_journal.py`

**Interfaces:**
- Produces: `SubmissionVerification(status, checked_at, matched_fields, differences, unverifiable_fields, error)`。
- Produces: 任务详情字段 `submissionVerification`，状态为 `passed|different|partial|failed|pending`。

- [ ] **Step 1: 写远端字段对比失败测试**

```python
def test_verifier_reports_visible_field_differences():
    result = verify(snapshot, remote_archive)
    assert result.status == 'different'
    assert result.differences['visibility'] == {'expected': 'private', 'actual': 'public'}

def test_unreadable_fields_are_partial_not_passed():
    assert verify(snapshot, remote_without_schedule).status == 'partial'
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest -q tests/bili_upload/test_submission_verifier.py tests/bili_upload/test_review.py`
Expected: FAIL，当前 review 只验证身份、分 P 和转码。

- [ ] **Step 3: 实现纯字段比较并在审核通过后持久化**

比较标题、简介、分区、标签、创作声明、可见性、封面、合集、定时发布、评论/弹幕开关和分 P；接口无法证明的字段进入 `unverifiable_fields`。不得因差异自动重新投稿。

- [ ] **Step 4: 补齐结构化审计日志**

关键路径统一输出 `[audit] event=<name> ... result=<value>`；上传每个确认分片写 DEBUG，每跨约 5% 写 INFO；设置、任务、路线、接管、录制、投稿、审核、修复、评论、回灌、删除和恢复均有事件。默认日志保留改为 60 天，并写测试确保敏感字段被脱敏。

- [ ] **Step 5: 运行验证并提交**

Run: `pytest -q tests/bili_upload/test_submission_verifier.py tests/bili_upload/test_review.py tests/bili_upload/test_journal.py tests/web/test_recording_sessions_routes.py`
Expected: PASS。

Commit: `feat: verify published archive settings`

### Task 7: 兼容配置迁移、全量验证、发布和群晖试运行

**Files:**
- Create: `scripts/migrate_legacy_settings.py`
- Modify: `tests/release/test_docker_image_contract.py`
- Modify: `tests/release/test_synology_release_contract.py`
- Modify: `docs/deployment/synology-container-manager.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `migrate_legacy_settings(old_path, new_path) -> MigrationReport`，只迁移设计明确允许的兼容字段。
- Produces: GHCR 新 beta 镜像和群晖 `blrec-next` 可回滚部署。

- [ ] **Step 1: 写迁移允许/禁止字段失败测试**

```python
def test_migration_keeps_safe_recording_settings_but_not_cookie_or_webhooks(tmp_path):
    report = migrate_legacy_settings(old, new)
    assert report.settings.header.user_agent == old.settings.header.user_agent
    assert report.settings.header.cookie == ''
    assert report.settings.logging.backup_count == 60
    assert report.settings.webhooks == new.settings.webhooks
```

- [ ] **Step 2: 实现备份优先、幂等迁移脚本**

运行前复制新版设置和 SQLite；迁移输出模板、分段、UA、弹幕、录像、封面和后处理，不迁移 Cookie、Webhooks、旧通知密钥和废弃磁盘字段。重复运行产生相同设置且不重复添加任务。

- [ ] **Step 3: 运行仓库全量验证**

Run: `pytest -q`
Expected: PASS。

Run: `black --check src tests scripts && isort --check-only src tests scripts && flake8 src tests scripts && mypy src/blrec`
Expected: PASS。

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless && npx ng lint && npm run build`
Expected: PASS。

- [ ] **Step 4: 构建并验证 Docker 镜像**

Run: `docker build -t ghcr.io/luwei/blrec:<new-beta> .`
Expected: 构建成功；容器健康检查通过，镜像内存在 `ip` 命令和前端生产包。

- [ ] **Step 5: 发布、更新群晖并轮换初始化 API Key**

提交并推送代码和新 beta tag，等待 GHCR workflow 成功。备份 `/volume1/docker/blrec-next/config` 与数据库，通过群晖现有 Container Manager 项目更新镜像；在 Compose 环境中静默写入新随机 API Key，不在终端或日志输出其值。

- [ ] **Step 6: 迁移安全配置并添加五个录制任务**

添加房间 `30038570`、`25654586`、`21045351`、`10802797`、`2604398`，保留新版网络/账号/容量/通知设置。先只启用监控和录制；没有扫码主账号时不启动真实投稿。

- [ ] **Step 7: 群晖验收**

验证 `ovs_eth0 -> 192.168.1.1`、`ovs_eth1 -> 192.168.50.1` 均独立通过 B 站 HTTPS 和公网出口探测，`docker0` 默认禁用；验证一条 SSE 同时更新任务、网络速率和上传进度，任务页无每秒 HTTP 轮询；模拟线路断开确认上传不换线、录像/弹幕保持业务粘性。记录 3～5 天观察起点和日志路径。

Commit: `release: prepare network pilot beta`
