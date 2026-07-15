from threading import RLock
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter

from .manager import NetworkRouteManager


class SourceAddressAdapter(HTTPAdapter):
    def __init__(self, source_address: str, *args: Any, **kwargs: Any) -> None:
        self._source_address = source_address
        super().__init__(*args, **kwargs)

    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any
    ) -> None:
        pool_kwargs['source_address'] = (self._source_address, 0)
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)


class RoutedRequestsSession(requests.Session):
    def __init__(self, manager: NetworkRouteManager) -> None:
        super().__init__()
        self.trust_env = False
        self._manager = manager
        self._route_sessions: Dict[Optional[str], requests.Session] = {}
        self._route_lock = RLock()

    def request(  # type: ignore[override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        selection = self._manager.select('recording')
        session = self._session_for(selection.source_address)
        try:
            response = session.request(method, url, **kwargs)
        except (requests.RequestException, OSError):
            self._manager.report_failure('recording', selection.interface_name)
            raise
        self._manager.report_success('recording', selection.interface_name)
        return response

    def close(self) -> None:
        with self._route_lock:
            sessions, self._route_sessions = self._route_sessions, {}
        for session in sessions.values():
            session.close()
        super().close()

    def _session_for(self, source_address: Optional[str]) -> requests.Session:
        with self._route_lock:
            session = self._route_sessions.get(source_address)
            if session is not None:
                return session
            session = requests.Session()
            session.trust_env = False
            if source_address is not None:
                adapter = SourceAddressAdapter(source_address)
                session.mount('http://', adapter)
                session.mount('https://', adapter)
            self._route_sessions[source_address] = session
            return session
