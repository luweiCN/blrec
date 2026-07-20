import asyncio
import hashlib
import os
import stat
import struct
import threading
import time
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from blrec.bili_upload.covers import (
    CoverLibrary,
    CoverResolutionError,
    CoverResolver,
    CoverWorkCoordinator,
    CoverWorkSaturated,
    InvalidCover,
    StoredCoverUnavailable,
)
from blrec.bili_upload.database import BiliUploadDatabase


def png(width: int = 1600, height: int = 1000) -> bytes:
    return (
        b'\x89PNG\r\n\x1a\n'
        + struct.pack('>I', 13)
        + b'IHDR'
        + struct.pack('>II', width, height)
        + b'\x08\x02\x00\x00\x00'
        + b'\x00\x00\x00\x00'
    )


def jpeg(width: int = 1600, height: int = 1000) -> bytes:
    return (
        b'\xff\xd8'
        + b'\xff\xe0\x00\x04xx'
        + b'\xff\xc0\x00\x0b\x08'
        + struct.pack('>HH', height, width)
        + b'\x01\x01\x11\x00'
        + b'\xff\xd9'
    )


async def seed_accounts(database: BiliUploadDatabase) -> None:
    for account_id in (1, 2):
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) VALUES(?,?,?,X\'00\',1,\'k\',\'active\',1,1)',
            (account_id, 40 + account_id, '账号{}'.format(account_id)),
        )


async def wait_for_digest_consumers(
    library: CoverLibrary, digest: str, count: int
) -> None:
    while True:
        work = library._digest_work.get(digest)
        if work is not None and work.consumers == count:
            return
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cover_worker_bounds_active_and_waiting_work_before_executor() -> None:
    coordinator = CoverWorkCoordinator(max_workers=2, max_waiting=8)
    loop = asyncio.get_running_loop()
    entered: asyncio.Queue[int] = asyncio.Queue()
    release = threading.Event()
    heartbeat = asyncio.Event()

    def blocking_work(index: int) -> int:
        loop.call_soon_threadsafe(entered.put_nowait, index)
        assert release.wait(5)
        return index

    async def run(index: int) -> int:
        return await coordinator.run(
            lambda: coordinator.offload(partial(blocking_work, index))
        )

    admitted = [asyncio.create_task(run(index)) for index in range(10)]
    await asyncio.wait_for(entered.get(), timeout=5)
    await asyncio.wait_for(entered.get(), timeout=5)
    loop.call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=5)

    assert coordinator.active_count == 2
    assert coordinator.waiting_count == 8
    assert coordinator.admitted_count == 10
    with pytest.raises(CoverWorkSaturated) as saturated:
        await run(10)
    assert saturated.value.retry_after == 1
    assert entered.empty()

    release.set()
    assert await asyncio.gather(*admitted) == list(range(10))
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_cover_worker_shutdown_drains_every_admitted_job() -> None:
    coordinator = CoverWorkCoordinator(max_workers=2, max_waiting=8)
    loop = asyncio.get_running_loop()
    entered: asyncio.Queue[int] = asyncio.Queue()
    release = threading.Event()

    def blocking_work(index: int) -> int:
        loop.call_soon_threadsafe(entered.put_nowait, index)
        assert release.wait(5)
        return index

    async def run(index: int) -> int:
        return await coordinator.run(
            lambda: coordinator.offload(partial(blocking_work, index))
        )

    admitted = [asyncio.create_task(run(index)) for index in range(10)]
    await asyncio.wait_for(entered.get(), timeout=5)
    await asyncio.wait_for(entered.get(), timeout=5)
    coordinator.close_admission()
    shutdown = asyncio.create_task(coordinator.shutdown())

    with pytest.raises(RuntimeError, match='closed'):
        await run(10)
    assert not shutdown.done()

    release.set()
    assert await asyncio.gather(*admitted) == list(range(10))
    await shutdown
    assert coordinator.admitted_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('content', (png(), jpeg()))
async def test_cover_library_scans_and_hashes_without_blocking_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    heartbeat = asyncio.Event()
    worker_thread_ids = []
    original = CoverLibrary._inspect_content

    def blocking_inspection(content: bytes) -> Any:
        worker_thread_ids.append(threading.get_ident())
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return original(content)

    monkeypatch.setattr(
        CoverLibrary, '_inspect_content', staticmethod(blocking_inspection)
    )
    task = asyncio.create_task(library.add(content, 'cover'))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        loop.call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), timeout=5)
        assert worker_thread_ids == [worker_thread_ids[0]]
        assert worker_thread_ids[0] != threading.get_ident()
    finally:
        release.set()
    await task
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_duplicate_cover_serializes_file_and_database_commit_by_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')
    loop = asyncio.get_running_loop()
    store_started = asyncio.Event()
    second_inspected = asyncio.Event()
    release_store = threading.Event()
    call_lock = threading.Lock()
    inspection_calls = 0
    store_calls = 0
    insert_calls = 0
    original_inspect = CoverLibrary._inspect_content
    original_store = CoverLibrary._store_file
    original_execute = database.execute

    def observed_inspection(content: bytes) -> Any:
        nonlocal inspection_calls
        with call_lock:
            inspection_calls += 1
            current = inspection_calls
        result = original_inspect(content)
        if current == 2:
            loop.call_soon_threadsafe(second_inspected.set)
        return result

    def blocking_store(
        cover_library: CoverLibrary, path: Path, content: bytes, digest: str
    ) -> bool:
        nonlocal store_calls
        with call_lock:
            store_calls += 1
        loop.call_soon_threadsafe(store_started.set)
        assert release_store.wait(5)
        return original_store(cover_library, path, content, digest)

    async def counted_execute(sql: str, parameters: Any = ()) -> int:
        nonlocal insert_calls
        if sql.startswith('INSERT INTO cover_assets'):
            insert_calls += 1
        return await original_execute(sql, parameters)

    monkeypatch.setattr(
        CoverLibrary, '_inspect_content', staticmethod(observed_inspection)
    )
    monkeypatch.setattr(CoverLibrary, '_store_file', blocking_store)
    monkeypatch.setattr(database, 'execute', counted_execute)
    first = asyncio.create_task(library.add(png(), 'first.png'))
    await asyncio.wait_for(store_started.wait(), timeout=5)
    second = asyncio.create_task(library.add(png(), 'second.png'))
    await asyncio.wait_for(second_inspected.wait(), timeout=5)
    digest = hashlib.sha256(png()).hexdigest()
    await asyncio.wait_for(wait_for_digest_consumers(library, digest, 2), timeout=5)
    release_store.set()

    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == second_result
    assert store_calls == 1
    assert insert_calls == 1
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_database_failure_keeps_file_for_waiting_digest_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')
    loop = asyncio.get_running_loop()
    first_insert_started = asyncio.Event()
    second_inspected = asyncio.Event()
    release_first_insert = asyncio.Event()
    call_lock = threading.Lock()
    inspection_calls = 0
    insert_calls = 0
    original_inspect = CoverLibrary._inspect_content
    original_execute = database.execute

    def observed_inspection(content: bytes) -> Any:
        nonlocal inspection_calls
        with call_lock:
            inspection_calls += 1
            current = inspection_calls
        result = original_inspect(content)
        if current == 2:
            loop.call_soon_threadsafe(second_inspected.set)
        return result

    async def fail_first_insert(sql: str, parameters: Any = ()) -> int:
        nonlocal insert_calls
        if sql.startswith('INSERT INTO cover_assets'):
            insert_calls += 1
            if insert_calls == 1:
                first_insert_started.set()
                await release_first_insert.wait()
                raise RuntimeError('database insert failed')
        return await original_execute(sql, parameters)

    monkeypatch.setattr(
        CoverLibrary, '_inspect_content', staticmethod(observed_inspection)
    )
    monkeypatch.setattr(database, 'execute', fail_first_insert)
    first = asyncio.create_task(library.add(png(), 'first.png'))
    await asyncio.wait_for(first_insert_started.wait(), timeout=5)
    second = asyncio.create_task(library.add(png(), 'second.png'))
    await asyncio.wait_for(second_inspected.wait(), timeout=5)
    digest = hashlib.sha256(png()).hexdigest()
    await asyncio.wait_for(wait_for_digest_consumers(library, digest, 2), timeout=5)
    release_first_insert.set()

    with pytest.raises(RuntimeError, match='database insert failed'):
        await first
    stored = await second
    expected_path = (
        tmp_path / 'covers' / '{}.png'.format(hashlib.sha256(png()).hexdigest())
    )
    assert stored.id == 1
    assert expected_path.read_bytes() == png()
    assert insert_calls == 2
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_cleanup_rechecks_waiting_consumer_after_database_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')
    cleanup_query_started = asyncio.Event()
    release_cleanup_query = asyncio.Event()
    insert_calls = 0
    store_results = []
    original_execute = database.execute
    original_scalar = database.scalar
    original_store = CoverLibrary._store_file

    async def fail_first_insert(sql: str, parameters: Any = ()) -> int:
        nonlocal insert_calls
        if sql.startswith('INSERT INTO cover_assets'):
            insert_calls += 1
            if insert_calls == 1:
                raise RuntimeError('database insert failed')
        return await original_execute(sql, parameters)

    async def block_cleanup_query(sql: str, parameters: Any = ()) -> Any:
        if sql.startswith('SELECT 1 FROM cover_assets'):
            cleanup_query_started.set()
            await release_cleanup_query.wait()
            return None
        return await original_scalar(sql, parameters)

    def observed_store(
        cover_library: CoverLibrary, path: Path, content: bytes, digest: str
    ) -> bool:
        created = original_store(cover_library, path, content, digest)
        store_results.append(created)
        return created

    monkeypatch.setattr(database, 'execute', fail_first_insert)
    monkeypatch.setattr(database, 'scalar', block_cleanup_query)
    monkeypatch.setattr(CoverLibrary, '_store_file', observed_store)
    first = asyncio.create_task(library.add(png(), 'first.png'))
    await asyncio.wait_for(cleanup_query_started.wait(), timeout=5)
    second = asyncio.create_task(library.add(png(), 'second.png'))
    digest = hashlib.sha256(png()).hexdigest()
    await asyncio.wait_for(wait_for_digest_consumers(library, digest, 2), timeout=5)
    release_cleanup_query.set()

    with pytest.raises(RuntimeError, match='database insert failed'):
        await first
    stored = await second

    assert stored.id == 1
    assert store_results == [True, False]
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_slow_cleanup_does_not_block_loop_or_rewrite_for_waiting_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')
    loop = asyncio.get_running_loop()
    digest = hashlib.sha256(png()).hexdigest()
    final_path = tmp_path / 'covers' / '{}.png'.format(digest)
    unlink_started = asyncio.Event()
    release_unlink = threading.Event()
    second_inspection_started = asyncio.Event()
    release_second_inspection = threading.Event()
    heartbeat = asyncio.Event()
    insert_calls = 0
    inspection_calls = 0
    store_results = []
    original_execute = database.execute
    original_inspect = CoverLibrary._inspect_content
    original_store = CoverLibrary._store_file
    original_unlink = Path.unlink

    async def fail_first_insert(sql: str, parameters: Any = ()) -> int:
        nonlocal insert_calls
        if sql.startswith('INSERT INTO cover_assets'):
            insert_calls += 1
            if insert_calls == 1:
                raise RuntimeError('database insert failed')
        return await original_execute(sql, parameters)

    def observed_inspection(content: bytes) -> Any:
        nonlocal inspection_calls
        inspection_calls += 1
        if inspection_calls == 2:
            loop.call_soon_threadsafe(second_inspection_started.set)
            assert release_second_inspection.wait(5)
        return original_inspect(content)

    def observed_store(
        cover_library: CoverLibrary, path: Path, content: bytes, digest: str
    ) -> bool:
        created = original_store(cover_library, path, content, digest)
        store_results.append(created)
        return created

    def slow_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == final_path and not unlink_started.is_set():
            loop.call_soon_threadsafe(unlink_started.set)
            assert release_unlink.wait(5)
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(database, 'execute', fail_first_insert)
    monkeypatch.setattr(
        CoverLibrary, '_inspect_content', staticmethod(observed_inspection)
    )
    monkeypatch.setattr(CoverLibrary, '_store_file', observed_store)
    monkeypatch.setattr(Path, 'unlink', slow_unlink)
    first = asyncio.create_task(library.add(png(), 'first.png'))
    await asyncio.wait_for(unlink_started.wait(), timeout=5)
    release_timer = threading.Timer(0.25, release_unlink.set)
    release_timer.start()
    second = asyncio.create_task(library.add(png(), 'second.png'))
    await asyncio.wait_for(second_inspection_started.wait(), timeout=5)
    started = time.monotonic()
    loop.call_later(0.01, heartbeat.set)
    release_second_inspection.set()
    try:
        await asyncio.wait_for(heartbeat.wait(), timeout=1)
    finally:
        release_unlink.set()
        release_timer.cancel()

    assert time.monotonic() - started < 0.1
    with pytest.raises(RuntimeError, match='database insert failed'):
        await first
    stored = await second
    assert stored.id == 1
    assert store_results == [True, False]
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_database_error_after_commit_does_not_delete_referenced_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')
    original_execute = database.execute

    async def commit_then_fail(sql: str, parameters: Any = ()) -> int:
        result = await original_execute(sql, parameters)
        if sql.startswith('INSERT INTO cover_assets'):
            raise RuntimeError('response lost after commit')
        return result

    monkeypatch.setattr(database, 'execute', commit_then_fail)
    with pytest.raises(RuntimeError, match='response lost'):
        await library.add(png(), 'cover.png')

    digest = hashlib.sha256(png()).hexdigest()
    expected_path = tmp_path / 'covers' / '{}.png'.format(digest)
    assert expected_path.read_bytes() == png()
    assert (
        await database.scalar(
            'SELECT COUNT(*) FROM cover_assets WHERE sha256=?', (digest,)
        )
        == 1
    )
    monkeypatch.setattr(database, 'execute', original_execute)
    recovered = await library.add(png(), 'cover.png')
    assert recovered.id == 1
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_missing_database_commit_cleans_new_cover_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')
    original_execute = database.execute

    async def discard_insert(sql: str, parameters: Any = ()) -> int:
        if sql.startswith('INSERT INTO cover_assets'):
            return 1
        return await original_execute(sql, parameters)

    monkeypatch.setattr(database, 'execute', discard_insert)
    with pytest.raises(StoredCoverUnavailable, match='metadata'):
        await library.add(png(), 'cover.png')

    digest = hashlib.sha256(png()).hexdigest()
    expected_path = tmp_path / 'covers' / '{}.png'.format(digest)
    assert not expected_path.exists()
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_database_failure_cleans_new_cover_file_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')
    cleanup_threads = []
    original_execute = database.execute
    original_cleanup = CoverLibrary._cleanup_file

    async def fail_insert(sql: str, parameters: Any = ()) -> int:
        if sql.startswith('INSERT INTO cover_assets'):
            raise RuntimeError('database insert failed')
        return await original_execute(sql, parameters)

    def observed_cleanup(path: Path, digest: str) -> None:
        cleanup_threads.append(threading.get_ident())
        original_cleanup(path, digest)

    monkeypatch.setattr(database, 'execute', fail_insert)
    monkeypatch.setattr(CoverLibrary, '_cleanup_file', staticmethod(observed_cleanup))
    with pytest.raises(RuntimeError, match='database insert failed'):
        await library.add(png(), 'cover.png')

    expected_path = (
        tmp_path / 'covers' / '{}.png'.format(hashlib.sha256(png()).hexdigest())
    )
    assert not expected_path.exists()
    assert cleanup_threads and cleanup_threads[0] != threading.get_ident()
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_cover_library_recovers_complete_orphan_metadata(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')
    digest = hashlib.sha256(png()).hexdigest()
    orphan = tmp_path / 'covers' / '{}.png'.format(digest)
    orphan.parent.mkdir()
    orphan.write_bytes(png())

    asset = await library.add(png(), 'recovered.png')

    assert asset.filename == 'recovered.png'
    assert await database.scalar('SELECT COUNT(*) FROM cover_assets') == 1
    assert orphan.read_bytes() == png()
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_cover_store_does_not_publish_final_path_before_file_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')
    loop = asyncio.get_running_loop()
    file_fsync_started = asyncio.Event()
    release_file_fsync = threading.Event()
    original_fsync = os.fsync
    blocked = False

    def blocking_fsync(descriptor: int) -> None:
        nonlocal blocked
        if stat.S_ISREG(os.fstat(descriptor).st_mode) and not blocked:
            blocked = True
            loop.call_soon_threadsafe(file_fsync_started.set)
            assert release_file_fsync.wait(5)
        original_fsync(descriptor)

    monkeypatch.setattr(os, 'fsync', blocking_fsync)
    addition = asyncio.create_task(library.add(png(), 'cover.png'))
    digest = hashlib.sha256(png()).hexdigest()
    final_path = tmp_path / 'covers' / '{}.png'.format(digest)
    try:
        await asyncio.wait_for(file_fsync_started.wait(), timeout=5)
        assert not final_path.exists()
    finally:
        release_file_fsync.set()

    await addition
    assert final_path.read_bytes() == png()
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_cover_store_cleans_temporary_file_when_atomic_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')

    def fail_publish(_source: Any, _destination: Any) -> None:
        raise OSError('atomic publish interrupted')

    monkeypatch.setattr(os, 'link', fail_publish)
    with pytest.raises(OSError, match='atomic publish interrupted'):
        await library.add(png(), 'cover.png')

    root = tmp_path / 'covers'
    assert list(root.iterdir()) == []
    assert await database.scalar('SELECT COUNT(*) FROM cover_assets') == 0
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_cover_library_never_overwrites_corrupted_orphan(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')
    digest = hashlib.sha256(png()).hexdigest()
    orphan = tmp_path / 'covers' / '{}.png'.format(digest)
    orphan.parent.mkdir()
    orphan.write_bytes(b'corrupt')

    with pytest.raises(InvalidCover, match='hash'):
        await library.add(png(), 'cover.png')

    assert orphan.read_bytes() == b'corrupt'
    assert await database.scalar('SELECT COUNT(*) FROM cover_assets') == 0
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_cover_library_rehashes_stored_file_and_never_overwrites_mismatch(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    library = CoverLibrary(database, tmp_path / 'covers')
    asset = await library.add(png(), 'cover.png')
    opened = await library.open(asset.id)
    opened.path.write_bytes(b'corrupt')

    with pytest.raises(InvalidCover, match='hash'):
        await library.add(png(), 'cover.png')

    assert opened.path.read_bytes() == b'corrupt'
    await library.shutdown()
    await database.close()


@pytest.mark.asyncio
async def test_cover_library_validates_deduplicates_and_opens_images(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        library = CoverLibrary(database, tmp_path / 'covers', clock=lambda: 1000)

        first = await library.add(png(), '../直播封面.png')
        duplicate = await library.add(png(), '重复文件名.png')
        second = await library.add(jpeg(), '另一张.jpg')
        opened = await library.open(first.id)

        assert duplicate == first
        assert first.filename == '直播封面.png'
        assert first.mime_type == 'image/png'
        assert (first.width, first.height) == (1600, 1000)
        assert second.mime_type == 'image/jpeg'
        assert [asset.id for asset in await library.list()] == [second.id, first.id]
        assert opened.path.read_bytes() == png()
        assert opened.view == first
        assert oct(opened.path.stat().st_mode & 0o777) == '0o600'
        assert oct((tmp_path / 'covers').stat().st_mode & 0o777) == '0o700'
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('content', 'message'),
    (
        (b'not-an-image', 'JPEG or PNG'),
        (png(100, 100), '1146'),
        (png() + b'x' * (2 * 1024 * 1024), '2 MiB'),
    ),
)
async def test_cover_library_rejects_invalid_images(
    tmp_path: Path, content: bytes, message: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        library = CoverLibrary(database, tmp_path / 'covers')
        with pytest.raises(InvalidCover, match=message):
            await library.add(content, 'cover.png')
        assert await database.scalar('SELECT COUNT(*) FROM cover_assets') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cover_library_refuses_a_database_path_outside_its_root(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        library = CoverLibrary(database, tmp_path / 'covers')
        asset = await library.add(png(), 'cover.png')
        await database.execute(
            'UPDATE cover_assets SET storage_path=? WHERE id=?',
            (str(tmp_path / 'outside.png'), asset.id),
        )

        with pytest.raises(StoredCoverUnavailable, match='outside'):
            await library.open(asset.id)
        with pytest.raises(StoredCoverUnavailable, match='outside'):
            await library.add(png(), 'cover.png')
    finally:
        await database.close()


class FakeProtocol:
    def __init__(self) -> None:
        self.calls = []
        self.fail = False

    async def upload_cover(
        self, bundle: Any, *, filename: str, mime_type: str, content: bytes
    ) -> str:
        self.calls.append((bundle, filename, mime_type, content))
        if self.fail:
            raise RuntimeError('upload failed')
        return 'https://archive.biliimg.com/{}/{}.jpg'.format(bundle, len(self.calls))


@pytest.mark.asyncio
async def test_cover_resolver_caches_remote_url_per_account(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        library = CoverLibrary(database, tmp_path / 'covers')
        asset = await library.add(png(), 'cover.png')
        protocol = FakeProtocol()

        async def load_bundle(account_id: int) -> str:
            return 'bundle-{}'.format(account_id)

        resolver = CoverResolver(
            database, library, protocol, bundle_loader=load_bundle, clock=lambda: 1000
        )

        first = await resolver.remote_url(asset.id, 1)
        cached = await resolver.remote_url(asset.id, 1)
        other_account = await resolver.remote_url(asset.id, 2)

        assert cached == first
        assert other_account != first
        assert len(protocol.calls) == 2
        assert await database.scalar('SELECT COUNT(*) FROM cover_asset_uploads') == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cover_resolver_does_not_cache_failed_upload(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        library = CoverLibrary(database, tmp_path / 'covers')
        asset = await library.add(jpeg(), 'cover.jpg')
        protocol = FakeProtocol()
        protocol.fail = True

        async def load_bundle(account_id: int) -> str:
            return 'bundle-{}'.format(account_id)

        resolver = CoverResolver(database, library, protocol, bundle_loader=load_bundle)

        with pytest.raises(RuntimeError, match='upload failed'):
            await resolver.remote_url(asset.id, 1)
        assert await database.scalar('SELECT COUNT(*) FROM cover_asset_uploads') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_live_cover_prefers_the_recorded_local_image(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        local_cover = tmp_path / 'recorded.png'
        local_cover.write_bytes(png())
        protocol = FakeProtocol()
        remote_calls = []

        async def load_remote(url: str) -> bytes:
            remote_calls.append(url)
            return jpeg()

        resolver = CoverResolver(
            database,
            CoverLibrary(database, tmp_path / 'covers'),
            protocol,
            bundle_loader=lambda account_id: async_value(
                'bundle-{}'.format(account_id)
            ),
            remote_loader=load_remote,
        )

        result = await resolver.live_url(
            1, local_path=str(local_cover), source_url='https://i0.hdslb.com/live.jpg'
        )

        assert result.startswith('https://archive.biliimg.com/')
        assert remote_calls == []
        assert protocol.calls == [('bundle-1', 'recorded.png', 'image/png', png())]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_live_cover_downloads_a_trusted_source_when_local_file_is_missing(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        protocol = FakeProtocol()
        remote_calls = []

        async def load_remote(url: str) -> bytes:
            remote_calls.append(url)
            return jpeg()

        resolver = CoverResolver(
            database,
            CoverLibrary(database, tmp_path / 'covers'),
            protocol,
            bundle_loader=lambda account_id: async_value(
                'bundle-{}'.format(account_id)
            ),
            remote_loader=load_remote,
        )

        await resolver.live_url(
            1,
            local_path=str(tmp_path / 'missing.jpg'),
            source_url='https://i0.hdslb.com/live.jpg',
        )

        assert remote_calls == ['https://i0.hdslb.com/live.jpg']
        assert protocol.calls == [('bundle-1', 'live-cover.jpg', 'image/jpeg', jpeg())]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_live_cover_rejects_an_untrusted_remote_source(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        remote_calls = []

        async def load_remote(url: str) -> bytes:
            remote_calls.append(url)
            return jpeg()

        resolver = CoverResolver(
            database,
            CoverLibrary(database, tmp_path / 'covers'),
            FakeProtocol(),
            bundle_loader=lambda account_id: async_value(account_id),
            remote_loader=load_remote,
        )

        with pytest.raises(CoverResolutionError, match='trusted'):
            await resolver.live_url(
                1, local_path=None, source_url='https://example.com/cover.jpg'
            )
        assert remote_calls == []
    finally:
        await database.close()


async def async_value(value: Any) -> Any:
    return value
