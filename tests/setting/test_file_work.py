import asyncio
import errno
import os
from pathlib import Path
from threading import Event, Lock
from typing import List

import pytest

from blrec.setting.file_work import (
    SettingsFileWorkCoordinator,
    SettingsFileWorkSaturated,
    validate_directory_sync,
)
from blrec.setting.models import Settings
from blrec.setting.models import (
    LoggingSettings,
    OutputSettings,
    SettingsIn,
    TaskOptions,
)


@pytest.mark.asyncio
async def test_atomic_dump_replaces_with_a_complete_loadable_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'settings.toml'
    original = Settings()
    original._path = str(path)
    original.dump()
    candidate = original.copy(deep=True)
    candidate._path = str(path)
    candidate.live_monitor.batch_size = 17
    coordinator = SettingsFileWorkCoordinator()

    try:
        await coordinator.atomic_dump(candidate)
    finally:
        await coordinator.shutdown()

    loaded = Settings.load(str(path))
    assert loaded.live_monitor.batch_size == 17
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob('.settings.toml.*.tmp')) == []


@pytest.mark.asyncio
async def test_atomic_dump_keeps_old_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / 'settings.toml'
    original = Settings()
    original._path = str(path)
    original.dump()
    candidate = original.copy(deep=True)
    candidate._path = str(path)
    candidate.live_monitor.batch_size = 17
    coordinator = SettingsFileWorkCoordinator()
    real_replace = os.replace

    def fail_replace(source: str, target: str) -> None:
        assert target == str(path)
        raise OSError('replace failed')

    monkeypatch.setattr(os, 'replace', fail_replace)
    try:
        with pytest.raises(OSError, match='replace failed'):
            await coordinator.atomic_dump(candidate)
    finally:
        monkeypatch.setattr(os, 'replace', real_replace)
        await coordinator.shutdown()

    loaded = Settings.load(str(path))
    assert loaded.live_monitor.batch_size == original.live_monitor.batch_size
    assert list(tmp_path.glob('.settings.toml.*.tmp')) == []


@pytest.mark.asyncio
async def test_atomic_dump_keeps_old_file_when_file_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / 'settings.toml'
    original = Settings()
    original._path = str(path)
    original.dump()
    candidate = original.copy(deep=True)
    candidate._path = str(path)
    candidate.live_monitor.batch_size = 17
    coordinator = SettingsFileWorkCoordinator()

    def fail_fsync(_descriptor: int) -> None:
        raise OSError('file fsync failed')

    monkeypatch.setattr(os, 'fsync', fail_fsync)
    try:
        with pytest.raises(OSError, match='file fsync failed'):
            await coordinator.atomic_dump(candidate)
    finally:
        await coordinator.shutdown()

    assert Settings.load(str(path)).live_monitor.batch_size == 29
    assert list(tmp_path.glob('.settings.toml.*.tmp')) == []


@pytest.mark.asyncio
async def test_atomic_dump_leaves_a_complete_new_file_when_directory_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import blrec.setting.file_work as file_work

    path = tmp_path / 'settings.toml'
    original = Settings()
    original._path = str(path)
    original.dump()
    candidate = original.copy(deep=True)
    candidate._path = str(path)
    candidate.live_monitor.batch_size = 17
    coordinator = SettingsFileWorkCoordinator()

    def fail_directory_fsync(_path: Path) -> None:
        raise OSError('directory fsync failed')

    monkeypatch.setattr(file_work, '_fsync_directory', fail_directory_fsync)
    try:
        with pytest.raises(OSError, match='directory fsync failed'):
            await coordinator.atomic_dump(candidate)
    finally:
        await coordinator.shutdown()

    assert Settings.load(str(path)).live_monitor.batch_size == 17
    assert list(tmp_path.glob('.settings.toml.*.tmp')) == []


@pytest.mark.asyncio
async def test_file_work_bounds_two_active_and_eight_waiting() -> None:
    coordinator = SettingsFileWorkCoordinator(max_active=2, max_waiting=8)
    release = Event()
    entered = Event()
    entered_count = 0
    entered_lock = Lock()

    def blocked(value: int) -> int:
        nonlocal entered_count
        with entered_lock:
            entered_count += 1
            if entered_count == 2:
                entered.set()
        release.wait()
        return value

    admitted = [
        asyncio.create_task(coordinator.run(blocked, value)) for value in range(10)
    ]
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 2)

    with pytest.raises(SettingsFileWorkSaturated):
        await coordinator.run(blocked, 10)

    release.set()
    assert await asyncio.gather(*admitted) == list(range(10))
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_shutdown_drains_running_and_queued_file_work() -> None:
    coordinator = SettingsFileWorkCoordinator(max_active=1, max_waiting=2)
    release = Event()
    completed: List[int] = []

    def blocked(value: int) -> None:
        release.wait()
        completed.append(value)

    tasks = [asyncio.create_task(coordinator.run(blocked, value)) for value in range(3)]
    await asyncio.sleep(0)
    shutdown = asyncio.create_task(coordinator.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()

    release.set()
    await shutdown
    await asyncio.gather(*tasks)
    assert sorted(completed) == [0, 1, 2]


def test_validate_directory_sync_preserves_response_codes(tmp_path: Path) -> None:
    assert validate_directory_sync(str(tmp_path)) == (0, 'ok')
    assert validate_directory_sync(str(tmp_path / 'missing')) == (
        errno.ENOTDIR,
        'not a directory',
    )


def test_request_models_never_probe_the_real_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError('request parsing touched the filesystem')

    monkeypatch.setattr(os.path, 'isdir', forbidden)
    monkeypatch.setattr(os, 'access', forbidden)
    monkeypatch.setattr(os, 'makedirs', forbidden)

    parsed = SettingsIn(
        output=OutputSettings(out_dir='~/recordings'),
        logging=LoggingSettings(log_dir='~/logs'),
    )
    options = TaskOptions.parse_obj({'output': {'pathTemplate': '{roomid}'}})

    assert parsed.output is not None
    assert parsed.output.out_dir.endswith('recordings')
    assert options.output.path_template == '{roomid}'


@pytest.mark.asyncio
async def test_settings_apply_reconciler_reloads_revision_until_caught_up(
    tmp_path: Path,
) -> None:
    from blrec.control.operations import ControlOperationJournal
    from blrec.setting.file_work import SettingsApplyReconciler

    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    entered = asyncio.Event()
    release = asyncio.Event()
    applied = []

    async def apply(target_key: str, action: str) -> None:
        applied.append((target_key, action))
        if len(applied) == 1:
            entered.set()
            await release.wait()

    reconciler = SettingsApplyReconciler(journal, apply)
    reconciler.start()
    try:
        first = await reconciler.submit('settings:header', 'apply')
        await entered.wait()
        second = await reconciler.submit('settings:header', 'apply')
        assert second.id == first.id
        release.set()
        await reconciler.wait_idle()

        final = await journal.get(first.id)
        assert final is not None and final.status == 'succeeded'
        assert applied == [('settings:header', 'apply'), ('settings:header', 'apply')]
    finally:
        release.set()
        await reconciler.shutdown()
        await journal.close()
