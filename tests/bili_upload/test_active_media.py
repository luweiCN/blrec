import asyncio
import builtins
import threading
from io import BytesIO
from pathlib import Path
from typing import Any, Dict

import pytest

import blrec.bili_upload.active_media as active_media
from blrec.bili_upload.active_media import ActiveMediaBusy, ActiveMediaService
from blrec.bili_upload.recording_content import FlvMediaSnapshot
from blrec.flv.common import create_metadata_tag
from blrec.flv.io import FlvWriter
from blrec.flv.models import FlvHeader


def _metadata(revision: int) -> Dict[str, Any]:
    return {
        'duration': float(revision),
        'filesize': float(revision),
        'lastkeyframelocation': float(revision),
        'lastkeyframetimestamp': float(revision),
        'keyframes': {
            'times': [0.0, float(revision)],
            'filepositions': [0.0, float(revision)],
        },
    }


async def _wait_until(predicate) -> None:
    for _attempt in range(1_000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError('condition was not reached')


@pytest.mark.asyncio
async def test_same_active_revision_is_built_once_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'active.flv'
    source.write_bytes(b'FLV')
    started = threading.Event()
    release = threading.Event()
    calls = []
    realpath_threads = []

    def resolve(path: str) -> str:
        realpath_threads.append(threading.current_thread().name)
        return path

    def build(path: str, source_size: int, metadata: Dict[str, Any]):
        calls.append((threading.current_thread().name, path, source_size, metadata))
        started.set()
        assert release.wait(5)
        return FlvMediaSnapshot.frozen(path, source_size)

    monkeypatch.setattr(FlvMediaSnapshot, 'create', build)
    monkeypatch.setattr(active_media.os.path, 'realpath', resolve)
    service = ActiveMediaService()
    requests = [
        asyncio.create_task(
            service.snapshot(1, str(source), source.stat().st_size, _metadata(1))
        )
        for _index in range(8)
    ]
    try:
        await _wait_until(started.is_set)
        assert service.admitted_count == 1
        assert service.in_flight_source_count == 1
        heartbeats = 0
        for _index in range(5):
            await asyncio.sleep(0)
            heartbeats += 1
        assert heartbeats == 5
        release.set()
        snapshots = await asyncio.gather(*requests)
        assert len(calls) == 1
        assert calls[0][0].startswith('blrec-active-media')
        assert realpath_threads == [calls[0][0]]
        assert all(snapshot is snapshots[0] for snapshot in snapshots)
        await _wait_until(lambda: service.admitted_count == 0)
        assert service.in_flight_source_count == 0
        assert service.completed_cache_entries == 0
        assert service.completed_cache_prefix_bytes == 0
    finally:
        release.set()
        await service.shutdown()


@pytest.mark.asyncio
async def test_realpath_open_and_flv_parsing_run_in_the_media_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'active.flv'
    output = BytesIO()
    writer = FlvWriter(output)
    writer.write_header(FlvHeader('FLV', 1, 5, 9))
    writer.write_tag(create_metadata_tag({'duration': 0.0, 'filesize': 0.0}))
    tail_start = output.tell()
    output.write(b'a' * 32)
    source.write_bytes(output.getvalue())
    realpath_threads = []
    open_threads = []
    original_realpath = active_media.os.path.realpath
    original_open = builtins.open

    def tracked_realpath(path: str) -> str:
        realpath_threads.append(threading.current_thread().name)
        return original_realpath(path)

    def tracked_open(*args, **kwargs):
        if args and str(args[0]) == str(source):
            open_threads.append(threading.current_thread().name)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(active_media.os.path, 'realpath', tracked_realpath)
    monkeypatch.setattr(builtins, 'open', tracked_open)
    service = ActiveMediaService()
    try:
        snapshot = await service.snapshot(
            1,
            str(source),
            source.stat().st_size,
            {
                'duration': 2.0,
                'filesize': float(source.stat().st_size),
                'lastkeyframelocation': float(tail_start + 16),
                'lastkeyframetimestamp': 1.0,
                'keyframes': {
                    'times': [0.0, 1.0],
                    'filepositions': [float(tail_start), float(tail_start + 16)],
                },
            },
        )
        assert snapshot.duration_ms == 2_000
        assert realpath_threads
        assert open_threads
        assert all(
            name.startswith('blrec-active-media')
            for name in realpath_threads + open_threads
        )
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_new_revision_replaces_the_shareable_snapshot_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'active.flv'
    source.write_bytes(b'FLV')
    both_started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0

    def build(path: str, source_size: int, metadata: Dict[str, Any]):
        nonlocal calls
        with lock:
            calls += 1
            if calls == 2:
                both_started.set()
        assert release.wait(5)
        return FlvMediaSnapshot.frozen(path, source_size)

    monkeypatch.setattr(FlvMediaSnapshot, 'create', build)
    service = ActiveMediaService()
    first = asyncio.create_task(service.snapshot(1, str(source), 3, _metadata(1)))
    second = asyncio.create_task(service.snapshot(1, str(source), 4, _metadata(2)))
    duplicate = asyncio.create_task(service.snapshot(1, str(source), 4, _metadata(2)))
    try:
        await _wait_until(both_started.is_set)
        assert calls == 2
        assert service.admitted_count == 2
        assert service.in_flight_source_count == 1
        release.set()
        first_snapshot, second_snapshot, duplicate_snapshot = await asyncio.gather(
            first, second, duplicate
        )
        assert first_snapshot.source_size == 3
        assert second_snapshot.source_size == 4
        assert duplicate_snapshot is second_snapshot
    finally:
        release.set()
        await service.shutdown()


@pytest.mark.asyncio
async def test_cancelling_one_waiter_does_not_cancel_the_shared_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def build(path: str, source_size: int, metadata: Dict[str, Any]):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(5)
        return FlvMediaSnapshot.frozen(path, source_size)

    monkeypatch.setattr(FlvMediaSnapshot, 'create', build)
    service = ActiveMediaService()
    cancelled = asyncio.create_task(
        service.snapshot(1, str(tmp_path / 'active.flv'), 3, _metadata(1))
    )
    survivor = asyncio.create_task(
        service.snapshot(1, str(tmp_path / 'active.flv'), 3, _metadata(1))
    )
    try:
        await _wait_until(started.is_set)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert not survivor.done()
        release.set()
        assert (await survivor).source_size == 3
        assert calls == 1
        await _wait_until(lambda: service.in_flight_source_count == 0)
    finally:
        release.set()
        await service.shutdown()


@pytest.mark.asyncio
async def test_failed_build_is_removed_and_the_next_request_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def build(path: str, source_size: int, metadata: Dict[str, Any]):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EOFError('incomplete FLV')
        return FlvMediaSnapshot.frozen(path, source_size)

    monkeypatch.setattr(FlvMediaSnapshot, 'create', build)
    service = ActiveMediaService()
    try:
        with pytest.raises(EOFError, match='incomplete FLV'):
            await service.snapshot(1, str(tmp_path / 'active.flv'), 3, _metadata(1))
        await _wait_until(lambda: service.in_flight_source_count == 0)
        snapshot = await service.snapshot(
            1, str(tmp_path / 'active.flv'), 3, _metadata(1)
        )
        assert snapshot.source_size == 3
        assert calls == 2
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_active_media_has_two_workers_and_eight_waiting_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak_active = 0
    calls = 0

    def build(path: str, source_size: int, metadata: Dict[str, Any]):
        nonlocal active, peak_active, calls
        with lock:
            active += 1
            calls += 1
            peak_active = max(peak_active, active)
        try:
            assert release.wait(5)
            return FlvMediaSnapshot.frozen(path, source_size)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(FlvMediaSnapshot, 'create', build)
    service = ActiveMediaService()
    requests = [
        asyncio.create_task(
            service.snapshot(
                part_id,
                str(tmp_path / '{}.flv'.format(part_id)),
                part_id,
                _metadata(part_id),
            )
        )
        for part_id in range(1, 11)
    ]
    try:
        await _wait_until(lambda: service.admitted_count == 10)
        await _wait_until(lambda: calls == 2)
        with pytest.raises(ActiveMediaBusy) as error:
            await service.snapshot(11, str(tmp_path / '11.flv'), 11, _metadata(11))
        assert error.value.retry_after == 1
        assert calls == 2
        assert peak_active == 2
        release.set()
        await asyncio.gather(*requests)
        assert peak_active == 2
    finally:
        release.set()
        await service.shutdown()


@pytest.mark.asyncio
async def test_completed_active_snapshots_retain_no_results_or_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def build(path: str, source_size: int, metadata: Dict[str, Any]):
        nonlocal calls
        calls += 1
        return FlvMediaSnapshot(
            path=path,
            source_size=source_size,
            source_tail_start=source_size,
            prefix=b'prefix',
            duration_ms=source_size,
        )

    monkeypatch.setattr(FlvMediaSnapshot, 'create', build)
    service = ActiveMediaService()
    try:
        for revision in range(1, 101):
            await service.snapshot(
                1, str(tmp_path / 'growing.flv'), revision, _metadata(revision)
            )
        for part_id in range(2, 102):
            await service.snapshot(
                part_id,
                str(tmp_path / '{}.flv'.format(part_id)),
                part_id,
                _metadata(part_id),
            )
        await _wait_until(lambda: service.admitted_count == 0)
        assert calls == 200
        assert service.in_flight_source_count == 0
        assert service.completed_cache_entries == 0
        assert service.completed_cache_prefix_bytes == 0
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_shutdown_drains_admitted_work_and_rejects_new_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def build(path: str, source_size: int, metadata: Dict[str, Any]):
        assert release.wait(5)
        return FlvMediaSnapshot.frozen(path, source_size)

    monkeypatch.setattr(FlvMediaSnapshot, 'create', build)
    service = ActiveMediaService()
    request = asyncio.create_task(
        service.snapshot(1, str(tmp_path / 'active.flv'), 3, _metadata(1))
    )
    await _wait_until(lambda: service.admitted_count == 1)
    shutdown = asyncio.create_task(service.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()
    with pytest.raises(RuntimeError, match='closed'):
        await service.snapshot(2, str(tmp_path / 'later.flv'), 4, _metadata(2))
    release.set()
    await request
    await shutdown
    assert service.admitted_count == 0
