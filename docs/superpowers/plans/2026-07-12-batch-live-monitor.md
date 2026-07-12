# BLREC Batch Live Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace one always-on danmaku WebSocket per configured room with one anonymous batch status coordinator and open WSS/recording only for confirmed live rooms.

**Architecture:** A process-wide `LiveStatusCoordinator` owns batching, state confirmation, fallback cooldown, and the circuit breaker. `RecordTask` registers a room callback; a small connection controller activates the existing `DanmakuClient`/`LiveMonitor` only after a confirmed `LIVE` transition and forwards WSS status hints back to the coordinator. Legacy per-room monitoring remains an explicit mutually exclusive emergency mode.

**Tech Stack:** Python 3.8, asyncio, aiohttp, attrs, Pydantic, pytest/pytest-asyncio; Angular 15, TypeScript 4.9, Jasmine/Karma.

## Global Constraints

- Routine batch status reads are anonymous: never attach Cookie, access token, Authorization, CSRF, APP signature, or an account-scoped device ID.
- Default batch size is exactly 29 UIDs; default interval is 30 seconds and accepted configuration is 30–60 seconds.
- A missing, malformed, timed-out, or partially returned room is `STALE/UNKNOWN`, never offline.
- Stop a live session only after two batch non-live observations, or one WSS `PREPARING/ROUND` hint plus one HTTP confirmation.
- A coordinator outage must not stop an already recording task.
- Per-room fallback uses singleflight and a 600-second cooldown; it must never fan out across the whole batch.
- Batch and legacy monitor modes are mutually exclusive; never run both state machines for one room.
- Keep Python compatible with 3.8; do not use `asyncio.to_thread`, `TaskGroup`, `match`, or PEP 604 union syntax.
- Do not add proxy rotation, token rotation, account pools, endpoint racing, or automatic anti-bot bypasses.

---

## File Map

- Create `src/blrec/bili/live_status.py`: coordinator-facing enums, immutable snapshots, batch results, metrics, and listener protocols.
- Create `src/blrec/bili/batch_status_client.py`: anonymous `get_status_info_by_uids` adapter and strict response validation.
- Create `src/blrec/bili/anonymous_room_client.py`: anonymous room/UID mapping, single-room confirmation, and fresh `RoomInfo` loading.
- Create `src/blrec/bili/live_status_coordinator.py`: batching, state machine, singleflight fallback, canary, breaker, and polling lifecycle.
- Create `src/blrec/bili/live_connection_controller.py`: one room's WSS/monitor activation and confirmed status delivery.
- Modify `src/blrec/bili/live_monitor.py`: external-status mode and forwarding of WSS hints without premature down events.
- Modify `src/blrec/core/recorder.py`: do not auto-start from stale `Live.room_info` while the external monitor is inactive.
- Modify `src/blrec/task/task.py`: register/unregister instead of opening WSS while offline.
- Modify `src/blrec/task/task_manager.py`: inject the process-wide coordinator into tasks.
- Modify `src/blrec/application.py`: create/start/stop the coordinator in lifecycle order and expose status.
- Modify `src/blrec/setting/models.py` and `src/blrec/setting/setting_manager.py`: validated monitor mode, interval, and batch size.
- Create `src/blrec/web/routers/live_status.py`; modify `src/blrec/web/routers/__init__.py` and `src/blrec/web/main.py`: read-only status endpoint.
- Create `tests/bili/`, `tests/task/`, and `tests/web/`: deterministic backend unit/integration tests with fake clocks and transports.
- Create `webapp/src/app/settings/live-monitor-settings/`: settings/status card and Jasmine tests.

### Task 1: Establish the backend test harness and status contracts

**Files:**
- Modify: `setup.cfg`
- Create: `tests/bili/test_live_status.py`
- Create: `src/blrec/bili/live_status.py`

**Interfaces:**
- Produces: `ObservedStatus`, `StatusSource`, `StatusSnapshot`, `BatchStatusResult`, `CoordinatorMetrics`, and `LiveStatusListener`.
- Consumes: existing `blrec.bili.models.LiveStatus` only at the adapter boundary; coordinator state must use `ObservedStatus` so `UNKNOWN` and `STALE` are representable.

- [ ] **Step 1: Add the test-only dependencies**

Add under `[options.extras_require] dev` in `setup.cfg`:

```ini
        pytest >= 7.4.4, < 8.0.0
        pytest-asyncio >= 0.21.2, < 0.22.0
```

- [ ] **Step 2: Write the failing immutable-contract test**

```python
# tests/bili/test_live_status.py
from dataclasses import FrozenInstanceError

import pytest

from blrec.bili.live_status import ObservedStatus, StatusSnapshot, StatusSource


def test_status_snapshot_is_immutable_and_keeps_unknown_state() -> None:
    snapshot = StatusSnapshot(
        uid=10,
        room_id=20,
        status=ObservedStatus.UNKNOWN,
        observed_at=30.0,
        source=StatusSource.BATCH,
        live_time=0,
        observation_key=None,
    )

    assert snapshot.status is ObservedStatus.UNKNOWN
    with pytest.raises(FrozenInstanceError):
        snapshot.room_id = 21  # type: ignore[misc]
```

- [ ] **Step 3: Run the contract test and confirm the import failure**

Run: `python -m pytest tests/bili/test_live_status.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'blrec.bili.live_status'`.

- [ ] **Step 4: Implement the complete shared contracts**

```python
# src/blrec/bili/live_status.py
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Dict, FrozenSet, Optional


class ObservedStatus(str, Enum):
    UNKNOWN = 'unknown'
    STALE = 'stale'
    PREPARING = 'preparing'
    LIVE = 'live'
    ROUND = 'round'


class StatusSource(str, Enum):
    BATCH = 'batch'
    CONFIRMATION = 'confirmation'
    WSS = 'wss'
    LOCAL = 'local'


class BreakerState(str, Enum):
    CLOSED = 'closed'
    OPEN = 'open'
    HALF_OPEN = 'half_open'
    PAUSED = 'paused'


@dataclass(frozen=True)
class StatusSnapshot:
    uid: int
    room_id: int
    status: ObservedStatus
    observed_at: float
    source: StatusSource
    live_time: int
    observation_key: Optional[str]


@dataclass(frozen=True)
class BatchStatusResult:
    snapshots: Dict[int, StatusSnapshot]
    missing_uids: FrozenSet[int]


@dataclass(frozen=True)
class CoordinatorMetrics:
    mode: str
    interval_seconds: int
    batch_size: int
    registered_rooms: int
    active_websockets: int
    last_success_at: Optional[float]
    snapshot_max_age_seconds: Optional[float]
    missing_results: int
    fallback_requests: int
    breaker_state: BreakerState
    breaker_reason: Optional[str]


LiveStatusListener = Callable[[StatusSnapshot], Awaitable[None]]
StatusConfirmer = Callable[[], Awaitable[StatusSnapshot]]
```

- [ ] **Step 5: Run and commit**

Run: `python -m pytest tests/bili/test_live_status.py -v`

Expected: PASS.

```bash
git add setup.cfg tests/bili/test_live_status.py src/blrec/bili/live_status.py
git commit -m "test: add live status coordinator contracts"
```

### Task 2: Implement the strictly anonymous batch API adapter

**Files:**
- Create: `tests/bili/test_batch_status_client.py`
- Create: `src/blrec/bili/batch_status_client.py`
- Create: `src/blrec/bili/anonymous_room_client.py`
- Modify: `src/blrec/bili/__init__.py`

**Interfaces:**
- Consumes: `BatchStatusResult`, `ObservedStatus`, `StatusSnapshot`, `StatusSource` from Task 1.
- Produces: `BatchStatusClient.fetch(uids: Sequence[int], observed_at: float) -> BatchStatusResult`, `AnonymousRoomClient.fetch_uid_mappings`, `confirm_status`, `load_room_info`, `BatchProtocolError`, and `BatchApiError`.

- [ ] **Step 1: Write failing tests for request anonymity and partial responses**

```python
# tests/bili/test_batch_status_client.py
import json
from typing import Any, Dict, List, Tuple

import pytest

from blrec.bili.batch_status_client import BatchStatusClient
from blrec.bili.live_status import ObservedStatus


class FakeResponse:
    status = 200

    async def __aenter__(self) -> 'FakeResponse':
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def text(self) -> str:
        return json.dumps({
            'code': 0,
            'data': {
                '10': {
                    'uid': 10,
                    'room_id': 20,
                    'live_status': 1,
                    'live_time': '2026-07-12 08:00:00',
                }
            },
        })


class FakeSession:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return FakeResponse()


@pytest.mark.asyncio
async def test_fetch_is_anonymous_and_marks_missing_uid() -> None:
    session = FakeSession()
    client = BatchStatusClient(session)  # type: ignore[arg-type]

    result = await client.fetch([10, 11], observed_at=100.0)

    _, request = session.calls[0]
    headers = request['headers']
    assert 'Cookie' not in headers
    assert 'Authorization' not in headers
    assert request['data'] == [('uids[]', '10'), ('uids[]', '11')]
    assert result.snapshots[10].status is ObservedStatus.LIVE
    assert result.missing_uids == frozenset({11})
```

Use this common assertion for anonymous mapping, confirmation, and room-info calls, then assert redirected mapping and numeric API codes:

```python
for _, request in session.calls:
    assert not ({'Cookie', 'Authorization', 'X-Api-Key'} & set(request['headers']))
assert mapping == {123: (real_room_id, uid)}
assert session.calls[0][1]['data'] == [('room_ids[]', '123')]
with pytest.raises(BatchApiError) as error:
    await error_client.fetch([10], observed_at=100.0)
assert error.value.code in (-352, -412)
assert 'cookie' not in str(error.value).lower()
```

- [ ] **Step 2: Run the adapter test and confirm it fails**

Run: `python -m pytest tests/bili/test_batch_status_client.py -v`

Expected: FAIL because `batch_status_client` does not exist.

- [ ] **Step 3: Implement strict parsing without credentials or retry loops**

```python
# src/blrec/bili/batch_status_client.py
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Sequence

import aiohttp

from .live_status import (
    BatchStatusResult,
    ObservedStatus,
    StatusSnapshot,
    StatusSource,
)


class BatchProtocolError(RuntimeError):
    pass


class BatchApiError(BatchProtocolError):
    def __init__(self, code: int) -> None:
        super().__init__('Bilibili API error {}'.format(code))
        self.code = code


class BatchStatusClient:
    PATH = '/room/v1/Room/get_status_info_by_uids'

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = 'https://api.live.bilibili.com',
        user_agent: str = 'BLREC batch live monitor',
    ) -> None:
        self._session = session
        self._url = base_url.rstrip('/') + self.PATH
        self._headers = {'Accept': 'application/json', 'User-Agent': user_agent}

    async def fetch(
        self, uids: Sequence[int], *, observed_at: float
    ) -> BatchStatusResult:
        unique_uids = tuple(dict.fromkeys(uids))
        data = [('uids[]', str(uid)) for uid in unique_uids]
        async with self._session.post(
            self._url, data=data, headers=self._headers, allow_redirects=False
        ) as response:
            body = await response.text()
            if response.status == 429:
                raise BatchProtocolError('HTTP 429')
            if response.status != 200:
                raise BatchProtocolError('HTTP {}'.format(response.status))
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise BatchProtocolError('response is not JSON') from exc
        code = payload.get('code')
        if isinstance(code, int) and code != 0:
            raise BatchApiError(code)
        if code != 0 or not isinstance(payload.get('data'), Mapping):
            raise BatchProtocolError('unexpected response envelope')

        snapshots: Dict[int, StatusSnapshot] = {}
        for key, value in payload['data'].items():
            if not isinstance(value, Mapping):
                continue
            snapshot = self._parse_item(value, observed_at)
            if snapshot.uid in unique_uids:
                snapshots[snapshot.uid] = snapshot
        return BatchStatusResult(
            snapshots=snapshots,
            missing_uids=frozenset(set(unique_uids) - set(snapshots)),
        )

    @staticmethod
    def _parse_item(item: Mapping[str, Any], observed_at: float) -> StatusSnapshot:
        try:
            uid = int(item['uid'])
            room_id = int(item['room_id'])
            raw_status = int(item['live_status'])
        except (KeyError, TypeError, ValueError) as exc:
            raise BatchProtocolError('invalid room item') from exc
        status_by_code = {
            0: ObservedStatus.PREPARING,
            1: ObservedStatus.LIVE,
            2: ObservedStatus.ROUND,
        }
        if raw_status not in status_by_code:
            raise BatchProtocolError('unknown live_status {}'.format(raw_status))
        live_time = BatchStatusClient._parse_live_time(item.get('live_time'))
        key = '{}:{}'.format(uid, live_time) if live_time else None
        return StatusSnapshot(
            uid=uid,
            room_id=room_id,
            status=status_by_code[raw_status],
            observed_at=observed_at,
            source=StatusSource.BATCH,
            live_time=live_time,
            observation_key=key,
        )

    @staticmethod
    def _parse_live_time(value: object) -> int:
        if not value or value == '0000-00-00 00:00:00':
            return 0
        if isinstance(value, int):
            return value
        try:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
            return int(parsed.timestamp())
        except ValueError as exc:
            raise BatchProtocolError('invalid live_time') from exc
```

Implement `AnonymousRoomClient` on a dedicated `aiohttp.ClientSession` created with `DummyCookieJar`. `fetch_uid_mappings(room_ids)` calls the multi-room basic-info endpoint with `room_ids[]`, `confirm_status(room_id)` performs one anonymous single-room status read, and `load_room_info(room_id)` returns a full `RoomInfo`. All three use `allow_redirects=False`, the same sanitized errors, and never accept a credential argument. The first implementation intentionally has no authenticated confirmation fallback; the existing per-room Cookie remains available only to live stream/WSS operations after confirmation.

- [ ] **Step 4: Export, run focused tests, and commit**

Add `BatchStatusClient` and `BatchProtocolError` to `src/blrec/bili/__init__.py` exports.

Run: `python -m pytest tests/bili/test_batch_status_client.py -v`

Expected: PASS, including the missing UID assertion.

```bash
git add src/blrec/bili/__init__.py src/blrec/bili/batch_status_client.py src/blrec/bili/anonymous_room_client.py tests/bili/test_batch_status_client.py
git commit -m "feat: add anonymous batch live status client"
```

### Task 3: Build batching, confirmation, and the coordinator breaker

**Files:**
- Create: `tests/bili/test_live_status_coordinator.py`
- Create: `src/blrec/bili/live_status_coordinator.py`

**Interfaces:**
- Consumes: `BatchStatusClient.fetch`, `LiveStatusListener`, and `StatusConfirmer`.
- Produces: `StatusCircuitBreaker`, `register(uid, room_id, listener, confirmer)`, `unregister(room_id)`, `observe_wss(room_id, status)`, `poll_once()`, `start()`, `stop()`, and `metrics(now)`.

- [ ] **Step 1: Write failing state-machine tests**

Create tests with an injected monotonic clock and scripted client. The core assertions must be exactly:

```python
@pytest.mark.asyncio
async def test_58_rooms_are_deduplicated_into_two_batches() -> None:
    client = ScriptedBatchClient()
    coordinator = LiveStatusCoordinator(client, batch_size=29, clock=lambda: 100.0)
    for uid in range(1, 59):
        coordinator.register(uid, uid + 1000, AsyncMock(), AsyncMock())

    await coordinator.poll_once()

    assert [len(call) for call in client.calls] == [29, 29]
    assert sorted(uid for call in client.calls for uid in call) == list(range(1, 59))


@pytest.mark.asyncio
async def test_missing_result_does_not_emit_offline() -> None:
    listener = AsyncMock()
    client = ScriptedBatchClient(results=[batch_live(1), batch_missing(1)])
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(1, 1001, listener, AsyncMock())

    await coordinator.poll_once()
    await coordinator.poll_once()

    assert [call.args[0].status for call in listener.await_args_list] == [
        ObservedStatus.LIVE
    ]


@pytest.mark.asyncio
async def test_offline_requires_two_batch_observations() -> None:
    listener = AsyncMock()
    client = ScriptedBatchClient(
        results=[batch_live(1), batch_preparing(1), batch_preparing(1)]
    )
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(1, 1001, listener, AsyncMock())

    await coordinator.poll_once()
    await coordinator.poll_once()
    await coordinator.poll_once()

    assert [call.args[0].status for call in listener.await_args_list] == [
        ObservedStatus.LIVE,
        ObservedStatus.PREPARING,
    ]
```

Define the scripted transport and snapshot builders in the same test file so no test depends on a live endpoint:

```python
from collections import deque
from typing import Deque, List, Optional, Sequence
from unittest.mock import AsyncMock

from blrec.bili.live_status import (
    BatchStatusResult,
    ObservedStatus,
    StatusSnapshot,
    StatusSource,
)


def batch_result(
    uid: int, status: Optional[ObservedStatus], live_time: int = 1
) -> BatchStatusResult:
    if status is None:
        return BatchStatusResult({}, frozenset({uid}))
    snapshot = StatusSnapshot(
        uid=uid,
        room_id=uid + 1000,
        status=status,
        observed_at=100.0,
        source=StatusSource.BATCH,
        live_time=live_time,
        observation_key='{}:{}'.format(uid, live_time) if live_time else None,
    )
    return BatchStatusResult({uid: snapshot}, frozenset())


class ScriptedBatchClient:
    def __init__(
        self,
        results: Optional[Sequence[BatchStatusResult]] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        self.calls: List[List[int]] = []
        self.results: Deque[BatchStatusResult] = deque(results or [])
        self.error = error

    async def fetch(
        self, uids: Sequence[int], *, observed_at: float
    ) -> BatchStatusResult:
        self.calls.append(list(uids))
        if self.error is not None:
            raise self.error
        if self.results:
            return self.results.popleft()
        return BatchStatusResult({}, frozenset(uids))
```

Use `batch_result(1, ObservedStatus.LIVE)`, `batch_result(1, None)`, and `batch_result(1, ObservedStatus.PREPARING)` in the three tests above. Add these exact focused assertions:

```python
assert listener.await_count == 1  # repeated (uid, live_time) is suppressed
assert confirmer.await_count == 1  # WSS negative shares one HTTP confirmation
assert coordinator.fallback_count == 1  # two requests inside 600 s are singleflight/cooldown
assert coordinator.metrics(100.0).breaker_state is BreakerState.OPEN
assert registration.current is ObservedStatus.LIVE  # breaker never synthesizes offline
assert canary_client.calls[-1] == [1]  # half-open releases one UID only
```

- [ ] **Step 2: Run the coordinator tests and verify they fail**

Run: `python -m pytest tests/bili/test_live_status_coordinator.py -v`

Expected: FAIL because `LiveStatusCoordinator` is undefined.

- [ ] **Step 3: Implement the minimal coordinator state record and public API**

Use these exact internal records and constructor defaults:

```python
@dataclass
class _Registration:
    uid: int
    room_id: int
    listener: LiveStatusListener
    confirmer: StatusConfirmer
    current: ObservedStatus = ObservedStatus.UNKNOWN
    observation_key: Optional[str] = None
    negative_count: int = 0
    wss_negative: bool = False
    last_fallback_at: float = float('-inf')


class LiveStatusCoordinator:
    def __init__(
        self,
        client: BatchStatusClient,
        *,
        interval_seconds: int = 30,
        batch_size: int = 29,
        fallback_cooldown_seconds: int = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds < 30 or interval_seconds > 60:
            raise ValueError('interval_seconds must be between 30 and 60')
        if batch_size < 1 or batch_size > 29:
            raise ValueError('batch_size must be between 1 and 29')
        self._client = client
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._fallback_cooldown_seconds = fallback_cooldown_seconds
        self._clock = clock
        self._registrations: Dict[int, _Registration] = {}
        self._fallback_tasks: Dict[int, asyncio.Task[StatusSnapshot]] = {}
        self._breaker = StatusCircuitBreaker(clock=clock)
        self._polling_task: Optional[asyncio.Task[None]] = None
```

Implement `poll_once()` to snapshot and sort registered UIDs, split with `range(0, len(uids), self._batch_size)`, run batches sequentially, and call `_apply_batch_result`. Never use `asyncio.gather` for all batches: sequential release is part of the anti-burst contract.

`start()` does not release the full set immediately: it first fetches exactly the lowest registered UID as a response-shape canary. Only a valid canary permits `poll_once()` to process configured batches. Breaker recovery follows sizes `1 → min(5, batch_size) → batch_size` on consecutive successful cycles and returns to 1 on any failure.

- [ ] **Step 4: Implement exact transition and breaker rules**

The transition function must follow this complete decision table:

```python
async def _apply_snapshot(
    self, registration: _Registration, snapshot: StatusSnapshot
) -> None:
    if snapshot.status in (ObservedStatus.UNKNOWN, ObservedStatus.STALE):
        return
    if snapshot.status is ObservedStatus.LIVE:
        registration.negative_count = 0
        registration.wss_negative = False
        same_broadcast = (
            registration.current is ObservedStatus.LIVE
            and (
                snapshot.observation_key is None
                or registration.observation_key == snapshot.observation_key
            )
        )
        if same_broadcast:
            return
        confirmed = await self._confirm(registration)
        if confirmed.status is not ObservedStatus.LIVE:
            return
        registration.current = ObservedStatus.LIVE
        registration.observation_key = (
            confirmed.observation_key
            or snapshot.observation_key
            or '{}:local:{}'.format(registration.uid, int(self._clock()))
        )
        await registration.listener(confirmed)
        return

    if registration.current is not ObservedStatus.LIVE:
        registration.current = snapshot.status
        return
    registration.negative_count += 1
    confirmed_offline = registration.wss_negative or registration.negative_count >= 2
    if not confirmed_offline:
        return
    registration.current = snapshot.status
    registration.negative_count = 0
    registration.wss_negative = False
    await registration.listener(snapshot)
```

`_confirm` must reuse one task per room, enforce the 600-second cooldown, and return an `UNKNOWN` snapshot when no fallback is allowed. `observe_wss` only marks a negative hint then calls `_confirm`; it must not emit an end by itself. `StatusCircuitBreaker` exposes `allow_canary(now)`, `record_success(batch_size)`, `record_failure(reason)`, and `resume()`; it opens for HTTP 429, API `-352/-412`, timeouts, structural errors, or a response missing more than half of the requested UIDs. While open, only a one-UID canary may run after exponential delay. After five structural canary failures, set state `PAUSED` until an explicit management resume.

- [ ] **Step 5: Run deterministic tests and commit**

Run: `python -m pytest tests/bili/test_live_status_coordinator.py -v`

Expected: PASS for all batching, state, cooldown, and breaker cases.

```bash
git add src/blrec/bili/live_status_coordinator.py tests/bili/test_live_status_coordinator.py
git commit -m "feat: coordinate batch live status transitions"
```

### Task 4: Connect confirmed status to the existing recorder lifecycle

**Files:**
- Create: `tests/task/test_live_connection_controller.py`
- Create: `src/blrec/bili/live_connection_controller.py`
- Modify: `src/blrec/bili/live_monitor.py`
- Modify: `src/blrec/core/recorder.py`
- Modify: `src/blrec/task/task.py`
- Modify: `src/blrec/task/task_manager.py`

**Interfaces:**
- Consumes: coordinator registration API from Task 3 and existing `DanmakuClient`, `LiveMonitor`, `Live`, and `Recorder`.
- Produces: `LiveConnectionController.on_confirmed_status(snapshot)`, `on_wss_hint(status)`, `active`, and `close()`; `Live.replace_room_info(room_info)`.

- [ ] **Step 1: Write lifecycle tests with fakes**

```python
@pytest.mark.asyncio
async def test_offline_registration_does_not_start_websocket() -> None:
    danmaku = AsyncMock()
    monitor = FakeMonitor()
    controller = LiveConnectionController(
        FakeLive(), danmaku, monitor, AsyncMock(return_value=object())
    )

    assert controller.active is False
    danmaku.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_live_starts_once_and_offline_releases_wss() -> None:
    danmaku = AsyncMock()
    monitor = FakeMonitor()
    controller = LiveConnectionController(
        FakeLive(), danmaku, monitor, AsyncMock(return_value=object())
    )

    await controller.on_confirmed_status(live_snapshot())
    await controller.on_confirmed_status(live_snapshot())
    await controller.on_confirmed_status(preparing_snapshot())

    danmaku.start.assert_awaited_once()
    danmaku.stop.assert_awaited_once()
    assert monitor.confirmed == [ObservedStatus.LIVE, ObservedStatus.PREPARING]
    assert controller.active is False
```

Define the fakes and snapshots above those tests:

```python
class FakeLive:
    room_id = 1001

    def replace_room_info(self, room_info: object) -> None:
        self.room_info = room_info


class FakeMonitor:
    def __init__(self) -> None:
        self.confirmed: List[ObservedStatus] = []
        self.enabled = False

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    async def apply_confirmed_status(self, status: ObservedStatus) -> None:
        self.confirmed.append(status)


def live_snapshot() -> StatusSnapshot:
    return StatusSnapshot(
        1, 1001, ObservedStatus.LIVE, 100.0, StatusSource.CONFIRMATION, 10, '1:10'
    )


def preparing_snapshot() -> StatusSnapshot:
    return StatusSnapshot(
        1, 1001, ObservedStatus.PREPARING, 130.0, StatusSource.BATCH, 10, '1:10'
    )
```

The integration test uses mocks and these exact assertions:

```python
await task.enable_monitor()
coordinator.register.assert_called_once()
assert coordinator.register.call_args.kwargs['confirmer'] == anonymous.confirm_status
danmaku.start.assert_not_awaited()

await task.disable_monitor()
coordinator.unregister.assert_called_once_with(task.room_info.room_id)
recorder.stop.assert_not_awaited()
```

- [ ] **Step 2: Run and verify the tests fail before production changes**

Run: `python -m pytest tests/task/test_live_connection_controller.py -v`

Expected: FAIL because the controller and injected coordinator do not exist.

- [ ] **Step 3: Add external-status mode to `LiveMonitor`**

Add constructor argument `status_sink: Optional[Callable[[ObservedStatus], Awaitable[None]]] = None`. In external mode, `_do_enable()` attaches the danmaku listener but does not call `_start_polling()`. Replace direct handling of `LIVE/PREPARING/ROUND` in `on_danmaku_received` with:

```python
if self._status_sink is not None:
    status = {
        DanmakuCommand.LIVE.value: ObservedStatus.LIVE,
        DanmakuCommand.PREPARING.value: (
            ObservedStatus.ROUND
            if danmu.get('round') == 1
            else ObservedStatus.PREPARING
        ),
    }.get(danmu_cmd)
    if status is not None:
        await self._status_sink(status)
        return
```

Expose `async def apply_confirmed_status(self, status: ObservedStatus) -> None` that maps `LIVE/PREPARING/ROUND` to existing `LiveStatus` and calls `_handle_status_change`. Initialize `_previous_status` as `LiveStatus.PREPARING` when external mode is enabled so the first confirmed LIVE emits `live_began`.

- [ ] **Step 4: Implement the room connection controller**

```python
class LiveConnectionController:
    def __init__(
        self,
        live: Live,
        danmaku: DanmakuClient,
        monitor: LiveMonitor,
        room_info_loader: Callable[[], Awaitable[RoomInfo]],
    ) -> None:
        self._live = live
        self._danmaku = danmaku
        self._monitor = monitor
        self._room_info_loader = room_info_loader
        self._active = False
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._active

    async def on_confirmed_status(self, snapshot: StatusSnapshot) -> None:
        async with self._lock:
            if snapshot.status is ObservedStatus.LIVE:
                if self._active:
                    return
                room_info = await self._room_info_loader()
                self._live.replace_room_info(room_info)
                self._monitor.enable()
                try:
                    await self._danmaku.start()
                    await self._monitor.apply_confirmed_status(ObservedStatus.LIVE)
                except BaseException:
                    self._monitor.disable()
                    await self._danmaku.stop()
                    raise
                self._active = True
                return
            if not self._active:
                return
            await self._monitor.apply_confirmed_status(snapshot.status)
            await self._danmaku.stop()
            self._monitor.disable()
            self._active = False

    async def close(self) -> None:
        async with self._lock:
            if self._active:
                await self._danmaku.stop()
                self._monitor.disable()
                self._active = False
```

The coordinator's `confirm_status()` calls `AnonymousRoomClient.confirm_status(room_id)` and returns a `StatusSnapshot` with source `CONFIRMATION`. The controller's room-info loader calls `AnonymousRoomClient.load_room_info(room_id)` before activating WSS. Add `Live.replace_room_info(room_info: RoomInfo)` as the only mutation point for this already-validated anonymous result. `on_wss_hint()` delegates to `LiveStatusCoordinator.observe_wss(room_id, status)`.

- [ ] **Step 5: Inject coordinator registration into tasks**

Change constructors to `RecordTask(room_id, *, live_status_coordinator: Optional[LiveStatusCoordinator] = None, anonymous_room_client: Optional[AnonymousRoomClient] = None, ...)` and `RecordTaskManager(settings_manager, live_status_coordinator, anonymous_room_client)`. In batch mode, `enable_monitor()` registers `self._live.user_info.uid`, the real room ID, controller listener, and anonymous confirmer; `disable_monitor()` unregisters then closes. On missing UID or a room redirect, the coordinator calls `fetch_uid_mappings` once under the existing 600-second fallback cooldown, updates the registration atomically, and deduplicates registrations that resolve to the same `(uid, real_room_id)`. Preserve the old start/stop path only when configured mode is `legacy`.

In `Recorder._do_start`, replace `if self._live.is_living():` with:

```python
if self._live_monitor.enabled and self._live.is_living():
    self._stream_available = True
    await self._start_recording()
else:
    self._print_waiting_message()
```

- [ ] **Step 6: Run lifecycle and existing smoke checks, then commit**

Run: `python -m pytest tests/task/test_live_connection_controller.py tests/bili -v`

Expected: PASS; fake offline tasks create zero WSS, status/room-info requests contain no Cookie/token, redirected room mappings deduplicate, and confirmed live creates one WSS.

```bash
git add src/blrec/bili/live_connection_controller.py src/blrec/bili/live_monitor.py src/blrec/core/recorder.py src/blrec/task/task.py src/blrec/task/task_manager.py tests/task/test_live_connection_controller.py
git commit -m "feat: open danmaku connections only while live"
```

### Task 5: Add validated settings, lifecycle ordering, metrics, and emergency mode

**Files:**
- Create: `tests/test_application_live_status.py`
- Create: `tests/web/test_live_status_routes.py`
- Modify: `src/blrec/setting/models.py`
- Modify: `src/blrec/setting/setting_manager.py`
- Modify: `src/blrec/application.py`
- Create: `src/blrec/web/routers/live_status.py`
- Modify: `src/blrec/web/routers/__init__.py`
- Modify: `src/blrec/web/main.py`

**Interfaces:**
- Produces: `LiveMonitorSettings`, `GET /api/v1/live-status`, and authenticated `POST /api/v1/live-status/resume`.
- Consumes: `LiveStatusCoordinator.metrics()` and lifecycle methods.

- [ ] **Step 1: Write failing validation and lifecycle tests**

```python
def test_live_monitor_settings_reject_unsafe_interval() -> None:
    with pytest.raises(ValidationError):
        LiveMonitorSettings(interval_seconds=10)


@pytest.mark.asyncio
async def test_application_stops_coordinator_after_tasks() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    app._task_manager = OrderedTaskManager(calls)
    app._live_status_coordinator = OrderedCoordinator(calls)
    app._destroy = lambda: calls.append('application.destroy')

    await app._exit()

    assert calls == [
        'tasks.stop',
        'tasks.destroy',
        'coordinator.stop',
        'application.destroy',
    ]
```

Define the lifecycle fakes in the same file:

```python
class OrderedTaskManager:
    def __init__(self, calls: List[str]) -> None:
        self._calls = calls

    async def stop_all_tasks(self, force: bool = False) -> None:
        self._calls.append('tasks.stop')

    async def destroy_all_tasks(self) -> None:
        self._calls.append('tasks.destroy')


class OrderedCoordinator:
    def __init__(self, calls: List[str]) -> None:
        self._calls = calls

    async def stop(self) -> None:
        self._calls.append('coordinator.stop')
```

The route tests use these exact assertions:

```python
response = client.get('/api/v1/live-status')
assert response.status_code == 200
assert response.json()['activeWebsockets'] == 0
assert response.json()['registeredRooms'] == 58

response = client.post('/api/v1/live-status/resume')
assert response.status_code == 401
```

- [ ] **Step 2: Add the exact Pydantic settings shape**

```python
class LiveMonitorSettings(BaseModel):
    mode: Literal['batch', 'legacy'] = 'batch'
    interval_seconds: Annotated[int, Field(ge=30, le=60)] = 30
    batch_size: Annotated[int, Field(ge=1, le=29)] = 29
    fallback_cooldown_seconds: Annotated[int, Field(ge=600, le=3600)] = 600
```

Add `live_monitor: LiveMonitorSettings = LiveMonitorSettings()` to `Settings`, and `live_monitor: Optional[LiveMonitorSettings] = None` to `SettingsIn`. `SettingsManager` must reject runtime mode changes while any task is recording; require an application restart for `batch ↔ legacy`.

- [ ] **Step 3: Wire application lifecycle with an isolated anonymous session**

During `Application.launch`, create an `aiohttp.ClientSession` with `aiohttp.DummyCookieJar()`, no default Cookie header, the existing shared connector/timeouts, then construct and start the coordinator before loading tasks. During exit, destroy tasks before stopping the coordinator and closing its session. Expose a frozen status DTO; do not return per-room cookies, headers, or raw upstream payloads.

- [ ] **Step 4: Add status and resume routes**

```python
router = APIRouter(prefix='/live-status', tags=['live-status'])


@router.get('')
async def get_live_status(
    application: Application = Depends(get_application),
) -> CoordinatorMetrics:
    return application.get_live_status_metrics()


@router.post('/resume', status_code=status.HTTP_204_NO_CONTENT)
async def resume_live_status(
    application: Application = Depends(get_application),
) -> None:
    application.resume_live_status_coordinator()
```

Register the router under the existing `/api/v1` root and reuse the existing API-key dependency/middleware; do not invent a second key.

- [ ] **Step 5: Run backend integration tests and commit**

Run: `python -m pytest tests/test_application_live_status.py tests/web/test_live_status_routes.py tests/bili tests/task -v`

Expected: PASS with lifecycle ordering and authentication assertions.

```bash
git add src/blrec/application.py src/blrec/setting/models.py src/blrec/setting/setting_manager.py src/blrec/web/main.py src/blrec/web/routers/__init__.py src/blrec/web/routers/live_status.py tests/test_application_live_status.py tests/web/test_live_status_routes.py
git commit -m "feat: expose batch monitor settings and health"
```

### Task 6: Add the Angular settings and status card

**Files:**
- Modify: `webapp/src/app/settings/shared/setting.model.ts`
- Create: `webapp/src/app/settings/live-monitor-settings/live-monitor-settings.component.ts`
- Create: `webapp/src/app/settings/live-monitor-settings/live-monitor-settings.component.html`
- Create: `webapp/src/app/settings/live-monitor-settings/live-monitor-settings.component.scss`
- Create: `webapp/src/app/settings/live-monitor-settings/live-monitor-settings.component.spec.ts`
- Create: `webapp/src/app/settings/shared/services/live-status.service.ts`
- Create: `webapp/src/app/settings/shared/services/live-status.service.spec.ts`
- Modify: `webapp/src/app/settings/settings.module.ts`
- Modify: `webapp/src/app/settings/settings.component.html`

**Interfaces:**
- Consumes: `GET /api/v1/live-status`, `POST /api/v1/live-status/resume`, and existing settings update service.
- Produces: typed `LiveMonitorSettings` and a discriminated `LiveStatusView` with explicit breaker state.

- [ ] **Step 1: Write failing component and service tests**

```typescript
it('shows that offline rooms use no websocket', () => {
  component.status = {
    state: 'ready',
    data: {
      mode: 'batch',
      intervalSeconds: 30,
      batchSize: 29,
      registeredRooms: 58,
      activeWebsockets: 0,
      lastSuccessAt: 100,
      snapshotMaxAgeSeconds: 12,
      missingResults: 0,
      fallbackRequests: 0,
      breakerState: 'closed',
      breakerReason: null,
    },
  };
  fixture.detectChanges();
  expect(fixture.nativeElement.textContent).toContain('活跃 WSS：0');
});
```

Add this service test for a single GET and manual resume POST; authentication remains the responsibility of the existing interceptor:

```typescript
it('loads health and resumes a paused coordinator', () => {
  service.getMetrics().subscribe((value) => expect(value.mode).toBe('batch'));
  const getRequest = http.expectOne('/api/v1/live-status');
  expect(getRequest.request.method).toBe('GET');
  getRequest.flush(metricsFixture);

  service.resume().subscribe();
  const postRequest = http.expectOne('/api/v1/live-status/resume');
  expect(postRequest.request.method).toBe('POST');
  postRequest.flush(null);
});
```

- [ ] **Step 2: Add type-safe settings and status unions**

```typescript
export interface LiveMonitorSettings {
  mode: 'batch' | 'legacy';
  intervalSeconds: number;
  batchSize: number;
  fallbackCooldownSeconds: number;
}

export interface LiveStatusMetrics {
  mode: 'batch' | 'legacy';
  intervalSeconds: number;
  batchSize: number;
  registeredRooms: number;
  activeWebsockets: number;
  lastSuccessAt: number | null;
  snapshotMaxAgeSeconds: number | null;
  missingResults: number;
  fallbackRequests: number;
  breakerState: 'closed' | 'open' | 'half_open' | 'paused';
  breakerReason: string | null;
}

export type LiveStatusView =
  | { state: 'loading' }
  | { state: 'ready'; data: LiveStatusMetrics }
  | { state: 'error'; message: string };
```

Add `liveMonitor: LiveMonitorSettings` to `Settings`. Do not use optional booleans that permit impossible loading/error combinations.

- [ ] **Step 3: Implement the focused card**

Use explicit union narrowing in the template. The status block is:

```html
<ng-container *ngIf="status.state === 'ready'">
  <p>模式：{{ status.data.mode }}</p>
  <p>轮询：{{ status.data.intervalSeconds }} 秒 / 每批 {{ status.data.batchSize }}</p>
  <p>房间：{{ status.data.registeredRooms }}</p>
  <p>活跃 WSS：{{ status.data.activeWebsockets }}</p>
  <p>缺项：{{ status.data.missingResults }}；兜底：{{ status.data.fallbackRequests }}</p>
  <nz-alert
    *ngIf="status.data.mode === 'legacy'"
    nzType="warning"
    nzMessage="旧模式会为离线房间维持连接"
  ></nz-alert>
  <button
    nz-button
    *ngIf="status.data.breakerState === 'paused'"
    (click)="resume()"
  >恢复小批 canary</button>
</ng-container>
```

The form exposes mode, interval 30–60, batch size 1–29, and cooldown 600–3600. The read-only status also renders last success, max snapshot age, and breaker reason. Do not add account or Cookie controls to this component.

- [ ] **Step 4: Register the component and run frontend checks**

Run: `cd webapp && npm test -- --watch=false --browsers=ChromeHeadless`

Expected: all Jasmine/Karma tests PASS.

Run: `cd webapp && npm run build`

Expected: Angular production build succeeds with no TypeScript errors.

```bash
git add webapp/src/app/settings
git commit -m "feat: show batch live monitor health"
```

### Task 7: Complete verification and prepare the 3–5 room rollout

**Files:**
- Create: `tests/integration/test_batch_live_monitor.py`
- Create: `docs/operations/batch-live-monitor-rollout.md`
- Create: `.github/workflows/test.yml`

**Interfaces:**
- Consumes: completed coordinator, task integration, status API, and settings UI.
- Produces: deterministic 58-room integration coverage and an operator gate; no real Bilibili write request.

- [ ] **Step 1: Add the end-to-end fake-service scenario**

The fake scenario must register 58 rooms, report all offline, move rooms 1–5 live with unique `live_time`, inject one missing batch item, one HTTP 429, and one WSS `PREPARING`, then return all rooms offline. Assert:

```python
assert fake_danmaku.total_start_calls == 5
assert fake_danmaku.max_concurrent_connections == 5
assert fake_danmaku.active_connections == 0
assert fake_recorder.started_broadcasts == 5
assert fake_recorder.stopped_broadcasts == 5
assert fake_recorder.stops_during_breaker == 0
assert fake_batch.max_requests_per_poll == 2
assert fake_single_room.max_requests_in_window(600) <= 5
```

- [ ] **Step 2: Add CI checks**

The workflow must run on Python 3.8 and the current project Python, install `.[dev]`, execute `pytest`, `black --check`, `isort --check-only`, `flake8`, and `mypy src/blrec`; a separate Node job runs `npm ci`, headless tests, and `npm run build` in `webapp/`.

- [ ] **Step 3: Write the operator rollout gate**

Document these exact gates: anonymous one-UID canary first; 3–5 rooms for at least three days; every selected room must complete one live; offline WSS must remain zero; discovery delay must remain within configured 30–60 seconds; no duplicate start/end; no request burst during 429/-352/-412; only then enable all 58 rooms. Include rollback to `legacy` as a restart-only emergency action and warn that it restores the previous request/WSS cost.

- [ ] **Step 4: Run the full verification suite**

Run:

```bash
python -m pytest -v
black --check src tests
isort --check-only src tests
flake8 src tests
mypy src/blrec
cd webapp && npm test -- --watch=false --browsers=ChromeHeadless
cd webapp && npm run build
docker build -t blrec:batch-monitor-test .
```

Expected: every command exits 0. The integration test reports zero offline WSS and no stop during breaker.

- [ ] **Step 5: Commit the verified rollout package**

```bash
git add .github/workflows/test.yml tests/integration/test_batch_live_monitor.py docs/operations/batch-live-monitor-rollout.md
git commit -m "test: verify batch live monitor rollout"
```

## Plan Self-Review

- Spec coverage: batching, deduplication, canary, partial responses, transition confirmation, WSS lifecycle, anonymous reads, fallback cooldown/singleflight, breaker, metrics, legacy exclusivity, tests, and 3–5 room rollout are assigned to Tasks 2–7.
- Type consistency: `StatusSnapshot`, `ObservedStatus`, `LiveStatusListener`, `StatusConfirmer`, and `CoordinatorMetrics` retain the Task 1 names through backend and API tasks; Angular uses the camelCase serialization already configured by Pydantic.
- Safety boundary: no task adds logged-in batch polling, proxy/account rotation, endpoint racing, or an automated challenge bypass.
