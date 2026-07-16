import socket
from typing import List, Tuple

import pytest

from blrec.networking.manager import NetworkInterface
from blrec.networking.resolver import SourceBoundResolver


@pytest.mark.asyncio
async def test_dns_query_binds_source_and_prefers_interface_gateway() -> None:
    calls: List[Tuple[str, str, str]] = []

    async def query(server: str, host: str, source: str) -> List[str]:
        calls.append((server, host, source))
        return ['203.0.113.7']

    interface = NetworkInterface(
        name='ovs_eth0',
        address='192.168.1.24',
        netmask='255.255.255.0',
        gateway='192.168.1.1',
        is_up=True,
        speed_mbps=1000,
        is_default=False,
        dns_servers=('8.8.8.8',),
    )
    resolver = SourceBoundResolver(interface, query=query)

    result = await resolver.resolve('api.bilibili.com', 443, socket.AF_INET)

    assert calls == [('192.168.1.1', 'api.bilibili.com', '192.168.1.24')]
    assert result == [
        {
            'hostname': 'api.bilibili.com',
            'host': '203.0.113.7',
            'port': 443,
            'family': socket.AF_INET,
            'proto': 0,
            'flags': 0,
        }
    ]


@pytest.mark.asyncio
async def test_dns_falls_back_to_system_nameserver_bound_to_same_source() -> None:
    calls: List[str] = []

    async def query(server: str, host: str, source: str) -> List[str]:
        calls.append(server)
        if server == '192.168.1.1':
            raise OSError('gateway does not serve DNS')
        return ['203.0.113.8']

    interface = NetworkInterface(
        name='eth0',
        address='192.168.1.24',
        netmask=None,
        gateway='192.168.1.1',
        is_up=True,
        speed_mbps=1000,
        is_default=False,
        dns_servers=('8.8.8.8',),
    )

    result = await SourceBoundResolver(interface, query=query).resolve(
        'member.bilibili.com', 443, socket.AF_INET
    )

    assert calls == ['192.168.1.1', '8.8.8.8']
    assert result[0]['host'] == '203.0.113.8'


@pytest.mark.asyncio
async def test_ip_literal_skips_dns() -> None:
    async def query(server: str, host: str, source: str) -> List[str]:
        raise AssertionError('DNS must not be called for an IP literal')

    resolver = SourceBoundResolver(None, query=query)

    result = await resolver.resolve('203.0.113.9', 80, socket.AF_INET)

    assert result[0]['host'] == '203.0.113.9'
