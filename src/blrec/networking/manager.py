from __future__ import annotations

import asyncio
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Tuple,
)

import aiohttp
import psutil

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
class NetworkInterface:
    name: str
    address: str
    netmask: Optional[str]
    gateway: Optional[str]
    is_up: bool
    speed_mbps: int
    is_default: bool


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


def _linux_gateways() -> Dict[str, str]:
    gateways: Dict[str, str] = {}
    try:
        with open('/proc/net/route', 'rt', encoding='ascii') as route_file:
            next(route_file, None)
            for line in route_file:
                fields = line.split()
                if len(fields) < 4 or fields[1] != '00000000':
                    continue
                flags = int(fields[3], 16)
                if not flags & 0x2:
                    continue
                gateways[fields[0]] = socket.inet_ntoa(
                    struct.pack('<L', int(fields[2], 16))
                )
    except (OSError, ValueError):
        pass
    return gateways


def _bsd_gateways() -> Dict[str, str]:
    gateways: Dict[str, str] = {}
    try:
        result = subprocess.run(
            ['netstat', '-rn', '-f', 'inet'],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return gateways
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] != 'default':
            continue
        gateways[fields[-1]] = fields[1]
    return gateways


def discover_interfaces() -> Dict[str, NetworkInterface]:
    gateways = _linux_gateways() or _bsd_gateways()
    stats = psutil.net_if_stats()
    result: Dict[str, NetworkInterface] = {}
    for name, addresses in psutil.net_if_addrs().items():
        stat = stats.get(name)
        if stat is None or not stat.isup:
            continue
        ipv4 = next(
            (
                address
                for address in addresses
                if address.family == socket.AF_INET
                and not address.address.startswith('127.')
            ),
            None,
        )
        if ipv4 is None:
            continue
        result[name] = NetworkInterface(
            name=name,
            address=ipv4.address,
            netmask=ipv4.netmask,
            gateway=gateways.get(name),
            is_up=True,
            speed_mbps=max(stat.speed, 0),
            is_default=name in gateways,
        )
    return result


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
    ) -> None:
        self._settings_provider = settings_provider
        self._interface_provider = interface_provider
        self._clock = clock
        self._failure_threshold = failure_threshold
        self._fallback_cooldown_seconds = fallback_cooldown_seconds
        self._health: Dict[NetworkPurpose, _RouteHealth] = {}
        self._probes: Dict[str, NetworkProbe] = {}

    def interfaces(self) -> Dict[str, NetworkInterface]:
        return dict(self._interface_provider())

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
            if route.primary_interface is None and route.fallback_interface is None:
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
        primary = interfaces.get(route.primary_interface or '')
        fallback = interfaces.get(route.fallback_interface or '')

        if health.fallback_until and now >= health.fallback_until:
            health.primary_failures = 0
            health.fallback_failures = 0
            health.fallback_until = 0.0

        if primary is None and route.primary_interface is not None:
            return self._fallback_or_system(purpose, fallback)

        if (
            route.failover_enabled
            and fallback is not None
            and health.fallback_until > now
        ):
            if health.fallback_failures < self._failure_threshold:
                return self._selection(purpose, fallback, 'fallback')
            return self._system_selection(purpose)

        if primary is not None:
            return self._selection(purpose, primary, 'primary')
        return self._system_selection(purpose)

    def report_failure(
        self, purpose: NetworkPurpose, interface_name: Optional[str]
    ) -> None:
        route = self._route_settings(purpose)
        health = self._health.setdefault(purpose, _RouteHealth())
        if interface_name == route.primary_interface:
            health.primary_failures += 1
            if (
                route.failover_enabled
                and route.fallback_interface
                and health.primary_failures >= self._failure_threshold
            ):
                health.fallback_until = self._clock() + self._fallback_cooldown_seconds
        elif interface_name == route.fallback_interface:
            health.fallback_failures += 1

    def report_success(
        self, purpose: NetworkPurpose, interface_name: Optional[str]
    ) -> None:
        route = self._route_settings(purpose)
        health = self._health.setdefault(purpose, _RouteHealth())
        if interface_name == route.primary_interface:
            health.primary_failures = 0
        elif interface_name == route.fallback_interface:
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
