from __future__ import annotations

import hashlib
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from blrec_dashboard_publisher.deduplication import exact_match_fingerprint
from blrec_dashboard_publisher.rating import (
    RATING_MODEL_VERSION,
    calculate_virtual_match_rating_timeline,
)
from pypinyin import Style, lazy_pinyin

from .dashboard import refresh_dashboard_state
from .database import connect_database
from .models import IngestBatch, IngestMatch, IngestMatchTeam, IngestPlayer


class IdempotencyConflict(Exception):
    pass


def _payload_sha256(batch: IngestBatch) -> str:
    value = batch.json(
        by_alias=True, exclude_none=False, sort_keys=True, separators=(',', ':')
    )
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _match_revision(match: IngestMatch) -> str:
    value = match.json(
        by_alias=True, exclude_none=False, sort_keys=True, separators=(',', ':')
    )
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _ingest_match_fingerprint(match: IngestMatch) -> Optional[str]:
    winner_role = 'ally' if match.result == 'W' else 'enemy'
    teams = []
    for team in (match.ally, match.enemy):
        teams.append(
            {
                'side': team.side,
                'kills': team.kills,
                'economy': team.economy,
                'players': [
                    {
                        'hero_name': player.hero_name,
                        'kills': player.kills,
                        'deaths': player.deaths,
                        'assists': player.assists,
                        'economy': player.economy,
                        'last_hits': player.last_hits,
                    }
                    for player in team.players
                ],
            }
        )
    winner_side = match.ally.side if winner_role == 'ally' else match.enemy.side
    return exact_match_fingerprint(
        mode=match.mode,
        duration_seconds=match.duration_seconds,
        winner_side=winner_side,
        teams=teams,
    )


def _stored_match_fingerprint(
    connection: sqlite3.Connection, match_id: int
) -> Optional[str]:
    match = connection.execute(
        'SELECT mode,duration_seconds,result FROM matches WHERE source_match_id=?',
        (match_id,),
    ).fetchone()
    if match is None:
        return None
    teams = []
    winner_side = ''
    for team in connection.execute(
        'SELECT role,side,kills,economy FROM match_teams '
        'WHERE match_id=? ORDER BY role',
        (match_id,),
    ).fetchall():
        role = str(team['role'])
        side = str(team['side'])
        if (str(match['result']) == 'W') == (role == 'ally'):
            winner_side = side
        players = connection.execute(
            'SELECT hero_name,kills,deaths,assists,economy,last_hits '
            'FROM match_participants WHERE match_id=? AND team_role=? '
            'ORDER BY slot',
            (match_id, role),
        ).fetchall()
        teams.append(
            {
                'side': side,
                'kills': team['kills'],
                'economy': team['economy'],
                'players': [dict(player) for player in players],
            }
        )
    return exact_match_fingerprint(
        mode=str(match['mode']),
        duration_seconds=int(match['duration_seconds']),
        winner_side=winner_side,
        teams=teams,
    )


def _players_for_fingerprints(
    connection: sqlite3.Connection, fingerprints: Iterable[Optional[str]]
) -> Set[int]:
    values = tuple(sorted({value for value in fingerprints if value is not None}))
    if not values:
        return set()
    placeholders = ','.join('?' for _ in values)
    return {
        int(row['player_id'])
        for row in connection.execute(
            'SELECT DISTINCT player_id FROM matches WHERE exact_fingerprint IN ('
            + placeholders
            + ')',
            values,
        ).fetchall()
    }


def _utc_iso(value: Any) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec='seconds')
        .replace('+00:00', 'Z')
    )


def _upsert_player(
    connection: sqlite3.Connection, player: IngestPlayer, now: int
) -> Set[int]:
    existing = connection.execute(
        'SELECT name,initial,room_label,avatar_url FROM players WHERE player_id=?',
        (player.id,),
    ).fetchone()
    existing_rooms = tuple(
        int(row['room_id'])
        for row in connection.execute(
            'SELECT room_id FROM player_rooms WHERE player_id=? ORDER BY room_id',
            (player.id,),
        ).fetchall()
    )
    existing_aliases = tuple(
        str(row['alias'])
        for row in connection.execute(
            'SELECT alias FROM player_aliases WHERE player_id=? ORDER BY alias',
            (player.id,),
        ).fetchall()
    )
    existing_live_rooms = tuple(
        (int(row['room_id']), str(row['title']), str(row['started_at']))
        for row in connection.execute(
            'SELECT room_id,title,started_at FROM player_live_rooms '
            'WHERE player_id=? ORDER BY room_id',
            (player.id,),
        ).fetchall()
    )
    room_ids = tuple(sorted(player.room_ids))
    aliases = tuple(sorted(player.aliases))
    live_rooms = tuple(
        sorted(
            (live_room.room_id, live_room.title, _utc_iso(live_room.started_at))
            for live_room in player.live_rooms
        )
    )
    avatar_url = None if player.avatar_url is None else str(player.avatar_url)
    changed = (
        existing is None
        or str(existing['name']) != player.name
        or str(existing['initial']) != player.initial
        or str(existing['room_label']) != player.room_label
        or existing['avatar_url'] != avatar_url
        or existing_rooms != room_ids
        or existing_aliases != aliases
        or existing_live_rooms != live_rooms
    )
    affected_player_ids = {
        int(row['player_id'])
        for room_id in room_ids
        for row in connection.execute(
            'SELECT player_id FROM player_rooms WHERE room_id=? AND player_id<>?',
            (room_id, player.id),
        ).fetchall()
    }
    if not changed and not affected_player_ids:
        return set()
    affected_player_ids.add(player.id)
    connection.execute(
        'INSERT INTO players('
        'player_id,name,initial,room_label,avatar_url,updated_at'
        ') VALUES(?,?,?,?,?,?) ON CONFLICT(player_id) DO UPDATE SET '
        'name=excluded.name,initial=excluded.initial,'
        'room_label=excluded.room_label,avatar_url=excluded.avatar_url,'
        'updated_at=excluded.updated_at',
        (player.id, player.name, player.initial, player.room_label, avatar_url, now),
    )
    connection.execute('DELETE FROM player_rooms WHERE player_id=?', (player.id,))
    for room_id in room_ids:
        connection.execute('DELETE FROM player_rooms WHERE room_id=?', (room_id,))
        connection.execute(
            'INSERT INTO player_rooms(player_id,room_id) VALUES(?,?)',
            (player.id, room_id),
        )
    connection.execute('DELETE FROM player_aliases WHERE player_id=?', (player.id,))
    connection.executemany(
        'INSERT INTO player_aliases(player_id,alias) VALUES(?,?)',
        ((player.id, alias) for alias in aliases),
    )
    connection.executemany(
        'INSERT INTO player_live_rooms('
        'player_id,room_id,title,started_at,updated_at) VALUES(?,?,?,?,?)',
        (
            (player.id, room_id, title, started_at, now)
            for room_id, title, started_at in live_rooms
        ),
    )
    return affected_player_ids


def get_live_rooms(database_path: Path) -> Optional[Dict[str, Any]]:
    connection = connect_database(database_path)
    try:
        state = connection.execute(
            'SELECT generated_at FROM dashboard_state WHERE singleton_id=1'
        ).fetchone()
        if state is None:
            return None
        rooms = connection.execute(
            'SELECT live.room_id,live.player_id,live.title,live.started_at '
            'FROM player_live_rooms live '
            'ORDER BY live.started_at DESC,live.room_id'
        ).fetchall()
        return {
            'schemaVersion': 1,
            'updatedAt': str(state['generated_at']),
            'rooms': [
                {
                    'roomId': int(room['room_id']),
                    'playerId': int(room['player_id']),
                    'title': str(room['title']),
                    'startedAt': str(room['started_at']),
                }
                for room in rooms
            ],
        }
    finally:
        connection.close()


def _delete_unreferenced_players(
    connection: sqlite3.Connection, current_player_ids: Sequence[int]
) -> None:
    parameters = tuple(sorted(current_player_ids))
    current_filter = ''
    if parameters:
        placeholders = ','.join('?' for _ in parameters)
        current_filter = 'player_id NOT IN ({}) AND '.format(placeholders)
    connection.execute(
        'DELETE FROM players WHERE ' + current_filter + 'NOT EXISTS('
        'SELECT 1 FROM matches WHERE matches.player_id=players.player_id)',
        parameters,
    )


def _insert_team(
    connection: sqlite3.Connection, match_id: int, team: IngestMatchTeam
) -> None:
    connection.execute(
        'INSERT INTO match_teams(match_id,role,side,color,kills,economy) '
        'VALUES(?,?,?,?,?,?)',
        (match_id, team.role, team.side, team.color, team.kills, team.economy),
    )
    connection.executemany(
        'INSERT INTO match_participants('
        'match_id,team_role,slot,player_name,hero_name,kills,deaths,assists,'
        'economy,last_hits,is_recorded_player'
        ') VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (
            (
                match_id,
                team.role,
                player.slot,
                player.name,
                player.hero_name,
                player.kills,
                player.deaths,
                player.assists,
                player.economy,
                player.last_hits,
                int(player.is_recorded_player),
            )
            for player in team.players
        ),
    )


def _upsert_match(
    connection: sqlite3.Connection, match: IngestMatch, now: int
) -> Tuple[bool, Set[int]]:
    revision = _match_revision(match)
    fingerprint = _ingest_match_fingerprint(match)
    existing = connection.execute(
        'SELECT revision_sha256,player_id,exact_fingerprint FROM matches '
        'WHERE source_match_id=?',
        (match.id,),
    ).fetchone()
    if existing is not None and str(existing['revision_sha256']) == revision:
        return False, set()
    affected_player_ids = {match.player_id}
    if existing is not None:
        affected_player_ids.add(int(existing['player_id']))
        affected_player_ids.update(
            _players_for_fingerprints(connection, (existing['exact_fingerprint'],))
        )
    replay_kind = None if match.replay is None else match.replay.kind
    replay_url = None if match.replay is None else str(match.replay.url)
    image_url = None if match.result_image is None else str(match.result_image.url)
    image_width = None if match.result_image is None else match.result_image.width
    image_height = None if match.result_image is None else match.result_image.height
    played_at = _utc_iso(match.played_at)
    played_at_epoch = int(match.played_at.timestamp())
    connection.execute(
        'INSERT INTO matches('
        'source_match_id,revision_sha256,player_id,season_key,mode,played_at,'
        'played_at_epoch,duration_seconds,result,stream_title,replay_kind,replay_url,'
        'result_image_url,result_image_width,result_image_height,exact_fingerprint,'
        'created_at,updated_at'
        ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(source_match_id) DO UPDATE SET '
        'revision_sha256=excluded.revision_sha256,player_id=excluded.player_id,'
        'season_key=excluded.season_key,mode=excluded.mode,'
        'played_at=excluded.played_at,played_at_epoch=excluded.played_at_epoch,'
        'duration_seconds=excluded.duration_seconds,result=excluded.result,'
        'stream_title=excluded.stream_title,replay_kind=excluded.replay_kind,'
        'replay_url=excluded.replay_url,result_image_url=excluded.result_image_url,'
        'result_image_width=excluded.result_image_width,'
        'result_image_height=excluded.result_image_height,'
        'exact_fingerprint=excluded.exact_fingerprint,'
        'updated_at=excluded.updated_at',
        (
            match.id,
            revision,
            match.player_id,
            match.season_key,
            match.mode,
            played_at,
            played_at_epoch,
            match.duration_seconds,
            match.result,
            match.stream_title,
            replay_kind,
            replay_url,
            image_url,
            image_width,
            image_height,
            fingerprint,
            now,
            now,
        ),
    )
    connection.execute('DELETE FROM match_teams WHERE match_id=?', (match.id,))
    _insert_team(connection, match.id, match.ally)
    _insert_team(connection, match.id, match.enemy)
    affected_player_ids.update(_players_for_fingerprints(connection, (fingerprint,)))
    connection.execute(
        'DELETE FROM removed_matches WHERE source_match_id=?', (match.id,)
    )
    return True, affected_player_ids


def _normalize_search(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', value).casefold()
    return ''.join(character for character in normalized if character.isalnum())


def _search_forms(value: str) -> Tuple[str, str, str]:
    return (
        _normalize_search(value),
        _normalize_search(''.join(lazy_pinyin(value, style=Style.NORMAL))),
        _normalize_search(''.join(lazy_pinyin(value, style=Style.FIRST_LETTER))),
    )


def _rebuild_match_search(
    connection: sqlite3.Connection, match_ids: Iterable[int]
) -> None:
    for match_id in sorted(set(match_ids)):
        connection.execute('DELETE FROM match_search WHERE match_id=?', (match_id,))
        match = connection.execute(
            'SELECT match.player_id,match.stream_title,player.name,'
            'player.room_label FROM matches match '
            'JOIN players player ON player.player_id=match.player_id '
            'WHERE match.source_match_id=?',
            (match_id,),
        ).fetchone()
        if match is None:
            continue
        segments: List[Tuple[str, str]] = [
            ('stream_title', str(match['stream_title'])),
            ('player_name', str(match['name'])),
            ('room_label', str(match['room_label'])),
        ]
        segments.extend(
            ('player_alias', str(row['alias']))
            for row in connection.execute(
                'SELECT alias FROM player_aliases WHERE player_id=? ORDER BY alias',
                (int(match['player_id']),),
            ).fetchall()
        )
        segments.extend(
            ('room_id', str(row['room_id']))
            for row in connection.execute(
                'SELECT room_id FROM player_rooms WHERE player_id=? ORDER BY room_id',
                (int(match['player_id']),),
            ).fetchall()
        )
        segments.extend(
            ('participant_name', str(row['player_name']))
            for row in connection.execute(
                'SELECT player_name FROM match_participants WHERE match_id=? '
                'ORDER BY team_role,slot',
                (match_id,),
            ).fetchall()
        )
        for kind, raw_value in segments:
            normalized, pinyin, initials = _search_forms(raw_value)
            if not normalized:
                continue
            connection.execute(
                'INSERT INTO match_search('
                'match_id,segment_kind,normalized,pinyin,initials'
                ') VALUES(?,?,?,?,?)',
                (match_id, kind, normalized, pinyin, initials),
            )


def _insert_rating_timeline(
    connection: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
    *,
    scope: str,
    season_key: str,
    previous_ability: Optional[float],
    previous_evidence: Optional[float],
    reset_visible_score: bool,
) -> Tuple[Optional[float], Optional[float]]:
    timeline = calculate_virtual_match_rating_timeline(
        results=[str(row['result']) for row in rows],
        previous_ability=previous_ability,
        previous_evidence=previous_evidence,
        reset_visible_score=reset_visible_score,
    )
    for match_number, (row, transition) in enumerate(zip(rows, timeline), start=1):
        after = transition.rating_after
        connection.execute(
            'INSERT INTO rating_events('
            'match_id,player_id,season_key,scope,match_number,result,'
            'score_before,score_delta,score_after,ability_after,evidence_after,'
            'provisional,model_version'
            ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                int(row['source_match_id']),
                int(row['player_id']),
                season_key,
                scope,
                match_number,
                str(row['result']),
                transition.score_before,
                transition.score_delta,
                transition.score_after,
                after.ability,
                after.evidence,
                int(after.provisional),
                RATING_MODEL_VERSION,
            ),
        )
    if not timeline:
        return previous_ability, previous_evidence
    final = timeline[-1].rating_after
    return final.ability, final.evidence


def _recompute_ratings(
    connection: sqlite3.Connection, player_ids: Iterable[int]
) -> int:
    parameters = tuple(sorted(set(player_ids)))
    if not parameters:
        return 0
    placeholders = ','.join('?' for _ in parameters)
    connection.execute(
        'DELETE FROM rating_events WHERE player_id IN ({})'.format(placeholders),
        parameters,
    )
    all_rows = connection.execute(
        'SELECT source_match_id,player_id,season_key,mode,result,played_at_epoch,'
        'exact_fingerprint '
        'FROM matches WHERE player_id IN ({}) '
        'ORDER BY player_id,played_at_epoch,source_match_id'.format(placeholders),
        parameters,
    ).fetchall()
    inserted = 0
    for player_id, player_group in groupby(
        all_rows, key=lambda row: int(row['player_id'])
    ):
        player_rows = []
        seen_fingerprints = set()
        for row in player_group:
            fingerprint = row['exact_fingerprint']
            if fingerprint is not None:
                value = str(fingerprint)
                if value in seen_fingerprints:
                    continue
                seen_fingerprints.add(value)
            player_rows.append(row)
        scopes = ('all', '3v3', 'brawl', '5v5')
        for scope in scopes:
            scoped_rows = [
                row for row in player_rows if scope == 'all' or row['mode'] == scope
            ]
            if not scoped_rows:
                continue
            previous_ability: Optional[float] = None
            previous_evidence: Optional[float] = None
            for season_key, season_group in groupby(
                scoped_rows, key=lambda row: str(row['season_key'])
            ):
                season_rows = list(season_group)
                previous_ability, previous_evidence = _insert_rating_timeline(
                    connection,
                    season_rows,
                    scope=scope,
                    season_key=season_key,
                    previous_ability=previous_ability,
                    previous_evidence=previous_evidence,
                    reset_visible_score=True,
                )
                inserted += len(season_rows)
            _insert_rating_timeline(
                connection,
                scoped_rows,
                scope=scope,
                season_key='all-time',
                previous_ability=None,
                previous_evidence=None,
                reset_visible_score=False,
            )
            inserted += len(scoped_rows)
    return inserted


def reconcile_match_fingerprints(database_path: Path) -> int:
    """Backfill exact identities and rebuild ratings after the schema upgrade."""

    connection = connect_database(database_path)
    try:
        connection.execute('BEGIN IMMEDIATE')
        changed = 0
        for row in connection.execute(
            'SELECT source_match_id,exact_fingerprint FROM matches '
            'ORDER BY source_match_id'
        ).fetchall():
            match_id = int(row['source_match_id'])
            fingerprint = _stored_match_fingerprint(connection, match_id)
            if row['exact_fingerprint'] == fingerprint:
                continue
            connection.execute(
                'UPDATE matches SET exact_fingerprint=? WHERE source_match_id=?',
                (fingerprint, match_id),
            )
            changed += 1
        if changed:
            player_ids = [
                int(row['player_id'])
                for row in connection.execute(
                    'SELECT player_id FROM players ORDER BY player_id'
                ).fetchall()
            ]
            _recompute_ratings(connection, player_ids)
            initialized = connection.execute(
                'SELECT 1 FROM ingestion_batches LIMIT 1'
            ).fetchone()
            if initialized is not None:
                refresh_dashboard_state(
                    connection, generated_at=datetime.now(timezone.utc)
                )
        connection.commit()
        return changed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def apply_ingest_batch(
    database_path: Path, *, idempotency_key: str, batch: IngestBatch
) -> Dict[str, Any]:
    payload_sha256 = _payload_sha256(batch)
    now = int(time.time())
    connection = connect_database(database_path)
    try:
        connection.execute('BEGIN IMMEDIATE')
        previous = connection.execute(
            'SELECT payload_sha256,match_count,removed_match_count '
            'FROM ingestion_batches WHERE idempotency_key=?',
            (idempotency_key,),
        ).fetchone()
        if previous is not None:
            if str(previous['payload_sha256']) != payload_sha256:
                raise IdempotencyConflict(idempotency_key)
            connection.rollback()
            return {
                'batchId': idempotency_key,
                'status': 'duplicate',
                'matchCount': int(previous['match_count']),
                'removedMatchCount': int(previous['removed_match_count']),
                'ratingEventCount': 0,
            }

        current_player_ids = {player.id for player in batch.players}
        changed_player_ids: Set[int] = set()
        for player in batch.players:
            changed_player_ids.update(_upsert_player(connection, player, now))

        changed_match_ids: Set[int] = set()
        rating_player_ids: Set[int] = set()
        for removed_match_id in batch.removed_match_ids:
            removed = connection.execute(
                'SELECT player_id,exact_fingerprint FROM matches '
                'WHERE source_match_id=?',
                (removed_match_id,),
            ).fetchone()
            if removed is not None:
                rating_player_ids.add(int(removed['player_id']))
                rating_player_ids.update(
                    _players_for_fingerprints(
                        connection, (removed['exact_fingerprint'],)
                    )
                )
            connection.execute(
                'DELETE FROM match_search WHERE match_id=?', (removed_match_id,)
            )
            connection.execute(
                'DELETE FROM matches WHERE source_match_id=?', (removed_match_id,)
            )
            connection.execute(
                'INSERT INTO removed_matches(source_match_id,removed_at) VALUES(?,?) '
                'ON CONFLICT(source_match_id) DO UPDATE SET '
                'removed_at=excluded.removed_at',
                (removed_match_id, now),
            )
        for match in batch.matches:
            changed, affected_player_ids = _upsert_match(connection, match, now)
            if changed:
                changed_match_ids.add(match.id)
                rating_player_ids.update(affected_player_ids)

        if changed_player_ids:
            placeholders = ','.join('?' for _ in changed_player_ids)
            changed_match_ids.update(
                int(row['source_match_id'])
                for row in connection.execute(
                    'SELECT source_match_id FROM matches WHERE player_id IN ('
                    + placeholders
                    + ')',
                    tuple(sorted(changed_player_ids)),
                ).fetchall()
            )
        _rebuild_match_search(connection, changed_match_ids)
        rating_event_count = (
            _recompute_ratings(connection, rating_player_ids)
            if rating_player_ids
            else 0
        )
        _delete_unreferenced_players(connection, tuple(current_player_ids))
        refresh_dashboard_state(connection, generated_at=batch.generated_at)
        connection.execute(
            'INSERT INTO ingestion_batches('
            'idempotency_key,payload_sha256,source_last_match_id,match_count,'
            'removed_match_count,applied_at'
            ') VALUES(?,?,?,?,?,?)',
            (
                idempotency_key,
                payload_sha256,
                batch.source_last_match_id,
                len(batch.matches),
                len(batch.removed_match_ids),
                now,
            ),
        )
        connection.commit()
        return {
            'batchId': idempotency_key,
            'status': 'applied',
            'matchCount': len(batch.matches),
            'removedMatchCount': len(batch.removed_match_ids),
            'ratingEventCount': rating_event_count,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _row_player(connection: sqlite3.Connection, player_id: int) -> Dict[str, Any]:
    row = connection.execute(
        'SELECT player_id,name,initial,room_label,avatar_url FROM players '
        'WHERE player_id=?',
        (player_id,),
    ).fetchone()
    if row is None:
        raise LookupError('player not found')
    return {
        'id': int(row['player_id']),
        'name': str(row['name']),
        'initial': str(row['initial']),
        'roomLabel': str(row['room_label']),
        'roomIds': [
            int(value['room_id'])
            for value in connection.execute(
                'SELECT room_id FROM player_rooms WHERE player_id=? ORDER BY room_id',
                (player_id,),
            ).fetchall()
        ],
        'aliases': [
            str(value['alias'])
            for value in connection.execute(
                'SELECT alias FROM player_aliases WHERE player_id=? ORDER BY alias',
                (player_id,),
            ).fetchall()
        ],
        'avatarUrl': None if row['avatar_url'] is None else str(row['avatar_url']),
    }


def _row_team(
    connection: sqlite3.Connection, match_id: int, role: str
) -> Dict[str, Any]:
    team = connection.execute(
        'SELECT side,color,kills,economy FROM match_teams '
        'WHERE match_id=? AND role=?',
        (match_id, role),
    ).fetchone()
    if team is None:
        raise LookupError('match team not found')
    players = connection.execute(
        'SELECT slot,player_name,hero_name,kills,deaths,assists,economy,'
        'last_hits,is_recorded_player FROM match_participants '
        'WHERE match_id=? AND team_role=? ORDER BY slot',
        (match_id, role),
    ).fetchall()
    return {
        'side': str(team['side']),
        'color': str(team['color']),
        'kills': team['kills'],
        'economy': team['economy'],
        'players': [
            {
                'slot': int(player['slot']),
                'name': str(player['player_name']),
                'heroName': str(player['hero_name']),
                'kills': player['kills'],
                'deaths': player['deaths'],
                'assists': player['assists'],
                'economy': player['economy'],
                'lastHits': player['last_hits'],
                'isRecordedPlayer': bool(player['is_recorded_player']),
            }
            for player in players
        ],
    }


def _row_rating(
    connection: sqlite3.Connection, match_id: int, *, scope: str, season_key: str
) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        'SELECT score_before,score_delta,score_after,match_number,provisional,'
        'model_version FROM rating_events '
        'WHERE match_id=? AND scope=? AND season_key=?',
        (match_id, scope, season_key),
    ).fetchone()
    if row is None:
        return None
    return {
        'scope': scope,
        'seasonKey': season_key,
        'matchNumber': int(row['match_number']),
        'scoreBefore': int(row['score_before']),
        'scoreDelta': int(row['score_delta']),
        'scoreAfter': int(row['score_after']),
        'provisional': bool(row['provisional']),
        'modelVersion': int(row['model_version']),
    }


def _row_match(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    rating_scope: str,
    rating_season: Optional[str],
) -> Dict[str, Any]:
    match_id = int(row['source_match_id'])
    season_key = str(row['season_key']) if rating_season is None else rating_season
    replay = None
    if row['replay_url'] is not None:
        replay = {'kind': str(row['replay_kind']), 'url': str(row['replay_url'])}
    result_image = None
    if row['result_image_url'] is not None:
        result_image = {
            'url': str(row['result_image_url']),
            'width': int(row['result_image_width']),
            'height': int(row['result_image_height']),
        }
    return {
        'id': match_id,
        'playerId': int(row['player_id']),
        'player': _row_player(connection, int(row['player_id'])),
        'seasonKey': str(row['season_key']),
        'mode': str(row['mode']),
        'playedAt': str(row['played_at']),
        'durationSeconds': int(row['duration_seconds']),
        'result': str(row['result']),
        'streamTitle': str(row['stream_title']),
        'ally': _row_team(connection, match_id, 'ally'),
        'enemy': _row_team(connection, match_id, 'enemy'),
        'rating': _row_rating(
            connection, match_id, scope=rating_scope, season_key=season_key
        ),
        'replay': replay,
        'resultImage': result_image,
    }


def _search_clause(query: str, parameters: List[Any]) -> str:
    normalized = _normalize_search(query)
    if not normalized:
        return ''
    if len(normalized) >= 3:
        parameters.append('"' + normalized.replace('"', '""') + '"')
        return (
            ' AND EXISTS(SELECT 1 FROM match_search search '
            'WHERE search.match_id=matches.source_match_id '
            'AND match_search MATCH ?)'
        )
    parameters.extend((normalized, normalized, normalized))
    return (
        ' AND EXISTS(SELECT 1 FROM match_search search '
        'WHERE search.match_id=matches.source_match_id '
        'AND (instr(search.normalized,?)>0 OR instr(search.pinyin,?)>0 '
        'OR instr(search.initials,?)>0))'
    )


def list_matches(
    database_path: Path,
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
    conditions = ' WHERE 1=1'
    parameters: List[Any] = []
    if season is not None:
        conditions += ' AND matches.season_key=?'
        parameters.append(season)
    if mode is not None:
        conditions += ' AND matches.mode=?'
        parameters.append(mode)
    if player_id is not None:
        conditions += ' AND matches.player_id=?'
        parameters.append(player_id)
    conditions += _search_clause(query, parameters)
    for hero in heroes:
        conditions += (
            ' AND EXISTS(SELECT 1 FROM match_participants participant '
            'WHERE participant.match_id=matches.source_match_id '
            'AND participant.hero_name=? COLLATE NOCASE)'
        )
        parameters.append(hero)
    connection = connect_database(database_path)
    try:
        total_row = connection.execute(
            'SELECT COUNT(*) FROM matches' + conditions, parameters
        ).fetchone()
        total = 0 if total_row is None else int(total_row[0])
        rows = connection.execute(
            'SELECT * FROM matches'
            + conditions
            + ' ORDER BY played_at_epoch DESC,source_match_id DESC LIMIT ? OFFSET ?',
            (*parameters, page_size, (page - 1) * page_size),
        ).fetchall()
        return {
            'items': [
                _row_match(
                    connection,
                    row,
                    rating_scope=rating_scope,
                    rating_season=rating_season,
                )
                for row in rows
            ],
            'page': page,
            'pageSize': page_size,
            'total': total,
        }
    finally:
        connection.close()


def get_match(
    database_path: Path,
    match_id: int,
    *,
    rating_scope: str,
    rating_season: Optional[str],
) -> Dict[str, Any]:
    connection = connect_database(database_path)
    try:
        row = connection.execute(
            'SELECT * FROM matches WHERE source_match_id=?', (match_id,)
        ).fetchone()
        if row is None:
            raise LookupError('match not found')
        return _row_match(
            connection, row, rating_scope=rating_scope, rating_season=rating_season
        )
    finally:
        connection.close()


def get_match_summary(
    database_path: Path,
    *,
    season: Optional[str],
    mode: Optional[str],
    player_id: Optional[int],
) -> Dict[str, int]:
    conditions = ' WHERE 1=1'
    parameters: List[Any] = []
    if season is not None:
        conditions += ' AND season_key=?'
        parameters.append(season)
    if mode is not None:
        conditions += ' AND mode=?'
        parameters.append(mode)
    if player_id is not None:
        conditions += ' AND player_id=?'
        parameters.append(player_id)
    connection = connect_database(database_path)
    try:
        row = connection.execute(
            'SELECT COUNT(*) AS matches,'
            "COALESCE(SUM(CASE result WHEN 'W' THEN 1 ELSE 0 END),0) AS wins,"
            'COUNT(DISTINCT player_id) AS players,'
            'COALESCE(ROUND(AVG(duration_seconds)),0) AS average_duration,'
            'COALESCE(SUM(CASE WHEN replay_url IS NOT NULL THEN 1 ELSE 0 END),0) '
            'AS replays FROM matches' + conditions,
            parameters,
        ).fetchone()
        assert row is not None
        return {
            'matches': int(row['matches']),
            'wins': int(row['wins']),
            'players': int(row['players']),
            'averageDurationSeconds': int(row['average_duration']),
            'replays': int(row['replays']),
        }
    finally:
        connection.close()
