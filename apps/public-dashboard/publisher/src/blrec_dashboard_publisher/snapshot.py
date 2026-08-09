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
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Set, Tuple

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
    VirtualMatchRating,
    calculate_virtual_match_rating,
)

__all__ = (
    'DashboardExportResult',
    'build_dashboard_snapshot',
    'export_dashboard_files',
)


SHANGHAI = timezone(timedelta(hours=8))
PUBLIC_MODES = ('all', '3v3', 'brawl', '5v5')
RAW_MODE_TO_PUBLIC = {'3v3': '3v3', '5v5': '5v5', 'aram': 'brawl', 'other': 'brawl'}
SEASON_NAMES = {'spring': '春季赛', 'summer': '夏季赛', 'autumn': '秋季赛'}
REQUIRED_TABLES = frozenset(
    (
        'recording_sessions',
        'vainglory_heroes',
        'vainglory_matches',
        'vainglory_match_players',
        'vainglory_players',
        'vainglory_player_rooms',
        'vainglory_player_sessions',
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

    def add(
        self,
        result: str,
        kills: Optional[int],
        deaths: Optional[int],
        assists: Optional[int],
        economy: Optional[int],
    ) -> None:
        self.matches += 1
        if result == 'W':
            self.wins += 1
        if kills is not None and deaths is not None and assists is not None:
            self.kda_matches += 1
            self.kills += kills
            self.deaths += deaths
            self.assists += assists
        if economy is not None:
            self.economy_matches += 1
            self.economy += economy

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
        hero.add(result, kills, deaths, assists, economy)

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
        self, rating: Optional[VirtualMatchRating] = None
    ) -> Mapping[str, Any]:
        return {
            'matches': self.matches,
            'wins': self.wins,
            'topHero': self.top_hero(),
            'form': list((self.results or [])[-5:]),
            'ratingScore': rating.score if rating is not None else None,
            'provisional': rating.provisional if rating is not None else False,
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
    if version_row is None or int(version_row[0]) < 54:
        raise sqlite3.DatabaseError('dashboard source database requires schema 54+')


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


def _match_rows(connection: sqlite3.Connection) -> List[sqlite3.Row]:
    return connection.execute(
        'SELECT player.id AS player_id,player.name AS player_name,'
        'session.id AS session_id,session.started_at,match.id AS match_id,'
        'match.started_at_ms,match.game_mode,'
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
        'JOIN vainglory_scan_jobs scan ON scan.session_id=session.id '
        'LEFT JOIN vainglory_match_players recorded '
        'ON recorded.match_id=match.id '
        'AND recorded.side=match.recorded_player_side '
        'AND recorded.slot=match.recorded_player_slot '
        'LEFT JOIN vainglory_heroes hero ON hero.id=recorded.hero_id '
        "AND length(trim(hero.label))>0 WHERE scan.stats_included=1 "
        'AND match.stats_eligible=1 '
        "AND match.game_mode IN ('3v3','5v5','aram','other') "
        "AND ((match.game_mode='3v3' AND match.team_size=3) "
        "OR (match.game_mode='5v5' AND match.team_size=5) "
        "OR match.game_mode IN ('aram','other')) "
        "AND CASE match.winner_side WHEN 'left' THEN match.left_color "
        "WHEN 'right' THEN match.right_color END IN ('teal','orange') "
        'ORDER BY session.started_at,session.id,match.started_at_ms,match.id'
    ).fetchall()


def _empty_player_modes() -> Dict[str, _Performance]:
    return {mode: _Performance() for mode in PUBLIC_MODES}


def _empty_hero_modes() -> Dict[str, _HeroPerformance]:
    return {mode: _HeroPerformance() for mode in PUBLIC_MODES}


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
    rows: List[sqlite3.Row],
    players: Mapping[int, Mapping[str, Any]],
    aliases: Mapping[int, List[str]],
    previous_ratings: MutableMapping[Tuple[int, str], _PreviousRating],
    *,
    reset_visible_score: bool,
) -> Mapping[str, Any]:
    player_modes: Dict[int, Dict[str, _Performance]] = {}
    hero_modes: Dict[str, Dict[str, _HeroPerformance]] = {}
    for row in rows:
        player_id = int(row['player_id'])
        public_mode = RAW_MODE_TO_PUBLIC[str(row['game_mode'])]
        result = 'W' if str(row['winner_color']) == 'teal' else 'L'
        hero_name = str(row['hero_name'])
        kills = None if row['kills'] is None else int(row['kills'])
        deaths = None if row['deaths'] is None else int(row['deaths'])
        assists = None if row['assists'] is None else int(row['assists'])
        economy = None if row['economy'] is None else int(row['economy'])
        modes = player_modes.setdefault(player_id, _empty_player_modes())
        modes[public_mode].add(result, hero_name, kills, deaths, assists, economy)
        modes['all'].add(result, hero_name, kills, deaths, assists, economy)
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
                'aliases': list(aliases.get(player_id, ())),
                'trend': 0,
                'form': list((modes['all'].results or [])[-5:]),
                'modes': {
                    mode: modes[mode].public_value(ratings[mode])
                    for mode in PUBLIC_MODES
                },
                'heroPool': _hero_pool(modes['all']),
                'heroPools': {mode: _hero_pool(modes[mode]) for mode in PUBLIC_MODES},
            }
        )

    public_heroes = [
        {
            'id': hero_name,
            'name': hero_name,
            'modes': {mode: modes[mode].public_value() for mode in PUBLIC_MODES},
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
    current_season = _season_for(generated_at)
    players, aliases = _player_metadata(connection)
    rows = _match_rows(connection)

    seasons_by_key: Dict[str, _Season] = {current_season.key: current_season}
    rows_by_season: Dict[str, List[sqlite3.Row]] = {}
    for row in rows:
        season = _season_for(
            datetime.fromtimestamp(int(row['started_at']), tz=timezone.utc)
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
            previous_ratings,
            reset_visible_score=True,
        )
    seasons = list(reversed(chronological_seasons))
    standings['all-time'] = _standings_for_rows(
        rows, players, aliases, {}, reset_visible_score=False
    )
    publication_date = generated_at.astimezone(SHANGHAI).date().isoformat()
    source_last_match_id = max((int(row['match_id']) for row in rows), default=0)
    body: Dict[str, Any] = {
        'schemaVersion': 2,
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
