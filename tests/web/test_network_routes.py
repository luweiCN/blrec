from typing import Dict, List, Optional
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.networking.manager import NetworkInterface, NetworkProbe, NetworkRouteManager
from blrec.networking.traffic import TrafficMeter
from blrec.setting.models import NetworkSettings
from blrec.web.routers import network


class FakeNetworkManager:
    def __init__(self) -> None:
        self.probed: Optional[str] = None
        self.events: List[str] = []
        self.traffic_meter = TrafficMeter()
        self.traffic_meter.record('eth0', 'recording', 'down', 4096)
        self.traffic_meter.record('eth0', 'upload', 'up', 1024)
        self._interface = NetworkInterface(
            name='eth0',
            address='192.168.1.20',
            netmask='255.255.255.0',
            gateway='192.168.1.1',
            is_up=True,
            speed_mbps=1000,
            is_default=True,
            dns_servers=('192.168.1.1',),
            kind='physical',
            enabled=True,
            upload_limit_bps=0,
        )

    def interfaces(self) -> Dict[str, NetworkInterface]:
        return {'eth0': self._interface}

    def cached_probes(self) -> Dict[str, NetworkProbe]:
        return {}

    async def refresh_interfaces(self, force: bool = False) -> None:
        self.events.append('refresh:{}'.format(force))

    async def probe(self, interface_name: Optional[str] = None) -> None:
        self.probed = interface_name
        self.events.append('probe:{}'.format(interface_name))

    async def update_interface(
        self,
        interface_name: str,
        *,
        enabled: Optional[bool] = None,
        upload_limit_bps: Optional[int] = None,
    ) -> None:
        self.events.append('update:{}'.format(interface_name))
        if interface_name != self._interface.name:
            raise KeyError(interface_name)
        self._interface = NetworkInterface(
            **{
                **self._interface.__dict__,
                'enabled': (self._interface.enabled if enabled is None else enabled),
                'upload_limit_bps': (
                    self._interface.upload_limit_bps
                    if upload_limit_bps is None
                    else upload_limit_bps
                ),
            }
        )


def client(manager: FakeNetworkManager) -> TestClient:
    network.manager = manager  # type: ignore[assignment]
    app = FastAPI()
    app.include_router(network.router, prefix='/api/v1')
    return TestClient(app)


def test_lists_host_network_interfaces() -> None:
    manager = FakeNetworkManager()

    response = client(manager).get('/api/v1/network/interfaces')

    assert response.status_code == 200
    assert manager.events == ['refresh:False']
    assert response.json() == {
        'interfaces': [
            {
                'name': 'eth0',
                'address': '192.168.1.20',
                'netmask': '255.255.255.0',
                'gateway': '192.168.1.1',
                'isUp': True,
                'speedMbps': 1000,
                'isDefault': True,
                'dnsServers': ['192.168.1.1'],
                'kind': 'physical',
                'enabled': True,
                'uploadLimitBps': 0,
                'uploadBps': 0.0,
                'downloadBps': 0.0,
                'uploadTotal': 1024,
                'downloadTotal': 4096,
                'probe': None,
            }
        ]
    }


def test_probes_one_interface_before_returning_snapshot() -> None:
    manager = FakeNetworkManager()

    response = client(manager).post(
        '/api/v1/network/probe', json={'interfaceName': 'eth0'}
    )

    assert response.status_code == 200
    assert manager.probed == 'eth0'
    assert manager.events == ['refresh:False', 'probe:eth0']


def test_updates_interface_settings_inline() -> None:
    manager = FakeNetworkManager()

    response = client(manager).patch(
        '/api/v1/network/interfaces/eth0',
        json={'enabled': False, 'uploadLimitBps': 1048576},
    )

    assert response.status_code == 200
    item = response.json()['interfaces'][0]
    assert item['enabled'] is False
    assert item['uploadLimitBps'] == 1048576
    assert manager.events == ['update:eth0', 'refresh:True']


def test_failed_interface_update_does_not_refresh() -> None:
    manager = FakeNetworkManager()

    response = client(manager).patch(
        '/api/v1/network/interfaces/missing', json={'enabled': False}
    )

    assert response.status_code == 404
    assert manager.events == ['update:missing']


def test_snapshot_uses_cached_interfaces_without_discovery(monkeypatch) -> None:
    clock = Mock(return_value=0.0)
    provider = Mock(
        return_value={
            'eth0': NetworkInterface(
                name='eth0',
                address='192.168.1.20',
                netmask='255.255.255.0',
                gateway='192.168.1.1',
                is_up=True,
                speed_mbps=1000,
                is_default=True,
            )
        }
    )
    manager = NetworkRouteManager(
        NetworkSettings, interface_provider=provider, clock=clock
    )
    monkeypatch.setattr(network, 'manager', manager)

    network.snapshot()
    clock.return_value = 11.0
    network.snapshot()

    assert provider.call_count == 1
