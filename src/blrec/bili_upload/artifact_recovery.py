from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Mapping, Optional

__all__ = ('RecoveredArtifact', 'probe_recording_artifact')


@dataclass(frozen=True)
class RecoveredArtifact:
    path: str
    size_bytes: int
    duration_seconds: Optional[int]


def probe_recording_artifact(path: str) -> Optional[RecoveredArtifact]:
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        return None
    if size_bytes <= 0:
        return None

    command = (
        'ffprobe',
        '-v',
        'error',
        '-read_intervals',
        '%+#1',
        '-select_streams',
        'v:0',
        '-show_entries',
        'stream=codec_type:format=duration',
        '-of',
        'json',
        path,
    )
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=15)
        if result.returncode != 0:
            return None
        document = json.loads(result.stdout.decode('utf8'))
    except (
        OSError,
        subprocess.TimeoutExpired,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return None
    if not isinstance(document, Mapping):
        return None
    streams = document.get('streams')
    if not isinstance(streams, list) or not any(
        isinstance(stream, Mapping) and stream.get('codec_type') == 'video'
        for stream in streams
    ):
        return None
    duration_seconds = _duration_seconds(document.get('format'))
    return RecoveredArtifact(
        path=path, size_bytes=size_bytes, duration_seconds=duration_seconds
    )


def _duration_seconds(value: Any) -> Optional[int]:
    if not isinstance(value, Mapping):
        return None
    raw_duration = value.get('duration')
    if isinstance(raw_duration, bool) or not isinstance(
        raw_duration, (int, float, str)
    ):
        return None
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        return None
    if duration < 0:
        return None
    return int(duration + 0.5)
