import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from blrec.setting.file_work import SettingsDirectoryError, SettingsFileWorkCoordinator
from blrec.setting.file_work import SettingsApplyReconciler
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


@pytest.mark.asyncio
async def test_global_patch_is_copy_on_write_and_dumps_once(tmp_path: Path) -> None:
    path = tmp_path / 'settings.toml'
    settings = Settings()
    settings._path = str(path)
    settings.dump()
    coordinator = SettingsFileWorkCoordinator()
    manager = SettingsManager(
        FakeSettingsApplication(), settings, file_work=coordinator  # type: ignore[arg-type]
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
        FakeSettingsApplication(), settings, file_work=coordinator  # type: ignore[arg-type]
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
    manager = SettingsManager(
        FakeSettingsApplication(), settings, file_work=coordinator  # type: ignore[arg-type]
    )
    coordinator.atomic_dump = AsyncMock(side_effect=OSError('disk full'))  # type: ignore[method-assign]

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
    manager = SettingsManager(
        FakeSettingsApplication(), settings, file_work=coordinator  # type: ignore[arg-type]
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
    manager = SettingsManager(
        FakeSettingsApplication(), settings, file_work=coordinator  # type: ignore[arg-type]
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
async def test_patch_persists_before_submitting_background_apply(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'settings.toml'
    settings = Settings(tasks=[{'roomId': 100}])
    settings._path = str(path)
    coordinator = SettingsFileWorkCoordinator()
    reconciler = FakeApplyReconciler()
    manager = SettingsManager(
        FakeSettingsApplication(), settings, file_work=coordinator  # type: ignore[arg-type]
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
    manager = SettingsManager(
        FakeSettingsApplication(), settings, file_work=file_work  # type: ignore[arg-type]
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
