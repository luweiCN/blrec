from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

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


def test_network_route_rejects_same_primary_and_fallback() -> None:
    with pytest.raises(ValidationError):
        NetworkRouteSettings(primary_interface='lan1', fallback_interface='lan1')


def test_purposes_have_independent_routes() -> None:
    settings = NetworkSettings(
        room_status={'primaryInterface': 'lan1'},
        danmaku={'primaryInterface': 'lan2'},
        recording={'primaryInterface': 'lan1'},
        upload={'primaryInterface': 'lan2'},
        bili_api={'primaryInterface': 'lan1'},
    )
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)

    assert manager.select('room_status').interface_name == 'lan1'
    assert manager.select('danmaku').interface_name == 'lan2'
    assert manager.select('recording').interface_name == 'lan1'
    assert manager.select('upload').interface_name == 'lan2'
    assert manager.select('bili_api').interface_name == 'lan1'


def test_sticky_failover_returns_to_primary_after_cooldown() -> None:
    now = [100.0]
    settings = NetworkSettings(
        upload={
            'primaryInterface': 'lan1',
            'fallbackInterface': 'lan2',
            'failoverEnabled': True,
        }
    )
    manager = NetworkRouteManager(
        lambda: settings,
        interface_provider=_interfaces,
        clock=lambda: now[0],
        failure_threshold=2,
        fallback_cooldown_seconds=60,
    )

    assert manager.select('upload').interface_name == 'lan1'
    manager.report_failure('upload', 'lan1')
    assert manager.select('upload').interface_name == 'lan1'
    manager.report_failure('upload', 'lan1')
    assert manager.select('upload').interface_name == 'lan2'
    assert manager.select('upload').interface_name == 'lan2'

    now[0] = 161.0
    assert manager.select('upload').interface_name == 'lan1'


def test_missing_primary_uses_available_fallback() -> None:
    settings = NetworkSettings(
        recording={'primaryInterface': 'missing', 'fallbackInterface': 'lan2'}
    )
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)

    selection = manager.select('recording')

    assert selection.interface_name == 'lan2'
    assert selection.source_address == '192.168.2.10'
    assert selection.role == 'fallback'


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
