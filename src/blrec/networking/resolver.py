from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

import aiohttp
import dns.asyncresolver
import dns.exception
import dns.resolver
from aiohttp.abc import AbstractResolver

from .platform import NetworkInterface

AsyncDnsQuery = Callable[[str, str, str], Awaitable[Sequence[str]]]
SyncDnsQuery = Callable[[str, str, str], Sequence[str]]


def _dns_servers(interface: NetworkInterface) -> List[str]:
    values: List[str] = []
    for value in (interface.gateway, *interface.dns_servers):
        if value is not None and value not in values:
            values.append(value)
    return values


async def _query_ipv4(server: str, host: str, source: str) -> Sequence[str]:
    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = [server]
    answer = await resolver.resolve(
        host, 'A', source=source, lifetime=3.0, raise_on_no_answer=True
    )
    return [str(getattr(item, 'address')) for item in answer]


def _query_ipv4_sync(server: str, host: str, source: str) -> Sequence[str]:
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = [server]
    answer = resolver.resolve(
        host, 'A', source=source, lifetime=3.0, raise_on_no_answer=True
    )
    return [str(getattr(item, 'address')) for item in answer]


def _literal(host: str) -> Optional[str]:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    return str(address) if address.version == 4 else None


def _aiohttp_result(hostname: str, host: str, port: int) -> Dict[str, Any]:
    return {
        'hostname': hostname,
        'host': host,
        'port': port,
        'family': socket.AF_INET,
        'proto': 0,
        'flags': 0,
    }


class SourceBoundResolver(AbstractResolver):
    def __init__(
        self,
        interface: Optional[NetworkInterface],
        *,
        query: AsyncDnsQuery = _query_ipv4,
    ) -> None:
        self._interface = interface
        self._query = query
        self._fallback: Optional[AbstractResolver] = None

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> List[Dict[str, Any]]:
        literal = _literal(host)
        if literal is not None:
            return [_aiohttp_result(host, literal, port)]
        if self._interface is None:
            if self._fallback is None:
                self._fallback = aiohttp.DefaultResolver()
            return await self._fallback.resolve(host, port, family)

        last_error: Optional[BaseException] = None
        for server in _dns_servers(self._interface):
            try:
                addresses = await self._query(server, host, self._interface.address)
            except (asyncio.TimeoutError, dns.exception.DNSException, OSError) as error:
                last_error = error
                continue
            unique = tuple(dict.fromkeys(addresses))
            if unique:
                return [_aiohttp_result(host, address, port) for address in unique]
        if last_error is not None:
            raise OSError('source-bound DNS resolution failed') from last_error
        raise OSError('no DNS server is available for interface')

    async def close(self) -> None:
        if self._fallback is not None:
            await self._fallback.close()


class SyncSourceBoundResolver:
    def __init__(
        self, interface: NetworkInterface, *, query: SyncDnsQuery = _query_ipv4_sync
    ) -> None:
        self._interface = interface
        self._query = query

    def resolve(self, host: str) -> Sequence[str]:
        literal = _literal(host)
        if literal is not None:
            return (literal,)
        last_error: Optional[BaseException] = None
        for server in _dns_servers(self._interface):
            try:
                addresses = self._query(server, host, self._interface.address)
            except (dns.exception.DNSException, OSError) as error:
                last_error = error
                continue
            unique = tuple(dict.fromkeys(addresses))
            if unique:
                return unique
        if last_error is not None:
            raise OSError('source-bound DNS resolution failed') from last_error
        raise OSError('no DNS server is available for interface')
