from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from threading import Lock, RLock
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .assets import get_match_assets
from .database import DatabaseTarget, connect_database, is_postgres
from .direct import (
    RevisionLoader,
    RuntimeLoader,
    _normalize_search,
    build_repository_state,
    load_runtime_source,
    read_source_revision,
)
from .replay_visibility import resolve_match_replays

AUDIENCES = ('public', 'owner')


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _played_at_epoch(value: object) -> int:
    text = str(value)
    moment = datetime.fromisoformat(
        text[:-1] + '+00:00' if text.endswith('Z') else text
    )
    if moment.tzinfo is None:
        raise ValueError('cached match time must include a timezone')
    return int(moment.timestamp())


def _rating_rows(dataset: Any) -> Mapping[int, Mapping[str, Mapping[str, Any]]]:
    values: Dict[int, Dict[str, Mapping[str, Any]]] = {}
    for key, rating in dataset.ratings.items():
        match_id, scope, season_key = key
        values.setdefault(int(match_id), {})['{}|{}'.format(scope, season_key)] = rating
    return values


def _insert_rows(connection: Any, *, insert_sql: str, copy_sql: str, rows: Any) -> None:
    if getattr(connection, 'dialect', 'sqlite') == 'postgresql':
        connection.copy_rows(copy_sql, rows)
        return
    connection.executemany(insert_sql, rows)


def _publish_dataset(
    connection: Any, *, audience: str, dataset: Any, published_at: int
) -> None:
    source_revision = int(dataset.source_revision)
    connection.execute(
        'INSERT INTO dashboard_cache_generations('
        'source_revision,audience,dashboard_payload,live_rooms_payload,published_at'
        ') VALUES(?,?,?,?,?) ON CONFLICT(source_revision,audience) DO UPDATE SET '
        'dashboard_payload=excluded.dashboard_payload,'
        'live_rooms_payload=excluded.live_rooms_payload,'
        'published_at=excluded.published_at',
        (
            source_revision,
            audience,
            dataset.dashboard_payload,
            _json_text(dataset.live_rooms).encode('utf-8'),
            published_at,
        ),
    )
    connection.execute(
        'DELETE FROM dashboard_cache_players ' 'WHERE source_revision=? AND audience=?',
        (source_revision, audience),
    )
    _insert_rows(
        connection,
        insert_sql=(
            'INSERT INTO dashboard_cache_players('
            'source_revision,audience,player_id,player_json) VALUES(?,?,?,?)'
        ),
        copy_sql=(
            'COPY dashboard_cache_players('
            'source_revision,audience,player_id,player_json) FROM STDIN'
        ),
        rows=(
            (source_revision, audience, int(player_id), _json_text(player))
            for player_id, player in dataset.players.items()
        ),
    )
    ratings = _rating_rows(dataset)
    _insert_rows(
        connection,
        insert_sql=(
            'INSERT INTO dashboard_cache_matches('
            'source_revision,audience,match_id,player_id,season_key,mode,'
            'played_at_epoch,result,duration_seconds,has_replay,'
            'match_json,ratings_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)'
        ),
        copy_sql=(
            'COPY dashboard_cache_matches('
            'source_revision,audience,match_id,player_id,season_key,mode,'
            'played_at_epoch,result,duration_seconds,has_replay,'
            'match_json,ratings_json) FROM STDIN'
        ),
        rows=(
            (
                source_revision,
                audience,
                int(match['id']),
                int(match['playerId']),
                str(match['seasonKey']),
                str(match['mode']),
                _played_at_epoch(match['playedAt']),
                str(match['result']),
                int(match['durationSeconds']),
                int(match.get('replay') is not None),
                _json_text(match),
                _json_text(ratings.get(int(match['id']), {})),
            )
            for match in dataset.matches
        ),
    )
    _insert_rows(
        connection,
        insert_sql=(
            'INSERT INTO dashboard_cache_match_search('
            'source_revision,audience,match_id,form_index,normalized,pinyin,initials'
            ') VALUES(?,?,?,?,?,?,?)'
        ),
        copy_sql=(
            'COPY dashboard_cache_match_search('
            'source_revision,audience,match_id,form_index,normalized,pinyin,initials'
            ') FROM STDIN'
        ),
        rows=(
            (
                source_revision,
                audience,
                int(match_id),
                index,
                str(forms[0]),
                str(forms[1]),
                str(forms[2]),
            )
            for match_id, values in dataset.search_forms.items()
            for index, forms in enumerate(values)
        ),
    )
    _insert_rows(
        connection,
        insert_sql=(
            'INSERT INTO dashboard_cache_match_heroes('
            'source_revision,audience,match_id,hero_name) VALUES(?,?,?,?)'
        ),
        copy_sql=(
            'COPY dashboard_cache_match_heroes('
            'source_revision,audience,match_id,hero_name) FROM STDIN'
        ),
        rows=(
            (source_revision, audience, int(match_id), str(hero_name))
            for match_id, heroes in dataset.heroes.items()
            for hero_name in sorted(heroes)
        ),
    )


def publish_dashboard_cache(
    database_target: DatabaseTarget, state: Any, *, published_at: Optional[int] = None
) -> int:
    public_revision = int(state.public.source_revision)
    owner_revision = int(state.owner.source_revision)
    if public_revision <= 0 or owner_revision <= 0:
        raise ValueError('dashboard cache source revision must be positive')
    if public_revision != owner_revision:
        raise ValueError('dashboard cache audiences must use the same source revision')
    publication_time = int(time.time()) if published_at is None else published_at
    if publication_time <= 0:
        raise ValueError('dashboard cache publication time must be positive')

    connection = connect_database(database_target)
    try:
        connection.execute('BEGIN IMMEDIATE')
        if is_postgres(database_target):
            connection.execute('SELECT pg_advisory_xact_lock(8675309003)')
        lock_clause = ' FOR UPDATE' if is_postgres(database_target) else ''
        active_rows = connection.execute(
            'SELECT source_revision FROM dashboard_cache_state' + lock_clause
        ).fetchall()
        if any(int(row['source_revision']) > public_revision for row in active_rows):
            raise ValueError('dashboard cache already contains a newer revision')
        for audience in AUDIENCES:
            _publish_dataset(
                connection,
                audience=audience,
                dataset=getattr(state, audience),
                published_at=publication_time,
            )
        connection.executemany(
            'INSERT INTO dashboard_cache_state('
            'audience,source_revision,published_at) VALUES(?,?,?) '
            'ON CONFLICT(audience) DO UPDATE SET '
            'source_revision=excluded.source_revision,'
            'published_at=excluded.published_at',
            ((audience, public_revision, publication_time) for audience in AUDIENCES),
        )
        connection.execute(
            'DELETE FROM dashboard_cache_generations WHERE source_revision NOT IN('
            'SELECT source_revision FROM('
            'SELECT DISTINCT source_revision FROM dashboard_cache_generations '
            'ORDER BY source_revision DESC LIMIT 2) retained)'
        )
        connection.commit()
        return public_revision
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def rebuild_dashboard_cache(
    source_target: DatabaseTarget,
    database_target: DatabaseTarget,
    *,
    runtime_loader: RuntimeLoader = load_runtime_source,
) -> int:
    source_revision, runtime = runtime_loader(source_target)
    state = build_repository_state(source_revision, runtime)
    return publish_dashboard_cache(database_target, state)


@dataclass(frozen=True)
class _CachedView:
    source_revision: int
    dashboard_payload: bytes
    live_rooms_payload: bytes


@dataclass(frozen=True)
class _CachedState:
    public: _CachedView
    owner: _CachedView


CacheBuilder = Callable[[], int]


def _load_cached_state(database_target: DatabaseTarget) -> _CachedState:
    connection = connect_database(database_target)
    try:
        rows = connection.execute(
            'SELECT state.audience,state.source_revision,'
            'generation.dashboard_payload,generation.live_rooms_payload '
            'FROM dashboard_cache_state state '
            'JOIN dashboard_cache_generations generation '
            'ON generation.source_revision=state.source_revision '
            'AND generation.audience=state.audience '
            'ORDER BY state.audience'
        ).fetchall()
    finally:
        connection.close()
    values = {
        str(row['audience']): _CachedView(
            source_revision=int(row['source_revision']),
            dashboard_payload=bytes(row['dashboard_payload']),
            live_rooms_payload=bytes(row['live_rooms_payload']),
        )
        for row in rows
    }
    if set(values) != set(AUDIENCES):
        raise RuntimeError('dashboard PostgreSQL cache has no complete publication')
    if values['public'].source_revision != values['owner'].source_revision:
        raise RuntimeError('dashboard PostgreSQL cache audiences are inconsistent')
    return _CachedState(public=values['public'], owner=values['owner'])


class PostgresDashboardRepository:
    """Serve bounded dashboard reads from a revisioned PostgreSQL cache.

    SQLite is accepted as a local test backend; production uses the same schema in
    PostgreSQL. The process retains only the two serialized dashboard/live-room
    artifacts. Match, search, hero, and rating state remains database-resident.
    """

    def __init__(
        self,
        *,
        source_target: DatabaseTarget,
        auxiliary_target: DatabaseTarget,
        revision_loader: RevisionLoader = read_source_revision,
        cache_builder: Optional[CacheBuilder] = None,
    ) -> None:
        self._source_target = source_target
        self._auxiliary_target = auxiliary_target
        self._revision_loader = revision_loader
        self._cache_builder = cache_builder
        self._state_lock = RLock()
        self._refresh_lock = Lock()
        self._state: Optional[_CachedState] = None

    def refresh(self, *, force: bool = False) -> bool:
        with self._refresh_lock:
            return self._refresh(force=force)

    def _refresh(self, *, force: bool) -> bool:
        expected_revision = self._revision_loader(self._source_target)
        with self._state_lock:
            previous = self._state
        if (
            not force
            and previous is not None
            and previous.public.source_revision == expected_revision
        ):
            return False
        try:
            next_state = _load_cached_state(self._auxiliary_target)
        except RuntimeError:
            if self._cache_builder is None:
                raise
            self._cache_builder()
            next_state = _load_cached_state(self._auxiliary_target)
        if next_state.public.source_revision < expected_revision:
            if self._cache_builder is None:
                raise RuntimeError(
                    'dashboard PostgreSQL cache revision {} is behind '
                    'source {}'.format(
                        next_state.public.source_revision, expected_revision
                    )
                )
            self._cache_builder()
            next_state = _load_cached_state(self._auxiliary_target)
        if next_state.public.source_revision < expected_revision:
            raise RuntimeError(
                'dashboard PostgreSQL cache builder did not publish '
                'source revision {}'.format(expected_revision)
            )
        with self._state_lock:
            self._state = next_state
        return (
            previous is None
            or previous.public.source_revision != next_state.public.source_revision
        )

    def source_revision(self) -> int:
        return self._revision_loader(self._source_target)

    def _current(self, *, owner_view: bool = False) -> _CachedView:
        with self._state_lock:
            if self._state is None:
                raise RuntimeError('dashboard PostgreSQL cache has not been loaded')
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

    @staticmethod
    def _audience(owner_view: bool) -> str:
        return 'owner' if owner_view else 'public'

    @staticmethod
    def _filters(
        *,
        revision: int,
        audience: str,
        season: Optional[str],
        mode: Optional[str],
        player_id: Optional[int],
        query: str,
        heroes: Sequence[str],
    ) -> Tuple[str, list[Any]]:
        clauses = ['matches.source_revision=?', 'matches.audience=?']
        parameters: list[Any] = [revision, audience]
        if season is not None:
            clauses.append('matches.season_key=?')
            parameters.append(season)
        if mode is not None:
            clauses.append('matches.mode=?')
            parameters.append(mode)
        if player_id is not None:
            clauses.append('matches.player_id=?')
            parameters.append(player_id)
        normalized_query = _normalize_search(query)
        if normalized_query:
            clauses.append(
                'EXISTS(SELECT 1 FROM dashboard_cache_match_search search '
                'WHERE search.source_revision=matches.source_revision '
                'AND search.audience=matches.audience '
                'AND search.match_id=matches.match_id '
                'AND (search.normalized LIKE ? OR search.pinyin LIKE ? '
                'OR search.initials LIKE ?))'
            )
            pattern = '%{}%'.format(normalized_query)
            parameters.extend((pattern, pattern, pattern))
        for hero in dict.fromkeys(value.casefold() for value in heroes):
            clauses.append(
                'EXISTS(SELECT 1 FROM dashboard_cache_match_heroes hero '
                'WHERE hero.source_revision=matches.source_revision '
                'AND hero.audience=matches.audience '
                'AND hero.match_id=matches.match_id AND hero.hero_name=?)'
            )
            parameters.append(hero)
        return ' AND '.join(clauses), parameters

    @staticmethod
    def _row_value(
        row: Mapping[str, Any],
        *,
        rating_scope: str,
        rating_season: Optional[str],
        result_image: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        match = dict(json.loads(str(row['match_json'])))
        player = json.loads(str(row['player_json']))
        ratings = json.loads(str(row['ratings_json']))
        season_key = str(match['seasonKey']) if rating_season is None else rating_season
        match['player'] = player
        match['rating'] = ratings.get('{}|{}'.format(rating_scope, season_key))
        match['resultImage'] = result_image
        return match

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
    ) -> Dict[str, Any]:
        current = self._current(owner_view=owner_view)
        audience = self._audience(owner_view)
        where, parameters = self._filters(
            revision=current.source_revision,
            audience=audience,
            season=season,
            mode=mode,
            player_id=player_id,
            query=query,
            heroes=heroes,
        )
        connection = connect_database(self._auxiliary_target)
        try:
            connection.execute('BEGIN')
            if is_postgres(self._auxiliary_target):
                connection.execute(
                    'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'
                )
            total_row = connection.execute(
                'SELECT COUNT(*) AS total FROM dashboard_cache_matches matches WHERE '
                + where,
                parameters,
            ).fetchone()
            rows = connection.execute(
                'SELECT matches.match_id,matches.match_json,matches.ratings_json,'
                'players.player_json,owner_matches.match_json AS owner_match_json '
                'FROM dashboard_cache_matches matches '
                'JOIN dashboard_cache_players players '
                'ON players.source_revision=matches.source_revision '
                'AND players.audience=matches.audience '
                'AND players.player_id=matches.player_id '
                'LEFT JOIN dashboard_cache_matches owner_matches '
                'ON owner_matches.source_revision=matches.source_revision '
                "AND owner_matches.audience='owner' "
                'AND owner_matches.match_id=matches.match_id WHERE '
                + where
                + ' ORDER BY matches.played_at_epoch DESC,matches.match_id DESC '
                'LIMIT ? OFFSET ?',
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        match_ids = [int(row['match_id']) for row in rows]
        assets = get_match_assets(self._auxiliary_target, match_ids)
        items = [
            self._row_value(
                row,
                rating_scope=rating_scope,
                rating_season=rating_season,
                result_image=assets.get(int(row['match_id'])),
            )
            for row in rows
        ]
        candidates = {
            int(row['match_id']): json.loads(str(row['owner_match_json']))
            for row in rows
            if row['owner_match_json'] is not None
        }
        return {
            'items': resolve_match_replays(
                self._auxiliary_target, items, candidates, owner_view=owner_view
            ),
            'page': page,
            'pageSize': page_size,
            'total': 0 if total_row is None else int(total_row['total']),
        }

    def get_match(
        self,
        match_id: int,
        *,
        rating_scope: str,
        rating_season: Optional[str],
        owner_view: bool = False,
    ) -> Mapping[str, Any]:
        current = self._current(owner_view=owner_view)
        connection = connect_database(self._auxiliary_target)
        try:
            row = connection.execute(
                'SELECT matches.match_id,matches.match_json,matches.ratings_json,'
                'players.player_json,owner_matches.match_json AS owner_match_json '
                'FROM dashboard_cache_matches matches '
                'JOIN dashboard_cache_players players '
                'ON players.source_revision=matches.source_revision '
                'AND players.audience=matches.audience '
                'AND players.player_id=matches.player_id '
                'LEFT JOIN dashboard_cache_matches owner_matches '
                'ON owner_matches.source_revision=matches.source_revision '
                "AND owner_matches.audience='owner' "
                'AND owner_matches.match_id=matches.match_id '
                'WHERE matches.source_revision=? AND matches.audience=? '
                'AND matches.match_id=?',
                (current.source_revision, self._audience(owner_view), match_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise LookupError('match not found')
        image = get_match_assets(self._auxiliary_target, (match_id,)).get(match_id)
        value = self._row_value(
            row,
            rating_scope=rating_scope,
            rating_season=rating_season,
            result_image=image,
        )
        candidates = (
            {}
            if row['owner_match_json'] is None
            else {match_id: json.loads(str(row['owner_match_json']))}
        )
        return resolve_match_replays(
            self._auxiliary_target, (value,), candidates, owner_view=owner_view
        )[0]

    def match_summary(
        self,
        *,
        season: Optional[str],
        mode: Optional[str],
        player_id: Optional[int],
        owner_view: bool = False,
    ) -> Mapping[str, int]:
        current = self._current(owner_view=owner_view)
        where, parameters = self._filters(
            revision=current.source_revision,
            audience=self._audience(owner_view),
            season=season,
            mode=mode,
            player_id=player_id,
            query='',
            heroes=(),
        )
        connection = connect_database(self._auxiliary_target)
        try:
            row = connection.execute(
                'SELECT COUNT(*) AS matches,'
                "COALESCE(SUM(CASE WHEN result='W' THEN 1 ELSE 0 END),0) AS wins,"
                'COUNT(DISTINCT player_id) AS players,'
                'COALESCE(SUM(duration_seconds),0) AS duration_seconds,'
                'COALESCE(SUM(has_replay),0) AS replays '
                'FROM dashboard_cache_matches matches WHERE ' + where,
                parameters,
            ).fetchone()
        finally:
            connection.close()
        matches = 0 if row is None else int(row['matches'])
        duration = 0 if row is None else int(row['duration_seconds'])
        return {
            'matches': matches,
            'wins': 0 if row is None else int(row['wins']),
            'players': 0 if row is None else int(row['players']),
            'averageDurationSeconds': 0 if not matches else round(duration / matches),
            'replays': 0 if row is None else int(row['replays']),
        }
