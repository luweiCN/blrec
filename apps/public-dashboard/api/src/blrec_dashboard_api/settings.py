from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SHA256_PATTERN = re.compile(r'^[0-9a-f]{64}$')


@dataclass(frozen=True)
class ApiSettings:
    database_path: Path
    ingest_token_sha256: str
    cors_origins: tuple[str, ...]
    owner_token_sha256: str = ''
    database_url: str = ''
    source_database_path: Path | None = None
    source_database_url: str = ''
    source_watch_seconds: float = 1.0
    repository_mode: str = 'direct'

    def __post_init__(self) -> None:
        if not _SHA256_PATTERN.fullmatch(self.ingest_token_sha256):
            raise ValueError('dashboard API ingest token SHA-256 is invalid')
        if self.owner_token_sha256 and not _SHA256_PATTERN.fullmatch(
            self.owner_token_sha256
        ):
            raise ValueError('dashboard API owner token SHA-256 is invalid')
        if not self.cors_origins:
            raise ValueError('dashboard API requires at least one CORS origin')
        if self.database_url and not self.database_url.startswith(
            ('postgresql://', 'postgresql+psycopg://')
        ):
            raise ValueError('dashboard API database URL must use PostgreSQL')
        if self.source_database_url and not self.source_database_url.startswith(
            ('postgresql://', 'postgresql+psycopg://')
        ):
            raise ValueError('dashboard API source database URL must use PostgreSQL')
        if self.source_watch_seconds <= 0:
            raise ValueError('dashboard API source watch interval must be positive')
        if self.repository_mode not in {'direct', 'postgres'}:
            raise ValueError('dashboard API repository mode must be direct or postgres')

    @property
    def database_target(self) -> Path | str:
        return self.database_url or self.database_path

    @property
    def source_database_target(self) -> Path | str:
        if self.source_database_url:
            return _database_url_for_schema(self.source_database_url, 'core')
        if self.database_url:
            return _database_url_for_schema(self.database_url, 'core')
        return self.source_database_path or self.database_path

    @classmethod
    def from_environment(cls) -> 'ApiSettings':
        database_path = os.environ.get(
            'DASHBOARD_API_DATABASE_PATH',
            '/var/lib/blrec-dashboard-api/dashboard.sqlite3',
        )
        database_url = os.environ.get('DASHBOARD_API_DATABASE_URL', '').strip()
        source_database_path = os.environ.get(
            'DASHBOARD_API_SOURCE_DATABASE_PATH', ''
        ).strip()
        source_database_url = os.environ.get(
            'DASHBOARD_API_SOURCE_DATABASE_URL', ''
        ).strip()
        token_sha256 = os.environ.get('DASHBOARD_API_INGEST_TOKEN_SHA256', '')
        owner_token_sha256 = os.environ.get(
            'DASHBOARD_API_OWNER_TOKEN_SHA256', ''
        ).strip()
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
            owner_token_sha256=owner_token_sha256,
            database_url=database_url,
            source_database_path=(
                Path(source_database_path) if source_database_path else None
            ),
            source_database_url=source_database_url,
            source_watch_seconds=float(
                os.environ.get('DASHBOARD_API_SOURCE_WATCH_SECONDS', '1')
            ),
            repository_mode=os.environ.get(
                'DASHBOARD_API_REPOSITORY_MODE', 'direct'
            ).strip(),
        )


def _database_url_for_schema(database_url: str, schema: str) -> str:
    parts = urlsplit(database_url)
    parameters = parse_qsl(parts.query, keep_blank_values=True)
    filtered = [(key, value) for key, value in parameters if key != 'options']
    options = next((value for key, value in parameters if key == 'options'), '')
    option_parts = [
        value for value in options.split() if not value.startswith('-csearch_path=')
    ]
    option_parts.append('-csearch_path={}'.format(schema))
    filtered.append(('options', ' '.join(option_parts)))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(filtered), parts.fragment)
    )
