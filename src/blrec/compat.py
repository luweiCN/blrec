from __future__ import annotations

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - exercised by the Python 3.8 CI job
    from backports.zoneinfo import ZoneInfo  # type: ignore

__all__ = ['ZoneInfo']
