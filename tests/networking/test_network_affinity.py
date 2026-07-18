import asyncio
from typing import Any, Dict, List, Tuple

import pytest
import requests
from pydantic import ValidationError

from blrec.networking.aiohttp_session import is_route_transport_failure
from blrec.networking.manager import (
    NetworkInterface,
    NetworkRouteManager,
    NetworkUnavailable,
)
from blrec.networking.requests_session import is_route_connection_failure
from blrec.setting.models import NetworkSettings


def _interfaces() -> Dict[str, NetworkInterface]:
    return {
        name: NetworkInterface(
            name=name,
            address='192.168.{}.24'.format(index),
            netmask='255.255.255.0',
            gateway='192.168.{}.1'.format(index),
            is_up=True,
            speed_mbps=1000,
            is_default=index == 1,
        )
        for index, name in enumerate(('eth0', 'eth1'), 1)
    }


def test_round_robin_rotates_anonymous_requests() -> None:
    settings = NetworkSettings(room_status={'mode': 'round_robin'})
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)

    assert manager.select('room_status', anonymous=True).interface_name == 'eth0'
    assert manager.select('room_status', anonymous=True).interface_name == 'eth1'
    assert manager.select('room_status', anonymous=True).interface_name == 'eth0'


def test_round_robin_affinity_stays_on_one_interface_until_released() -> None:
    settings = NetworkSettings(recording={'mode': 'round_robin'})
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)

    first = manager.select('recording', anonymous=True, affinity_key='live:1')
    again = manager.select('recording', anonymous=True, affinity_key='live:1')
    manager.release_affinity('recording', 'live:1')
    next_live = manager.select('recording', anonymous=True, affinity_key='live:2')

    assert first.interface_name == again.interface_name == 'eth0'
    assert next_live.interface_name == 'eth1'


def test_disabled_interfaces_are_not_round_robin_candidates() -> None:
    settings = NetworkSettings(
        room_status={'mode': 'round_robin'}, interfaces={'eth0': {'enabled': False}}
    )
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)

    assert manager.select('room_status', anonymous=True).interface_name == 'eth1'
    assert manager.select('room_status', anonymous=True).interface_name == 'eth1'


def test_fixed_route_fails_over_only_after_two_connection_failures() -> None:
    settings = NetworkSettings(
        recording={'mode': 'fixed', 'interface': 'eth0', 'failoverEnabled': True}
    )
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)

    manager.report_failure('recording', 'eth0')
    assert manager.select('recording').interface_name == 'eth0'
    manager.report_failure('recording', 'eth0')
    assert manager.select('recording').interface_name == 'eth1'


def test_failover_and_recovery_emit_safe_route_audit_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.networking.manager.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    settings = NetworkSettings(
        recording={'mode': 'fixed', 'interface': 'eth0', 'failoverEnabled': True}
    )
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)

    manager.report_failure('recording', 'eth0')
    manager.report_failure('recording', 'eth0')
    fallback = manager.select('recording')
    manager.report_success('recording', 'eth0')

    assert fallback.role == 'fallback'
    assert (
        'network_route_unhealthy',
        {
            'level': 'WARNING',
            'purpose': 'recording',
            'interface': 'eth0',
            'failures': 2,
        },
    ) in events
    assert any(
        event == 'network_route_selected'
        and fields['interface'] == 'eth1'
        and fields['role'] == 'fallback'
        for event, fields in events
    )
    assert any(event == 'network_route_recovered' for event, _fields in events)


def test_upload_never_fails_over_to_another_interface() -> None:
    settings = NetworkSettings(
        upload={'mode': 'fixed', 'interface': 'eth0', 'failoverEnabled': True}
    )
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)
    manager.report_failure('upload', 'eth0')
    manager.report_failure('upload', 'eth0')

    with pytest.raises(NetworkUnavailable):
        manager.select('upload')


def test_account_and_upload_routes_reject_round_robin_configuration() -> None:
    with pytest.raises(ValidationError):
        NetworkSettings(upload={'mode': 'round_robin'})
    with pytest.raises(ValidationError):
        NetworkSettings(bili_api={'mode': 'round_robin'})


def test_business_http_error_does_not_change_route_health() -> None:
    settings = NetworkSettings(
        room_status={'interface': 'eth0', 'failoverEnabled': True}
    )
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)

    manager.report_http_result('room_status', 'eth0', 412)
    manager.report_http_result('room_status', 'eth0', 500)

    assert manager.select('room_status').interface_name == 'eth0'


def test_remote_read_timeout_does_not_mark_a_network_interface_unhealthy() -> None:
    assert is_route_connection_failure(requests.ReadTimeout()) is False
    assert is_route_transport_failure(asyncio.TimeoutError()) is False


def test_connection_setup_failure_is_treated_as_a_route_failure() -> None:
    assert is_route_connection_failure(requests.ConnectionError()) is True
