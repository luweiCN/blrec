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
        )

    def interfaces(self) -> Dict[str, NetworkInterface]:
        return {'eth0': self._interface}

    def cached_probes(self) -> Dict[str, NetworkProbe]:
        return {}

    async def probe(self, interface_name: Optional[str] = None) -> None:
        self.probed = interface_name


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
