from __future__ import annotations

import logging
import time
import unicodedata
from dataclasses import dataclass
from itertools import groupby
from threading import Lock, RLock
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from blrec_dashboard_publisher.deduplication import exact_match_fingerprint
from blrec_dashboard_publisher.rating import (
    RATING_MODEL_VERSION,
    calculate_virtual_match_rating_timeline,
)
from blrec_dashboard_publisher.snapshot import build_dashboard_runtime_source
from pypinyin import Style, lazy_pinyin

from .assets import get_match_assets
from .dashboard import (
    load_dashboard_trends,
    merge_current_dashboard_publication,
    persist_dashboard_publication,
)
from .database import DatabaseTarget, connect_database, is_postgres

LOGGER = logging.getLogger(__name__)

RuntimeLoader = Callable[[DatabaseTarget], Tuple[int, Mapping[str, Any]]]
RevisionLoader = Callable[[DatabaseTarget], int]


@dataclass(frozen=True)
class _Dataset:
    source_revision: int
    document: Mapping[str, Any]
    players: Mapping[int, Mapping[str, Any]]
    matches: Tuple[Mapping[str, Any], ...]
    matches_by_id: Mapping[int, Mapping[str, Any]]
    search_forms: Mapping[int, Tuple[Tuple[str, str, str], ...]]
    heroes: Mapping[int, frozenset[str]]
    ratings: Mapping[Tuple[int, str, str], Mapping[str, Any]]
    live_rooms: Mapping[str, Any]


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
                        'last_hits': player.get('lastHits'),
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
    }


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
                reset_visible_score=False,
            )
            for number, (match, transition) in enumerate(
                zip(scoped, timeline), start=1
            ):
                events[(int(match['id']), scope, 'all-time')] = _rating_value(
                    transition, scope=scope, season_key='all-time', match_number=number
                )
    return events


def _dataset(
    source_revision: int, runtime: Mapping[str, Any], trends: Mapping[str, Any]
) -> _Dataset:
    snapshot = dict(runtime['snapshot'])
    snapshot['matches'] = []
    players = {int(player['id']): dict(player) for player in runtime.get('players', ())}
    matches = tuple(
        sorted(
            (dict(match) for match in runtime.get('matches', ())),
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
    return _Dataset(
        source_revision=source_revision,
        document={'snapshot': snapshot, 'trends': trends},
        players=players,
        matches=matches,
        matches_by_id={int(match['id']): match for match in matches},
        search_forms=forms,
        heroes=heroes,
        ratings=_rating_events(matches),
        live_rooms={
            'schemaVersion': 1,
            'updatedAt': str(snapshot['generatedAt']),
            'rooms': live_rooms,
        },
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
        self._dataset: Optional[_Dataset] = None

    def refresh(self, *, force: bool = False) -> bool:
        with self._refresh_lock:
            started_at = time.perf_counter()
            expected_revision = self._revision_loader(self._source_target)
            with self._state_lock:
                current = self._dataset
            if (
                not force
                and current is not None
                and current.source_revision == expected_revision
            ):
                return False
            source_revision, runtime = self._runtime_loader(self._source_target)
            if source_revision < expected_revision:
                raise RuntimeError('dashboard source changed during refresh')
            snapshot = runtime['snapshot']
            try:
                persist_dashboard_publication(
                    self._auxiliary_target,
                    source_revision=source_revision,
                    snapshot=snapshot,
                )
                trends = load_dashboard_trends(self._auxiliary_target)
            except Exception:
                LOGGER.exception(
                    'failed to persist dashboard trend history; serving direct data'
                )
                try:
                    trends = load_dashboard_trends(self._auxiliary_target)
                except Exception:
                    trends = {
                        'schemaVersion': 1,
                        'updatedAt': str(snapshot['generatedAt']),
                        'publications': [],
                    }
                trends = merge_current_dashboard_publication(trends, snapshot)
            next_dataset = _dataset(source_revision, runtime, trends)
            with self._state_lock:
                self._dataset = next_dataset
            LOGGER.info(
                'dashboard cache refreshed source_revision=%s players=%s matches=%s '
                'seconds=%.3f',
                source_revision,
                len(next_dataset.players),
                len(next_dataset.matches),
                time.perf_counter() - started_at,
            )
            return current is None or current.source_revision != source_revision

    def source_revision(self) -> int:
        return self._revision_loader(self._source_target)

    def _current(self) -> _Dataset:
        with self._state_lock:
            if self._dataset is None:
                raise RuntimeError('dashboard source has not been loaded')
            return self._dataset

    def dashboard_document(self) -> Tuple[Mapping[str, Any], str]:
        current = self._current()
        return current.document, str(current.source_revision)

    def live_rooms(self) -> Tuple[Mapping[str, Any], str]:
        current = self._current()
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
    ) -> Dict[str, Any]:
        current = self._current()
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
        return {
            'items': [
                self._public_match(
                    current,
                    match,
                    rating_scope=rating_scope,
                    rating_season=rating_season,
                    result_image=assets.get(int(match['id'])),
                )
                for match in selected
            ],
            'page': page,
            'pageSize': page_size,
            'total': total,
        }

    def get_match(
        self, match_id: int, *, rating_scope: str, rating_season: Optional[str]
    ) -> Mapping[str, Any]:
        current = self._current()
        match = current.matches_by_id.get(match_id)
        if match is None:
            raise LookupError('match not found')
        image = get_match_assets(self._auxiliary_target, (match_id,)).get(match_id)
        return self._public_match(
            current,
            match,
            rating_scope=rating_scope,
            rating_season=rating_season,
            result_image=image,
        )

    def match_summary(
        self, *, season: Optional[str], mode: Optional[str], player_id: Optional[int]
    ) -> Mapping[str, int]:
        matches = [
            match
            for match in self._current().matches
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
