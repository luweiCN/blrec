from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from .rating import (
    CARRYOVER_MATCH_CAP,
    CATCHUP_LIMIT,
    CATCHUP_LOSS_MULTIPLIER,
    CATCHUP_PROTECTION_GAP,
    CATCHUP_RATE,
    MINIMUM_OUTCOME_DELTA,
    NEUTRAL_DISPLAY_SCORE,
    PRIOR_MATCHES,
    PROBABILITY_SCALE,
    PROVISIONAL_MATCHES,
    RATING_MODEL_VERSION,
    SEASON_RESET_DISPLAY_SCORE,
    RatingForecast,
    RatingGoalForecast,
    VirtualMatchRating,
    calculate_rating_forecast,
    calculate_virtual_match_rating,
)

__all__ = (
    'DashboardExportResult',
    'build_dashboard_api_source',
    'build_dashboard_snapshot',
    'build_dashboard_snapshot_from_records',
    'export_dashboard_files',
)


SHANGHAI = timezone(timedelta(hours=8))
PUBLIC_MODES = ('all', '3v3', 'brawl', '5v5')
RAW_MODE_TO_PUBLIC = {
    '3v3': '3v3',
    '5v5': '5v5',
    'aram': 'brawl',
    'other': 'brawl',
    'brawl': 'brawl',
}
SEASON_NAMES = {'spring': '春季赛', 'summer': '夏季赛', 'autumn': '秋季赛'}
HERO_SYNERGY_MIN_MATCHES = 5
HERO_SYNERGY_PRIOR_MATCHES = 5
HERO_SYNERGY_LIMIT = 3
QUERY_BATCH_SIZE = 500
REQUIRED_TABLES = frozenset(
    (
        'recording_sessions',
        'vainglory_heroes',
        'vainglory_matches',
        'vainglory_match_players',
        'vainglory_players',
        'vainglory_player_rooms',
        'vainglory_player_sessions',
        'vainglory_publications',
        'vainglory_scan_jobs',
    )
)


@dataclass(frozen=True)
class DashboardExportResult:
    manifest_path: Path
    snapshot_path: Path
    manifest: Mapping[str, Any]
    sha256: str


@dataclass(frozen=True)
class _Season:
    key: str
    year: int
    name: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class _PreviousRating:
    ability: float
    evidence: float


@dataclass
class _HeroUsageTotals:
    matches: int = 0
    wins: int = 0
    kda_matches: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    economy_matches: int = 0
    economy: int = 0
    economy_duration_seconds: int = 0

    def add(
        self,
        result: str,
        kills: Optional[int],
        deaths: Optional[int],
        assists: Optional[int],
        economy: Optional[int],
        duration_seconds: Optional[int],
    ) -> None:
        self.matches += 1
        if result == 'W':
            self.wins += 1
        if kills is not None and deaths is not None and assists is not None:
            self.kda_matches += 1
            self.kills += kills
            self.deaths += deaths
            self.assists += assists
        if (
            economy is not None
            and duration_seconds is not None
            and duration_seconds > 0
        ):
            self.economy_matches += 1
            self.economy += economy
            self.economy_duration_seconds += duration_seconds

    def public_value(self, name: str) -> Mapping[str, Any]:
        return {
            'name': name,
            'matches': self.matches,
            'wins': self.wins,
            'stats': {
                'kdaMatches': self.kda_matches,
                'kills': self.kills,
                'deaths': self.deaths,
                'assists': self.assists,
                'economyMatches': self.economy_matches,
                'economy': self.economy,
                'economyDurationSeconds': self.economy_duration_seconds,
            },
        }


@dataclass
class _Performance:
    matches: int = 0
    wins: int = 0
    results: Optional[List[str]] = None
    heroes: Optional[MutableMapping[str, _HeroUsageTotals]] = None

    def add(
        self,
        result: str,
        hero_name: str,
        kills: Optional[int],
        deaths: Optional[int],
        assists: Optional[int],
        economy: Optional[int],
        duration_seconds: Optional[int],
    ) -> None:
        self.matches += 1
        if result == 'W':
            self.wins += 1
        if self.results is None:
            self.results = []
        self.results.append(result)
        if not hero_name:
            return
        if self.heroes is None:
            self.heroes = {}
        hero = self.heroes.setdefault(hero_name, _HeroUsageTotals())
        hero.add(result, kills, deaths, assists, economy, duration_seconds)

    def top_hero(self) -> str:
        heroes: Mapping[str, _HeroUsageTotals] = self.heroes or {}
        if not heroes:
            return ''
        return min(
            heroes,
            key=lambda name: (
                -heroes[name].matches,
                -heroes[name].wins,
                name.casefold(),
                name,
            ),
        )

    def public_value(
        self,
        rating: Optional[VirtualMatchRating] = None,
        *,
        reset_visible_score: bool = True,
    ) -> Mapping[str, Any]:
        forecast = (
            calculate_rating_forecast(
                rating=rating,
                win_rate=self.wins / self.matches,
                reset_visible_score=reset_visible_score,
            )
            if rating is not None and self.matches > 0
            else None
        )
        return {
            'matches': self.matches,
            'wins': self.wins,
            'topHero': self.top_hero(),
            'form': list((self.results or [])[-5:]),
            'ratingScore': rating.score if rating is not None else None,
            'provisional': rating.provisional if rating is not None else False,
            'ratingForecast': _rating_forecast_value(forecast),
        }


@dataclass
class _HeroPerformance:
    matches: int = 0
    wins: int = 0
    players: Optional[Set[int]] = None

    def add(self, player_id: int, result: str) -> None:
        self.matches += 1
        if result == 'W':
            self.wins += 1
        if self.players is None:
            self.players = set()
        self.players.add(player_id)

    def public_value(self) -> Mapping[str, int]:
        return {
            'matches': self.matches,
            'wins': self.wins,
            'players': len(self.players or ()),
        }


def _rating_goal_forecast_value(
    forecast: Optional[RatingGoalForecast],
) -> Optional[Mapping[str, Any]]:
    if forecast is None:
        return None
    return {
        'targetDisplayScore': forecast.target_display_score,
        'allWinMatches': forecast.all_win_matches,
        'currentWinRateMatches': forecast.current_win_rate_matches,
    }


def _rating_forecast_value(
    forecast: Optional[RatingForecast],
) -> Optional[Mapping[str, Any]]:
    if forecast is None:
        return None
    return {
        'nextWinScore': forecast.next_win_score,
        'nextLossScore': forecast.next_loss_score,
        'nextDivision': _rating_goal_forecast_value(forecast.next_division),
        'nextTier': _rating_goal_forecast_value(forecast.next_tier),
        'ultimate': _rating_goal_forecast_value(forecast.ultimate),
    }


def _season_for(moment: datetime) -> _Season:
    local = moment.astimezone(SHANGHAI)
    if local.month < 5:
        name = 'spring'
        starts_at = datetime(local.year, 1, 1, tzinfo=SHANGHAI)
        ends_at = datetime(local.year, 5, 1, tzinfo=SHANGHAI)
    elif local.month < 9:
        name = 'summer'
        starts_at = datetime(local.year, 5, 1, tzinfo=SHANGHAI)
        ends_at = datetime(local.year, 9, 1, tzinfo=SHANGHAI)
    else:
        name = 'autumn'
        starts_at = datetime(local.year, 9, 1, tzinfo=SHANGHAI)
        ends_at = datetime(local.year + 1, 1, 1, tzinfo=SHANGHAI)
    return _Season(
        key='{}-{}'.format(starts_at.year, name),
        year=starts_at.year,
        name=name,
        starts_at=starts_at,
        ends_at=ends_at,
    )


def _format_period(season: _Season) -> str:
    final_day = season.ends_at - timedelta(days=1)
    if final_day.year == season.starts_at.year:
        return '{}.{:02d}.{:02d}—{:02d}.{:02d}'.format(
            season.starts_at.year,
            season.starts_at.month,
            season.starts_at.day,
            final_day.month,
            final_day.day,
        )
    return '{}.{:02d}.{:02d}—{}.{:02d}.{:02d}'.format(
        season.starts_at.year,
        season.starts_at.month,
        season.starts_at.day,
        final_day.year,
        final_day.month,
        final_day.day,
    )


def _season_option(season: _Season, current_key: str) -> Mapping[str, Any]:
    label = '{} {}'.format(season.year, SEASON_NAMES[season.name])
    current = season.key == current_key
    return {
        'key': season.key,
        'label': label,
        'shortLabel': (
            '本期 · {}'.format(SEASON_NAMES[season.name]) if current else label
        ),
        'period': _format_period(season),
        'current': current,
        'startsAt': season.starts_at.isoformat(),
        'endsAt': season.ends_at.isoformat(),
    }


def _validate_schema(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = REQUIRED_TABLES - tables
    if missing:
        raise sqlite3.DatabaseError(
            'dashboard source database is missing tables: {}'.format(
                ', '.join(sorted(missing))
            )
        )
    version_row = connection.execute(
        'SELECT COALESCE(MAX(version),0) FROM schema_migrations'
    ).fetchone()
    if version_row is None or int(version_row[0]) < 67:
        raise sqlite3.DatabaseError('dashboard source database requires schema 67+')


def _player_metadata(
    connection: sqlite3.Connection,
) -> Tuple[Mapping[int, Mapping[str, Any]], Mapping[int, List[str]]]:
    players: Dict[int, Dict[str, Any]] = {}
    for row in connection.execute(
        'SELECT id,name FROM vainglory_players ORDER BY id'
    ).fetchall():
        players[int(row['id'])] = {'name': str(row['name']), 'rooms': []}
    for row in connection.execute(
        'SELECT player_id,room_id FROM vainglory_player_rooms '
        'ORDER BY player_id,room_id'
    ).fetchall():
        player = players.get(int(row['player_id']))
        if player is not None:
            player['rooms'].append(int(row['room_id']))

    aliases: Dict[int, List[str]] = {player_id: [] for player_id in players}
    rows = connection.execute(
        'SELECT COALESCE(room.player_id,direct.player_id) AS player_id,'
        'trim(session.anchor_name) AS anchor_name '
        'FROM recording_sessions session '
        'LEFT JOIN vainglory_player_rooms room '
        'ON room.room_id=session.room_id AND session.room_id>0 '
        'LEFT JOIN vainglory_player_sessions direct '
        'ON direct.session_id=session.id '
        'WHERE COALESCE(room.player_id,direct.player_id) IS NOT NULL '
        'AND length(trim(COALESCE(session.anchor_name,\'\')))>0 '
        'GROUP BY COALESCE(room.player_id,direct.player_id),'
        'trim(session.anchor_name) '
        'ORDER BY COALESCE(room.player_id,direct.player_id),'
        'trim(session.anchor_name) COLLATE NOCASE'
    ).fetchall()
    for row in rows:
        player_id = int(row['player_id'])
        player = players.get(player_id)
        if player is None:
            continue
        alias = str(row['anchor_name'])
        if alias.casefold() != str(player['name']).casefold():
            aliases.setdefault(player_id, []).append(alias)
    return players, aliases


def _live_rooms_by_player(
    connection: sqlite3.Connection,
) -> Mapping[int, List[Mapping[str, Any]]]:
    values: Dict[int, List[Mapping[str, Any]]] = {}
    seen_rooms: Set[int] = set()
    rows = connection.execute(
        'SELECT room.player_id,session.room_id,session.title,'
        'COALESCE(session.live_start_time,session.started_at) AS live_started_at '
        'FROM recording_sessions session '
        'JOIN vainglory_player_rooms room ON room.room_id=session.room_id '
        "WHERE session.source_kind='live' AND session.state='open' "
        'AND session.live_end_time IS NULL AND session.ended_at IS NULL '
        'AND session.room_id>0 '
        'ORDER BY session.room_id,live_started_at DESC,session.id DESC'
    ).fetchall()
    for row in rows:
        room_id = int(row['room_id'])
        if room_id in seen_rooms:
            continue
        seen_rooms.add(room_id)
        player_id = int(row['player_id'])
        started_at = datetime.fromtimestamp(
            int(row['live_started_at']), tz=timezone.utc
        )
        values.setdefault(player_id, []).append(
            {
                'roomId': room_id,
                'title': str(row['title'] or ''),
                'startedAt': _utc_iso(started_at),
            }
        )
    return values


def _match_played_at(row: Mapping[str, Any]) -> int:
    part_started_at = int(row['record_start_time'])
    result_at_ms = int(row['result_at_ms'])
    started_at_ms = int(row['started_at_ms'])
    duration_seconds = int(row['duration_seconds'])
    if started_at_ms > 0:
        measured_duration = (result_at_ms - started_at_ms) / 1000
        if abs(measured_duration - duration_seconds) <= 30:
            return part_started_at + started_at_ms // 1000
    return part_started_at + (result_at_ms - duration_seconds * 1000) // 1000


def _match_rows(connection: sqlite3.Connection) -> List[Mapping[str, Any]]:
    rows = connection.execute(
        'SELECT player.id AS player_id,player.name AS player_name,'
        'session.id AS session_id,session.started_at,session.title AS stream_title,'
        'match.id AS match_id,match.result_frame_path,'
        'match.result_part_id,part.part_index,part.record_start_time,'
        'match.result_at_ms,match.started_at_ms,match.duration_seconds,'
        'match.game_mode,match.winner_side,match.recorded_player_side,'
        'match.recorded_player_slot,match.left_color,match.right_color,'
        'match.left_kills,match.right_kills,match.left_economy,'
        'match.right_economy,'
        "CASE match.winner_side WHEN 'left' THEN match.left_color "
        "WHEN 'right' THEN match.right_color ELSE 'unknown' END AS winner_color,"
        "COALESCE(hero.label,'') AS hero_name,recorded.kills,"
        'recorded.deaths,recorded.assists,recorded.economy '
        'FROM recording_sessions session '
        'LEFT JOIN vainglory_player_rooms room '
        'ON room.room_id=session.room_id AND session.room_id>0 '
        'LEFT JOIN vainglory_player_sessions direct '
        'ON direct.session_id=session.id '
        'JOIN vainglory_players player '
        'ON player.id=COALESCE(room.player_id,direct.player_id) '
        'JOIN vainglory_matches match ON match.session_id=session.id '
        'JOIN recording_parts part ON part.id=match.result_part_id '
        'JOIN vainglory_scan_jobs scan ON scan.session_id=session.id '
        'LEFT JOIN vainglory_match_players recorded '
        'ON recorded.match_id=match.id '
        'AND recorded.side=match.recorded_player_side '
        'AND recorded.slot=match.recorded_player_slot '
        'LEFT JOIN vainglory_heroes hero ON hero.id=recorded.hero_id '
        "AND length(trim(hero.label))>0 WHERE scan.stats_included=1 "
        'AND match.stats_eligible=1 '
        "AND match.game_mode IN ('3v3','5v5','aram','other') "
        "AND match.recorded_player_side IN ('left','right') "
        'AND match.recorded_player_slot BETWEEN 1 AND match.team_size '
        "AND ((match.game_mode='3v3' AND match.team_size=3) "
        "OR (match.game_mode='5v5' AND match.team_size=5) "
        "OR match.game_mode IN ('aram','other')) "
        "AND CASE match.winner_side WHEN 'left' THEN match.left_color "
        "WHEN 'right' THEN match.right_color END IN ('teal','orange') "
        'ORDER BY part.record_start_time,match.result_at_ms,match.id'
    ).fetchall()
    values: List[Mapping[str, Any]] = []
    for row in rows:
        value = dict(row)
        value['played_at'] = _match_played_at(value)
        values.append(value)
    return sorted(values, key=lambda row: (int(row['played_at']), int(row['match_id'])))


def _id_batches(values: Sequence[int]) -> Iterator[Tuple[int, ...]]:
    for start in range(0, len(values), QUERY_BATCH_SIZE):
        yield tuple(values[start : start + QUERY_BATCH_SIZE])


def _lineups_by_match(
    connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]
) -> Mapping[int, List[Mapping[str, Any]]]:
    match_ids = tuple(sorted(int(row['match_id']) for row in rows))
    lineups: Dict[int, List[Mapping[str, Any]]] = {}
    for batch in _id_batches(match_ids):
        placeholders = ','.join('?' for _ in batch)
        lineup_rows = connection.execute(
            'SELECT participant.match_id,participant.side,participant.slot,'
            "COALESCE(participant.player_name,'') AS player_name,"
            "COALESCE(hero.label,'') AS hero_name,participant.kills,"
            'participant.deaths,participant.assists,participant.economy,'
            'participant.last_hits FROM vainglory_match_players participant '
            'LEFT JOIN vainglory_heroes hero ON hero.id=participant.hero_id '
            'WHERE participant.match_id IN ('
            + placeholders
            + ") AND participant.side IN ('left','right') "
            "ORDER BY participant.match_id,CASE participant.side "
            "WHEN 'left' THEN 0 ELSE 1 END,participant.slot",
            batch,
        ).fetchall()
        for lineup_row in lineup_rows:
            lineups.setdefault(int(lineup_row['match_id']), []).append(dict(lineup_row))
    return lineups


def _publications_by_session(
    connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]
) -> Mapping[int, Mapping[str, Any]]:
    session_ids = tuple(sorted({int(row['session_id']) for row in rows}))
    publications: Dict[int, Mapping[str, Any]] = {}
    for batch in _id_batches(session_ids):
        placeholders = ','.join('?' for _ in batch)
        publication_rows = connection.execute(
            'SELECT publication.id,publication.session_id,publication.bvid,'
            'publication.source_kind,publication.upload_job_id '
            'FROM vainglory_publications publication '
            'WHERE publication.session_id IN ('
            + placeholders
            + ') AND publication.public_visible_at IS NOT NULL '
            'AND publication.id=(SELECT latest.id '
            'FROM vainglory_publications latest '
            'WHERE latest.session_id=publication.session_id '
            'AND latest.public_visible_at IS NOT NULL '
            'ORDER BY latest.public_visible_at DESC,latest.id DESC LIMIT 1)',
            batch,
        ).fetchall()
        publications.update(
            (int(row['session_id']), dict(row)) for row in publication_rows
        )
    return publications


def _publication_parts(
    connection: sqlite3.Connection, publications: Mapping[int, Mapping[str, Any]]
) -> Mapping[int, List[Tuple[int, int, int]]]:
    publication_ids = tuple(
        sorted(int(publication['id']) for publication in publications.values())
    )
    parts: Dict[int, List[Tuple[int, int, int]]] = {}
    upload_page_by_publication: Dict[int, int] = {}
    for batch in _id_batches(publication_ids):
        placeholders = ','.join('?' for _ in batch)
        archive_rows = connection.execute(
            'SELECT publication.id AS publication_id,'
            'archive_part.recording_part_id,archive_part.page,'
            'archive_part.duration_seconds '
            'FROM vainglory_publications publication '
            'JOIN vainglory_archive_imports imported '
            'ON imported.account_id=publication.account_id '
            'AND imported.bvid=publication.bvid '
            'JOIN vainglory_archive_parts archive_part '
            'ON archive_part.import_id=imported.id '
            'WHERE publication.id IN ('
            + placeholders
            + ") AND publication.source_kind='archive' "
            'AND archive_part.recording_part_id IS NOT NULL '
            'ORDER BY publication.id,archive_part.page',
            batch,
        ).fetchall()
        for row in archive_rows:
            parts.setdefault(int(row['publication_id']), []).append(
                (
                    int(row['recording_part_id']),
                    int(row['page']),
                    (
                        0
                        if row['duration_seconds'] is None
                        else int(row['duration_seconds'])
                    ),
                )
            )

        upload_rows = connection.execute(
            'SELECT publication.id AS publication_id,'
            'recording.id AS recording_part_id,'
            'recording.record_duration_seconds '
            'FROM vainglory_publications publication '
            'JOIN upload_parts remote ON remote.job_id=publication.upload_job_id '
            'AND remote.cid IS NOT NULL '
            'JOIN recording_parts recording '
            'ON recording.session_id=publication.session_id '
            'AND recording.part_index=remote.part_index '
            'WHERE publication.id IN ('
            + placeholders
            + ") AND publication.source_kind='upload' "
            'ORDER BY publication.id,remote.part_index',
            batch,
        ).fetchall()
        for row in upload_rows:
            publication_id = int(row['publication_id'])
            page = upload_page_by_publication.get(publication_id, 0) + 1
            upload_page_by_publication[publication_id] = page
            parts.setdefault(publication_id, []).append(
                (
                    int(row['recording_part_id']),
                    page,
                    (
                        0
                        if row['record_duration_seconds'] is None
                        else int(row['record_duration_seconds'])
                    ),
                )
            )
    return parts


def _match_replay(
    row: Mapping[str, Any],
    publication: Optional[Mapping[str, Any]],
    parts: Sequence[Tuple[int, int, int]],
) -> Optional[Mapping[str, str]]:
    if publication is None:
        return None
    bvid = str(publication['bvid'])
    full_url = 'https://www.bilibili.com/video/{}'.format(bvid)
    current = next(
        (part for part in parts if part[0] == int(row['result_part_id'])), None
    )
    if current is None:
        return {'kind': 'full', 'url': full_url}
    _part_id, page, _part_duration = current
    result_at_ms = int(row['result_at_ms'])
    duration_seconds = int(row['duration_seconds'])
    started_at_ms = int(row['started_at_ms'])
    if started_at_ms > 0:
        measured_duration = (result_at_ms - started_at_ms) / 1000
        if abs(measured_duration - duration_seconds) <= 30:
            seconds = started_at_ms // 1000
            return {
                'kind': 'match',
                'url': '{}?p={}&t={}'.format(full_url, page, seconds),
            }
    inferred_start_ms = result_at_ms - duration_seconds * 1000
    if inferred_start_ms >= 0:
        return {
            'kind': 'match',
            'url': '{}?p={}&t={}'.format(full_url, page, inferred_start_ms // 1000),
        }
    remaining_ms = -inferred_start_ms
    previous_parts = sorted(
        (
            (previous_page, previous_duration)
            for _previous_id, previous_page, previous_duration in parts
            if previous_page < page and previous_duration > 0
        ),
        reverse=True,
    )
    for previous_page, previous_duration in previous_parts:
        duration_ms = previous_duration * 1000
        if remaining_ms <= duration_ms + 30000:
            seconds = max(0, duration_ms - remaining_ms) // 1000
            return {
                'kind': 'match',
                'url': '{}?p={}&t={}'.format(full_url, previous_page, seconds),
            }
        remaining_ms -= duration_ms
    return {'kind': 'match', 'url': '{}?p={}&t=0'.format(full_url, page)}


def _public_matches(
    rows: Sequence[Mapping[str, Any]],
    lineups: Mapping[int, List[Mapping[str, Any]]],
    publications: Mapping[int, Mapping[str, Any]],
    publication_parts: Mapping[int, List[Tuple[int, int, int]]],
) -> List[Mapping[str, Any]]:
    values: List[Mapping[str, Any]] = []
    for row in rows:
        match_id = int(row['match_id'])
        recorded_side = str(row['recorded_player_side'])
        enemy_side = 'right' if recorded_side == 'left' else 'left'

        def team_value(side: str) -> Mapping[str, Any]:
            prefix = side
            players = []
            for participant in lineups.get(match_id, ()):
                if str(participant['side']) != side:
                    continue
                player_value: Dict[str, Any] = {
                    'slot': int(participant['slot']),
                    'name': str(participant['player_name'] or '未知玩家'),
                    'heroName': str(participant['hero_name']),
                    'isRecordedPlayer': (
                        side == recorded_side
                        and int(participant['slot']) == int(row['recorded_player_slot'])
                    ),
                }
                for source, target in (
                    ('kills', 'kills'),
                    ('deaths', 'deaths'),
                    ('assists', 'assists'),
                    ('economy', 'economy'),
                    ('last_hits', 'lastHits'),
                ):
                    player_value[target] = (
                        None
                        if participant[source] is None
                        else int(participant[source])
                    )
                players.append(player_value)
            return {
                'role': 'ally' if side == recorded_side else 'enemy',
                'side': side,
                'color': str(row['{}_color'.format(prefix)]),
                'kills': (
                    None
                    if row['{}_kills'.format(prefix)] is None
                    else int(row['{}_kills'.format(prefix)])
                ),
                'economy': (
                    None
                    if row['{}_economy'.format(prefix)] is None
                    else int(row['{}_economy'.format(prefix)])
                ),
                'players': players,
            }

        publication = publications.get(int(row['session_id']))
        replay = _match_replay(
            row,
            publication,
            (
                ()
                if publication is None
                else publication_parts.get(int(publication['id']), ())
            ),
        )
        played_at = datetime.fromtimestamp(int(row['played_at']), tz=timezone.utc)
        value: Dict[str, Any] = {
            'id': match_id,
            'playerId': int(row['player_id']),
            'seasonKey': _season_for(played_at).key,
            'mode': RAW_MODE_TO_PUBLIC[str(row['game_mode'])],
            'playedAt': _utc_iso(played_at),
            'durationSeconds': int(row['duration_seconds']),
            'result': ('W' if str(row['winner_side']) == recorded_side else 'L'),
            'streamTitle': str(row['stream_title'] or ''),
            'ally': team_value(recorded_side),
            'enemy': team_value(enemy_side),
        }
        if replay is not None:
            value['replay'] = replay
        values.append(value)
    return sorted(
        values,
        key=lambda value: (str(value['playedAt']), int(value['id'])),
        reverse=True,
    )


def build_dashboard_api_source(
    connection: sqlite3.Connection, *, now: Optional[datetime] = None
) -> Mapping[str, Any]:
    connection.row_factory = sqlite3.Row
    _validate_schema(connection)
    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        raise ValueError('dashboard API source time must include a timezone')
    players, aliases = _player_metadata(connection)
    live_rooms = _live_rooms_by_player(connection)
    rows = _match_rows(connection)
    lineups = _lineups_by_match(connection, rows)
    publications = _publications_by_session(connection, rows)
    publication_parts = _publication_parts(connection, publications)
    public_matches = _public_matches(rows, lineups, publications, publication_parts)
    rows_by_id = {int(row['match_id']): row for row in rows}
    matches = []
    for public_match in public_matches:
        value = dict(public_match)
        source_row = rows_by_id[int(value['id'])]
        value['resultFramePath'] = (
            None
            if source_row['result_frame_path'] is None
            else str(source_row['result_frame_path'])
        )
        matches.append(value)
    public_players = []
    for player_id, metadata in sorted(players.items()):
        name = str(metadata['name'])
        public_players.append(
            {
                'id': player_id,
                'name': name,
                'initial': name[:1],
                'roomLabel': _room_label(list(metadata['rooms'])),
                'roomIds': list(metadata['rooms']),
                'liveRooms': list(live_rooms.get(player_id, ())),
                'aliases': list(aliases.get(player_id, ())),
                'avatarUrl': None,
            }
        )
    return {
        'schemaVersion': 1,
        'generatedAt': _utc_iso(generated_at),
        'sourceLastMatchId': max((int(row['match_id']) for row in rows), default=0),
        'players': public_players,
        'matches': matches,
    }


def _empty_player_modes() -> Dict[str, _Performance]:
    return {mode: _Performance() for mode in PUBLIC_MODES}


def _empty_hero_modes() -> Dict[str, _HeroPerformance]:
    return {mode: _HeroPerformance() for mode in PUBLIC_MODES}


def _empty_synergy_modes() -> Dict[str, Mapping[str, List[Mapping[str, Any]]]]:
    return {mode: {'best': [], 'worst': []} for mode in PUBLIC_MODES}


def _hero_synergies(
    rows: Sequence[Mapping[str, Any]], lineups: Mapping[int, List[Mapping[str, Any]]]
) -> Mapping[str, Mapping[str, Mapping[str, List[Mapping[str, Any]]]]]:
    totals: Dict[str, Dict[str, List[int]]] = {}
    pairs: Dict[str, Dict[str, Dict[str, List[int]]]] = {}
    for row in rows:
        match_id = int(row['match_id'])
        public_mode = RAW_MODE_TO_PUBLIC[str(row['game_mode'])]
        for side in ('left', 'right'):
            hero_names = sorted(
                {
                    str(participant['hero_name'])
                    for participant in lineups.get(match_id, ())
                    if str(participant['side']) == side
                    and str(participant['hero_name'])
                },
                key=lambda name: (name.casefold(), name),
            )
            if not hero_names:
                continue
            won = str(row['winner_side']) == side
            for mode in (public_mode, 'all'):
                for hero_name in hero_names:
                    hero_totals = totals.setdefault(hero_name, {}).setdefault(
                        mode, [0, 0]
                    )
                    hero_totals[0] += 1
                    hero_totals[1] += int(won)
                    hero_pairs = pairs.setdefault(hero_name, {}).setdefault(mode, {})
                    for partner_name in hero_names:
                        if partner_name == hero_name:
                            continue
                        pair_totals = hero_pairs.setdefault(partner_name, [0, 0])
                        pair_totals[0] += 1
                        pair_totals[1] += int(won)

    values: Dict[str, Dict[str, Mapping[str, List[Mapping[str, Any]]]]] = {}
    for hero_name, modes in pairs.items():
        public_modes = _empty_synergy_modes()
        for mode, partners in modes.items():
            hero_matches, hero_wins = totals[hero_name][mode]
            base_win_rate = hero_wins / hero_matches if hero_matches else 0.5
            eligible = [
                (partner_name, pair_totals[0], pair_totals[1])
                for partner_name, pair_totals in partners.items()
                if pair_totals[0] >= HERO_SYNERGY_MIN_MATCHES
            ]

            def smoothed(item: Tuple[str, int, int]) -> float:
                _name, matches, wins = item
                return (wins + base_win_rate * HERO_SYNERGY_PRIOR_MATCHES) / (
                    matches + HERO_SYNERGY_PRIOR_MATCHES
                )

            ranking_limit = min(HERO_SYNERGY_LIMIT, max(1, len(eligible) // 2))
            best = sorted(
                eligible,
                key=lambda item: (
                    -smoothed(item),
                    -item[1],
                    -item[2],
                    item[0].casefold(),
                    item[0],
                ),
            )[:ranking_limit]
            best_names = {item[0] for item in best}
            worst = sorted(
                (item for item in eligible if item[0] not in best_names),
                key=lambda item: (
                    smoothed(item),
                    -item[1],
                    item[2],
                    item[0].casefold(),
                    item[0],
                ),
            )[:ranking_limit]
            public_modes[mode] = {
                'best': [
                    {'name': name, 'matches': matches, 'wins': wins}
                    for name, matches, wins in best
                ],
                'worst': [
                    {'name': name, 'matches': matches, 'wins': wins}
                    for name, matches, wins in worst
                ],
            }
        values[hero_name] = public_modes
    return values


def _hero_pool(performance: _Performance) -> List[Mapping[str, Any]]:
    heroes = performance.heroes or {}
    return [
        values.public_value(hero_name)
        for hero_name, values in sorted(
            heroes.items(),
            key=lambda item: (
                -item[1].matches,
                -item[1].wins,
                item[0].casefold(),
                item[0],
            ),
        )
    ]


def _room_label(rooms: List[int]) -> str:
    if not rooms:
        return '历史录播'
    return '直播间 ' + ' / '.join(str(room_id) for room_id in rooms)


def _standings_for_rows(
    rows: Sequence[Mapping[str, Any]],
    players: Mapping[int, Mapping[str, Any]],
    aliases: Mapping[int, List[str]],
    lineups: Mapping[int, List[Mapping[str, Any]]],
    previous_ratings: MutableMapping[Tuple[int, str], _PreviousRating],
    *,
    reset_visible_score: bool,
) -> Mapping[str, Any]:
    player_modes: Dict[int, Dict[str, _Performance]] = {}
    hero_modes: Dict[str, Dict[str, _HeroPerformance]] = {}
    for row in rows:
        player_id = int(row['player_id'])
        public_mode = RAW_MODE_TO_PUBLIC[str(row['game_mode'])]
        result = (
            'W' if str(row['winner_side']) == str(row['recorded_player_side']) else 'L'
        )
        hero_name = str(row['hero_name'])
        kills = None if row['kills'] is None else int(row['kills'])
        deaths = None if row['deaths'] is None else int(row['deaths'])
        assists = None if row['assists'] is None else int(row['assists'])
        economy = None if row['economy'] is None else int(row['economy'])
        duration_seconds = (
            None if row['duration_seconds'] is None else int(row['duration_seconds'])
        )
        modes = player_modes.setdefault(player_id, _empty_player_modes())
        modes[public_mode].add(
            result, hero_name, kills, deaths, assists, economy, duration_seconds
        )
        modes['all'].add(
            result, hero_name, kills, deaths, assists, economy, duration_seconds
        )
        if not hero_name:
            continue
        hero = hero_modes.setdefault(hero_name, _empty_hero_modes())
        hero[public_mode].add(player_id, result)
        hero['all'].add(player_id, result)

    public_players: List[Mapping[str, Any]] = []
    for player_id in sorted(player_modes):
        metadata = players[player_id]
        modes = player_modes[player_id]
        ratings: Dict[str, Optional[VirtualMatchRating]] = {}
        for mode in PUBLIC_MODES:
            previous = previous_ratings.get((player_id, mode))
            performance = modes[mode]
            rating = calculate_virtual_match_rating(
                results=performance.results or (),
                previous_ability=(previous.ability if previous is not None else None),
                previous_evidence=(previous.evidence if previous is not None else None),
                reset_visible_score=reset_visible_score,
            )
            ratings[mode] = rating
            if rating is not None:
                previous_ratings[(player_id, mode)] = _PreviousRating(
                    ability=rating.ability, evidence=rating.evidence
                )
        name = str(metadata['name'])
        public_players.append(
            {
                'id': player_id,
                'name': name,
                'initial': name[:1],
                'roomLabel': _room_label(list(metadata['rooms'])),
                'roomIds': list(metadata['rooms']),
                'aliases': list(aliases.get(player_id, ())),
                'trend': 0,
                'form': list((modes['all'].results or [])[-5:]),
                'modes': {
                    mode: modes[mode].public_value(
                        ratings[mode], reset_visible_score=reset_visible_score
                    )
                    for mode in PUBLIC_MODES
                },
                'heroPool': _hero_pool(modes['all']),
                'heroPools': {mode: _hero_pool(modes[mode]) for mode in PUBLIC_MODES},
            }
        )

    synergies = _hero_synergies(rows, lineups)
    public_heroes = [
        {
            'id': hero_name,
            'name': hero_name,
            'modes': {mode: modes[mode].public_value() for mode in PUBLIC_MODES},
            'synergies': synergies.get(hero_name, _empty_synergy_modes()),
        }
        for hero_name, modes in sorted(
            hero_modes.items(), key=lambda item: (item[0].casefold(), item[0])
        )
    ]
    return {'players': public_players, 'heroes': public_heroes}


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def build_dashboard_snapshot(
    connection: sqlite3.Connection, *, now: Optional[datetime] = None
) -> Mapping[str, Any]:
    connection.row_factory = sqlite3.Row
    _validate_schema(connection)
    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        raise ValueError('dashboard snapshot time must include a timezone')
    players, aliases = _player_metadata(connection)
    rows = _match_rows(connection)
    lineups = _lineups_by_match(connection, rows)
    publications = _publications_by_session(connection, rows)
    publication_parts = _publication_parts(connection, publications)
    public_matches = _public_matches(rows, lineups, publications, publication_parts)

    return build_dashboard_snapshot_from_records(
        players=players,
        aliases=aliases,
        rows=rows,
        lineups=lineups,
        public_matches=public_matches,
        generated_at=generated_at,
    )


def build_dashboard_snapshot_from_records(
    *,
    players: Mapping[int, Mapping[str, Any]],
    aliases: Mapping[int, List[str]],
    rows: Sequence[Mapping[str, Any]],
    lineups: Mapping[int, List[Mapping[str, Any]]],
    public_matches: Sequence[Mapping[str, Any]],
    generated_at: datetime,
) -> Mapping[str, Any]:
    """Build the public ranking document from normalized match records.

    Both the NAS exporter and the public API use this pure aggregation boundary so
    season inheritance, hero statistics, forecasts, and tie-break rules cannot
    drift while the dashboard moves away from static publications.
    """
    if generated_at.tzinfo is None:
        raise ValueError('dashboard snapshot time must include a timezone')
    current_season = _season_for(generated_at)

    seasons_by_key: Dict[str, _Season] = {current_season.key: current_season}
    rows_by_season: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        season = _season_for(
            datetime.fromtimestamp(int(row['played_at']), tz=timezone.utc)
        )
        seasons_by_key[season.key] = season
        rows_by_season.setdefault(season.key, []).append(row)
    chronological_seasons = sorted(
        seasons_by_key.values(), key=lambda value: value.starts_at
    )
    previous_ratings: Dict[Tuple[int, str], _PreviousRating] = {}
    standings = {}
    for season in chronological_seasons:
        standings[season.key] = _standings_for_rows(
            rows_by_season.get(season.key, []),
            players,
            aliases,
            lineups,
            previous_ratings,
            reset_visible_score=True,
        )
    seasons = list(reversed(chronological_seasons))
    standings['all-time'] = _standings_for_rows(
        rows, players, aliases, lineups, {}, reset_visible_score=False
    )
    publication_date = generated_at.astimezone(SHANGHAI).date().isoformat()
    source_last_match_id = max((int(row['match_id']) for row in rows), default=0)
    body: Dict[str, Any] = {
        'schemaVersion': 3,
        'publicationDate': publication_date,
        'generatedAt': _utc_iso(generated_at),
        'sourceLastMatchId': source_last_match_id,
        'sourceMatchCount': len(rows),
        'ratingModel': {
            'version': RATING_MODEL_VERSION,
            'priorMatches': PRIOR_MATCHES,
            'carryoverMatchCap': CARRYOVER_MATCH_CAP,
            'provisionalMatches': PROVISIONAL_MATCHES,
            'neutralDisplayScore': NEUTRAL_DISPLAY_SCORE,
            'seasonResetDisplayScore': SEASON_RESET_DISPLAY_SCORE,
            'probabilityScale': PROBABILITY_SCALE,
            'minimumOutcomeDelta': MINIMUM_OUTCOME_DELTA,
            'catchupRate': CATCHUP_RATE,
            'catchupLimit': CATCHUP_LIMIT,
            'catchupProtectionGap': CATCHUP_PROTECTION_GAP,
            'catchupLossMultiplier': CATCHUP_LOSS_MULTIPLIER,
        },
        'currentSeasonKey': current_season.key,
        'seasons': [_season_option(season, current_season.key) for season in seasons]
        + [
            {
                'key': 'all-time',
                'label': '跨赛季总榜',
                'shortLabel': '总榜',
                'period': '全部已收录对局',
                'current': False,
                'startsAt': None,
                'endsAt': None,
            }
        ],
        'standings': standings,
        'matches': list(public_matches),
    }
    content_revision = hashlib.sha256(
        _json_bytes(
            {
                key: body[key]
                for key in (
                    'sourceLastMatchId',
                    'sourceMatchCount',
                    'ratingModel',
                    'currentSeasonKey',
                    'seasons',
                    'standings',
                    'matches',
                )
            }
        )
    ).hexdigest()
    body['contentRevision'] = content_revision
    digest = hashlib.sha256(_json_bytes(body)).hexdigest()
    body['snapshotId'] = '{}-{}'.format(
        generated_at.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'), digest[:8]
    )
    return body


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        + '\n'
    ).encode('utf-8')


def _atomic_replace(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix='.{}.'.format(path.name), suffix='.tmp', dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'wb') as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _write_immutable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError('dashboard snapshot ID collision')
        return
    _atomic_replace(path, content)


def export_dashboard_files(
    database_path: Path, output_directory: Path, *, now: Optional[datetime] = None
) -> DashboardExportResult:
    resolved_database = database_path.expanduser().resolve(strict=True)
    connection = sqlite3.connect(
        '{}?mode=ro'.format(resolved_database.as_uri()), uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=5000')
        connection.execute('BEGIN')
        snapshot = build_dashboard_snapshot(connection, now=now)
        connection.execute('COMMIT')
    except BaseException:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        raise
    finally:
        connection.close()

    snapshot_content = _json_bytes(snapshot)
    sha256 = hashlib.sha256(snapshot_content).hexdigest()
    snapshot_id = str(snapshot['snapshotId'])
    output = output_directory.expanduser().resolve()
    snapshot_path = output / 'snapshots' / '{}.json'.format(snapshot_id)
    manifest_path = output / 'manifest.json'
    _write_immutable(snapshot_path, snapshot_content)
    manifest: Mapping[str, Any] = {
        'schemaVersion': 1,
        'snapshotId': snapshot_id,
        'snapshotPath': 'snapshots/{}.json'.format(snapshot_id),
        'publicationDate': snapshot['publicationDate'],
        'generatedAt': snapshot['generatedAt'],
        'sourceLastMatchId': snapshot['sourceLastMatchId'],
        'contentRevision': snapshot['contentRevision'],
        'sha256': sha256,
        'bytes': len(snapshot_content),
    }
    _atomic_replace(manifest_path, _json_bytes(manifest))
    return DashboardExportResult(
        manifest_path=manifest_path,
        snapshot_path=snapshot_path,
        manifest=manifest,
        sha256=sha256,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='从 BLREC SQLite 生成公网排行榜静态快照'
    )
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument(
        '--skip-player-avatars',
        action='store_true',
        help='只导出排行榜 JSON，不从绑定的 B 站直播间同步玩家头像',
    )
    return parser.parse_args()


def main() -> None:
    arguments = _parse_args()
    result = export_dashboard_files(arguments.database, arguments.output)
    print(
        'dashboard snapshot exported: {} -> {}'.format(
            result.manifest['snapshotId'], result.manifest_path
        )
    )
    if not arguments.skip_player_avatars:
        from .avatars import sync_player_avatars

        avatar_result = sync_player_avatars(result.snapshot_path, arguments.output)
        print(
            'player avatars synced: {}/{}'.format(
                avatar_result.downloaded, avatar_result.attempted
            )
        )


if __name__ == '__main__':
    main()
