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
from blrec.setting.models import (
    LoggingSettings,
    OutputSettings,
    Settings,
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
async def test_cancelled_atomic_dump_waits_until_replace_finishes(
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
    replace_entered = Event()
    replace_release = Event()
    real_replace = os.replace

    def blocked_replace(source: str, target: str) -> None:
        replace_entered.set()
        replace_release.wait()
        real_replace(source, target)

    monkeypatch.setattr(os, 'replace', blocked_replace)
    task = asyncio.create_task(coordinator.atomic_dump(candidate))
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, replace_entered.wait),
            timeout=1,
        )
        task.cancel()
        await asyncio.sleep(0)

        assert not task.done()

        replace_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        replace_release.set()
        await coordinator.shutdown()

    assert Settings.load(str(path)).live_monitor.batch_size == 17


@pytest.mark.asyncio
async def test_cancelled_atomic_dump_waits_until_directory_fsync_finishes(
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
    fsync_entered = Event()
    fsync_release = Event()
    real_fsync_directory = file_work._fsync_directory

    def blocked_fsync_directory(directory: Path) -> None:
        fsync_entered.set()
        fsync_release.wait()
        real_fsync_directory(directory)

    monkeypatch.setattr(file_work, '_fsync_directory', blocked_fsync_directory)
    task = asyncio.create_task(coordinator.atomic_dump(candidate))
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, fsync_entered.wait),
            timeout=1,
        )
        task.cancel()
        await asyncio.sleep(0)

        assert not task.done()

        fsync_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        fsync_release.set()
        await coordinator.shutdown()

    assert Settings.load(str(path)).live_monitor.batch_size == 17


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


@pytest.mark.asyncio
async def test_settings_apply_reconciler_recovers_reserved_work_after_capacity_frees(
    tmp_path: Path,
) -> None:
    from blrec.control.operations import ControlOperationJournal, ControlStepInput
    from blrec.setting.file_work import SettingsApplyReconciler

    journal = ControlOperationJournal(
        tmp_path / 'control.sqlite3', max_nonterminal_per_lane=1
    )
    await journal.open()
    await journal.admit(
        lane='settings-apply',
        kind='blocker',
        target_key='blocker',
        steps=[ControlStepInput(key='blocker')],
    )
    applied = []

    async def apply(target_key: str, action: str) -> None:
        applied.append((target_key, action))

    reconciler = SettingsApplyReconciler(journal, apply)

    async def persist() -> None:
        return None

    def commit_live() -> None:
        return None

    try:
        operations = await reconciler.commit_revisions(
            (('settings:header', 'apply'), ('settings:live_monitor', 'apply')),
            persist,
            commit_live,
        )
        assert operations == ()

        reconciler.start()
        await asyncio.wait_for(reconciler.wait_idle(), timeout=1)

        assert applied == [
            ('settings:header', 'apply'),
            ('settings:live_monitor', 'apply'),
        ]
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_settings_apply_reconciler_does_not_apply_a_reserved_revision_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blrec.control.operations import ControlOperationJournal
    from blrec.setting.file_work import SettingsApplyReconciler

    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    current_value = {'value': 'old'}
    applied = []
    first_apply_finished = asyncio.Event()

    async def apply(_target_key: str, _action: str) -> None:
        applied.append(current_value['value'])
        first_apply_finished.set()

    reconciler = SettingsApplyReconciler(journal, apply)
    await reconciler.submit('settings:header', 'apply')
    original_get_revision = journal.get_revision
    get_revision_entered = asyncio.Event()
    allow_get_revision = asyncio.Event()
    first_get_revision = True

    async def blocked_get_revision(lane: str, target_key: str):
        nonlocal first_get_revision
        if first_get_revision:
            first_get_revision = False
            get_revision_entered.set()
            await allow_get_revision.wait()
        return await original_get_revision(lane, target_key)

    monkeypatch.setattr(journal, 'get_revision', blocked_get_revision)
    commit_entered = asyncio.Event()
    allow_commit = asyncio.Event()

    async def persist() -> None:
        commit_entered.set()
        await allow_commit.wait()

    def commit_live() -> None:
        current_value['value'] = 'new'

    commit_task = None
    reconciler.start()
    try:
        await asyncio.wait_for(get_revision_entered.wait(), timeout=1)
        commit_task = asyncio.create_task(
            reconciler.commit_revisions(
                (('settings:header', 'apply'),), persist, commit_live
            )
        )
        try:
            await asyncio.wait_for(commit_entered.wait(), timeout=0.05)
        except asyncio.TimeoutError:
            pass
        allow_get_revision.set()
        await asyncio.wait_for(commit_entered.wait(), timeout=1)
        await asyncio.wait_for(first_apply_finished.wait(), timeout=1)
        allow_commit.set()
        await commit_task
        await asyncio.wait_for(reconciler.wait_idle(), timeout=1)

        assert applied == ['old', 'new']
    finally:
        allow_get_revision.set()
        allow_commit.set()
        if commit_task is not None:
            await asyncio.gather(commit_task, return_exceptions=True)
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_settings_apply_retry_does_not_recover_an_unrequested_failure(
    tmp_path: Path,
) -> None:
    from blrec.control.operations import ControlOperationJournal
    from blrec.setting.file_work import SettingsApplyReconciler

    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()

    async def apply(_target_key: str, _action: str) -> None:
        return None

    reconciler = SettingsApplyReconciler(journal, apply)
    try:
        failed = await journal.submit_revision(
            lane=reconciler.LANE,
            kind='apply',
            target_key='settings:logging',
            action='apply',
        )
        claim = await journal.claim_next(reconciler.LANE)
        assert claim is not None
        await journal.finish_step(
            claim, status='failed', error_code='SETTINGS_APPLY_FAILED'
        )

        recovered = await reconciler.retry(('settings:header',))

        assert recovered == ()
        assert await journal.queued_count(reconciler.LANE) == 0
        revision = await journal.get_revision(reconciler.LANE, 'settings:logging')
        assert revision is not None
        assert revision.operation_id == failed.id
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_settings_commit_does_not_recover_an_unrequested_failure(
    tmp_path: Path,
) -> None:
    from blrec.control.operations import ControlOperationJournal
    from blrec.setting.file_work import SettingsApplyReconciler

    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()

    async def apply(_target_key: str, _action: str) -> None:
        return None

    async def persist() -> None:
        return None

    def commit_live() -> None:
        return None

    reconciler = SettingsApplyReconciler(journal, apply)
    try:
        failed = await journal.submit_revision(
            lane=reconciler.LANE,
            kind='apply',
            target_key='settings:logging',
            action='apply',
        )
        claim = await journal.claim_next(reconciler.LANE)
        assert claim is not None
        await journal.finish_step(
            claim, status='failed', error_code='SETTINGS_APPLY_FAILED'
        )

        recovered = await reconciler.commit_revisions(
            (('settings:header', 'apply'),), persist, commit_live
        )

        assert [operation.target_key for operation in recovered] == ['settings:header']
        logging_revision = await journal.get_revision(
            reconciler.LANE, 'settings:logging'
        )
        assert logging_revision is not None
        assert logging_revision.operation_id == failed.id
    finally:
        await journal.close()
