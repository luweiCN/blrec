from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from blrec_dashboard_api.models import AssetBatch
from blrec_dashboard_api.realtime import (
    DashboardRealtimeBroker,
    encode_event,
    event_response,
)
from blrec_dashboard_api.settings import ApiSettings


def test_database_urls_select_public_and_core_schemas(tmp_path: Path) -> None:
    settings = ApiSettings(
        database_path=tmp_path / 'dashboard.sqlite3',
        database_url=(
            'postgresql://dashboard:secret@127.0.0.1/dashboard?'
            'connect_timeout=5&options=-csearch_path%3Dpublic'
        ),
        ingest_token_sha256=hashlib.sha256(b'token').hexdigest(),
        cors_origins=('https://vg.luwei.host',),
    )

    assert 'search_path%3Dpublic' in str(settings.database_target)
    assert 'search_path%3Dcore' in str(settings.source_database_target)
    assert 'connect_timeout=5' in str(settings.source_database_target)


def test_source_database_url_can_use_a_dedicated_read_only_role(tmp_path: Path) -> None:
    settings = ApiSettings(
        database_path=tmp_path / 'dashboard.sqlite3',
        database_url='postgresql://public@127.0.0.1/dashboard',
        source_database_url=(
            'postgresql://reader@127.0.0.1/dashboard?'
            'connect_timeout=5&options=-csearch_path%3Dpublic'
        ),
        ingest_token_sha256=hashlib.sha256(b'token').hexdigest(),
        cors_origins=('https://vg.luwei.host',),
    )

    assert 'reader@' in str(settings.source_database_target)
    assert 'search_path%3Dcore' in str(settings.source_database_target)
    assert 'search_path%3Dpublic' not in str(settings.source_database_target)
    assert 'connect_timeout=5' in str(settings.source_database_target)


def test_database_url_rejects_non_postgresql_servers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='PostgreSQL'):
        ApiSettings(
            database_path=tmp_path / 'dashboard.sqlite3',
            database_url='mysql://dashboard:secret@127.0.0.1/dashboard',
            ingest_token_sha256=hashlib.sha256(b'token').hexdigest(),
            cors_origins=('https://vg.luwei.host',),
        )


def test_repository_mode_rejects_unknown_cache_backends(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='repository mode'):
        ApiSettings(
            database_path=tmp_path / 'dashboard.sqlite3',
            ingest_token_sha256=hashlib.sha256(b'token').hexdigest(),
            cors_origins=('https://vg.luwei.host',),
            repository_mode='redis',
        )


def test_asset_batch_rejects_overlapping_updates_and_removals() -> None:
    with pytest.raises(ValueError, match='update and remove'):
        AssetBatch.parse_obj(
            {
                'schemaVersion': 1,
                'generatedAt': '2026-08-11T00:00:00Z',
                'images': [
                    {
                        'matchId': 1,
                        'url': 'https://example.com/1.webp',
                        'width': 1600,
                        'height': 900,
                        'sha256': 'a' * 64,
                    }
                ],
                'removedMatchIds': [1],
            }
        )


def test_realtime_broker_coalesces_a_slow_subscriber_to_resync() -> None:
    async def exercise() -> None:
        broker = DashboardRealtimeBroker(queue_size=1)
        subscription = broker.subscribe()

        await broker.publish('dashboard', {'revision': 'first'})
        await broker.publish('dashboard', {'revision': 'second'})

        event = await asyncio.wait_for(subscription.get(), timeout=1)
        assert event.type == 'resync'
        assert event.data == {}

    asyncio.run(exercise())


def test_realtime_event_encoding_is_valid_sse() -> None:
    assert encode_event('dashboard', {'revision': 'abc'}) == (
        b'event: dashboard\ndata: {"revision":"abc"}\n\n'
    )


def test_realtime_endpoint_has_proxy_safe_streaming_headers() -> None:
    class Request:
        async def is_disconnected(self) -> bool:
            return True

    response = event_response(  # type: ignore[arg-type]
        Request(), DashboardRealtimeBroker()
    )

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    assert response.headers['cache-control'] == 'no-cache'
    assert response.headers['x-accel-buffering'] == 'no'
