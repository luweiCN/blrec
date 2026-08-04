from __future__ import annotations

import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

_TITLE_TIME_PATTERN = re.compile(
    r'(?<!\d)(20\d{2})\s*(?:年|[-/.])\s*(\d{1,2})\s*'
    r'(?:月|[-/.])\s*(\d{1,2})\s*(?:日|号)?\s*'
    r'(\d{1,2})\s*(?:点|时|[:：])\s*(\d{1,2})\s*分?'
)
_SHANGHAI = ZoneInfo('Asia/Shanghai')


def current_season_started_at(now: int) -> int:
    local = datetime.fromtimestamp(int(now), _SHANGHAI)
    if local.month < 5:
        month = 1
    elif local.month < 9:
        month = 5
    else:
        month = 9
    return int(datetime(local.year, month, 1, tzinfo=_SHANGHAI).timestamp())


def resolve_recording_started_at(
    title: str,
    *,
    published_at: Optional[int],
    fallback: int,
    maximum_publish_gap_seconds: int = 12 * 60 * 60,
) -> int:
    title_time = _title_timestamp(title)
    if title_time is None:
        return int(published_at or fallback)
    if published_at is None:
        return title_time
    if abs(title_time - int(published_at)) > maximum_publish_gap_seconds:
        return title_time
    return int(published_at)


def _title_timestamp(title: str) -> Optional[int]:
    for match in reversed(tuple(_TITLE_TIME_PATTERN.finditer(title))):
        try:
            value = datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
                int(match.group(5)),
                tzinfo=_SHANGHAI,
            )
        except ValueError:
            continue
        return int(value.timestamp())
    return None
