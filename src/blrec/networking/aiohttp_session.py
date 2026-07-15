from __future__ import annotations

import socket
from typing import Any, Dict, Optional, Tuple

import aiohttp

from blrec.bili.net import timeout

from .manager import NetworkPurpose, NetworkRouteManager, RouteSelection


class RoutedAiohttpSession:
    """Small ClientSession-compatible facade that selects a route per call."""

    def __init__(
        self,
        pool: 'AiohttpSessionPool',
        purpose: NetworkPurpose,
        *,
        anonymous: bool = False,
    ) -> None:
        self._pool = pool
        self._purpose = purpose
        self._anonymous = anonymous
        self.cookie_jar = aiohttp.DummyCookieJar()
        self.auth = None
        self.trust_env = False
        self.headers: Dict[str, str] = {}

    @property
    def closed(self) -> bool:
        return self._pool.closed

    def request(self, *args: Any, **kwargs: Any) -> Any:
        return self._pool.session(self._purpose, self._anonymous).request(
            *args, **kwargs
        )

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._pool.session(self._purpose, self._anonymous).get(*args, **kwargs)

    def head(self, *args: Any, **kwargs: Any) -> Any:
        return self._pool.session(self._purpose, self._anonymous).head(*args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> Any:
        return self._pool.session(self._purpose, self._anonymous).post(*args, **kwargs)

    def ws_connect(self, *args: Any, **kwargs: Any) -> Any:
        return self._pool.session(self._purpose, self._anonymous).ws_connect(
            *args, **kwargs
        )

    async def close(self) -> None:
        # The application owns the shared pool.
        return None


class AiohttpSessionPool:
    def __init__(self, manager: NetworkRouteManager) -> None:
        self._manager = manager
        self._sessions: Dict[
            Tuple[NetworkPurpose, Optional[str], bool], aiohttp.ClientSession
        ] = {}
        self._clients: Dict[Tuple[NetworkPurpose, bool], RoutedAiohttpSession] = {}
        self.closed = False

    def client(
        self, purpose: NetworkPurpose, *, anonymous: bool = False
    ) -> RoutedAiohttpSession:
        key = (purpose, anonymous)
        client = self._clients.get(key)
        if client is None:
            client = RoutedAiohttpSession(self, purpose, anonymous=anonymous)
            self._clients[key] = client
        return client

    def session(
        self, purpose: NetworkPurpose, anonymous: bool = False
    ) -> aiohttp.ClientSession:
        if self.closed:
            raise RuntimeError('network session pool is closed')
        selection = self._manager.select(purpose)
        key = (purpose, selection.source_address, anonymous)
        session = self._sessions.get(key)
        if session is None or session.closed:
            session = self._create_session(purpose, selection, anonymous)
            self._sessions[key] = session
        return session

    def _create_session(
        self, purpose: NetworkPurpose, selection: RouteSelection, anonymous: bool
    ) -> aiohttp.ClientSession:
        trace_config = aiohttp.TraceConfig()

        async def request_end(
            _session: aiohttp.ClientSession, _context: Any, _params: Any
        ) -> None:
            self._manager.report_success(purpose, selection.interface_name)

        async def request_exception(
            _session: aiohttp.ClientSession, _context: Any, _params: Any
        ) -> None:
            self._manager.report_failure(purpose, selection.interface_name)

        request_end_signal: Any = trace_config.on_request_end
        request_end_signal.append(request_end)
        request_exception_signal: Any = trace_config.on_request_exception
        request_exception_signal.append(request_exception)
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            limit=200,
            local_addr=(
                (selection.source_address, 0) if selection.source_address else None
            ),
        )
        return aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.DummyCookieJar() if anonymous else aiohttp.CookieJar(),
            timeout=timeout,
            trust_env=False,
            raise_for_status=not anonymous,
            trace_configs=[trace_config],
        )

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        sessions, self._sessions = list(self._sessions.values()), {}
        for session in sessions:
            await session.close()
