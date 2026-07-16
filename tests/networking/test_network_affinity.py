from typing import Dict

import pytest
from pydantic import ValidationError

from blrec.networking.manager import (
    NetworkInterface,
    NetworkRouteManager,
    NetworkUnavailable,
)
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
