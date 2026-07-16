from __future__ import annotations

import ipaddress
import json
import shutil
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import psutil


@dataclass(frozen=True)
class NetworkInterface:
    name: str
    address: str
    netmask: Optional[str]
    gateway: Optional[str]
    is_up: bool
    speed_mbps: int
    is_default: bool
    dns_servers: Tuple[str, ...] = ()
    kind: str = 'physical'
    enabled: bool = True
    upload_limit_bps: int = 0


def _run_ip_json(*args: str) -> List[Dict[str, Any]]:
    result = subprocess.run(
        ['ip', '-j', *args], check=False, capture_output=True, text=True, timeout=3
    )
    if result.returncode != 0:
        return []
    value = json.loads(result.stdout)
    return value if isinstance(value, list) else []


def _read_dns_servers() -> Tuple[str, ...]:
    result: List[str] = []
    try:
        with open('/etc/resolv.conf', 'rt', encoding='ascii') as file:
            for line in file:
                fields = line.split()
                if len(fields) != 2 or fields[0] != 'nameserver':
                    continue
                try:
                    address = ipaddress.ip_address(fields[1])
                except ValueError:
                    continue
                if address.version == 4 and fields[1] not in result:
                    result.append(fields[1])
    except (OSError, UnicodeError):
        pass
    return tuple(result)


def _interface_kind(name: str) -> str:
    lowered = name.lower()
    if lowered == 'docker0' or lowered.startswith(
        ('br-', 'veth', 'virbr', 'podman', 'cni')
    ):
        return 'bridge'
    if lowered.startswith(('tun', 'tap', 'wg')):
        return 'tunnel'
    return 'physical'


def _prefix_netmask(prefix_length: object) -> Optional[str]:
    if not isinstance(prefix_length, (int, str)):
        return None
    try:
        prefix = int(prefix_length)
        return str(ipaddress.IPv4Network(f'0.0.0.0/{prefix}').netmask)
    except (TypeError, ValueError):
        return None


def _parse_linux_interfaces(
    addresses: Sequence[Mapping[str, Any]],
    routes: Sequence[Mapping[str, Any]],
    *,
    stats: Mapping[str, Any],
    dns_servers: Tuple[str, ...],
) -> Dict[str, NetworkInterface]:
    gateways: Dict[str, str] = {}
    main_interfaces = set()
    for route in routes:
        if route.get('dst', 'default') != 'default':
            continue
        name = route.get('dev')
        gateway = route.get('gateway')
        if not isinstance(name, str) or not isinstance(gateway, str):
            continue
        gateways.setdefault(name, gateway)
        if route.get('table', 'main') in ('main', 254, '254'):
            main_interfaces.add(name)

    result: Dict[str, NetworkInterface] = {}
    for entry in addresses:
        name = entry.get('ifname')
        if not isinstance(name, str) or name == 'lo':
            continue
        ipv4 = next(
            (
                item
                for item in entry.get('addr_info', [])
                if item.get('family') == 'inet'
                and item.get('scope', 'global') == 'global'
                and not str(item.get('local', '')).startswith('127.')
            ),
            None,
        )
        if ipv4 is None:
            continue
        address = ipv4.get('local')
        if not isinstance(address, str):
            continue
        stat = stats.get(name)
        is_up = bool(stat is not None and stat.isup)
        if not is_up:
            continue
        gateway = gateways.get(name)
        kind = _interface_kind(name)
        result[name] = NetworkInterface(
            name=name,
            address=address,
            netmask=_prefix_netmask(ipv4.get('prefixlen')),
            gateway=gateway,
            is_up=True,
            speed_mbps=max(int(getattr(stat, 'speed', 0)), 0),
            is_default=name in main_interfaces,
            dns_servers=dns_servers,
            kind=kind,
            enabled=gateway is not None and kind != 'bridge',
        )
    return result


def _proc_gateways() -> Dict[str, str]:
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


def _fallback_interfaces() -> Dict[str, NetworkInterface]:
    gateways = _proc_gateways() if sys.platform.startswith('linux') else _bsd_gateways()
    stats = psutil.net_if_stats()
    dns_servers = _read_dns_servers()
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
        gateway = gateways.get(name)
        kind = _interface_kind(name)
        result[name] = NetworkInterface(
            name=name,
            address=ipv4.address,
            netmask=ipv4.netmask,
            gateway=gateway,
            is_up=True,
            speed_mbps=max(stat.speed, 0),
            is_default=name in gateways,
            dns_servers=dns_servers,
            kind=kind,
            enabled=gateway is not None and kind != 'bridge',
        )
    return result


def discover_interfaces() -> Dict[str, NetworkInterface]:
    if sys.platform.startswith('linux') and shutil.which('ip') is not None:
        try:
            parsed = _parse_linux_interfaces(
                _run_ip_json('-4', 'address', 'show'),
                _run_ip_json('-4', 'route', 'show', 'table', 'all'),
                stats=psutil.net_if_stats(),
                dns_servers=_read_dns_servers(),
            )
        except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
            parsed = {}
        if parsed:
            return parsed
    return _fallback_interfaces()
