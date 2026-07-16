from typing import Dict, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.networking.manager import NetworkInterface, NetworkProbe
from blrec.web.routers import network


class FakeNetworkManager:
    def __init__(self) -> None:
        self.probed: Optional[str] = None
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

    async def probe(self, interface_name: Optional[str] = None) -> None:
        self.probed = interface_name

    async def update_interface(
        self,
        interface_name: str,
        *,
        enabled: Optional[bool] = None,
        upload_limit_bps: Optional[int] = None,
    ) -> None:
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
    response = client(FakeNetworkManager()).get('/api/v1/network/interfaces')

    assert response.status_code == 200
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
