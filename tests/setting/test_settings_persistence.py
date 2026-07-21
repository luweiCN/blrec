import asyncio
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from typing import Optional, Sequence, Tuple
from unittest.mock import AsyncMock

import pytest

from blrec.setting.file_work import (
    SettingsApplyReconciler,
    SettingsDirectoryError,
    SettingsFileWorkCoordinator,
)
from blrec.setting.models import LiveMonitorSettings, Settings, SettingsIn, TaskOptions
from blrec.setting.setting_manager import SettingsManager


class FakeSettingsApplication:
    def __init__(self) -> None:
        self._live_status_coordinator = None
        self._task_manager = SimpleNamespace(
            apply_task_recorder_settings=lambda *_args: None
        )

    def has_recording_task(self) -> bool:
        return False


class FakeApplyReconciler:
    def __init__(self) -> None:
        self.calls = []

    async def submit(self, target_key: str, action: str) -> SimpleNamespace:
        self.calls.append((target_key, action))
        return SimpleNamespace(id='operation-{}'.format(len(self.calls)))

    async def commit_revisions(self, revisions, persist, commit_live):
        await persist()
        commit_live()
        return tuple(
            [await self.submit(target_key, action) for target_key, action in revisions]
        )

    async def retry(self, _target_keys):
        return ()


class BlockingSecondApplyReconciler(FakeApplyReconciler):
    def __init__(self) -> None:
        super().__init__()
        self.completed = []
        self.second_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def submit(self, target_key: str, action: str) -> SimpleNamespace:
        self.calls.append((target_key, action))
        if len(self.calls) == 2:
            self.second_entered.set()
            await self.release.wait()
        self.completed.append((target_key, action))
        return SimpleNamespace(id='operation-{}'.format(len(self.calls)))


@pytest.mark.asyncio
@pytest.mark.parametrize('failing_method', ('recover_revision_gaps', 'claim_next'))
@pytest.mark.parametrize('observer', ('wait_idle', 'shutdown'))
async def test_settings_apply_worker_failure_reaches_lifecycle_waiters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_method: str, observer: str
) -> None:
    from blrec.control.operations import ControlOperationJournal

    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()

    async def apply(_target_key: str, _action: str) -> None:
        pass

    async def fail_journal_call(*_args: object, **_kwargs: object) -> object:
        raise OSError('{} failed'.format(failing_method))

    monkeypatch.setattr(journal, failing_method, fail_journal_call)
    reconciler = SettingsApplyReconciler(journal, apply)
    reconciler.start()
    try:
        with pytest.raises(OSError, match='{} failed'.format(failing_method)):
            await asyncio.wait_for(getattr(reconciler, observer)(), timeout=0.2)
    finally:
        worker = reconciler._worker
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
        await journal.close()


@pytest.mark.asyncio
async def test_global_patch_is_copy_on_write_and_dumps_once(tmp_path: Path) -> None:
    path = tmp_path / 'settings.toml'
    settings = Settings()
    settings._path = str(path)
    settings.dump()
    coordinator = SettingsFileWorkCoordinator()
    manager = SettingsManager(  # type: ignore[arg-type]
        FakeSettingsApplication(), settings, file_work=coordinator
    )
    dumps = 0
    original_atomic_dump = coordinator.atomic_dump

    async def count_dump(candidate: Settings, **kwargs) -> None:
        nonlocal dumps
        dumps += 1
        await original_atomic_dump(candidate, **kwargs)

    coordinator.atomic_dump = count_dump  # type: ignore[method-assign]
    try:
        result, operations = await manager.change_settings_with_operations(
            SettingsIn.parse_obj({'liveMonitor': {'batchSize': 17}})
        )
    finally:
        await coordinator.shutdown()

    assert result.live_monitor == LiveMonitorSettings(batch_size=17)
    assert operations == ()
    assert dumps == 1
    assert Settings.load(str(path)).live_monitor.batch_size == 17


@pytest.mark.asyncio
async def test_noop_patch_does_not_dump_or_submit_apply(tmp_path: Path) -> None:
    settings = Settings()
    settings._path = str(tmp_path / 'settings.toml')
    coordinator = SettingsFileWorkCoordinator()
    reconciler = FakeApplyReconciler()
    manager = SettingsManager(
        FakeSettingsApplication(),
        settings,
        file_work=coordinator,  # type: ignore[arg-type]
    )
    manager.set_apply_reconciler(reconciler)  # type: ignore[arg-type]
    coordinator.atomic_dump = AsyncMock()  # type: ignore[method-assign]

    try:
        _result, operations = await manager.change_settings_with_operations(
            SettingsIn.parse_obj({'liveMonitor': {'batchSize': 29}})
        )
    finally:
        await coordinator.shutdown()

    coordinator.atomic_dump.assert_not_awaited()
    assert reconciler.calls == []
    assert operations == ()


@pytest.mark.asyncio
async def test_failed_global_dump_keeps_live_settings_unchanged(tmp_path: Path) -> None:
    settings = Settings()
    settings._path = str(tmp_path / 'settings.toml')
    coordinator = SettingsFileWorkCoordinator()
    manager = SettingsManager(  # type: ignore[arg-type]
        FakeSettingsApplication(), settings, file_work=coordinator
    )
    coordinator.atomic_dump = AsyncMock(  # type: ignore[method-assign]
        side_effect=OSError('disk full')
    )

    try:
        with pytest.raises(OSError, match='disk full'):
            await manager.change_settings_with_operations(
                SettingsIn.parse_obj({'liveMonitor': {'batchSize': 17}})
            )
    finally:
        await coordinator.shutdown()

    assert settings.live_monitor.batch_size == 29


@pytest.mark.asyncio
async def test_output_directory_is_validated_in_the_file_worker(tmp_path: Path) -> None:
    settings = Settings()
    settings._path = str(tmp_path / 'settings.toml')
    original_out_dir = settings.output.out_dir
    coordinator = SettingsFileWorkCoordinator()
    manager = SettingsManager(  # type: ignore[arg-type]
        FakeSettingsApplication(), settings, file_work=coordinator
    )

    try:
        with pytest.raises(SettingsDirectoryError) as captured:
            await manager.change_settings_with_operations(
                SettingsIn.parse_obj({'output': {'outDir': str(tmp_path / 'missing')}})
            )
    finally:
        await coordinator.shutdown()

    assert captured.value.code != 0
    assert settings.output.out_dir == original_out_dir
    assert not (tmp_path / 'settings.toml').exists()


@pytest.mark.asyncio
async def test_global_and_task_patches_share_one_mutation_order(tmp_path: Path) -> None:
    settings = Settings(tasks=[{'roomId': 100}])
    settings._path = str(tmp_path / 'settings.toml')
    coordinator = SettingsFileWorkCoordinator()
    manager = SettingsManager(  # type: ignore[arg-type]
        FakeSettingsApplication(), settings, file_work=coordinator
    )
    first_entered = asyncio.Event()
    first_release = asyncio.Event()
    writes = 0

    async def blocked_dump(candidate: Settings, **_kwargs) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            first_entered.set()
            await first_release.wait()

    coordinator.atomic_dump = blocked_dump  # type: ignore[method-assign]
    global_patch = asyncio.create_task(
        manager.change_settings_with_operations(
            SettingsIn.parse_obj({'liveMonitor': {'batchSize': 17}})
        )
    )
    await first_entered.wait()
    task_patch = asyncio.create_task(
        manager.change_task_options_with_operations(
            100, TaskOptions.parse_obj({'recorder': {'readTimeout': 5}})
        )
    )
    await asyncio.sleep(0)
    assert writes == 1

    first_release.set()
    await global_patch
    await task_patch
    await coordinator.shutdown()

    assert writes == 2
    assert settings.live_monitor.batch_size == 17
    assert settings.tasks[0].recorder.read_timeout == 5


@pytest.mark.asyncio
async def test_cancelled_persisted_patch_finishes_before_a_second_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import blrec.setting.file_work as file_work

    path = tmp_path / 'settings.toml'
    settings = Settings()
    settings._path = str(path)
    settings.dump()
    coordinator = SettingsFileWorkCoordinator()
    reconciler = FakeApplyReconciler()
    manager = SettingsManager(  # type: ignore[arg-type]
        FakeSettingsApplication(), settings, file_work=coordinator
    )
    manager.set_apply_reconciler(reconciler)  # type: ignore[arg-type]
    first_replace_entered = Event()
    first_replace_release = Event()
    replace_lock = Lock()
    replace_calls = 0
    real_replace = file_work.os.replace

    def block_first_replace(source: str, target: str) -> None:
        nonlocal replace_calls
        with replace_lock:
            replace_calls += 1
            call = replace_calls
        if call == 1:
            first_replace_entered.set()
            first_replace_release.wait()
        real_replace(source, target)

    monkeypatch.setattr(file_work.os, 'replace', block_first_replace)
    original_atomic_dump = coordinator.atomic_dump
    second_dump_entered = asyncio.Event()
    dump_calls = 0

    async def tracked_atomic_dump(candidate: Settings, **kwargs) -> None:
        nonlocal dump_calls
        dump_calls += 1
        if dump_calls == 2:
            second_dump_entered.set()
        await original_atomic_dump(candidate, **kwargs)

    coordinator.atomic_dump = tracked_atomic_dump  # type: ignore[method-assign]
    first = asyncio.create_task(
        manager.change_settings_with_operations(
            SettingsIn.parse_obj({'liveMonitor': {'batchSize': 17}})
        )
    )
    second = None
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None, first_replace_entered.wait
            ),
            timeout=1,
        )
        first.cancel()
        await asyncio.sleep(0)
        second = asyncio.create_task(
            manager.change_settings_with_operations(
                SettingsIn.parse_obj({'liveMonitor': {'batchSize': 19}})
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not first.done()
        assert not second_dump_entered.is_set()

        first_replace_release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
    finally:
        first_replace_release.set()
        await asyncio.gather(
            first, *((second,) if second is not None else ()), return_exceptions=True
        )
        await coordinator.shutdown()

    assert settings.live_monitor.batch_size == 19
    assert Settings.load(str(path)).live_monitor.batch_size == 19
    assert reconciler.calls == [
        ('settings:live_monitor', 'apply'),
        ('settings:live_monitor', 'apply'),
    ]


@pytest.mark.asyncio
async def test_cancelled_multisection_patch_submits_every_revision(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'settings.toml'
    settings = Settings()
    settings._path = str(path)
    settings.dump()
    coordinator = SettingsFileWorkCoordinator()
    reconciler = BlockingSecondApplyReconciler()
    manager = SettingsManager(
        FakeSettingsApplication(),
        settings,
        file_work=coordinator,  # type: ignore[arg-type]
    )
    manager.set_apply_reconciler(reconciler)  # type: ignore[arg-type]
    patch = asyncio.create_task(
        manager.change_settings_with_operations(
            SettingsIn.parse_obj(
                {
                    'header': {'userAgent': 'cancel-safe-agent'},
                    'liveMonitor': {'batchSize': 17},
                }
            )
        )
    )
    try:
        await asyncio.wait_for(reconciler.second_entered.wait(), timeout=1)
        patch.cancel()
        await asyncio.sleep(0)

        assert not patch.done()

        reconciler.release.set()
        with pytest.raises(asyncio.CancelledError):
            await patch
    finally:
        reconciler.release.set()
        await asyncio.gather(patch, return_exceptions=True)
        await coordinator.shutdown()

    loaded = Settings.load(str(path))
    assert loaded.header.user_agent == settings.header.user_agent == 'cancel-safe-agent'
    assert loaded.live_monitor.batch_size == settings.live_monitor.batch_size == 17
    assert reconciler.completed == [
        ('settings:header', 'apply'),
        ('settings:live_monitor', 'apply'),
    ]


@pytest.mark.asyncio
async def test_cancelled_admitted_task_state_write_keeps_file_and_memory_aligned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import blrec.setting.file_work as file_work

    path = tmp_path / 'settings.toml'
    settings = Settings(tasks=[{'roomId': 100}])
    settings._path = str(path)
    settings.dump()
    coordinator = SettingsFileWorkCoordinator()
    manager = SettingsManager(
        FakeSettingsApplication(),
        settings,
        file_work=coordinator,  # type: ignore[arg-type]
    )
    replace_entered = Event()
    replace_release = Event()
    real_replace = file_work.os.replace

    def blocked_replace(source: str, target: str) -> None:
        replace_entered.set()
        replace_release.wait()
        real_replace(source, target)

    monkeypatch.setattr(file_work.os, 'replace', blocked_replace)
    write = asyncio.create_task(
        manager.change_task_desired_states([100], enable_monitor=False)
    )
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, replace_entered.wait),
            timeout=1,
        )
        write.cancel()
        await asyncio.sleep(0)

        assert not write.done()

        replace_release.set()
        with pytest.raises(asyncio.CancelledError):
            await write
    finally:
        replace_release.set()
        await asyncio.gather(write, return_exceptions=True)
        await coordinator.shutdown()

    assert settings.tasks[0].enable_monitor is False
    assert Settings.load(str(path)).tasks[0].enable_monitor is False


@pytest.mark.asyncio
async def test_revision_activation_failure_is_recovered_by_an_identical_patch(
    tmp_path: Path,
) -> None:
    from blrec.control.operations import (
        ControlOperationJournal,
        ControlOperationSnapshot,
    )

    path = tmp_path / 'settings.toml'
    settings = Settings()
    settings._path = str(path)
    settings.dump()
    coordinator = SettingsFileWorkCoordinator()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    applied = []

    async def apply(target_key: str, action: str) -> None:
        applied.append((target_key, action))

    reconciler = SettingsApplyReconciler(journal, apply)
    manager = SettingsManager(
        FakeSettingsApplication(),
        settings,
        file_work=coordinator,  # type: ignore[arg-type]
    )
    manager.set_apply_reconciler(reconciler)
    original_recover = journal.recover_revision_gaps
    recover_calls = 0

    async def fail_first_recovery(
        *,
        lane: str,
        kind: str,
        unassigned_only: bool = False,
        target_keys: Optional[Sequence[str]] = None,
    ) -> Sequence[ControlOperationSnapshot]:
        nonlocal recover_calls
        recover_calls += 1
        if recover_calls == 1:
            raise OSError('control database temporarily unavailable')
        return await original_recover(
            lane=lane,
            kind=kind,
            unassigned_only=unassigned_only,
            target_keys=target_keys,
        )

    journal.recover_revision_gaps = fail_first_recovery  # type: ignore[method-assign]
    request = SettingsIn.parse_obj(
        {'header': {'userAgent': 'recoverable-agent'}, 'liveMonitor': {'batchSize': 17}}
    )
    try:
        with pytest.raises(OSError, match='temporarily unavailable'):
            await manager.change_settings_with_operations(request)

        assert settings.header.user_agent == 'recoverable-agent'
        assert settings.live_monitor.batch_size == 17
        assert Settings.load(str(path)).header.user_agent == 'recoverable-agent'
        assert (
            await journal.get_revision('settings-apply', 'settings:header')
        ).operation_id is None
        assert (
            await journal.get_revision('settings-apply', 'settings:live_monitor')
        ).operation_id is None

        _result, operations = await manager.change_settings_with_operations(request)
        assert len(operations) == 2

        reconciler.start()
        await asyncio.wait_for(reconciler.wait_idle(), timeout=1)
        assert applied == [
            ('settings:header', 'apply'),
            ('settings:live_monitor', 'apply'),
        ]
    finally:
        await reconciler.shutdown()
        await coordinator.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_revision_reservation_failure_keeps_live_old_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blrec.control.operations import (
        ControlOperationJournal,
        ControlRevisionSnapshot,
    )

    path = tmp_path / 'settings.toml'
    settings = Settings()
    settings._path = str(path)
    settings.dump()
    original_batch_size = settings.live_monitor.batch_size
    coordinator = SettingsFileWorkCoordinator()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    applied = []

    async def apply(target_key: str, action: str) -> None:
        applied.append((target_key, action, settings.live_monitor.batch_size))

    reconciler = SettingsApplyReconciler(journal, apply)
    manager = SettingsManager(
        FakeSettingsApplication(),
        settings,
        file_work=coordinator,  # type: ignore[arg-type]
    )
    manager.set_apply_reconciler(reconciler)
    original_reserve = journal.reserve_revisions
    reserve_calls = 0

    async def fail_first_reservation(
        *, lane: str, kind: str, revisions: Sequence[Tuple[str, str]]
    ) -> Sequence[ControlRevisionSnapshot]:
        nonlocal reserve_calls
        reserve_calls += 1
        if reserve_calls == 1:
            raise OSError('control database temporarily unavailable')
        return await original_reserve(lane=lane, kind=kind, revisions=revisions)

    monkeypatch.setattr(journal, 'reserve_revisions', fail_first_reservation)
    request = SettingsIn.parse_obj({'liveMonitor': {'batchSize': 17}})
    try:
        with pytest.raises(OSError, match='temporarily unavailable'):
            await manager.change_settings_with_operations(request)

        assert Settings.load(str(path)).live_monitor.batch_size == 17
        assert settings.live_monitor.batch_size == original_batch_size
        assert (
            await journal.get_revision('settings-apply', 'settings:live_monitor')
            is None
        )
        assert applied == []

        _result, operations = await manager.change_settings_with_operations(request)
        assert len(operations) == 1
        assert settings.live_monitor.batch_size == 17

        reconciler.start()
        await asyncio.wait_for(reconciler.wait_idle(), timeout=1)
        assert applied == [('settings:live_monitor', 'apply', 17)]
    finally:
        await reconciler.shutdown()
        await coordinator.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_failed_persist_does_not_create_or_apply_a_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blrec.control.operations import ControlOperationJournal

    path = tmp_path / 'settings.toml'
    settings = Settings()
    settings._path = str(path)
    settings.dump()
    original_batch_size = settings.live_monitor.batch_size
    coordinator = SettingsFileWorkCoordinator()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    applied = []

    async def apply(target_key: str, action: str) -> None:
        applied.append((target_key, action, settings.live_monitor.batch_size))

    async def fail_dump(*_args: object, **_kwargs: object) -> None:
        raise OSError('persist failed')

    monkeypatch.setattr(coordinator, 'atomic_dump', fail_dump)
    reconciler = SettingsApplyReconciler(journal, apply)
    manager = SettingsManager(
        FakeSettingsApplication(),
        settings,
        file_work=coordinator,  # type: ignore[arg-type]
    )
    manager.set_apply_reconciler(reconciler)
    try:
        with pytest.raises(OSError, match='persist failed'):
            await manager.change_settings_with_operations(
                SettingsIn.parse_obj({'liveMonitor': {'batchSize': 17}})
            )

        assert settings.live_monitor.batch_size == original_batch_size
        assert Settings.load(str(path)).live_monitor.batch_size == original_batch_size
        assert (
            await journal.get_revision('settings-apply', 'settings:live_monitor')
            is None
        )

        reconciler.start()
        await asyncio.wait_for(reconciler.wait_idle(), timeout=1)
        assert applied == []
    finally:
        await reconciler.shutdown()
        await coordinator.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_patch_persists_before_submitting_background_apply(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'settings.toml'
    settings = Settings(tasks=[{'roomId': 100}])
    settings._path = str(path)
    coordinator = SettingsFileWorkCoordinator()
    reconciler = FakeApplyReconciler()
    manager = SettingsManager(  # type: ignore[arg-type]
        FakeSettingsApplication(), settings, file_work=coordinator
    )
    manager.set_apply_reconciler(reconciler)  # type: ignore[arg-type]

    try:
        _result, operations = await manager.change_task_options_with_operations(
            100, TaskOptions.parse_obj({'recorder': {'readTimeout': 5}})
        )
    finally:
        await coordinator.shutdown()

    assert Settings.load(str(path)).tasks[0].recorder.read_timeout == 5
    assert reconciler.calls == [('task-settings:100:recorder', 'apply')]
    assert operations == ('operation-1',)


@pytest.mark.asyncio
async def test_patch_returns_while_apply_is_still_blocked(tmp_path: Path) -> None:
    from blrec.control.operations import ControlOperationJournal

    settings = Settings()
    settings._path = str(tmp_path / 'settings.toml')
    file_work = SettingsFileWorkCoordinator()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_apply(_target_key: str, _action: str) -> None:
        entered.set()
        await release.wait()

    reconciler = SettingsApplyReconciler(journal, blocked_apply)
    manager = SettingsManager(  # type: ignore[arg-type]
        FakeSettingsApplication(), settings, file_work=file_work
    )
    manager.set_apply_reconciler(reconciler)
    reconciler.start()
    try:
        _result, operations = await asyncio.wait_for(
            manager.change_settings_with_operations(
                SettingsIn.parse_obj({'liveMonitor': {'batchSize': 17}})
            ),
            timeout=1,
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        operation = await journal.get(operations[0])
        assert operation is not None and operation.status == 'running'
    finally:
        release.set()
        await reconciler.shutdown()
        await file_work.shutdown()
        await journal.close()
