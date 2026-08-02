from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any, List

import dns.exception
import dns.resolver

_source_address = os.environ.get('BLREC_SOURCE_ADDRESS', '').strip()
_dns_servers = tuple(
    dict.fromkeys(
        value.strip()
        for value in os.environ.get('BLREC_DNS_SERVERS', '').split(',')
        if value.strip()
    )
)
_original_getaddrinfo = socket.getaddrinfo


def _source_bound_getaddrinfo(
    host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
) -> List[Any]:
    if not isinstance(host, (str, bytes)) or family not in (0, socket.AF_INET):
        return _original_getaddrinfo(host, port, family, type, proto, flags)
    try:
        hostname = host.decode('ascii') if isinstance(host, bytes) else host
    except UnicodeDecodeError:
        return _original_getaddrinfo(host, port, family, type, proto, flags)
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return _original_getaddrinfo(host, port, family, type, proto, flags)

    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = list(_dns_servers)
    try:
        answer = resolver.resolve(
            hostname, 'A', source=_source_address, lifetime=5.0, raise_on_no_answer=True
        )
    except (dns.exception.DNSException, OSError) as error:
        raise socket.gaierror(
            socket.EAI_AGAIN, 'source-bound DNS resolution failed'
        ) from error

    result: List[Any] = []
    seen = set()
    for item in answer:
        address = str(getattr(item, 'address'))
        for resolved in _original_getaddrinfo(
            address, port, socket.AF_INET, type, proto, flags
        ):
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
    if result:
        return result
    raise socket.gaierror(socket.EAI_NONAME, 'source-bound DNS returned no address')


if _source_address and _dns_servers:
    socket.getaddrinfo = _source_bound_getaddrinfo
