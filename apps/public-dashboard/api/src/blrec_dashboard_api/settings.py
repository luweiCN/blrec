from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_SHA256_PATTERN = re.compile(r'^[0-9a-f]{64}$')


@dataclass(frozen=True)
class ApiSettings:
    database_path: Path
    ingest_token_sha256: str
    cors_origins: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _SHA256_PATTERN.fullmatch(self.ingest_token_sha256):
            raise ValueError('dashboard API ingest token SHA-256 is invalid')
        if not self.cors_origins:
            raise ValueError('dashboard API requires at least one CORS origin')

    @classmethod
    def from_environment(cls) -> 'ApiSettings':
        database_path = os.environ.get(
            'DASHBOARD_API_DATABASE_PATH',
            '/var/lib/blrec-dashboard-api/dashboard.sqlite3',
        )
        token_sha256 = os.environ.get('DASHBOARD_API_INGEST_TOKEN_SHA256', '')
        cors_origins = tuple(
            value.strip()
            for value in os.environ.get(
                'DASHBOARD_API_CORS_ORIGINS', 'https://vg.luwei.host'
            ).split(',')
            if value.strip()
        )
        return cls(
            database_path=Path(database_path),
            ingest_token_sha256=token_sha256,
            cors_origins=cors_origins,
        )
