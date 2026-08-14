from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from blrec_dashboard_publisher.publisher import (
    DashboardPublishError,
    _environment_int,
    _parse_args,
    _read_source_revision,
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
