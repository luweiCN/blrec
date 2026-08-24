from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from threading import Lock
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(',', ':')
    ).encode('utf-8')


def _revision(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_player(player: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(player)
    pools = result.get('heroPools')
    if not isinstance(pools, Mapping):
        pools = {'all': list(result.get('heroPool') or [])}
        result['heroPools'] = pools
    result.pop('heroPool', None)
    return result


def _player_directory(players: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    fields = ('id', 'name', 'initial', 'roomLabel', 'roomIds', 'aliases')
    return {
        str(player['id']): {field: player.get(field) for field in fields}
        for player in players
        if player.get('id') is not None
    }


@dataclass(frozen=True)
class ResourcePayload:
    payload: bytes
    revision: str


class DashboardResourceDocument:
    """Immutable v2 resource slices derived once from the current v1 document."""

    def __init__(self, dashboard_payload: bytes, source_revision: str) -> None:
        document = json.loads(dashboard_payload)
        snapshot = document['snapshot']
        trends = document['trends']
        standings = snapshot['standings']
        self.source_revision = str(source_revision)
        self._snapshot = snapshot
        self._trends = trends
        self._standings: Dict[str, ResourcePayload] = {}
        self._environments: Dict[str, ResourcePayload] = {}
        for season_id, raw in standings.items():
            standing = {
                'schemaVersion': 1,
                'seasonId': season_id,
                'players': [
                    _canonical_player(player) for player in raw.get('players', [])
                ],
                'heroes': list(raw.get('heroes', [])),
            }
            standing_bytes = _json_bytes(standing)
            self._standings[str(season_id)] = ResourcePayload(
                standing_bytes, _revision(standing_bytes)
            )
            environment = {
                'schemaVersion': 1,
                'seasonId': season_id,
                'environmentHeroes': list(raw.get('environmentHeroes', [])),
            }
            environment_bytes = _json_bytes(environment)
            self._environments[str(season_id)] = ResourcePayload(
                environment_bytes, _revision(environment_bytes)
            )
        trend_bytes = _json_bytes(trends.get('publications', []))
        self.trends_revision = _revision(trend_bytes)
        all_time = standings.get('all-time') or {}
        summary = {
            'schemaVersion': 1,
            'snapshotId': snapshot['snapshotId'],
            'contentRevision': snapshot.get('contentRevision'),
            'publicationDate': snapshot['publicationDate'],
            'generatedAt': snapshot['generatedAt'],
            'sourceLastMatchId': snapshot['sourceLastMatchId'],
            'sourceMatchCount': snapshot['sourceMatchCount'],
            'ratingModel': snapshot['ratingModel'],
            'currentSeasonKey': snapshot['currentSeasonKey'],
            'seasons': snapshot['seasons'],
            'playersById': _player_directory(all_time.get('players', [])),
            'resources': {
                'standings': {
                    season_id: resource.revision
                    for season_id, resource in self._standings.items()
                },
                'environment': {
                    season_id: resource.revision
                    for season_id, resource in self._environments.items()
                },
                'trends': self.trends_revision,
                'matches': self.source_revision,
                'liveRooms': self.source_revision,
            },
        }
        summary_bytes = _json_bytes(summary)
        self.summary = ResourcePayload(summary_bytes, _revision(summary_bytes))
        self._trend_queries: Dict[Tuple[Any, ...], ResourcePayload] = {}
        self._trend_lock = Lock()

    @property
    def seasons(self) -> Tuple[str, ...]:
        return tuple(self._standings)

    def standings(self, season_id: str) -> ResourcePayload:
        try:
            return self._standings[season_id]
        except KeyError as error:
            raise KeyError('unknown season') from error

    def environment(self, season_id: str) -> ResourcePayload:
        try:
            return self._environments[season_id]
        except KeyError as error:
            raise KeyError('unknown season') from error

    def trends(
        self,
        *,
        season_id: str,
        mode: str,
        player_ids: Sequence[int],
        from_date: Optional[str],
        to_date: Optional[str],
    ) -> ResourcePayload:
        if season_id not in self._standings:
            raise KeyError('unknown season')
        normalized_ids = tuple(sorted(set(int(value) for value in player_ids)))
        publications = list(self._trends.get('publications', []))
        latest = (
            date.fromisoformat(str(publications[-1]['publicationDate']))
            if publications
            else date.fromisoformat(str(self._snapshot['publicationDate']))
        )
        end = date.fromisoformat(to_date) if to_date else latest
        start = date.fromisoformat(from_date) if from_date else end - timedelta(days=29)
        if start > end:
            raise ValueError('from must not be after to')
        key = (season_id, mode, normalized_ids, start.isoformat(), end.isoformat())
        with self._trend_lock:
            cached = self._trend_queries.get(key)
        if cached is not None:
            return cached
        filtered = []
        selected_ids = set(normalized_ids)
        for publication in publications:
            publication_date = date.fromisoformat(str(publication['publicationDate']))
            if publication_date < start or publication_date > end:
                continue
            season = (publication.get('standings') or {}).get(season_id) or {}
            rows = list(season.get(mode) or [])
            if selected_ids:
                rows = [
                    row for row in rows if int(row.get('playerId') or 0) in selected_ids
                ]
            filtered.append(
                {
                    'snapshotId': publication['snapshotId'],
                    'publicationDate': publication['publicationDate'],
                    'standings': {season_id: {mode: rows}},
                }
            )
        result = {
            'schemaVersion': 1,
            'updatedAt': self._trends['updatedAt'],
            'query': {
                'seasonId': season_id,
                'mode': mode,
                'playerIds': list(normalized_ids),
                'from': start.isoformat(),
                'to': end.isoformat(),
            },
            'publications': filtered,
        }
        payload = _json_bytes(result)
        resource = ResourcePayload(payload, _revision(payload))
        with self._trend_lock:
            self._trend_queries[key] = resource
        return resource

    def revision_manifest(self) -> Mapping[str, Any]:
        return json.loads(self.summary.payload)['resources']


class DashboardResourceCache:
    def __init__(self) -> None:
        self._lock = Lock()
        self._public: Optional[DashboardResourceDocument] = None
        self._owner: Optional[DashboardResourceDocument] = None

    def replace(
        self, public: Tuple[bytes, str], owner: Tuple[bytes, str]
    ) -> Tuple[Mapping[str, Any], ...]:
        next_public = DashboardResourceDocument(*public)
        next_owner = DashboardResourceDocument(*owner)
        with self._lock:
            previous = self._public
            self._public = next_public
            self._owner = next_owner
        if previous is None:
            return ()
        changes = []
        if previous.summary.revision != next_public.summary.revision:
            changes.append(
                {'resource': 'summary', 'revision': next_public.summary.revision}
            )
        for resource_name, previous_values, next_values in (
            ('standings', previous._standings, next_public._standings),
            ('environment', previous._environments, next_public._environments),
        ):
            for season_id, resource in next_values.items():
                old = previous_values.get(season_id)
                if old is None or old.revision != resource.revision:
                    changes.append(
                        {
                            'resource': resource_name,
                            'seasonId': season_id,
                            'revision': resource.revision,
                        }
                    )
        if previous.trends_revision != next_public.trends_revision:
            changes.append(
                {'resource': 'trends', 'revision': next_public.trends_revision}
            )
        return tuple(changes)

    def current(self, *, owner_view: bool = False) -> DashboardResourceDocument:
        with self._lock:
            value = self._owner if owner_view else self._public
        if value is None:
            raise RuntimeError('dashboard v2 resource cache is empty')
        return value
