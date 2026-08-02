from __future__ import annotations

import re
import sqlite3
from typing import Iterable, Optional, Tuple

_LIVE_ROOM_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?live\.bilibili\.com/(?:h5/)?(\d+)', re.IGNORECASE
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
    room_match = _LIVE_ROOM_PATTERN.search(combined)
    room_id = 0 if room_match is None else int(room_match.group(1))
    if room_id > 0:
        known = connection.execute(
            'SELECT anchor_uid,anchor_name FROM recording_sessions '
            'WHERE room_id=? AND anchor_name!=\'\' AND '
            + _TRUSTED_SESSION_SQL
            + ' ORDER BY started_at DESC,id DESC LIMIT 1',
            (room_id,),
        ).fetchone()
        if known is not None:
            return (
                room_id,
                None if known['anchor_uid'] is None else int(known['anchor_uid']),
                str(known['anchor_name']),
            )

    known_anchors = connection.execute(
        'SELECT room_id,anchor_uid,anchor_name,MAX(started_at) AS latest '
        'FROM recording_sessions WHERE anchor_name!=\'\' AND '
        + _TRUSTED_SESSION_SQL
        + ' GROUP BY room_id,anchor_uid,anchor_name '
        'ORDER BY length(anchor_name) DESC,latest DESC'
    ).fetchall()
    normalized_title = title.casefold()
    for known in known_anchors:
        anchor_name = str(known['anchor_name']).strip()
        anchor_uid = None if known['anchor_uid'] is None else int(known['anchor_uid'])
        if not anchor_name or anchor_name.casefold() not in normalized_title:
            continue
        if anchor_uid in excluded_uids or anchor_name.casefold() in excluded_names:
            continue
        return room_id or int(known['room_id']), anchor_uid, anchor_name

    for pattern in _EXPLICIT_NAME_PATTERNS:
        matched = pattern.search(title)
        if matched is None:
            continue
        inferred_name = matched.group(1).strip()
        if inferred_name.casefold() not in excluded_names:
            return room_id, None, inferred_name
    return room_id, None, ''
