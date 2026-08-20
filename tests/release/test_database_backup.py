import sqlite3
from pathlib import Path

import pytest

from scripts.backup_blrec_database import _postgres_dump_command, main


def test_sqlite_backup_is_nonempty_and_valid(tmp_path: Path, capsys: object) -> None:
    database = tmp_path / 'blrec.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY,value TEXT)')
        connection.execute("INSERT INTO sample(value) VALUES('saved')")

    main(
        (
            '--database',
            str(database),
            '--backup-dir',
            str(tmp_path / 'backups'),
            '--label',
            'test',
            '--database-url-env',
            'BLREC_TEST_UNUSED_DATABASE_URL',
            '--recording-journal',
            str(tmp_path / 'missing-recording-journal.sqlite3'),
        )
    )

    backups = tuple((tmp_path / 'backups').glob('*.sqlite3'))
    assert len(backups) == 1
    assert backups[0].stat().st_size > 0
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute('PRAGMA quick_check').fetchone() == ('ok',)
        assert connection.execute('SELECT value FROM sample').fetchone() == ('saved',)


def test_backup_includes_local_recording_journal_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / 'blrec.sqlite3'
    recording_journal = tmp_path / 'recording-journal.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY)')
    with sqlite3.connect(recording_journal) as connection:
        connection.execute(
            'CREATE TABLE recording_outbox_events('
            'sequence INTEGER PRIMARY KEY,event_id TEXT NOT NULL)'
        )
        connection.execute(
            "INSERT INTO recording_outbox_events VALUES(1,'event-pending')"
        )
    monkeypatch.setenv('BLREC_RECORDING_JOURNAL_DATABASE', str(recording_journal))

    main(
        (
            '--database',
            str(database),
            '--backup-dir',
            str(tmp_path / 'backups'),
            '--label',
            'test',
            '--database-url-env',
            'BLREC_TEST_UNUSED_DATABASE_URL',
        )
    )

    backups = tuple((tmp_path / 'backups').glob('*.sqlite3'))
    assert len(backups) == 2
    outbox_backup = next(
        backup for backup in backups if 'recording-journal' in backup.name
    )
    with sqlite3.connect(outbox_backup) as connection:
        assert connection.execute('PRAGMA quick_check').fetchone() == ('ok',)
        assert connection.execute(
            'SELECT event_id FROM recording_outbox_events'
        ).fetchone() == ('event-pending',)


def test_postgres_dump_command_does_not_expose_password(tmp_path: Path) -> None:
    command = _postgres_dump_command(
        {
            'host': '127.0.0.1',
            'port': '15432',
            'user': 'blrec_core',
            'password': 'must-not-appear',
            'dbname': 'blrec_dashboard',
        },
        tmp_path / 'backup.dump',
        'core',
    )

    assert all('must-not-appear' not in value for value in command)
    assert '--schema=core' in command
    assert '--dbname=blrec_dashboard' in command
