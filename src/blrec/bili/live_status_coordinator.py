from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import (
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

from ..exception import exception_callback, submit_exception
from .batch_status_client import BatchApiError, BatchStatusClient
from .live_status import (
    BatchStatusResult,
    BreakerState,
    CoordinatorMetrics,
    LiveStatusListener,
    ObservedStatus,
    StatusConfirmer,
    StatusSnapshot,
    StatusSource,
)

__all__ = ('LiveStatusCoordinator', 'StatusCircuitBreaker')

_RegistrationState = Tuple[ObservedStatus, Optional[str], int, bool]
_RoomMappingLoader = Callable[[Sequence[int]], Awaitable[Dict[int, Tuple[int, int]]]]
_RoomStatusConfirmer = Callable[[int], Awaitable[StatusSnapshot]]
_RegistrationConfirmer = Union[StatusConfirmer, _RoomStatusConfirmer]


@dataclass
class _Registration:
    registration_key: int
    uid: int
    room_id: int
    listener: LiveStatusListener
    confirmer: _RegistrationConfirmer
    requested_room_id: int
    mapping_loader: Optional[_RoomMappingLoader]
    confirmer_uses_room_id: bool
    mapping_resolved: bool
    current: ObservedStatus = ObservedStatus.UNKNOWN
    observation_key: Optional[str] = None
    negative_count: int = 0
    wss_negative: bool = False
    last_mapping_at: float = float('-inf')


@dataclass
class _FallbackEntry:
    task: asyncio.Task[StatusSnapshot]
    started_at: float
    generations: Dict[int, _Registration]
    consumed: Dict[int, _Registration]


class StatusCircuitBreaker:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        base_delay_seconds: int = 30,
        max_delay_seconds: int = 600,
    ) -> None:
        self._clock = clock
        self._base_delay_seconds = base_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._state = BreakerState.CLOSED
        self._reason: Optional[str] = None
        self._next_canary_at = float('-inf')
        self._failure_streak = 0
        self._canary_failures = 0
        self._recovery_stage = 0

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    def allow_canary(self, now: float) -> bool:
        if self._state is BreakerState.PAUSED:
            return False
        if self._state is BreakerState.OPEN:
            if now < self._next_canary_at:
                return False
            self._state = BreakerState.HALF_OPEN
            self._recovery_stage = 0
        return True

    def recovery_limit(self, batch_size: int) -> Optional[int]:
        if self._state is not BreakerState.HALF_OPEN:
            return None
        if self._recovery_stage == 0:
            return 1
        return min(5, batch_size)

    def record_success(self, batch_size: int) -> None:
        self._failure_streak = 0
        self._canary_failures = 0
        self._reason = None
        if self._state is not BreakerState.HALF_OPEN:
            self._state = BreakerState.CLOSED
            return
        if self._recovery_stage == 0:
            self._recovery_stage = 1
            return
        self._state = BreakerState.CLOSED
        self._recovery_stage = 0

    def record_failure(self, reason: str) -> None:
        failed_canary = (
            self._state is BreakerState.HALF_OPEN and self._recovery_stage == 0
        )
        if failed_canary:
            self._canary_failures += 1
        else:
            self._canary_failures = 0

        self._reason = reason
        self._failure_streak += 1
        self._recovery_stage = 0
        if self._canary_failures >= 5:
            self._state = BreakerState.PAUSED
            return

        delay = min(
            self._base_delay_seconds * (2 ** (self._failure_streak - 1)),
            self._max_delay_seconds,
        )
        self._state = BreakerState.OPEN
        self._next_canary_at = self._clock() + delay

    def resume(self) -> None:
        self._state = BreakerState.OPEN
        self._reason = None
        self._next_canary_at = self._clock()
        self._failure_streak = 0
        self._canary_failures = 0
        self._recovery_stage = 0


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
        self._fallback_tasks: Dict[int, _FallbackEntry] = {}
        self._breaker = StatusCircuitBreaker(clock=clock)
        self._polling_task: Optional[asyncio.Task[None]] = None
        self._poll_lock = asyncio.Lock()
        self._fallback_count = 0
        self._missing_results = 0
        self._last_success_at: Optional[float] = None

    @property
    def fallback_count(self) -> int:
        return self._fallback_count

    def register(
        self,
        uid: int,
        room_id: int,
        listener: LiveStatusListener,
        confirmer: _RegistrationConfirmer,
        *,
        requested_room_id: Optional[int] = None,
        mapping_loader: Optional[_RoomMappingLoader] = None,
        confirmer_uses_room_id: bool = False,
    ) -> _Registration:
        requested_room_id = requested_room_id or room_id
        registration_key = requested_room_id
        mapping_resolved = mapping_loader is None or (
            uid > 0 and requested_room_id == room_id
        )
        registration = _Registration(
            registration_key,
            uid,
            room_id,
            listener,
            confirmer,
            requested_room_id,
            mapping_loader,
            confirmer_uses_room_id,
            mapping_resolved,
        )
        previous = self._registrations.pop(registration_key, None)
        if previous is not None:
            self._remove_fallback_owner(previous)
        self._registrations[registration_key] = registration
        return registration

    def unregister(self, registration_key: int) -> None:
        registration = self._registrations.pop(registration_key, None)
        if registration is not None:
            self._remove_fallback_owner(registration)

    def _remove_fallback_owner(self, registration: _Registration) -> None:
        entry = self._fallback_tasks.get(registration.room_id)
        if entry is None:
            return
        registration_key = registration.registration_key
        if entry.generations.get(registration_key) is registration:
            entry.generations.pop(registration_key)
        if entry.consumed.get(registration_key) is registration:
            entry.consumed.pop(registration_key)
        has_current_owner = any(
            item.room_id == registration.room_id
            for item in self._registrations.values()
        )
        if (
            not has_current_owner
            and self._fallback_tasks.get(registration.room_id) is entry
        ):
            self._fallback_tasks.pop(registration.room_id)

    def resume(self) -> None:
        self._breaker.resume()

    async def observe_wss(self, registration_key: int, status: ObservedStatus) -> None:
        if status not in (ObservedStatus.PREPARING, ObservedStatus.ROUND):
            return
        registration = self._registrations.get(registration_key)
        if registration is None:
            return
        registration.wss_negative = True
        confirmed = await self._confirm(registration)
        if self._registrations.get(registration_key) is registration:
            await self._apply_snapshot(registration, confirmed)

    async def poll_once(self) -> None:
        async with self._poll_lock:
            await self._resolve_uid_mappings()
            uids = sorted(
                {item.uid for item in self._registrations.values() if item.uid > 0}
            )
            await self._poll_uids(uids)

    async def start(self) -> None:
        if self._polling_task is not None and not self._polling_task.done():
            return
        self._polling_task = asyncio.create_task(self._polling_loop())
        self._polling_task.add_done_callback(exception_callback)

    async def stop(self) -> None:
        task = self._polling_task
        if task is None:
            return
        self._polling_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def metrics(self, now: float) -> CoordinatorMetrics:
        if self._last_success_at is None:
            snapshot_age = None
        else:
            snapshot_age = max(0.0, now - self._last_success_at)
        return CoordinatorMetrics(
            mode='batch',
            interval_seconds=self._interval_seconds,
            batch_size=self._batch_size,
            registered_rooms=len(self._registrations),
            active_websockets=sum(
                item.current is ObservedStatus.LIVE
                for item in self._registrations.values()
            ),
            last_success_at=self._last_success_at,
            snapshot_max_age_seconds=snapshot_age,
            missing_results=self._missing_results,
            fallback_requests=self._fallback_count,
            breaker_state=self._breaker.state,
            breaker_reason=self._breaker.reason,
        )

    async def _polling_loop(self) -> None:
        while True:
            async with self._poll_lock:
                await self._resolve_uid_mappings()
                uids = sorted(
                    {item.uid for item in self._registrations.values() if item.uid > 0}
                )
                if uids:
                    canary_succeeded = await self._poll_uids([uids[0]], forced=True)
                    break
            await asyncio.sleep(self._interval_seconds)
        if canary_succeeded:
            await self.poll_once()
        while True:
            await asyncio.sleep(self._interval_seconds)
            await self.poll_once()

    async def _resolve_uid_mappings(self) -> None:
        now = self._clock()
        groups: Dict[_RoomMappingLoader, List[_Registration]] = {}
        for registration in self._registrations.values():
            loader = registration.mapping_loader
            if loader is None or registration.mapping_resolved:
                continue
            if now - registration.last_mapping_at < self._fallback_cooldown_seconds:
                continue
            registration.last_mapping_at = now
            groups.setdefault(loader, []).append(registration)

        for loader, registrations in groups.items():
            requested_room_ids = tuple(
                dict.fromkeys(item.requested_room_id for item in registrations)
            )
            try:
                mappings = await loader(requested_room_ids)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                submit_exception(exc)
                continue

            updates: List[Tuple[_Registration, int, int]] = []
            for registration in registrations:
                if not any(
                    item is registration for item in self._registrations.values()
                ):
                    continue
                mapping = mappings.get(registration.requested_room_id)
                if mapping is None:
                    continue
                real_room_id, uid = mapping
                if (
                    isinstance(real_room_id, bool)
                    or not isinstance(real_room_id, int)
                    or real_room_id <= 0
                    or isinstance(uid, bool)
                    or not isinstance(uid, int)
                    or uid <= 0
                ):
                    continue
                updates.append((registration, real_room_id, uid))

            if updates:
                self._apply_mapping_updates(updates)

    def _apply_mapping_updates(
        self, updates: Sequence[Tuple[_Registration, int, int]]
    ) -> None:
        for registration, real_room_id, uid in updates:
            if (
                self._registrations.get(registration.registration_key)
                is not registration
            ):
                continue
            registration.uid = uid
            registration.room_id = real_room_id
            registration.mapping_resolved = True

    async def _poll_uids(self, uids: Sequence[int], forced: bool = False) -> bool:
        if not uids:
            return True
        now = self._clock()
        if self._breaker.state is BreakerState.PAUSED:
            return False
        if self._breaker.state is BreakerState.OPEN:
            if not self._breaker.allow_canary(now):
                return False

        recovery_limit = self._breaker.recovery_limit(self._batch_size)
        if recovery_limit is not None:
            batches = [list(uids[:recovery_limit])]
        elif forced:
            batches = [list(uids)]
        else:
            batches = [
                list(uids[index : index + self._batch_size])
                for index in range(0, len(uids), self._batch_size)
            ]

        failure_reason: Optional[str] = None
        for batch in batches:
            try:
                result = await self._client.fetch(batch, observed_at=self._clock())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure_reason = self._failure_reason(exc)
                break

            large_missing = await self._apply_batch_result(batch, result)
            if large_missing and failure_reason is None:
                failure_reason = (
                    'batch response missing more than half of requested UIDs'
                )
            if large_missing and recovery_limit is not None:
                break

        if failure_reason is not None:
            self._breaker.record_failure(failure_reason)
            return False

        self._breaker.record_success(self._batch_size)
        self._last_success_at = self._clock()
        return True

    async def _apply_batch_result(
        self, batch: Sequence[int], result: BatchStatusResult
    ) -> bool:
        requested = set(batch)
        missing = requested - set(result.snapshots)
        missing.update(requested & set(result.missing_uids))
        self._missing_results += len(missing)

        registrations = list(self._registrations.values())
        for uid in batch:
            if uid in missing:
                continue
            snapshot = result.snapshots.get(uid)
            if snapshot is None:
                continue
            for registration in registrations:
                if registration.uid != uid:
                    continue
                if (
                    self._registrations.get(registration.registration_key)
                    is not registration
                ):
                    continue
                await self._apply_snapshot(registration, snapshot)
        return len(missing) > len(batch) / 2

    async def _apply_snapshot(
        self, registration: _Registration, snapshot: StatusSnapshot
    ) -> None:
        if snapshot.status in (ObservedStatus.UNKNOWN, ObservedStatus.STALE):
            return
        if snapshot.status is ObservedStatus.LIVE:
            previous = self._registration_state(registration)
            registration.negative_count = 0
            registration.wss_negative = False
            same_broadcast = registration.current is ObservedStatus.LIVE and (
                snapshot.observation_key is None
                or registration.observation_key == snapshot.observation_key
            )
            if same_broadcast:
                return
            confirmed = await self._confirm(registration)
            if (
                self._registrations.get(registration.registration_key)
                is not registration
            ):
                self._restore_registration_state(registration, previous)
                return
            if confirmed.status is not ObservedStatus.LIVE:
                return
            registration.current = ObservedStatus.LIVE
            registration.observation_key = (
                confirmed.observation_key
                or snapshot.observation_key
                or '{}:local:{}'.format(registration.uid, int(self._clock()))
            )
            await self._notify(registration, confirmed, previous)
            return

        if registration.current is not ObservedStatus.LIVE:
            registration.current = snapshot.status
            return
        previous = self._registration_state(registration)
        registration.negative_count += 1
        confirmed_offline = (
            registration.wss_negative or registration.negative_count >= 2
        )
        if not confirmed_offline:
            return
        registration.current = snapshot.status
        registration.negative_count = 0
        registration.wss_negative = False
        await self._notify(registration, snapshot, previous)

    async def _notify(
        self,
        registration: _Registration,
        snapshot: StatusSnapshot,
        previous: _RegistrationState,
    ) -> None:
        try:
            await registration.listener(snapshot)
        except asyncio.CancelledError:
            self._restore_registration_state(registration, previous)
            raise
        except Exception as exc:
            self._restore_registration_state(registration, previous)
            submit_exception(exc)

    async def _confirm(self, registration: _Registration) -> StatusSnapshot:
        now = self._clock()
        entry = self._fallback_tasks.get(registration.room_id)
        generation = (
            entry.generations.get(registration.registration_key)
            if entry is not None
            else None
        )
        expired = (
            entry is not None
            and entry.task.done()
            and now - entry.started_at >= self._fallback_cooldown_seconds
        )
        if (
            entry is None
            or expired
            or generation is None
            or generation is not registration
        ):
            task = asyncio.create_task(self._call_confirmer(registration))
            entry = _FallbackEntry(
                task=task,
                started_at=now,
                generations={
                    item.registration_key: item
                    for item in self._registrations.values()
                    if item.room_id == registration.room_id
                },
                consumed={},
            )
            entry.generations[registration.registration_key] = registration
            self._fallback_tasks[registration.room_id] = entry
            self._fallback_count += 1
        elif (
            entry.task.done()
            and entry.consumed.get(registration.registration_key) is registration
        ):
            return self._unknown_snapshot(registration)
        try:
            snapshot = await entry.task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark_fallback_consumed(entry, registration)
            submit_exception(exc)
            return self._unknown_snapshot(registration)
        self._mark_fallback_consumed(entry, registration)
        return snapshot

    def _mark_fallback_consumed(
        self, entry: _FallbackEntry, registration: _Registration
    ) -> None:
        registration_key = registration.registration_key
        if (
            self._registrations.get(registration_key) is registration
            and entry.generations.get(registration_key) is registration
        ):
            entry.consumed[registration_key] = registration

    @staticmethod
    async def _call_confirmer(registration: _Registration) -> StatusSnapshot:
        if registration.confirmer_uses_room_id:
            room_confirmer = cast(_RoomStatusConfirmer, registration.confirmer)
            return await room_confirmer(registration.room_id)
        status_confirmer = cast(StatusConfirmer, registration.confirmer)
        return await status_confirmer()

    def _unknown_snapshot(self, registration: _Registration) -> StatusSnapshot:
        return StatusSnapshot(
            uid=registration.uid,
            room_id=registration.room_id,
            status=ObservedStatus.UNKNOWN,
            observed_at=self._clock(),
            source=StatusSource.CONFIRMATION,
            live_time=0,
            observation_key=None,
        )

    @staticmethod
    def _registration_state(registration: _Registration) -> _RegistrationState:
        return (
            registration.current,
            registration.observation_key,
            registration.negative_count,
            registration.wss_negative,
        )

    @staticmethod
    def _restore_registration_state(
        registration: _Registration, previous: _RegistrationState
    ) -> None:
        (
            registration.current,
            registration.observation_key,
            registration.negative_count,
            registration.wss_negative,
        ) = previous

    @staticmethod
    def _failure_reason(exc: Exception) -> str:
        if isinstance(exc, BatchApiError):
            return 'Bilibili API error {}'.format(exc.code)
        if isinstance(exc, asyncio.TimeoutError):
            return 'timeout'
        return str(exc) or type(exc).__name__
