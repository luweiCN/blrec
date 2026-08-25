from __future__ import annotations

import ctypes
import gc
import json
import logging
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
from threading import Lock, RLock
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from blrec_dashboard_publisher.deduplication import exact_match_fingerprint
from blrec_dashboard_publisher.rating import (
    RATING_MODEL_VERSION,
    calculate_virtual_match_rating_timeline,
    resolve_afk_rating_adjustment,
)
from blrec_dashboard_publisher.snapshot import SHANGHAI, build_dashboard_runtime_source
from pypinyin import Style, lazy_pinyin

from .assets import get_match_assets
from .dashboard import MAX_TREND_PUBLICATIONS, current_dashboard_publication
from .database import DatabaseTarget, connect_database, is_postgres
from .replay_visibility import resolve_match_replays

LOGGER = logging.getLogger(__name__)

RuntimeLoader = Callable[[DatabaseTarget], Tuple[int, Mapping[str, Any]]]
RevisionLoader = Callable[[DatabaseTarget], int]


def _load_malloc_trim() -> Optional[Callable[[int], int]]:
    if not sys.platform.startswith('linux'):
        return None
    try:
        trim = ctypes.CDLL(None).malloc_trim
    except (AttributeError, OSError):
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return trim


_MALLOC_TRIM = _load_malloc_trim()


def _release_unused_process_memory() -> None:
    gc.collect()
    if _MALLOC_TRIM is not None:
        _MALLOC_TRIM(0)


@dataclass(frozen=True)
class _Dataset:
    source_revision: int
    dashboard_payload: bytes
    players: Mapping[int, Mapping[str, Any]]
    matches: Tuple[Mapping[str, Any], ...]
    matches_by_id: Mapping[int, Mapping[str, Any]]
    search_forms: Mapping[int, Tuple[Tuple[str, str, str], ...]]
    heroes: Mapping[int, frozenset[str]]
    ratings: Mapping[Tuple[int, str, str], Mapping[str, Any]]
    live_rooms: Mapping[str, Any]


@dataclass(frozen=True)
class _RepositoryState:
    public: _Dataset
    owner: _Dataset


@dataclass
class _TrendPerformance:
    rating_score: float
    ranking_score: float
    matches: int
    wins: int


def read_source_revision(database_target: DatabaseTarget) -> int:
    connection = connect_database(database_target)
    try:
        row = connection.execute(
            'SELECT revision FROM dashboard_source_state WHERE singleton_id=1'
        ).fetchone()
        if row is None or int(row['revision']) <= 0:
            raise RuntimeError('dashboard source revision is missing')
        return int(row['revision'])
    finally:
        connection.close()


def load_runtime_source(
    database_target: DatabaseTarget,
) -> Tuple[int, Mapping[str, Any]]:
    connection = connect_database(database_target)
    try:
        connection.execute('BEGIN')
        if is_postgres(database_target):
            connection.execute(
                'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'
            )
        row = connection.execute(
            'SELECT revision FROM dashboard_source_state WHERE singleton_id=1'
        ).fetchone()
        if row is None or int(row['revision']) <= 0:
            raise RuntimeError('dashboard source revision is missing')
        revision = int(row['revision'])
        runtime = build_dashboard_runtime_source(connection)
        connection.commit()
        return revision, runtime
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _normalize_search(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', value).casefold()
    return ''.join(character for character in normalized if character.isalnum())


def _search_forms(value: str) -> Tuple[str, str, str]:
    return (
        _normalize_search(value),
        _normalize_search(''.join(lazy_pinyin(value, style=Style.NORMAL))),
        _normalize_search(''.join(lazy_pinyin(value, style=Style.FIRST_LETTER))),
    )


def _match_fingerprint(match: Mapping[str, Any]) -> Optional[str]:
    ally = match['ally']
    enemy = match['enemy']
    winner_side = ally['side'] if match['result'] == 'W' else enemy['side']
    teams = []
    for team in (ally, enemy):
        teams.append(
            {
                'side': team['side'],
                'kills': team.get('kills'),
                'economy': team.get('economy'),
                'players': [
                    {
                        'hero_name': player.get('heroName'),
                        'kills': player.get('kills'),
                        'deaths': player.get('deaths'),
                        'assists': player.get('assists'),
                        'economy': player.get('economy'),
                    }
                    for player in team['players']
                ],
            }
        )
    return exact_match_fingerprint(
        mode=str(match['mode']),
        duration_seconds=int(match['durationSeconds']),
        winner_side=str(winner_side),
        teams=teams,
    )


def _rating_value(
    transition: Any, *, scope: str, season_key: str, match_number: int
) -> Mapping[str, Any]:
    after = transition.rating_after
    return {
        'scope': scope,
        'seasonKey': season_key,
        'matchNumber': match_number,
        'scoreBefore': int(transition.score_before),
        'scoreDelta': int(transition.score_delta),
        'scoreAfter': int(transition.score_after),
        'provisional': bool(after.provisional),
        'modelVersion': RATING_MODEL_VERSION,
        'afkAdjustment': transition.afk_adjustment.kind,
        'afkPlayerDeficit': transition.afk_adjustment.net_player_deficit,
    }


def _match_afk_adjustment(match: Mapping[str, Any]) -> Any:
    ally_players = tuple(match['ally']['players'])
    enemy_players = tuple(match['enemy']['players'])
    recorded = next(
        (player for player in ally_players if player.get('isRecordedPlayer')), None
    )
    if recorded is None:
        return resolve_afk_rating_adjustment(
            result=str(match['result']),
            recorded_status='unknown',
            teammate_statuses=(),
            enemy_statuses=(),
        )
    return resolve_afk_rating_adjustment(
        result=str(match['result']),
        recorded_status=str(recorded.get('afkStatus', 'unknown')),
        teammate_statuses=tuple(
            str(player.get('afkStatus', 'unknown'))
            for player in ally_players
            if player is not recorded
        ),
        enemy_statuses=tuple(
            str(player.get('afkStatus', 'unknown')) for player in enemy_players
        ),
    )


def _rating_events(
    matches: Sequence[Mapping[str, Any]]
) -> Mapping[Tuple[int, str, str], Mapping[str, Any]]:
    events: Dict[Tuple[int, str, str], Mapping[str, Any]] = {}
    ordered = sorted(
        matches,
        key=lambda match: (
            int(match['playerId']),
            str(match['playedAt']),
            int(match['id']),
        ),
    )
    for _player_id, player_group in groupby(
        ordered, key=lambda match: int(match['playerId'])
    ):
        player_matches = []
        seen_fingerprints = set()
        for match in player_group:
            if match.get('duplicateOfMatchId') is not None:
                continue
            fingerprint = _match_fingerprint(match)
            if fingerprint is not None:
                if fingerprint in seen_fingerprints:
                    continue
                seen_fingerprints.add(fingerprint)
            player_matches.append(match)
        for scope in ('all', '3v3', 'brawl', '5v5'):
            scoped = [
                match
                for match in player_matches
                if scope == 'all' or match['mode'] == scope
            ]
            if not scoped:
                continue
            previous_ability: Optional[float] = None
            previous_evidence: Optional[float] = None
            for season_key, season_group in groupby(
                scoped, key=lambda match: str(match['seasonKey'])
            ):
                season_matches = list(season_group)
                timeline = calculate_virtual_match_rating_timeline(
                    results=[str(match['result']) for match in season_matches],
                    afk_adjustments=[
                        _match_afk_adjustment(match) for match in season_matches
                    ],
                    previous_ability=previous_ability,
                    previous_evidence=previous_evidence,
                    reset_visible_score=True,
                )
                for number, (match, transition) in enumerate(
                    zip(season_matches, timeline), start=1
                ):
                    events[(int(match['id']), scope, season_key)] = _rating_value(
                        transition,
                        scope=scope,
                        season_key=season_key,
                        match_number=number,
                    )
                if timeline:
                    final = timeline[-1].rating_after
                    previous_ability = final.ability
                    previous_evidence = final.evidence
            timeline = calculate_virtual_match_rating_timeline(
                results=[str(match['result']) for match in scoped],
                afk_adjustments=[_match_afk_adjustment(match) for match in scoped],
                reset_visible_score=False,
            )
            for number, (match, transition) in enumerate(
                zip(scoped, timeline), start=1
            ):
                events[(int(match['id']), scope, 'all-time')] = _rating_value(
                    transition, scope=scope, season_key='all-time', match_number=number
                )
    return events


def _match_publication_date(match: Mapping[str, Any]) -> str:
    value = str(match['playedAt'])
    moment = datetime.fromisoformat(
        value[:-1] + '+00:00' if value.endswith('Z') else value
    )
    if moment.tzinfo is None:
        raise ValueError('dashboard match time must include a timezone')
    return moment.astimezone(SHANGHAI).date().isoformat()


def _rank_trend_performances(
    performances: Mapping[int, _TrendPerformance]
) -> List[Mapping[str, Any]]:
    candidates = sorted(
        performances.items(),
        key=lambda item: (
            -item[1].ranking_score,
            -item[1].matches,
            -(item[1].wins / item[1].matches if item[1].matches else 0.0),
            item[0],
        ),
    )
    return [
        {'playerId': player_id, 'rank': rank, 'ratingScore': performance.rating_score}
        for rank, (player_id, performance) in enumerate(candidates, start=1)
    ]


def _trend_standings_from_state(
    state: Mapping[str, Mapping[str, Mapping[int, _TrendPerformance]]]
) -> Mapping[str, Any]:
    return {
        season_key: {
            mode: _rank_trend_performances(modes.get(mode, {}))
            for mode in ('all', '3v3', 'brawl', '5v5')
        }
        for season_key, modes in state.items()
    }


def _rating_trends(
    snapshot: Mapping[str, Any],
    matches: Sequence[Mapping[str, Any]],
    ratings: Mapping[Tuple[int, str, str], Mapping[str, Any]],
) -> Mapping[str, Any]:
    state: Dict[str, Dict[str, Dict[int, _TrendPerformance]]] = {}
    publications: List[Mapping[str, Any]] = []
    source_last_match_id = 0
    ordered = sorted(
        matches, key=lambda match: (str(match['playedAt']), int(match['id']))
    )
    for publication_date, date_group in groupby(ordered, key=_match_publication_date):
        changed = False
        for match in date_group:
            match_id = int(match['id'])
            player_id = int(match['playerId'])
            season_key = str(match['seasonKey'])
            mode = str(match['mode'])
            if (match_id, 'all', season_key) not in ratings:
                continue
            changed = True
            source_last_match_id = max(source_last_match_id, match_id)
            for scope in ('all', mode):
                for rating_season in (season_key, 'all-time'):
                    event = ratings.get((match_id, scope, rating_season))
                    if event is None:
                        continue
                    performances = state.setdefault(rating_season, {}).setdefault(
                        scope, {}
                    )
                    previous = performances.get(player_id)
                    rating_score = int(event['scoreAfter']) / 3
                    ranking_score = (
                        rating_score
                        if rating_season == 'all-time'
                        else max(
                            rating_score,
                            int(event.get('scoreBefore', event['scoreAfter'])) / 3,
                            (
                                previous.ranking_score
                                if previous is not None
                                else rating_score
                            ),
                        )
                    )
                    performances[player_id] = _TrendPerformance(
                        rating_score=rating_score,
                        ranking_score=ranking_score,
                        matches=1 if previous is None else previous.matches + 1,
                        wins=(
                            int(str(match['result']) == 'W')
                            if previous is None
                            else previous.wins + int(str(match['result']) == 'W')
                        ),
                    )
        if changed:
            publications.append(
                {
                    'snapshotId': 'match-history-{}-{}'.format(
                        publication_date, source_last_match_id
                    ),
                    'publicationDate': publication_date,
                    'sourceLastMatchId': source_last_match_id,
                    'standings': _trend_standings_from_state(state),
                }
            )

    current = current_dashboard_publication(snapshot)
    current_date = str(current['publicationDate'])
    publications = [
        publication
        for publication in publications
        if str(publication['publicationDate']) != current_date
    ]
    publications.append(current)
    publications.sort(key=lambda publication: str(publication['publicationDate']))
    return {
        'schemaVersion': 1,
        'updatedAt': str(snapshot['generatedAt']),
        'publications': publications[-MAX_TREND_PUBLICATIONS:],
    }


def _dataset(
    source_revision: int,
    runtime: Mapping[str, Any],
    *,
    owner_view: bool,
    shared_owner: Optional[_Dataset] = None,
) -> _Dataset:
    snapshot_source = (
        runtime['snapshot']
        if owner_view
        else runtime.get('publicSnapshot', runtime['snapshot'])
    )
    snapshot = dict(snapshot_source)
    snapshot['matches'] = []
    if shared_owner is None:
        players = {
            int(player['id']): dict(player)
            for player in runtime.get('players', ())
            if owner_view or bool(player.get('publicVisible', True))
        }
    else:
        players = {
            int(player['id']): shared_owner.players[int(player['id'])]
            for player in runtime.get('players', ())
            if bool(player.get('publicVisible', True))
        }

    def match_value(source: Mapping[str, Any]) -> Mapping[str, Any]:
        value = dict(source)
        for team_name in ('ally', 'enemy'):
            team = dict(value[team_name])
            players = []
            for source_player in team['players']:
                player = dict(source_player)
                player.setdefault('afkStatus', 'unknown')
                players.append(player)
            team['players'] = players
            value[team_name] = team
        access = str(value.pop('replayAccess', 'public'))
        if not owner_view and access != 'public':
            value.pop('replay', None)
        return value

    if shared_owner is None:
        matches = tuple(
            sorted(
                (
                    match_value(match)
                    for match in runtime.get('matches', ())
                    if int(match['playerId']) in players
                ),
                key=lambda match: (str(match['playedAt']), int(match['id'])),
                reverse=True,
            )
        )
        forms: Dict[int, Tuple[Tuple[str, str, str], ...]] = {}
        heroes: Dict[int, frozenset[str]] = {}
        for match in matches:
            match_id = int(match['id'])
            player = players[int(match['playerId'])]
            segments = [
                str(match.get('streamTitle') or ''),
                str(player['name']),
                str(player['roomLabel']),
                *(str(value) for value in player.get('aliases', ())),
                *(str(value) for value in player.get('roomIds', ())),
                *(
                    str(participant['name'])
                    for team in (match['ally'], match['enemy'])
                    for participant in team['players']
                ),
            ]
            forms[match_id] = tuple(_search_forms(value) for value in segments if value)
            heroes[match_id] = frozenset(
                str(participant['heroName']).casefold()
                for team in (match['ally'], match['enemy'])
                for participant in team['players']
                if participant.get('heroName')
            )
    else:
        replay_access = {
            int(match['id']): str(match.get('replayAccess', 'public'))
            for match in runtime.get('matches', ())
        }
        selected_matches = []
        for match in shared_owner.matches:
            if int(match['playerId']) not in players:
                continue
            if replay_access.get(int(match['id']), 'public') == 'public':
                selected_matches.append(match)
                continue
            sanitized = dict(match)
            sanitized.pop('replay', None)
            selected_matches.append(sanitized)
        matches = tuple(selected_matches)
        match_ids = {int(match['id']) for match in matches}
        forms = {
            match_id: value
            for match_id, value in shared_owner.search_forms.items()
            if match_id in match_ids
        }
        heroes = {
            match_id: value
            for match_id, value in shared_owner.heroes.items()
            if match_id in match_ids
        }
    live_rooms: List[Dict[str, Any]] = [
        {
            'roomId': int(room['roomId']),
            'playerId': player_id,
            'title': str(room['title']),
            'startedAt': str(room['startedAt']),
        }
        for player_id, player in players.items()
        for room in player.get('liveRooms', ())
    ]
    live_rooms.sort(
        key=lambda room: (str(room['startedAt']), int(room['roomId'])), reverse=True
    )
    ratings = (
        shared_owner.ratings if shared_owner is not None else _rating_events(matches)
    )
    dashboard_payload = json.dumps(
        {'snapshot': snapshot, 'trends': _rating_trends(snapshot, matches, ratings)},
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ':'),
    ).encode('utf-8')
    return _Dataset(
        source_revision=source_revision,
        dashboard_payload=dashboard_payload,
        players=players,
        matches=matches,
        matches_by_id={int(match['id']): match for match in matches},
        search_forms=forms,
        heroes=heroes,
        ratings=ratings,
        live_rooms={
            'schemaVersion': 1,
            'updatedAt': str(snapshot['generatedAt']),
            'rooms': live_rooms,
        },
    )


def build_repository_state(
    source_revision: int, runtime: Mapping[str, Any]
) -> _RepositoryState:
    owner_dataset = _dataset(source_revision, runtime, owner_view=True)
    return _RepositoryState(
        public=_dataset(
            source_revision, runtime, owner_view=False, shared_owner=owner_dataset
        ),
        owner=owner_dataset,
    )


class DirectDashboardRepository:
    def __init__(
        self,
        *,
        source_target: DatabaseTarget,
        auxiliary_target: DatabaseTarget,
        revision_loader: RevisionLoader = read_source_revision,
        runtime_loader: RuntimeLoader = load_runtime_source,
    ) -> None:
        self._source_target = source_target
        self._auxiliary_target = auxiliary_target
        self._revision_loader = revision_loader
        self._runtime_loader = runtime_loader
        self._state_lock = RLock()
        self._refresh_lock = Lock()
        self._state: Optional[_RepositoryState] = None

    def refresh(self, *, force: bool = False) -> bool:
        with self._refresh_lock:
            started_at = time.perf_counter()
            expected_revision = self._revision_loader(self._source_target)
            with self._state_lock:
                current = self._state
            if (
                not force
                and current is not None
                and current.public.source_revision == expected_revision
            ):
                return False
            source_revision, runtime = self._runtime_loader(self._source_target)
            if source_revision < expected_revision:
                raise RuntimeError('dashboard source changed during refresh')
            previous_revision = (
                None if current is None else current.public.source_revision
            )
            next_state = build_repository_state(source_revision, runtime)
            with self._state_lock:
                self._state = next_state
            del current
            del runtime
            _release_unused_process_memory()
            LOGGER.info(
                'dashboard cache refreshed source_revision=%s '
                'public_players=%s public_matches=%s owner_players=%s '
                'owner_matches=%s seconds=%.3f',
                source_revision,
                len(next_state.public.players),
                len(next_state.public.matches),
                len(next_state.owner.players),
                len(next_state.owner.matches),
                time.perf_counter() - started_at,
            )
            return previous_revision != source_revision

    def source_revision(self) -> int:
        return self._revision_loader(self._source_target)

    def _current(self, *, owner_view: bool = False) -> _Dataset:
        with self._state_lock:
            if self._state is None:
                raise RuntimeError('dashboard source has not been loaded')
            return self._state.owner if owner_view else self._state.public

    def dashboard_document(
        self, *, owner_view: bool = False
    ) -> Tuple[Mapping[str, Any], str]:
        current = self._current(owner_view=owner_view)
        return json.loads(current.dashboard_payload), str(current.source_revision)

    def dashboard_payload(self, *, owner_view: bool = False) -> Tuple[bytes, str]:
        current = self._current(owner_view=owner_view)
        return current.dashboard_payload, str(current.source_revision)

    def live_rooms(self, *, owner_view: bool = False) -> Tuple[Mapping[str, Any], str]:
        current = self._current(owner_view=owner_view)
        return current.live_rooms, str(current.source_revision)

    def _public_match(
        self,
        current: _Dataset,
        match: Mapping[str, Any],
        *,
        rating_scope: str,
        rating_season: Optional[str],
        result_image: Optional[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        value = dict(match)
        match_id = int(match['id'])
        value['player'] = current.players[int(match['playerId'])]
        season_key = str(match['seasonKey']) if rating_season is None else rating_season
        value['rating'] = current.ratings.get((match_id, rating_scope, season_key))
        value['resultImage'] = result_image
        return value

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
        normalized_query = _normalize_search(query)
        required_heroes = {hero.casefold() for hero in heroes}
        matches = []
        for match in current.matches:
            match_id = int(match['id'])
            if season is not None and match['seasonKey'] != season:
                continue
            if mode is not None and match['mode'] != mode:
                continue
            if player_id is not None and int(match['playerId']) != player_id:
                continue
            if required_heroes and not required_heroes.issubset(
                current.heroes[match_id]
            ):
                continue
            if normalized_query and not any(
                normalized_query in form
                for forms in current.search_forms[match_id]
                for form in forms
            ):
                continue
            matches.append(match)
        total = len(matches)
        start = (page - 1) * page_size
        selected = matches[start : start + page_size]
        assets = get_match_assets(
            self._auxiliary_target, (int(match['id']) for match in selected)
        )
        items = [
            self._public_match(
                current,
                match,
                rating_scope=rating_scope,
                rating_season=rating_season,
                result_image=assets.get(int(match['id'])),
            )
            for match in selected
        ]
        owner_matches = self._current(owner_view=True).matches_by_id
        return {
            'items': resolve_match_replays(
                self._auxiliary_target,
                items,
                {
                    int(match['id']): owner_matches.get(int(match['id']), {})
                    for match in selected
                },
                owner_view=owner_view,
            ),
            'page': page,
            'pageSize': page_size,
            'total': total,
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
        match = current.matches_by_id.get(match_id)
        if match is None:
            raise LookupError('match not found')
        image = get_match_assets(self._auxiliary_target, (match_id,)).get(match_id)
        value = self._public_match(
            current,
            match,
            rating_scope=rating_scope,
            rating_season=rating_season,
            result_image=image,
        )
        owner_match = self._current(owner_view=True).matches_by_id.get(match_id, {})
        return resolve_match_replays(
            self._auxiliary_target,
            (value,),
            {match_id: owner_match},
            owner_view=owner_view,
        )[0]

    def match_summary(
        self,
        *,
        season: Optional[str],
        mode: Optional[str],
        player_id: Optional[int],
        owner_view: bool = False,
    ) -> Mapping[str, int]:
        matches = [
            match
            for match in self._current(owner_view=owner_view).matches
            if (season is None or match['seasonKey'] == season)
            and (mode is None or match['mode'] == mode)
            and (player_id is None or int(match['playerId']) == player_id)
        ]
        return {
            'matches': len(matches),
            'wins': sum(match['result'] == 'W' for match in matches),
            'players': len({int(match['playerId']) for match in matches}),
            'averageDurationSeconds': (
                0
                if not matches
                else round(
                    sum(int(match['durationSeconds']) for match in matches)
                    / len(matches)
                )
            ),
            'replays': sum(match.get('replay') is not None for match in matches),
        }
