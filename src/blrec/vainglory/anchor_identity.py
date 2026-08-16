from __future__ import annotations

import re
import sqlite3
from typing import Iterable, Optional, Tuple

_LIVE_ROOM_PATTERNS = (
    re.compile(
        r'(?:https?://)?(?:www\.|m\.)?live\.bilibili\.com/(?:h5/)?(\d+)', re.IGNORECASE
    ),
    re.compile(r'(?:原)?直播间(?:号|ID)?\s*[:：#]?\s*(\d{3,})', re.IGNORECASE),
)
_EXPLICIT_NAME_PATTERNS = (
    re.compile(
        r'([^\s【】\[\]()（）:：]{1,32})的直播(?:回放|录像|录播)?', re.IGNORECASE
    ),
    re.compile(r'主播\s*[:：]\s*([^\s【】\[\]()（）:：]{1,32})', re.IGNORECASE),
)
_TRUSTED_SESSION_SQL = (
    "broadcast_session_key NOT LIKE 'bili-migration:%' "
    "AND broadcast_session_key NOT LIKE 'bili-archive:%'"
)


def infer_recorded_anchor(
    connection: sqlite3.Connection,
    title: str,
    description: str,
    *,
    excluded_anchor_uids: Iterable[int] = (),
    excluded_anchor_names: Iterable[str] = (),
) -> Tuple[int, Optional[int], str]:
    """Infer the person in a recording, never the account that uploaded it."""
    excluded_uids = {int(value) for value in excluded_anchor_uids if int(value) > 0}
    excluded_names = {
        str(value).strip().casefold()
        for value in excluded_anchor_names
        if str(value).strip()
    }
    combined = '{}\n{}'.format(title, description)
    room_ids = []
    for pattern in _LIVE_ROOM_PATTERNS:
        for matched in pattern.finditer(combined):
            room_id = int(matched.group(1))
            if room_id > 0 and room_id not in room_ids:
                room_ids.append(room_id)
    excluded_room_ids = set()
    for room_id in room_ids:
        bound = connection.execute(
            'SELECT player.name,latest.anchor_uid,latest.anchor_name '
            'FROM vainglory_player_rooms room '
            'JOIN vainglory_players player ON player.id=room.player_id '
            'LEFT JOIN recording_sessions latest ON latest.id=('
            'SELECT known.id FROM recording_sessions known '
            'WHERE known.room_id=room.room_id '
            'ORDER BY known.started_at DESC,known.id DESC LIMIT 1) '
            'WHERE room.room_id=?',
            (room_id,),
        ).fetchone()
        if bound is not None:
            bound_uid = (
                None if bound['anchor_uid'] is None else int(bound['anchor_uid'])
            )
            bound_name = str(bound['anchor_name'] or bound['name']).strip()
            if bound_uid in excluded_uids or bound_name.casefold() in excluded_names:
                excluded_room_ids.add(room_id)
                continue
            return (room_id, bound_uid, bound_name)
    for room_id in room_ids:
        known = connection.execute(
            'SELECT anchor_uid,anchor_name FROM recording_sessions '
            'WHERE room_id=? AND anchor_name!=\'\' AND '
            + _TRUSTED_SESSION_SQL
            + ' ORDER BY started_at DESC,id DESC LIMIT 1',
            (room_id,),
        ).fetchone()
        if known is not None:
            known_uid = (
                None if known['anchor_uid'] is None else int(known['anchor_uid'])
            )
            known_name = str(known['anchor_name']).strip()
            if known_uid in excluded_uids or known_name.casefold() in excluded_names:
                excluded_room_ids.add(room_id)
                continue
            return (room_id, known_uid, known_name)

    eligible_room_ids = [
        room_id for room_id in room_ids if room_id not in excluded_room_ids
    ]
    room_id = eligible_room_ids[0] if len(eligible_room_ids) == 1 else 0

    known_anchors = connection.execute(
        'SELECT room_id,anchor_uid,anchor_name,MAX(started_at) AS latest '
        'FROM recording_sessions WHERE anchor_name!=\'\' AND '
        + _TRUSTED_SESSION_SQL
        + ' GROUP BY room_id,anchor_uid,anchor_name '
        'ORDER BY length(anchor_name) DESC,latest DESC'
    ).fetchall()
    normalized_sources = (title.casefold(), description.casefold())
    for known in known_anchors:
        anchor_name = str(known['anchor_name']).strip()
        anchor_uid = None if known['anchor_uid'] is None else int(known['anchor_uid'])
        if not anchor_name or not any(
            anchor_name.casefold() in source for source in normalized_sources
        ):
            continue
        if anchor_uid in excluded_uids or anchor_name.casefold() in excluded_names:
            continue
        return room_id or int(known['room_id']), anchor_uid, anchor_name

    aliases = connection.execute(
        'SELECT alias.alias,alias.player_id FROM vainglory_player_aliases alias '
        'ORDER BY length(alias.alias) DESC,alias.alias'
    ).fetchall()
    alias_matches = [
        alias
        for alias in aliases
        if len(str(alias['alias']).strip()) >= 2
        and str(alias['alias']).strip().casefold() not in excluded_names
        and any(
            str(alias['alias']).strip().casefold() in source
            for source in normalized_sources
        )
    ]
    matched_player_ids = {int(alias['player_id']) for alias in alias_matches}
    if len(matched_player_ids) == 1:
        return room_id, None, str(alias_matches[0]['alias']).strip()

    for pattern in _EXPLICIT_NAME_PATTERNS:
        matched = pattern.search(combined)
        if matched is None:
            continue
        inferred_name = matched.group(1).strip()
        if inferred_name.casefold() not in excluded_names:
            return room_id, None, inferred_name
    return room_id, None, ''
