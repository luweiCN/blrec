import sqlite3
from pathlib import Path

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
        )
    )

    backups = tuple((tmp_path / 'backups').glob('*.sqlite3'))
    assert len(backups) == 1
    assert backups[0].stat().st_size > 0
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute('PRAGMA quick_check').fetchone() == ('ok',)
        assert connection.execute('SELECT value FROM sample').fetchone() == ('saved',)


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
