from __future__ import annotations

import json
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Any, Mapping, Optional, Sequence, Tuple

from .database import DatabaseTarget, connect_database
from .direct import RevisionLoader, read_source_revision
from .service import get_match, get_match_summary, list_matches


@dataclass(frozen=True)
class _AudienceState:
    source_revision: int
    dashboard_payload: bytes
    live_rooms_payload: bytes


@dataclass(frozen=True)
class _RepositoryState:
    public: _AudienceState
    owner: _AudienceState


def _load_state(database_target: DatabaseTarget) -> _RepositoryState:
    connection = connect_database(database_target)
    try:
        rows = connection.execute(
            'SELECT audience,source_revision,dashboard_payload,live_rooms_payload '
            'FROM dashboard_audience_state ORDER BY audience'
        ).fetchall()
    finally:
        connection.close()
    values = {
        str(row['audience']): _AudienceState(
            source_revision=int(row['source_revision']),
            dashboard_payload=bytes(row['dashboard_payload']),
            live_rooms_payload=bytes(row['live_rooms_payload']),
        )
        for row in rows
    }
    if set(values) != {'public', 'owner'}:
        raise RuntimeError('dashboard incremental cache has no complete publication')
    if values['public'].source_revision != values['owner'].source_revision:
        raise RuntimeError('dashboard incremental cache audiences are inconsistent')
    return _RepositoryState(public=values['public'], owner=values['owner'])


class NormalizedDashboardRepository:
    """Serve dashboard bytes and bounded SQL reads from the incremental cache."""

    def __init__(
        self,
        *,
        source_target: DatabaseTarget,
        auxiliary_target: DatabaseTarget,
        revision_loader: RevisionLoader = read_source_revision,
    ) -> None:
        self._source_target = source_target
        self._auxiliary_target = auxiliary_target
        self._revision_loader = revision_loader
        self._state_lock = RLock()
        self._refresh_lock = Lock()
        self._state: Optional[_RepositoryState] = None

    def refresh(self, *, force: bool = False) -> bool:
        with self._refresh_lock:
            next_state = _load_state(self._auxiliary_target)
            with self._state_lock:
                previous = self._state
                if (
                    not force
                    and previous is not None
                    and previous.public.source_revision
                    == next_state.public.source_revision
                ):
                    return False
                self._state = next_state
            return (
                previous is None
                or previous.public.source_revision != next_state.public.source_revision
            )

    def source_revision(self) -> int:
        return self._revision_loader(self._source_target)

    def _current(self, *, owner_view: bool = False) -> _AudienceState:
        with self._state_lock:
            if self._state is None:
                raise RuntimeError('dashboard incremental cache has not been loaded')
            return self._state.owner if owner_view else self._state.public

    def dashboard_document(
        self, *, owner_view: bool = False
    ) -> Tuple[Mapping[str, Any], str]:
        payload, revision = self.dashboard_payload(owner_view=owner_view)
        return json.loads(payload), revision

    def dashboard_payload(self, *, owner_view: bool = False) -> Tuple[bytes, str]:
        current = self._current(owner_view=owner_view)
        return current.dashboard_payload, str(current.source_revision)

    def live_rooms(self, *, owner_view: bool = False) -> Tuple[Mapping[str, Any], str]:
        current = self._current(owner_view=owner_view)
        return json.loads(current.live_rooms_payload), str(current.source_revision)

    def list_matches(
        self,
        *,
        page: int,
        page_size: int,
        season: Optional[str],
        mode: Optional[str],
        player_id: Optional[int],
        query: str,
        heroes: Sequence[str],
        rating_scope: str,
        rating_season: Optional[str],
        owner_view: bool = False,
    ) -> Mapping[str, Any]:
        return list_matches(
            self._auxiliary_target,
            page=page,
            page_size=page_size,
            season=season,
            mode=mode,
            player_id=player_id,
            query=query,
            heroes=heroes,
            rating_scope=rating_scope,
            rating_season=rating_season,
            owner_view=owner_view,
        )

    def get_match(
        self,
        match_id: int,
        *,
        rating_scope: str,
        rating_season: Optional[str],
        owner_view: bool = False,
    ) -> Mapping[str, Any]:
        return get_match(
            self._auxiliary_target,
            match_id,
            rating_scope=rating_scope,
            rating_season=rating_season,
            owner_view=owner_view,
        )

    def match_summary(
        self,
        *,
        season: Optional[str],
        mode: Optional[str],
        player_id: Optional[int],
        owner_view: bool = False,
    ) -> Mapping[str, int]:
        return get_match_summary(
            self._auxiliary_target,
            season=season,
            mode=mode,
            player_id=player_id,
            owner_view=owner_view,
        )
