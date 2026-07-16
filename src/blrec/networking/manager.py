from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass, replace
from threading import RLock
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Tuple,
)

import aiohttp

from .platform import NetworkInterface, discover_interfaces
from .resolver import SourceBoundResolver

if TYPE_CHECKING:
    from blrec.setting.models import NetworkRouteSettings, NetworkSettings

NetworkPurpose = Literal['room_status', 'danmaku', 'recording', 'upload', 'bili_api']

_PROBE_HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/136.0.0.0 Safari/537.36'
    ),
}


@dataclass(frozen=True)
class RouteSelection:
    purpose: NetworkPurpose
    interface_name: Optional[str]
    source_address: Optional[str]
    role: Literal['primary', 'fallback', 'round_robin', 'system']


@dataclass(frozen=True)
class NetworkProbe:
    reachable: bool
    latency_ms: Optional[int]
    external_ip: Optional[str]
    error: Optional[str]
    checked_at: float


@dataclass(frozen=True)
class NetworkNotificationState:
    event: Literal['network_unavailable', 'network_failover']
    object_key: str
    healthy: bool
    title: str
    detail: str


class NetworkUnavailable(RuntimeError):
    pass


class NetworkRouteManager:
    def __init__(
        self,
        settings_provider: Callable[[], 'NetworkSettings'],
        *,
        interface_provider: Callable[
            [], Mapping[str, NetworkInterface]
        ] = discover_interfaces,
        clock: Callable[[], float] = time.monotonic,
        failure_threshold: int = 2,
        fallback_cooldown_seconds: int = 60,
        settings_persister: Optional[
            Callable[['NetworkSettings'], Awaitable[None]]
        ] = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._interface_provider = interface_provider
        self._clock = clock
        self._failure_threshold = failure_threshold
        self._fallback_cooldown_seconds = fallback_cooldown_seconds
        self._probes: Dict[str, NetworkProbe] = {}
        self._settings_persister = settings_persister
        self._failures: Dict[Tuple[NetworkPurpose, str], int] = {}
        self._unavailable_since: Dict[Tuple[NetworkPurpose, str], float] = {}
        self._round_robin_cursors: Dict[NetworkPurpose, int] = {}
        self._affinities: Dict[Tuple[NetworkPurpose, str], str] = {}
        self._lock = RLock()

    def interfaces(self) -> Dict[str, NetworkInterface]:
        configured = self._settings_provider().interfaces
        result: Dict[str, NetworkInterface] = {}
        for name, interface in self._interface_provider().items():
            settings = configured.get(name)
            if settings is None:
                result[name] = interface
            else:
                result[name] = replace(
                    interface,
                    enabled=settings.enabled,
                    upload_limit_bps=settings.upload_limit_bps,
                )
        return result

    def set_settings_persister(
        self, persister: Callable[['NetworkSettings'], Awaitable[None]]
    ) -> None:
        self._settings_persister = persister

    async def update_interface(
        self,
        interface_name: str,
        *,
        enabled: Optional[bool] = None,
        upload_limit_bps: Optional[int] = None,
    ) -> None:
        interfaces = self.interfaces()
        interface = interfaces.get(interface_name)
        if interface is None:
            raise KeyError(interface_name)
        from blrec.setting.models import NetworkInterfaceSettings

        current = self._settings_provider()
        updated = current.copy(deep=True)
        item = updated.interfaces.get(
            interface_name,
            NetworkInterfaceSettings(
                enabled=interface.enabled, upload_limit_bps=interface.upload_limit_bps
            ),
        )
        if enabled is not None:
            item.enabled = enabled
        if upload_limit_bps is not None:
            item.upload_limit_bps = upload_limit_bps
        updated.interfaces[interface_name] = item
        if self._settings_persister is not None:
            await self._settings_persister(updated)
        else:
            current.interfaces = updated.interfaces

    def cached_probes(self) -> Dict[str, NetworkProbe]:
        return dict(self._probes)

    def interface(self, interface_name: Optional[str]) -> Optional[NetworkInterface]:
        if interface_name is None:
            return None
        return self.interfaces().get(interface_name)

    def notification_states(self) -> List[NetworkNotificationState]:
        states: List[NetworkNotificationState] = []
        for interface_name, probe in self._probes.items():
            states.append(
                NetworkNotificationState(
                    event='network_unavailable',
                    object_key='network-interface:{}'.format(interface_name),
                    healthy=probe.reachable,
                    title=('网络接口已恢复' if probe.reachable else '网络接口不可用'),
                    detail='{}：{}'.format(
                        interface_name,
                        (
                            '{} ms'.format(probe.latency_ms)
                            if probe.reachable
                            else probe.error or '探测失败'
                        ),
                    ),
                )
            )
        purposes: Tuple[NetworkPurpose, ...] = (
            'room_status',
            'danmaku',
            'recording',
            'upload',
            'bili_api',
        )
        for purpose in purposes:
            route = self._route_settings(purpose)
            if route.interface is None:
                continue
            selection: Optional[RouteSelection]
            try:
                selection = self.select(purpose)
            except NetworkUnavailable:
                selection = None
            states.append(
                NetworkNotificationState(
                    event='network_failover',
                    object_key='network-route:{}:failover'.format(purpose),
                    healthy=selection is not None and selection.role != 'fallback',
                    title=(
                        '网络路由已恢复主线路'
                        if selection is not None and selection.role != 'fallback'
                        else '网络路由已切换备用线路'
                    ),
                    detail='{} 当前使用 {}'.format(
                        purpose,
                        (
                            selection.interface_name
                            if selection is not None
                            else '无可用线路'
                        )
                        or '系统默认网络',
                    ),
                )
            )
            states.append(
                NetworkNotificationState(
                    event='network_unavailable',
                    object_key='network-route:{}:unavailable'.format(purpose),
                    healthy=selection is not None and selection.role != 'system',
                    title=(
                        '网络路由已恢复'
                        if selection is not None and selection.role != 'system'
                        else '网络路由不可用'
                    ),
                    detail='{} 当前使用 {}'.format(
                        purpose,
                        (
                            selection.interface_name
                            if selection is not None
                            else '无可用线路'
                        )
                        or '系统默认网络',
                    ),
                )
            )
        return states

    async def probe(self, interface_name: Optional[str] = None) -> None:
        interfaces = self.interfaces()
        if interface_name is not None:
            interface = interfaces.get(interface_name)
            if interface is None:
                raise KeyError(interface_name)
            result = await self._probe_interface(interface)
            self._probes[interface_name] = result
            self._report_probe_result(interface_name, result)
            return
        results = await asyncio.gather(
            *(self._probe_interface(interface) for interface in interfaces.values())
        )
        self._probes.update(
            (interface.name, result)
            for interface, result in zip(interfaces.values(), results)
        )
        for interface, result in zip(interfaces.values(), results):
            self._report_probe_result(interface.name, result)

    def _report_probe_result(self, interface_name: str, result: NetworkProbe) -> None:
        purposes: Tuple[NetworkPurpose, ...] = (
            'room_status',
            'danmaku',
            'recording',
            'upload',
            'bili_api',
        )
        for purpose in purposes:
            if result.reachable:
                self.report_success(purpose, interface_name)
            else:
                self.report_failure(purpose, interface_name)

    def select(
        self,
        purpose: NetworkPurpose,
        *,
        affinity_key: Optional[str] = None,
        anonymous: bool = False,
    ) -> RouteSelection:
        route = self._route_settings(purpose)
        with self._lock:
            interfaces = self.interfaces()
            candidates = [
                interface
                for name, interface in sorted(interfaces.items())
                if interface.enabled and self._is_healthy(purpose, name)
            ]
            affinity = (purpose, affinity_key) if affinity_key is not None else None
            if affinity is not None:
                name = self._affinities.get(affinity)
                if name is not None:
                    interface = interfaces.get(name)
                    if (
                        interface is not None
                        and interface.enabled
                        and (
                            self._is_healthy(purpose, name)
                            or not route.failover_enabled
                            or purpose == 'upload'
                        )
                    ):
                        return self._selection(purpose, interface, 'primary')
                    self._affinities.pop(affinity, None)

            if route.mode == 'round_robin' and anonymous:
                if not candidates:
                    raise NetworkUnavailable(
                        'No enabled and healthy interface for {}'.format(purpose)
                    )
                cursor = self._round_robin_cursors.get(purpose, 0)
                interface = candidates[cursor % len(candidates)]
                self._round_robin_cursors[purpose] = cursor + 1
                if affinity is not None:
                    self._affinities[affinity] = interface.name
                return self._selection(purpose, interface, 'round_robin')

            configured = interfaces.get(route.interface or '')
            if route.interface is None:
                return self._system_selection(purpose)
            if (
                configured is not None
                and configured.enabled
                and self._is_healthy(purpose, configured.name)
            ):
                if affinity is not None:
                    self._affinities[affinity] = configured.name
                return self._selection(purpose, configured, 'primary')

            allow_failover = route.failover_enabled and purpose != 'upload'
            if allow_failover:
                fallback = next(
                    (
                        interface
                        for interface in candidates
                        if interface.name != route.interface
                    ),
                    None,
                )
                if fallback is not None:
                    if affinity is not None:
                        self._affinities[affinity] = fallback.name
                    return self._selection(purpose, fallback, 'fallback')
            raise NetworkUnavailable(
                'Configured network interface is unavailable for {}'.format(purpose)
            )

    def release_affinity(self, purpose: NetworkPurpose, affinity_key: str) -> None:
        with self._lock:
            self._affinities.pop((purpose, affinity_key), None)

    def report_failure(
        self, purpose: NetworkPurpose, interface_name: Optional[str]
    ) -> None:
        if interface_name is None:
            return
        key = (purpose, interface_name)
        with self._lock:
            failures = self._failures.get(key, 0) + 1
            self._failures[key] = failures
            if failures >= self._failure_threshold:
                self._unavailable_since[key] = self._clock()

    def report_success(
        self, purpose: NetworkPurpose, interface_name: Optional[str]
    ) -> None:
        if interface_name is None:
            return
        key = (purpose, interface_name)
        with self._lock:
            self._failures.pop(key, None)
            self._unavailable_since.pop(key, None)

    def report_http_result(
        self, purpose: NetworkPurpose, interface_name: Optional[str], status: int
    ) -> None:
        # HTTP status codes are application results, not evidence that a route failed.
        return None

    def _is_healthy(self, purpose: NetworkPurpose, interface_name: str) -> bool:
        key = (purpose, interface_name)
        unavailable_at = self._unavailable_since.get(key)
        if unavailable_at is None:
            return True
        if self._clock() - unavailable_at >= self._fallback_cooldown_seconds:
            self._unavailable_since.pop(key, None)
            self._failures.pop(key, None)
            return True
        return False

    async def _probe_interface(self, interface: NetworkInterface) -> NetworkProbe:
        started_at = self._clock()
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            local_addr=(interface.address, 0),
            resolver=SourceBoundResolver(interface),
        )
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=8),
                trust_env=False,
                headers=_PROBE_HEADERS,
            ) as session:
                async with session.get(
                    'https://api.bilibili.com/x/web-interface/nav',
                    allow_redirects=False,
                ) as response:
                    response.raise_for_status()
                    await response.read()
                latency_ms = max(0, round((self._clock() - started_at) * 1000))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            return NetworkProbe(
                reachable=False,
                latency_ms=None,
                external_ip=None,
                error=type(error).__name__,
                checked_at=time.time(),
            )
        external_ip: Optional[str] = None
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    family=socket.AF_INET,
                    local_addr=(interface.address, 0),
                    resolver=SourceBoundResolver(interface),
                ),
                timeout=aiohttp.ClientTimeout(total=8),
                trust_env=False,
                headers=_PROBE_HEADERS,
            ) as session:
                async with session.get(
                    'https://api.ipify.org?format=json', allow_redirects=False
                ) as response:
                    response.raise_for_status()
                    payload = await response.json()
                    value = payload.get('ip')
                    if isinstance(value, str):
                        external_ip = value
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            pass
        return NetworkProbe(
            reachable=True,
            latency_ms=latency_ms,
            external_ip=external_ip,
            error=None,
            checked_at=time.time(),
        )

    def _route_settings(self, purpose: NetworkPurpose) -> 'NetworkRouteSettings':
        return getattr(self._settings_provider(), purpose)

    @staticmethod
    def _selection(
        purpose: NetworkPurpose,
        interface: NetworkInterface,
        role: Literal['primary', 'fallback', 'round_robin'],
    ) -> RouteSelection:
        return RouteSelection(
            purpose=purpose,
            interface_name=interface.name,
            source_address=interface.address,
            role=role,
        )

    @staticmethod
    def _system_selection(purpose: NetworkPurpose) -> RouteSelection:
        return RouteSelection(
            purpose=purpose, interface_name=None, source_address=None, role='system'
        )
