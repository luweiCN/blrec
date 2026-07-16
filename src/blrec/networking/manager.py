from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass, replace
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
    role: Literal['primary', 'fallback', 'system']


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


@dataclass
class _RouteHealth:
    primary_failures: int = 0
    fallback_failures: int = 0
    fallback_until: float = 0.0


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
        self._health: Dict[NetworkPurpose, _RouteHealth] = {}
        self._probes: Dict[str, NetworkProbe] = {}
        self._settings_persister = settings_persister

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
            selection = self.select(purpose)
            states.append(
                NetworkNotificationState(
                    event='network_failover',
                    object_key='network-route:{}:failover'.format(purpose),
                    healthy=selection.role != 'fallback',
                    title=(
                        '网络路由已恢复主线路'
                        if selection.role != 'fallback'
                        else '网络路由已切换备用线路'
                    ),
                    detail='{} 当前使用 {}'.format(
                        purpose, selection.interface_name or '系统默认网络'
                    ),
                )
            )
            states.append(
                NetworkNotificationState(
                    event='network_unavailable',
                    object_key='network-route:{}:unavailable'.format(purpose),
                    healthy=selection.role != 'system',
                    title=(
                        '网络路由已恢复'
                        if selection.role != 'system'
                        else '网络路由不可用'
                    ),
                    detail='{} 当前使用 {}'.format(
                        purpose, selection.interface_name or '系统默认网络'
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
            self._probes[interface_name] = await self._probe_interface(interface)
            return
        results = await asyncio.gather(
            *(self._probe_interface(interface) for interface in interfaces.values())
        )
        self._probes.update(
            (interface.name, result)
            for interface, result in zip(interfaces.values(), results)
        )

    def select(self, purpose: NetworkPurpose) -> RouteSelection:
        route = self._route_settings(purpose)
        interfaces = self.interfaces()
        health = self._health.setdefault(purpose, _RouteHealth())
        now = self._clock()
        primary = interfaces.get(route.interface or '')
        fallback = next(
            (
                interface
                for name, interface in interfaces.items()
                if name != route.interface and interface.enabled
            ),
            None,
        )

        if health.fallback_until and now >= health.fallback_until:
            health.primary_failures = 0
            health.fallback_failures = 0
            health.fallback_until = 0.0

        if primary is None and route.interface is not None:
            return self._fallback_or_system(purpose, fallback)

        if (
            route.failover_enabled
            and fallback is not None
            and health.fallback_until > now
        ):
            if health.fallback_failures < self._failure_threshold:
                return self._selection(purpose, fallback, 'fallback')
            return self._system_selection(purpose)

        if primary is not None and primary.enabled:
            return self._selection(purpose, primary, 'primary')
        return self._system_selection(purpose)

    def report_failure(
        self, purpose: NetworkPurpose, interface_name: Optional[str]
    ) -> None:
        route = self._route_settings(purpose)
        health = self._health.setdefault(purpose, _RouteHealth())
        if interface_name == route.interface:
            health.primary_failures += 1
            if (
                route.failover_enabled
                and any(
                    name != route.interface and interface.enabled
                    for name, interface in self.interfaces().items()
                )
                and health.primary_failures >= self._failure_threshold
            ):
                health.fallback_until = self._clock() + self._fallback_cooldown_seconds
        elif interface_name is not None and interface_name != route.interface:
            health.fallback_failures += 1

    def report_success(
        self, purpose: NetworkPurpose, interface_name: Optional[str]
    ) -> None:
        route = self._route_settings(purpose)
        health = self._health.setdefault(purpose, _RouteHealth())
        if interface_name == route.interface:
            health.primary_failures = 0
        elif interface_name is not None and interface_name != route.interface:
            health.fallback_failures = 0

    async def _probe_interface(self, interface: NetworkInterface) -> NetworkProbe:
        started_at = self._clock()
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET, local_addr=(interface.address, 0)
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
                    family=socket.AF_INET, local_addr=(interface.address, 0)
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
        role: Literal['primary', 'fallback'],
    ) -> RouteSelection:
        return RouteSelection(
            purpose=purpose,
            interface_name=interface.name,
            source_address=interface.address,
            role=role,
        )

    def _fallback_or_system(
        self, purpose: NetworkPurpose, fallback: Optional[NetworkInterface]
    ) -> RouteSelection:
        if fallback is not None:
            return self._selection(purpose, fallback, 'fallback')
        return self._system_selection(purpose)

    @staticmethod
    def _system_selection(purpose: NetworkPurpose) -> RouteSelection:
        return RouteSelection(
            purpose=purpose, interface_name=None, source_address=None, role='system'
        )
