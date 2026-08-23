import asyncio
import hashlib
import tempfile
from pathlib import Path

import pytest
from labeler import database_backup, database_migrate


async def _chunks(*values: bytes):
    for value in values:
        yield value


def test_store_backup_stream_is_atomic_and_prunes_old_backups() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        for index in range(2):
            name = f'vision-lab-2026082{index + 1}T010203Z-{index:012x}.dump'
            path = directory / name
            path.write_bytes(b'old')
            path.touch()
        content = b'complete-postgres-backup'
        digest = hashlib.sha256(content).hexdigest()
        name = f'vision-lab-20260823T010203Z-{digest[:12]}.dump'

        result = asyncio.run(
            database_backup.store_backup_stream(
                _chunks(content[:5], content[5:]),
                directory=directory,
                filename=name,
                expected_length=len(content),
                maximum_bytes=1_000_000,
                keep=2,
            )
        )

        assert result == {'name': name, 'size_bytes': len(content), 'sha256': digest}
        assert (directory / name).read_bytes() == content
        assert len(database_backup.list_backups(directory)) == 2


def test_incomplete_backup_is_removed() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        name = 'vision-lab-20260823T010203Z-0123456789ab.dump'

        with pytest.raises(ValueError, match='未完整上传'):
            asyncio.run(
                database_backup.store_backup_stream(
                    _chunks(b'short'),
                    directory=directory,
                    filename=name,
                    expected_length=10,
                    maximum_bytes=1_000_000,
                    keep=2,
                )
            )

        assert not (directory / name).exists()
        assert not tuple(directory.glob('*.tmp'))


def test_backup_name_rejects_paths_and_arbitrary_files() -> None:
    for value in ('../escape.dump', 'vision.dump', 'vision-lab-1.dump'):
        with pytest.raises(ValueError):
            database_backup.validate_backup_name(value)


def test_postgres_migration_target_must_be_local() -> None:
    for value in (
        'postgresql://vision:secret@localhost/blrec_vision',
        'postgresql://vision:secret@127.0.0.1:5432/blrec_vision',
        'postgresql:///blrec_vision',
    ):
        database_migrate._require_local_target(value)

    with pytest.raises(ValueError, match='必须位于本机'):
        database_migrate._require_local_target(
            'postgresql://vision:secret@database.example/blrec_vision'
        )
