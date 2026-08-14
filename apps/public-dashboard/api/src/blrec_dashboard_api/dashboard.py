from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from blrec_dashboard_publisher.snapshot import build_dashboard_snapshot_from_records

from .database import DatabaseTarget, connect_database

_TREND_MODES = ('all', '3v3', 'brawl', '5v5')
_MAX_TREND_PUBLICATIONS = 180


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _player_metadata(
    connection: Any,
) -> Tuple[Mapping[int, Mapping[str, Any]], Mapping[int, List[str]]]:
    players: Dict[int, Dict[str, Any]] = {}
    for row in connection.execute(
        'SELECT player_id,name FROM players ORDER BY player_id'
    ).fetchall():
        players[int(row['player_id'])] = {'name': str(row['name']), 'rooms': []}
    for row in connection.execute(
        'SELECT player_id,room_id FROM player_rooms ORDER BY player_id,room_id'
    ).fetchall():
        player = players.get(int(row['player_id']))
        if player is not None:
            player['rooms'].append(int(row['room_id']))
    aliases: Dict[int, List[str]] = {player_id: [] for player_id in players}
    for row in connection.execute(
        'SELECT player_id,alias FROM player_aliases '
        'ORDER BY player_id,lower(alias),alias'
    ).fetchall():
        aliases.setdefault(int(row['player_id']), []).append(str(row['alias']))
    return players, aliases


def _ranking_rows(connection: Any) -> List[Mapping[str, Any]]:
    rows = connection.execute(
        'SELECT match.source_match_id AS match_id,match.player_id,'
        'match.exact_fingerprint,'
        'match.mode AS game_mode,match.played_at_epoch AS played_at,'
        'match.duration_seconds,match.result,ally.side AS recorded_player_side,'
        'enemy.side AS enemy_side,COALESCE(recorded.hero_name,\'\') AS hero_name,'
        'recorded.kills,recorded.deaths,recorded.assists,recorded.economy '
        'FROM matches match '
        'JOIN match_teams ally ON ally.match_id=match.source_match_id '
        "AND ally.role='ally' "
        'JOIN match_teams enemy ON enemy.match_id=match.source_match_id '
        "AND enemy.role='enemy' "
        'LEFT JOIN match_participants recorded '
        'ON recorded.match_id=match.source_match_id '
        "AND recorded.team_role='ally' AND recorded.is_recorded_player=1 "
        'ORDER BY match.played_at_epoch,match.source_match_id'
    ).fetchall()
    values: List[Mapping[str, Any]] = []
    for row in rows:
        value = dict(row)
        value['winner_side'] = (
            str(row['recorded_player_side'])
            if str(row['result']) == 'W'
            else str(row['enemy_side'])
        )
        values.append(value)
    return values


def _environment_rows(connection: Any) -> List[Mapping[str, Any]]:
    rows = connection.execute(
        'SELECT match.source_match_id AS match_id,match.mode AS game_mode,'
        'match.played_at_epoch AS played_at,match.duration_seconds,'
        'match.exact_fingerprint,ally.side AS recorded_player_side,'
        'enemy.side AS enemy_side,match.result '
        'FROM matches match '
        'JOIN match_teams ally ON ally.match_id=match.source_match_id '
        "AND ally.role='ally' "
        'JOIN match_teams enemy ON enemy.match_id=match.source_match_id '
        "AND enemy.role='enemy' "
        'ORDER BY match.played_at_epoch,match.source_match_id'
    ).fetchall()
    values: List[Mapping[str, Any]] = []
    for row in rows:
        value = dict(row)
        value['winner_side'] = (
            str(row['recorded_player_side'])
            if str(row['result']) == 'W'
            else str(row['enemy_side'])
        )
        values.append(value)
    return values


def _lineups_by_match(connection: Any) -> Mapping[int, List[Mapping[str, Any]]]:
    lineups: Dict[int, List[Mapping[str, Any]]] = {}
    rows = connection.execute(
        'SELECT participant.match_id,team.side,participant.slot,'
        'participant.player_name,participant.hero_name,participant.kills,'
        'participant.deaths,participant.assists,participant.economy,'
        'participant.last_hits FROM match_participants participant '
        'JOIN match_teams team ON team.match_id=participant.match_id '
        'AND team.role=participant.team_role '
        'ORDER BY participant.match_id,team.side,participant.slot'
    ).fetchall()
    for row in rows:
        lineups.setdefault(int(row['match_id']), []).append(dict(row))
    return lineups


def _ranked_trend_rows(
    players: Sequence[Mapping[str, Any]], mode: str
) -> List[Mapping[str, int]]:
    candidates: List[Tuple[int, int, int, float]] = []
    for player in players:
        player_id = int(player['id'])
        performance = player['modes'][mode]
        rating_score = performance['ratingScore']
        matches = int(performance['matches'])
        wins = int(performance['wins'])
        if rating_score is None:
            continue
        candidates.append(
            (player_id, int(rating_score), matches, wins / matches if matches else 0.0)
        )
    candidates.sort(key=lambda row: (-row[1], -row[2], -row[3], row[0]))
    return [
        {'playerId': row[0], 'rank': index + 1, 'ratingScore': row[1]}
        for index, row in enumerate(candidates)
    ]


def _trend_standings(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    values: Dict[str, Mapping[str, Any]] = {}
    standings = snapshot['standings']
    for season_key, season_standings in standings.items():
        players = season_standings['players']
        values[str(season_key)] = {
            mode: _ranked_trend_rows(players, mode) for mode in _TREND_MODES
        }
    return values


def refresh_dashboard_state(
    connection: Any, *, generated_at: datetime
) -> Mapping[str, Any]:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError('dashboard generation time must include a timezone')
    players, aliases = _player_metadata(connection)
    snapshot = build_dashboard_snapshot_from_records(
        players=players,
        aliases=aliases,
        rows=_ranking_rows(connection),
        environment_rows=_environment_rows(connection),
        lineups=_lineups_by_match(connection),
        public_matches=(),
        generated_at=generated_at,
    )
    now = int(time.time())
    snapshot_id = str(snapshot['snapshotId'])
    generated_at_text = str(snapshot['generatedAt'])
    connection.execute(
        'INSERT INTO dashboard_state('
        'singleton_id,snapshot_id,content_revision,generated_at,snapshot_json,'
        'updated_at) VALUES(1,?,?,?,?,?) '
        'ON CONFLICT(singleton_id) DO UPDATE SET '
        'snapshot_id=excluded.snapshot_id,'
        'content_revision=excluded.content_revision,'
        'generated_at=excluded.generated_at,snapshot_json=excluded.snapshot_json,'
        'updated_at=excluded.updated_at',
        (
            snapshot_id,
            str(snapshot['contentRevision']),
            generated_at_text,
            _json_text(snapshot),
            now,
        ),
    )
    connection.execute(
        'INSERT INTO dashboard_trend_publications('
        'publication_date,snapshot_id,generated_at,source_last_match_id,'
        'standings_json,updated_at) VALUES(?,?,?,?,?,?) '
        'ON CONFLICT(publication_date) DO UPDATE SET '
        'snapshot_id=excluded.snapshot_id,generated_at=excluded.generated_at,'
        'source_last_match_id=excluded.source_last_match_id,'
        'standings_json=excluded.standings_json,updated_at=excluded.updated_at',
        (
            str(snapshot['publicationDate']),
            snapshot_id,
            generated_at_text,
            int(snapshot['sourceLastMatchId']),
            _json_text(_trend_standings(snapshot)),
            now,
        ),
    )
    connection.execute(
        'DELETE FROM dashboard_trend_publications WHERE publication_date NOT IN('
        'SELECT publication_date FROM dashboard_trend_publications '
        'ORDER BY publication_date DESC LIMIT ?)',
        (_MAX_TREND_PUBLICATIONS,),
    )
    return snapshot


def ensure_dashboard_state(database_target: DatabaseTarget) -> None:
    connection = connect_database(database_target)
    try:
        connection.execute('BEGIN IMMEDIATE')
        initialized = connection.execute(
            'SELECT 1 FROM ingestion_batches LIMIT 1'
        ).fetchone()
        state = connection.execute(
            'SELECT 1 FROM dashboard_state WHERE singleton_id=1'
        ).fetchone()
        if initialized is not None and state is None:
            refresh_dashboard_state(connection, generated_at=datetime.now(timezone.utc))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_dashboard_document(
    database_target: DatabaseTarget,
) -> Optional[Tuple[Mapping[str, Any], str]]:
    connection = connect_database(database_target)
    try:
        state = connection.execute(
            'SELECT snapshot_json,content_revision,generated_at '
            'FROM dashboard_state WHERE singleton_id=1'
        ).fetchone()
        if state is None:
            return None
        snapshot = json.loads(str(state['snapshot_json']))
        publications = []
        for row in connection.execute(
            'SELECT publication_date,snapshot_id,generated_at,'
            'source_last_match_id,standings_json '
            'FROM dashboard_trend_publications ORDER BY publication_date'
        ).fetchall():
            publications.append(
                {
                    'snapshotId': str(row['snapshot_id']),
                    'publicationDate': str(row['publication_date']),
                    'sourceLastMatchId': int(row['source_last_match_id']),
                    'standings': json.loads(str(row['standings_json'])),
                }
            )
        return (
            {
                'snapshot': snapshot,
                'trends': {
                    'schemaVersion': 1,
                    'updatedAt': str(state['generated_at']),
                    'publications': publications,
                },
            },
            str(state['content_revision']),
        )
    finally:
        connection.close()
