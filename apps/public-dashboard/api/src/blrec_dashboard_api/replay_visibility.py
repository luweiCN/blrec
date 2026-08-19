from __future__ import annotations

import re
import time
from typing import Any, Dict, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .database import DatabaseTarget, connect_database, is_postgres

CACHE_TTL_SECONDS = 15 * 60
CLAIM_TIMEOUT_SECONDS = 2 * 60
BVID_PATTERN = re.compile(r'^BV[0-9A-Za-z]{4,18}$')


def replay_bvid(replay: object) -> Optional[str]:
    if not isinstance(replay, Mapping):
        return None
    url = replay.get('url')
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in {
        'bilibili.com',
        'www.bilibili.com',
    }:
        return None
    parts = tuple(part for part in parsed.path.split('/') if part)
    if len(parts) != 2 or parts[0] != 'video':
        return None
    return parts[1] if BVID_PATTERN.fullmatch(parts[1]) else None


def _cache_rows(connection: Any, bvids: Sequence[str]) -> Dict[str, Mapping[str, Any]]:
    if not bvids:
        return {}
    placeholders = ','.join('?' for _value in bvids)
    rows = connection.execute(
        'SELECT bvid,state,expires_at,claimed_at FROM replay_visibility_checks '
        'WHERE bvid IN ({})'.format(placeholders),
        tuple(bvids),
    ).fetchall()
    return {str(row['bvid']): row for row in rows}


def resolve_match_replays(
    target: DatabaseTarget,
    matches: Sequence[Mapping[str, Any]],
    candidates: Mapping[int, Mapping[str, Any]],
    *,
    owner_view: bool,
    now: Optional[int] = None,
) -> list[Dict[str, Any]]:
    values = [dict(match) for match in matches]
    if owner_view:
        for value in values:
            replay = candidates.get(int(value['id']), {}).get('replay')
            if isinstance(replay, Mapping):
                value['replay'] = replay
                value['replayStatus'] = 'available'
            else:
                value.pop('replay', None)
                value['replayStatus'] = 'unavailable'
        return values

    checked_at = int(time.time()) if now is None else now
    replay_by_match: Dict[int, Mapping[str, Any]] = {}
    bvid_by_match: Dict[int, str] = {}
    for value in values:
        match_id = int(value['id'])
        replay = candidates.get(match_id, {}).get('replay')
        bvid = replay_bvid(replay)
        if bvid is not None and isinstance(replay, Mapping):
            replay_by_match[match_id] = replay
            bvid_by_match[match_id] = bvid
        value.pop('replay', None)

    unique_bvids = tuple(dict.fromkeys(bvid_by_match.values()))
    if not unique_bvids:
        for value in values:
            value['replayStatus'] = 'unavailable'
        return values
    connection = connect_database(target)
    try:
        connection.execute('BEGIN IMMEDIATE')
        rows = _cache_rows(connection, unique_bvids)
        states: Dict[str, str] = {}
        for bvid in unique_bvids:
            row = rows.get(bvid)
            if row is None:
                connection.execute(
                    'INSERT INTO replay_visibility_checks('
                    'bvid,state,checked_at,expires_at,requested_at,claimed_at,'
                    'attempt_count,next_attempt_at,last_error,updated_at'
                    ") VALUES(?,'pending',NULL,NULL,?,NULL,0,?,NULL,?) "
                    'ON CONFLICT(bvid) DO NOTHING',
                    (bvid, checked_at, checked_at, checked_at),
                )
                states[bvid] = 'checking'
                continue
            state = str(row['state'])
            expires_at = row['expires_at']
            if (
                state in {'public', 'unavailable'}
                and expires_at is not None
                and int(expires_at) > checked_at
            ):
                states[bvid] = state
                continue
            claimed_at = row['claimed_at']
            if (
                state in {'public', 'unavailable'}
                or state == 'checking'
                and (
                    claimed_at is None
                    or int(claimed_at) <= checked_at - CLAIM_TIMEOUT_SECONDS
                )
            ):
                connection.execute(
                    "UPDATE replay_visibility_checks SET state='pending',"
                    'checked_at=NULL,expires_at=NULL,requested_at=?,claimed_at=NULL,'
                    'attempt_count=0,next_attempt_at=?,last_error=NULL,updated_at=? '
                    'WHERE bvid=? AND ('
                    "(state IN ('public','unavailable') "
                    'AND (expires_at IS NULL OR expires_at<=?)) OR '
                    "(state='checking' "
                    'AND (claimed_at IS NULL OR claimed_at<=?)))',
                    (
                        checked_at,
                        checked_at,
                        checked_at,
                        bvid,
                        checked_at,
                        checked_at - CLAIM_TIMEOUT_SECONDS,
                    ),
                )
            states[bvid] = 'checking'
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    for value in values:
        match_id = int(value['id'])
        bvid = bvid_by_match.get(match_id)
        if bvid is None:
            value['replayStatus'] = 'unavailable'
        elif states.get(bvid) == 'public':
            value['replay'] = replay_by_match[match_id]
            value['replayStatus'] = 'available'
        elif states.get(bvid) == 'unavailable':
            value['replayStatus'] = 'unavailable'
        else:
            value['replayStatus'] = 'checking'
    return values


def claim_replay_visibility(
    target: DatabaseTarget, *, now: Optional[int] = None
) -> Optional[str]:
    claimed_at = int(time.time()) if now is None else now
    connection = connect_database(target)
    try:
        connection.execute('BEGIN IMMEDIATE')
        lock_clause = ' FOR UPDATE SKIP LOCKED' if is_postgres(target) else ''
        row = connection.execute(
            'SELECT bvid FROM replay_visibility_checks WHERE '
            "(state='pending' AND next_attempt_at<=?) OR "
            "(state='checking' AND (claimed_at IS NULL OR claimed_at<=?)) "
            'ORDER BY requested_at,bvid LIMIT 1' + lock_clause,
            (claimed_at, claimed_at - CLAIM_TIMEOUT_SECONDS),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        bvid = str(row['bvid'])
        connection.execute(
            "UPDATE replay_visibility_checks SET state='checking',claimed_at=?,"
            'attempt_count=attempt_count+1,updated_at=? WHERE bvid=?',
            (claimed_at, claimed_at, bvid),
        )
        connection.commit()
        return bvid
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_replay_visibility(
    target: DatabaseTarget,
    bvid: str,
    *,
    public_visible: bool,
    now: Optional[int] = None,
) -> str:
    if BVID_PATTERN.fullmatch(bvid) is None:
        raise ValueError('invalid BVID')
    completed_at = int(time.time()) if now is None else now
    state = 'public' if public_visible else 'unavailable'
    connection = connect_database(target)
    try:
        cursor = connection.execute(
            'UPDATE replay_visibility_checks SET state=?,checked_at=?,expires_at=?,'
            'claimed_at=NULL,next_attempt_at=?,last_error=NULL,updated_at=? '
            'WHERE bvid=?',
            (
                state,
                completed_at,
                completed_at + CACHE_TTL_SECONDS,
                completed_at + CACHE_TTL_SECONDS,
                completed_at,
                bvid,
            ),
        )
        connection.commit()
        if cursor.rowcount != 1:
            raise LookupError('replay visibility task not found')
        return state
    finally:
        connection.close()


def fail_replay_visibility(
    target: DatabaseTarget, bvid: str, error: str, *, now: Optional[int] = None
) -> int:
    if BVID_PATTERN.fullmatch(bvid) is None:
        raise ValueError('invalid BVID')
    failed_at = int(time.time()) if now is None else now
    connection = connect_database(target)
    try:
        row = connection.execute(
            'SELECT attempt_count FROM replay_visibility_checks WHERE bvid=?', (bvid,)
        ).fetchone()
        if row is None:
            raise LookupError('replay visibility task not found')
        delay = min(300, max(5, 2 ** min(int(row['attempt_count']), 8)))
        connection.execute(
            "UPDATE replay_visibility_checks SET state='pending',claimed_at=NULL,"
            'next_attempt_at=?,last_error=?,updated_at=? WHERE bvid=?',
            (
                failed_at + delay,
                error.strip()[:500] or 'unknown error',
                failed_at,
                bvid,
            ),
        )
        connection.commit()
        return delay
    finally:
        connection.close()
