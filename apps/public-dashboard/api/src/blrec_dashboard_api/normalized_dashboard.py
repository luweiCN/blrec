from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from blrec_dashboard_publisher.snapshot import build_dashboard_snapshot_from_records

from .direct import _rating_trends


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(',', ':')
    ).encode('utf-8')


def _visibility_clause(owner_view: bool, *, alias: str = 'player') -> str:
    return '' if owner_view else ' WHERE {}.public_visible=1'.format(alias)


def _player_metadata(
    connection: Any, *, owner_view: bool
) -> Tuple[Mapping[int, Mapping[str, Any]], Mapping[int, List[str]]]:
    players: Dict[int, Dict[str, Any]] = {}
    for row in connection.execute(
        'SELECT player_id,name FROM players player'
        + _visibility_clause(owner_view)
        + ' ORDER BY player_id'
    ).fetchall():
        players[int(row['player_id'])] = {'name': str(row['name']), 'rooms': []}
    for row in connection.execute(
        'SELECT room.player_id,room.room_id FROM player_rooms room '
        'JOIN players player ON player.player_id=room.player_id'
        + _visibility_clause(owner_view)
        + ' ORDER BY room.player_id,room.room_id'
    ).fetchall():
        player = players.get(int(row['player_id']))
        if player is not None:
            player['rooms'].append(int(row['room_id']))
    aliases: Dict[int, List[str]] = {player_id: [] for player_id in players}
    for row in connection.execute(
        'SELECT alias.player_id,alias.alias FROM player_aliases alias '
        'JOIN players player ON player.player_id=alias.player_id'
        + _visibility_clause(owner_view)
        + ' ORDER BY alias.player_id,lower(alias.alias),alias.alias'
    ).fetchall():
        aliases.setdefault(int(row['player_id']), []).append(str(row['alias']))
    return players, aliases


def _ranking_rows(connection: Any, *, owner_view: bool) -> List[Mapping[str, Any]]:
    rows = connection.execute(
        'SELECT match.source_match_id AS match_id,match.player_id,'
        'match.exact_fingerprint,match.stats_eligible,'
        'match.mode AS game_mode,match.played_at_epoch AS played_at,'
        'match.duration_seconds,match.result,ally.side AS recorded_player_side,'
        'enemy.side AS enemy_side,COALESCE(recorded.hero_name,\'\') AS hero_name,'
        'recorded.kills,recorded.deaths,recorded.assists,recorded.economy '
        'FROM matches match '
        'JOIN players player ON player.player_id=match.player_id '
        'JOIN match_teams ally ON ally.match_id=match.source_match_id '
        "AND ally.role='ally' "
        'JOIN match_teams enemy ON enemy.match_id=match.source_match_id '
        "AND enemy.role='enemy' "
        'LEFT JOIN match_participants recorded '
        'ON recorded.match_id=match.source_match_id '
        "AND recorded.team_role='ally' AND recorded.is_recorded_player=1"
        + _visibility_clause(owner_view)
        + ' ORDER BY match.played_at_epoch,match.source_match_id'
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


def _environment_rows(connection: Any, *, owner_view: bool) -> List[Mapping[str, Any]]:
    rows = connection.execute(
        'SELECT match.source_match_id AS match_id,match.mode AS game_mode,'
        'match.played_at_epoch AS played_at,match.duration_seconds,'
        'match.exact_fingerprint,match.stats_eligible,'
        'ally.side AS recorded_player_side,enemy.side AS enemy_side,match.result '
        'FROM matches match '
        'JOIN players player ON player.player_id=match.player_id '
        'JOIN match_teams ally ON ally.match_id=match.source_match_id '
        "AND ally.role='ally' "
        'JOIN match_teams enemy ON enemy.match_id=match.source_match_id '
        "AND enemy.role='enemy'"
        + _visibility_clause(owner_view)
        + ' ORDER BY match.played_at_epoch,match.source_match_id'
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


def _lineups_by_match(
    connection: Any, *, owner_view: bool
) -> Mapping[int, List[Mapping[str, Any]]]:
    lineups: Dict[int, List[Mapping[str, Any]]] = {}
    rows = connection.execute(
        'SELECT participant.match_id,team.side,participant.slot,'
        'participant.player_name,participant.hero_name,participant.kills,'
        'participant.deaths,participant.assists,participant.economy,'
        'participant.last_hits FROM match_participants participant '
        'JOIN match_teams team ON team.match_id=participant.match_id '
        'AND team.role=participant.team_role '
        'JOIN matches match ON match.source_match_id=participant.match_id '
        'JOIN players player ON player.player_id=match.player_id'
        + _visibility_clause(owner_view)
        + ' ORDER BY participant.match_id,team.side,participant.slot'
    ).fetchall()
    for row in rows:
        lineups.setdefault(int(row['match_id']), []).append(dict(row))
    return lineups


def _trend_inputs(
    connection: Any, *, owner_view: bool
) -> Tuple[
    Sequence[Mapping[str, Any]], Mapping[Tuple[int, str, str], Mapping[str, Any]]
]:
    matches = [
        {
            'id': int(row['source_match_id']),
            'playerId': int(row['player_id']),
            'seasonKey': str(row['season_key']),
            'mode': str(row['mode']),
            'playedAt': str(row['played_at']),
            'result': str(row['result']),
        }
        for row in connection.execute(
            'SELECT match.source_match_id,match.player_id,match.season_key,'
            'match.mode,match.played_at,match.result FROM matches match '
            'JOIN players player ON player.player_id=match.player_id'
            + _visibility_clause(owner_view)
            + ' ORDER BY match.played_at_epoch,match.source_match_id'
        ).fetchall()
    ]
    ratings = {
        (int(row['match_id']), str(row['scope']), str(row['season_key'])): {
            'scoreBefore': int(row['score_before']),
            'scoreAfter': int(row['score_after']),
        }
        for row in connection.execute(
            'SELECT rating.match_id,rating.scope,rating.season_key,'
            'rating.score_before,rating.score_after FROM rating_events rating '
            'JOIN players player ON player.player_id=rating.player_id'
            + _visibility_clause(owner_view)
        ).fetchall()
    }
    return matches, ratings


def _live_rooms(
    connection: Any, *, owner_view: bool, generated_at: str
) -> Mapping[str, Any]:
    rows = connection.execute(
        'SELECT live.room_id,live.player_id,live.title,live.started_at '
        'FROM player_live_rooms live '
        'JOIN players player ON player.player_id=live.player_id'
        + _visibility_clause(owner_view)
        + ' ORDER BY live.started_at DESC,live.room_id DESC'
    ).fetchall()
    return {
        'schemaVersion': 1,
        'updatedAt': generated_at,
        'rooms': [
            {
                'roomId': int(row['room_id']),
                'playerId': int(row['player_id']),
                'title': str(row['title']),
                'startedAt': str(row['started_at']),
            }
            for row in rows
        ],
    }


def refresh_dashboard_state(
    connection: Any, *, generated_at: datetime, source_revision: int
) -> None:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError('dashboard generation time must include a timezone')
    if source_revision <= 0:
        raise ValueError('dashboard source revision must be positive')
    published_at = int(time.time())
    generated_at_text = generated_at.isoformat(timespec='seconds').replace(
        '+00:00', 'Z'
    )
    for audience in ('owner', 'public'):
        owner_view = audience == 'owner'
        players, aliases = _player_metadata(connection, owner_view=owner_view)
        rows = _ranking_rows(connection, owner_view=owner_view)
        lineups = _lineups_by_match(connection, owner_view=owner_view)
        snapshot = build_dashboard_snapshot_from_records(
            players=players,
            aliases=aliases,
            rows=rows,
            environment_rows=_environment_rows(connection, owner_view=owner_view),
            lineups=lineups,
            public_matches=(),
            generated_at=generated_at,
        )
        trend_matches, ratings = _trend_inputs(connection, owner_view=owner_view)
        dashboard_payload = _json_bytes(
            {
                'snapshot': snapshot,
                'trends': _rating_trends(snapshot, trend_matches, ratings),
            }
        )
        live_rooms_payload = _json_bytes(
            _live_rooms(
                connection, owner_view=owner_view, generated_at=generated_at_text
            )
        )
        connection.execute(
            'INSERT INTO dashboard_audience_state('
            'audience,source_revision,dashboard_payload,live_rooms_payload,'
            'published_at) VALUES(?,?,?,?,?) '
            'ON CONFLICT(audience) DO UPDATE SET '
            'source_revision=excluded.source_revision,'
            'dashboard_payload=excluded.dashboard_payload,'
            'live_rooms_payload=excluded.live_rooms_payload,'
            'published_at=excluded.published_at',
            (
                audience,
                source_revision,
                dashboard_payload,
                live_rooms_payload,
                published_at,
            ),
        )
