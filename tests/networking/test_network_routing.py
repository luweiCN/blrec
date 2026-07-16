from typing import Any, Dict, List

import pytest

from blrec.bili_upload.protocol import AiohttpProtocolTransport
from blrec.networking.manager import NetworkInterface, NetworkRouteManager
from blrec.networking.requests_session import SourceAddressAdapter
from blrec.setting.models import NetworkRouteSettings, NetworkSettings


def _interfaces() -> Dict[str, NetworkInterface]:
    return {
        'lan1': NetworkInterface(
            name='lan1',
            address='192.168.1.10',
            netmask='255.255.255.0',
            gateway='192.168.1.1',
            is_up=True,
            speed_mbps=1000,
            is_default=True,
        ),
        'lan2': NetworkInterface(
            name='lan2',
            address='192.168.2.10',
            netmask='255.255.255.0',
            gateway='192.168.2.1',
            is_up=True,
            speed_mbps=1000,
            is_default=False,
        ),
    }


def test_legacy_network_route_migrates_to_fixed_interface() -> None:
    route = NetworkRouteSettings(
        primary_interface='lan1', fallback_interface='lan2', failover_enabled=True
    )

    assert route.mode == 'fixed'
    assert route.interface == 'lan1'
    assert route.failover_enabled is True


def test_purposes_have_independent_routes() -> None:
    settings = NetworkSettings(
        room_status={'interface': 'lan1'},
        danmaku={'interface': 'lan2'},
        recording={'interface': 'lan1'},
        upload={'interface': 'lan2'},
        bili_api={'interface': 'lan1'},
    )
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)

    assert manager.select('room_status').interface_name == 'lan1'
    assert manager.select('danmaku').interface_name == 'lan2'
    assert manager.select('recording').interface_name == 'lan1'
    assert manager.select('upload').interface_name == 'lan2'
    assert manager.select('bili_api').interface_name == 'lan1'


def test_interface_settings_override_discovery_defaults() -> None:
    settings = NetworkSettings(
        interfaces={'lan1': {'enabled': False, 'uploadLimitBps': 1024}}
    )
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)

    interface = manager.interfaces()['lan1']

    assert interface.enabled is False
    assert interface.upload_limit_bps == 1024


def test_source_address_adapter_passes_binding_to_urllib3() -> None:
    adapter = SourceAddressAdapter('192.168.2.10')

    adapter.init_poolmanager(2, 4)

    assert adapter.poolmanager.connection_pool_kw['source_address'] == (
        '192.168.2.10',
        0,
    )


@pytest.mark.parametrize(
    'operation', ['preupload', 'preupload_init', 'upload_chunk', 'complete_upload']
)
def test_upload_transport_operations_use_upload_route(operation: str) -> None:
    assert AiohttpProtocolTransport.purpose_for_operation(operation) == 'upload'


def test_non_upload_protocol_operations_use_bili_api_route() -> None:
    assert AiohttpProtocolTransport.purpose_for_operation('submit_archive') == (
        'bili_api'
    )


@pytest.mark.asyncio
async def test_probe_uses_browser_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    session_options: List[Dict[str, Any]] = []

    class FakeResponse:
        async def __aenter__(self) -> 'FakeResponse':
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def read(self) -> bytes:
            return b'{}'

        async def json(self) -> Dict[str, str]:
            return {'ip': '203.0.113.10'}

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            session_options.append(kwargs)

        async def __aenter__(self) -> 'FakeSession':
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def get(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(
        'blrec.networking.manager.aiohttp.TCPConnector', lambda **kwargs: object()
    )
    monkeypatch.setattr('blrec.networking.manager.aiohttp.ClientSession', FakeSession)
    manager = NetworkRouteManager(NetworkSettings, interface_provider=_interfaces)

    await manager.probe('lan1')

    assert session_options[0]['headers']['User-Agent'].startswith('Mozilla/5.0')
