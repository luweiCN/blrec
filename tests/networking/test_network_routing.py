from typing import Any, Dict, List, Tuple

import pytest

from blrec.bili_upload.protocol import AiohttpProtocolTransport
from blrec.networking.aiohttp_session import AiohttpSessionPool
from blrec.networking.manager import NetworkInterface, NetworkRouteManager
from blrec.networking.requests_session import (
    ResolvedHTTPConnection,
    SourceAddressAdapter,
)
from blrec.networking.resolver import SourceBoundResolver
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


def test_resolved_http_connection_connects_to_resolved_ip_without_changing_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Tuple[object, object]] = []
    expected_socket = object()

    def create_connection(address: object, *args: object, **kwargs: object) -> object:
        calls.append((address, kwargs.get('source_address')))
        return expected_socket

    monkeypatch.setattr(
        'blrec.networking.requests_session.connection.create_connection',
        create_connection,
    )
    connection = ResolvedHTTPConnection(
        'example.com',
        80,
        source_address=('192.168.2.10', 0),
        resolver=lambda host: ('203.0.113.20',),
    )

    result = connection._new_conn()

    assert result is expected_socket
    assert connection.host == 'example.com'
    assert calls == [(('203.0.113.20', 80), ('192.168.2.10', 0))]


def test_aiohttp_pool_passes_anonymous_and_affinity_to_route_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Tuple[str, bool, object]] = []

    class FakeManager:
        def select(
            self, purpose: str, *, anonymous: bool, affinity_key: object
        ) -> object:
            calls.append((purpose, anonymous, affinity_key))
            return type(
                'Selection', (), {'source_address': None, 'interface_name': None}
            )()

    class FakeSession:
        closed = False

        def get(self, *args: object, **kwargs: object) -> str:
            return 'response'

    pool = AiohttpSessionPool(FakeManager())  # type: ignore[arg-type]
    monkeypatch.setattr(pool, '_create_session', lambda *args: FakeSession())

    result = pool.client('danmaku', anonymous=True, affinity_key='room:100').get(
        'wss://example.com'
    )

    assert result == 'response'
    assert calls == [('danmaku', True, 'room:100')]


def test_aiohttp_session_connector_uses_source_bound_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector_options: Dict[str, object] = {}

    class FakeConnector:
        def __init__(self, **kwargs: object) -> None:
            connector_options.update(kwargs)

    class FakeClientSession:
        def __init__(self, **kwargs: object) -> None:
            self.closed = False

    monkeypatch.setattr(
        'blrec.networking.aiohttp_session.aiohttp.TCPConnector', FakeConnector
    )
    monkeypatch.setattr(
        'blrec.networking.aiohttp_session.aiohttp.ClientSession', FakeClientSession
    )
    manager = NetworkRouteManager(
        lambda: NetworkSettings(recording={'interface': 'lan1'}),
        interface_provider=_interfaces,
    )

    AiohttpSessionPool(manager).session('recording', anonymous=True)

    assert isinstance(connector_options['resolver'], SourceBoundResolver)
    assert connector_options['local_addr'] == ('192.168.1.10', 0)


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
