from __future__ import annotations

import sys

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:  # pragma: no cover - exercised by the Python 3.8 CI job
    from backports.zoneinfo import ZoneInfo

__all__ = ['ZoneInfo']
