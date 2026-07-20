import asyncio
import threading
from typing import Any, Dict, List, Tuple
from unittest.mock import Mock

import pytest

from blrec.bili_upload.protocol import AiohttpProtocolTransport, ProtocolRequest
from blrec.networking.aiohttp_session import AiohttpSessionPool
from blrec.networking.manager import NetworkInterface, NetworkRouteManager
from blrec.networking.requests_session import (
    ResolvedHTTPConnection,
    SourceAddressAdapter,
)
from blrec.networking.resolver import SourceBoundResolver
from blrec.setting.models import (
    NetworkInterfaceSettings,
    NetworkRouteSettings,
    NetworkSettings,
)


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


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


def test_every_route_selection_is_audited_with_replayable_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    monkeypatch.setattr(
        'blrec.networking.manager.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    settings = NetworkSettings(recording={'interface': 'lan1'})
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)

    manager.select('recording', affinity_key='room:100', anonymous=True)
    manager.select('recording', affinity_key='room:100', anonymous=True)

    selected = [fields for event, fields in events if event == 'network_route_selected']
    assert len(selected) == 2
    assert selected[0] == {
        'level': 'DEBUG',
        'purpose': 'recording',
        'interface': 'lan1',
        'source_address': '192.168.1.10',
        'role': 'primary',
        'reason': 'configured',
        'anonymous': True,
        'affinity_key': 'room:100',
        'result': 'selected',
    }


def test_interface_settings_override_discovery_defaults() -> None:
    settings = NetworkSettings(
        interfaces={'lan1': {'enabled': False, 'uploadLimitBps': 1024}}
    )
    manager = NetworkRouteManager(lambda: settings, interface_provider=_interfaces)

    interface = manager.interfaces()['lan1']

    assert interface.enabled is False
    assert interface.upload_limit_bps == 1024


@pytest.mark.asyncio
async def test_interfaces_uses_cache_until_async_refresh() -> None:
    clock = Clock()
    provider = Mock(return_value=_interfaces())
    manager = NetworkRouteManager(
        lambda: NetworkSettings(),
        interface_provider=provider,
        interface_cache_ttl_seconds=10,
        clock=clock,
    )

    assert provider.call_count == 1
    manager.interfaces()
    manager.interfaces()
    await manager.refresh_interfaces()

    assert provider.call_count == 1

    clock.value += 11
    manager.interfaces()
    manager.interfaces()

    assert provider.call_count == 1

    await manager.refresh_interfaces()

    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_refresh_interfaces_can_force_a_fresh_discovery() -> None:
    provider = Mock(return_value=_interfaces())
    manager = NetworkRouteManager(
        lambda: NetworkSettings(), interface_provider=provider
    )

    await manager.refresh_interfaces(force=True)

    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_refresh_interfaces_runs_provider_in_executor() -> None:
    provider_threads: List[int] = []

    def provider() -> Dict[str, NetworkInterface]:
        provider_threads.append(threading.get_ident())
        return _interfaces()

    clock = Clock()
    manager = NetworkRouteManager(
        lambda: NetworkSettings(),
        interface_provider=provider,
        interface_cache_ttl_seconds=10,
        clock=clock,
    )
    clock.value += 11

    await manager.refresh_interfaces()

    assert provider_threads[0] == threading.get_ident()
    assert provider_threads[1] != threading.get_ident()


@pytest.mark.asyncio
@pytest.mark.parametrize('force', [False, True], ids=['expired', 'forced'])
async def test_concurrent_refreshes_share_interface_discovery(force: bool) -> None:
    provider_calls: List[None] = []
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    def provider() -> Dict[str, NetworkInterface]:
        provider_calls.append(None)
        if len(provider_calls) > 1:
            refresh_started.set()
            release_refresh.wait(timeout=1)
        return _interfaces()

    clock = Clock()
    manager = NetworkRouteManager(
        lambda: NetworkSettings(),
        interface_provider=provider,
        interface_cache_ttl_seconds=10,
        clock=clock,
    )
    clock.value += 11

    first = asyncio.create_task(manager.refresh_interfaces(force=force))
    for _ in range(100):
        if refresh_started.is_set():
            break
        await asyncio.sleep(0.001)
    assert refresh_started.is_set()
    second = asyncio.create_task(manager.refresh_interfaces(force=force))
    await asyncio.sleep(0)
    release_refresh.set()

    await asyncio.gather(first, second)

    assert len(provider_calls) == 2


def test_interfaces_apply_current_settings_without_rediscovery() -> None:
    settings = NetworkSettings()
    provider = Mock(return_value=_interfaces())
    manager = NetworkRouteManager(lambda: settings, interface_provider=provider)

    assert manager.interfaces()['lan1'].enabled is True
    settings.interfaces['lan1'] = NetworkInterfaceSettings(
        enabled=False, upload_limit_bps=2048
    )
    interface = manager.interfaces()['lan1']

    assert interface.enabled is False
    assert interface.upload_limit_bps == 2048
    assert provider.call_count == 1


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
async def test_upload_transport_streams_limited_body_with_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}

    class FakeResponse:
        status = 200
        headers: Dict[str, str] = {}

        async def read(self) -> bytes:
            return b'{}'

    class RequestContext:
        async def __aenter__(self) -> FakeResponse:
            captured['chunks'] = [chunk async for chunk in captured['kwargs']['data']]
            return FakeResponse()

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeSession:
        def request(self, method: str, url: str, **kwargs: Any) -> RequestContext:
            captured['method'] = method
            captured['url'] = url
            captured['kwargs'] = kwargs
            return RequestContext()

    manager = NetworkRouteManager(
        lambda: NetworkSettings(upload={'interface': 'lan1'}),
        interface_provider=_interfaces,
    )
    transport = AiohttpProtocolTransport(route_manager=manager)

    async def get_session(*args: object) -> FakeSession:
        return FakeSession()

    monkeypatch.setattr(transport, '_get_session', get_session)
    body = b'x' * (130 * 1024)

    await transport.send(
        ProtocolRequest(
            operation='upload_chunk',
            method='PUT',
            url='https://upload.example/part',
            headers={},
            body=body,
        )
    )

    assert captured['kwargs']['headers']['Content-Length'] == str(len(body))
    assert b''.join(captured['chunks']) == body
    assert max(map(len, captured['chunks'])) <= 64 * 1024
    traffic = manager.traffic_meter.snapshot()[0]
    assert traffic.interface_name == 'lan1'
    assert traffic.upload_total == len(body)


@pytest.mark.asyncio
async def test_probe_uses_browser_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    session_options: List[Dict[str, Any]] = []
    requested_urls: List[str] = []

    class FakeResponse:
        async def __aenter__(self) -> 'FakeResponse':
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def read(self) -> bytes:
            return b'{}'

        async def json(self) -> Dict[str, Dict[str, str]]:
            return {'data': {'addr': '203.0.113.10'}}

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            session_options.append(kwargs)

        async def __aenter__(self) -> 'FakeSession':
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def get(self, url: str, **kwargs: object) -> FakeResponse:
            requested_urls.append(url)
            return FakeResponse()

    monkeypatch.setattr(
        'blrec.networking.manager.aiohttp.TCPConnector', lambda **kwargs: object()
    )
    monkeypatch.setattr('blrec.networking.manager.aiohttp.ClientSession', FakeSession)
    manager = NetworkRouteManager(NetworkSettings, interface_provider=_interfaces)

    await manager.probe('lan1')

    assert len(session_options) == 1
    assert session_options[0]['headers']['User-Agent'].startswith('Mozilla/5.0')
    assert requested_urls == ['https://api.bilibili.com/x/web-interface/zone']
    assert manager.cached_probes()['lan1'].external_ip == '203.0.113.10'
