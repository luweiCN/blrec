from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from blrec_dashboard_publisher import publisher as publisher_module
from blrec_dashboard_publisher.publisher import (
    DashboardPublishError,
    _environment_int,
    _parse_args,
    _publish,
    _read_source_revision,
    _WorkerConfiguration,
)


def test_worker_reads_the_persistent_source_revision(tmp_path: Path) -> None:
    database = tmp_path / 'source.sqlite3'
    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            'CREATE TABLE dashboard_source_state('
            'singleton_id INTEGER PRIMARY KEY,revision INTEGER NOT NULL)'
        )
        connection.execute(
            'INSERT INTO dashboard_source_state(singleton_id,revision) VALUES(1,42)'
        )
        connection.commit()
    finally:
        connection.close()

    assert _read_source_revision(database) == 42


def test_worker_rejects_an_invalid_source_revision(tmp_path: Path) -> None:
    database = tmp_path / 'source.sqlite3'
    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            'CREATE TABLE dashboard_source_state('
            'singleton_id INTEGER PRIMARY KEY,revision INTEGER NOT NULL)'
        )
        connection.execute(
            'INSERT INTO dashboard_source_state(singleton_id,revision) VALUES(1,0)'
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DashboardPublishError, match='变更标记无效'):
        _read_source_revision(database)


def test_asset_worker_cli_has_no_static_snapshot_switches() -> None:
    arguments = _parse_args(['--once', '--api-url', 'https://example.com'])

    assert arguments.once is True
    assert not hasattr(arguments, 'force')
    assert not hasattr(arguments, 'publish_static_data')


def test_environment_integer_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('DASHBOARD_TEST_SECONDS', 'invalid')

    with pytest.raises(DashboardPublishError, match='必须是整数'):
        _environment_int('DASHBOARD_TEST_SECONDS', 10)


def test_cache_sync_runs_before_oss_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = []

    class ApiClient:
        selection = SimpleNamespace(interface_name=None, source_address=None)
        post_cache_batch = object()

        def __init__(self, **_kwargs: object) -> None:
            pass

        def close(self) -> None:
            events.append('api-close')

    def cache_sync(**_kwargs: object) -> SimpleNamespace:
        events.append('cache')
        return SimpleNamespace(
            synced=True,
            batch_count=1,
            match_count=1,
            removed_match_count=0,
            source_revision=7,
        )

    def unavailable_oss(**_kwargs: object) -> None:
        events.append('oss')
        raise DashboardPublishError('OSS unavailable')

    monkeypatch.setenv('DASHBOARD_API_TOKEN', 'token')
    monkeypatch.setenv('ALIBABA_CLOUD_ACCESS_KEY_ID', 'test-key')
    monkeypatch.setenv('ALIBABA_CLOUD_ACCESS_KEY_SECRET', 'test-secret')
    monkeypatch.setattr(publisher_module, 'load_network_settings', lambda _path: None)
    monkeypatch.setattr(
        publisher_module, 'NetworkRouteManager', lambda _loader: object()
    )
    monkeypatch.setattr(publisher_module, 'DashboardApiClient', ApiClient)
    monkeypatch.setattr(publisher_module, 'sync_dashboard_cache_once', cache_sync)
    monkeypatch.setattr(publisher_module, 'OssDashboardStore', unavailable_oss)
    configuration = _WorkerConfiguration(
        database=tmp_path / 'source.sqlite3',
        settings=tmp_path / 'settings.toml',
        state=tmp_path / 'state',
        endpoint='https://oss.example.com',
        bucket='test',
        prefix='data',
        watch_seconds=1,
        debounce_seconds=2,
        reconcile_seconds=60,
        retry_seconds=1,
        api_url='https://api.example.com',
        result_frames=tmp_path / 'frames',
        public_data_base_url='https://data.example.com',
    )

    with pytest.raises(DashboardPublishError, match='OSS unavailable'):
        _publish(configuration)

    assert events == ['cache', 'oss', 'api-close']
