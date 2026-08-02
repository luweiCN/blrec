from __future__ import annotations

from typing import Any

EXCLUDED_TITLE_MARKER = '直播剪辑'


def is_excluded_title(*values: Any) -> bool:
    return any(
        isinstance(value, str) and EXCLUDED_TITLE_MARKER in value for value in values
    )
