import socket
from threading import RLock
from typing import Any, Callable, Dict, Optional, Sequence, Type

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError
from urllib3.util import connection

from .manager import NetworkRouteManager, RouteSelection
from .resolver import SyncSourceBoundResolver

Resolver = Callable[[str], Sequence[str]]


class _ResolvedConnectionMixin:
    _default_resolver: Optional[Resolver] = None

    def __init__(
        self, *args: Any, resolver: Optional[Resolver] = None, **kwargs: Any
    ) -> None:
        self._resolver = resolver or self._default_resolver or (lambda host: (host,))
        super().__init__(*args, **kwargs)  # type: ignore[call-arg]

    def _new_conn(self) -> socket.socket:
        last_error: Optional[OSError] = None
        for address in self._resolver(self.host):  # type: ignore[attr-defined]
            try:
                return connection.create_connection(
                    (address, self.port),  # type: ignore[attr-defined]
                    self.timeout,  # type: ignore[attr-defined]
                    source_address=self.source_address,  # type: ignore[attr-defined]
                    socket_options=self.socket_options,  # type: ignore[attr-defined]
                )
            except OSError as error:
                last_error = error
        raise NewConnectionError(
            self,  # type: ignore[arg-type]
            'Failed to establish a source-bound connection: {}'.format(last_error),
        )


class ResolvedHTTPConnection(_ResolvedConnectionMixin, HTTPConnection):
    pass


class ResolvedHTTPSConnection(_ResolvedConnectionMixin, HTTPSConnection):
    pass


class SourceAddressAdapter(HTTPAdapter):
    def __init__(
        self,
        source_address: str,
        *args: Any,
        resolver: Optional[Resolver] = None,
        **kwargs: Any,
    ) -> None:
        self._source_address = source_address
        self._resolver = resolver or (lambda host: (host,))
        super().__init__(*args, **kwargs)

    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any
    ) -> None:
        pool_kwargs['source_address'] = (self._source_address, 0)
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)
        http_connection = self._connection_class(ResolvedHTTPConnection, self._resolver)
        https_connection = self._connection_class(
            ResolvedHTTPSConnection, self._resolver
        )
        http_pool = type(
            'SourceBoundHTTPConnectionPool',
            (HTTPConnectionPool,),
            {'ConnectionCls': http_connection},
        )
        https_pool = type(
            'SourceBoundHTTPSConnectionPool',
            (HTTPSConnectionPool,),
            {'ConnectionCls': https_connection},
        )
        self.poolmanager.pool_classes_by_scheme['http'] = http_pool
        self.poolmanager.pool_classes_by_scheme['https'] = https_pool

    @staticmethod
    def _connection_class(
        base: Type[_ResolvedConnectionMixin], resolver: Resolver
    ) -> Type[_ResolvedConnectionMixin]:
        return type(
            'SourceBound{}'.format(base.__name__),
            (base,),
            {'_default_resolver': staticmethod(resolver)},
        )


class RoutedRequestsSession(requests.Session):
    def __init__(
        self, manager: NetworkRouteManager, *, affinity_key: Optional[str] = None
    ) -> None:
        super().__init__()
        self.trust_env = False
        self._manager = manager
        self._route_sessions: Dict[Optional[str], requests.Session] = {}
        self._route_lock = RLock()
        self._affinity_root = affinity_key or 'recording:{}'.format(id(self))
        self._affinity_generation = 0

    @property
    def _affinity_key(self) -> str:
        return '{}:{}'.format(self._affinity_root, self._affinity_generation)

    def begin_live(self) -> None:
        self._manager.release_affinity('recording', self._affinity_key)
        self._affinity_generation += 1

    def request(  # type: ignore[override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        selection = self._manager.select(
            'recording', anonymous=True, affinity_key=self._affinity_key
        )
        session = self._session_for(selection)
        try:
            response = session.request(method, url, **kwargs)
        except (requests.RequestException, OSError):
            self._manager.report_failure('recording', selection.interface_name)
            raise
        self._manager.report_success('recording', selection.interface_name)
        return response

    def close(self) -> None:
        self._manager.release_affinity('recording', self._affinity_key)
        with self._route_lock:
            sessions, self._route_sessions = self._route_sessions, {}
        for session in sessions.values():
            session.close()
        super().close()

    def _session_for(self, selection: RouteSelection) -> requests.Session:
        source_address = selection.source_address
        with self._route_lock:
            session = self._route_sessions.get(source_address)
            if session is not None:
                return session
            session = requests.Session()
            session.trust_env = False
            if source_address is not None:
                interface = self._manager.interface(selection.interface_name)
                resolver = (
                    None
                    if interface is None
                    else SyncSourceBoundResolver(interface).resolve
                )
                adapter = SourceAddressAdapter(source_address, resolver=resolver)
                session.mount('http://', adapter)
                session.mount('https://', adapter)
            self._route_sessions[source_address] = session
            return session
