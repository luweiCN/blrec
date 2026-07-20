from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Optional
from unittest.mock import AsyncMock

import pytest

from blrec.control.operations import ControlOperationJournal, ControlStepInput
from blrec.setting.models import Settings, TaskSettings
from blrec.setting.setting_manager import SettingsManager
from blrec.task.membership_reconciler import RoomMembershipReconciler


class FakeSettingsApplication:
    pass


class FakeTaskManager:
    def __init__(self) -> None:
        self.tasks: Dict[int, object] = {}
        self.add_calls = []
        self.remove_calls = []

    def has_task(self, room_id: int) -> bool:
        return room_id in self.tasks

    async def add_task(
        self, settings: TaskSettings, *, apply_desired_state: bool = True
    ) -> None:
        self.add_calls.append((settings.room_id, apply_desired_state))
        self.tasks[settings.room_id] = object()

    async def remove_task(self, room_id: int) -> None:
        self.remove_calls.append(room_id)
        self.tasks.pop(room_id, None)

    def get_all_task_room_ids(self):
        yield from self.tasks


class FakeTaskControl:
    def __init__(self) -> None:
        self.calls = []

    async def submit(self, kind, room_ids, *, rejected, force):
        self.calls.append((kind, tuple(room_ids), rejected, force))
        return SimpleNamespace(id='task-state-operation')


def make_settings_manager(settings: Settings) -> SettingsManager:
    manager = SettingsManager(
        FakeSettingsApplication(), settings  # type: ignore[arg-type]
    )
    manager.dump_settings = AsyncMock()  # type: ignore[method-assign]
    return manager


@pytest.mark.asyncio
async def test_collect_is_deduplicated_and_returns_resolved_terminal_result(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def resolve(room_id: int) -> int:
        entered.set()
        await release.wait()
        return 3582149 if room_id == 6 else room_id

    policy = AsyncMock()
    settings = make_settings_manager(Settings())
    tasks = FakeTaskManager()
    controls = FakeTaskControl()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        settings,
        tasks,  # type: ignore[arg-type]
        controls,  # type: ignore[arg-type]
        room_id_resolver=resolve,
        upload_policy_enabler=policy,
    )
    reconciler.start()
    try:
        first = await reconciler.submit_collect(6, upload=True)
        duplicate = await reconciler.submit_collect(6, upload=True)

        assert duplicate.id == first.id
        await entered.wait()
        assert first.status == 'accepted'
        release.set()
        await reconciler.wait_idle()

        final = await journal.get(first.id)
        assert final is not None
        assert final.status == 'succeeded'
        assert final.result is not None
        assert final.result['requestedRoomId'] == 6
        assert final.result['resolvedRoomId'] == 3582149
        assert final.result['collected'] is True
        assert final.result['upload'] is True
        assert [step.key for step in final.steps] == [
            'resolve',
            'add',
            'desired-state',
            'policy',
        ]
        assert {step.status for step in final.steps} == {'succeeded'}
        assert tasks.add_calls == [(3582149, False)]
        assert controls.calls == [('start', (3582149,), {}, False)]
        policy.assert_awaited_once_with(3582149)
        settings.dump_settings.assert_awaited_once()  # type: ignore[attr-defined]
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_failed_collect_retry_creates_new_attempt_and_skips_existing_add(
    tmp_path: Path,
) -> None:
    async def resolve(room_id: int) -> int:
        return room_id

    policy = AsyncMock(side_effect=[RuntimeError('not ready'), None])
    settings = make_settings_manager(Settings())
    tasks = FakeTaskManager()
    controls = FakeTaskControl()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        settings,
        tasks,  # type: ignore[arg-type]
        controls,  # type: ignore[arg-type]
        room_id_resolver=resolve,
        upload_policy_enabler=policy,
    )
    reconciler.start()
    try:
        first = await reconciler.submit_collect(100, upload=True)
        await reconciler.wait_idle()
        failed = await journal.get(first.id)
        assert failed is not None
        assert failed.status == 'failed'
        assert failed.error_code == 'UPLOAD_POLICY_FAILED'
        assert failed.result is not None
        assert failed.result['upload'] is False

        retry = await reconciler.submit_collect(100, upload=True)
        await reconciler.wait_idle()
        final = await journal.get(retry.id)

        assert retry.id != first.id
        assert retry.attempt == 2
        assert final is not None and final.status == 'succeeded'
        assert tasks.add_calls == [(100, False)]
        assert settings.dump_settings.await_count == 1  # type: ignore[attr-defined]
        assert policy.await_count == 2
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_remove_tears_down_before_one_settings_dump_and_absent_is_noop(
    tmp_path: Path,
) -> None:
    settings = make_settings_manager(Settings(tasks=[TaskSettings(room_id=100)]))
    tasks = FakeTaskManager()
    tasks.tasks[100] = object()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        settings,
        tasks,  # type: ignore[arg-type]
        FakeTaskControl(),  # type: ignore[arg-type]
        room_id_resolver=AsyncMock(side_effect=lambda room_id: room_id),
    )
    reconciler.start()
    try:
        operation = await reconciler.submit_remove([100])
        assert await reconciler.pending_removal_room_ids() == {100}
        await reconciler.wait_idle()

        final = await journal.get(operation.id)
        assert final is not None and final.status == 'succeeded'
        assert tasks.remove_calls == [100]
        assert not settings.has_task_settings(100)
        settings.dump_settings.assert_awaited_once()  # type: ignore[attr-defined]

        second = await reconciler.submit_remove([100])
        await reconciler.wait_idle()
        second_final = await journal.get(second.id)
        assert second_final is not None and second_final.status == 'succeeded'
        assert tasks.remove_calls == [100]
        assert settings.dump_settings.await_count == 1  # type: ignore[attr-defined]
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_recovered_add_observes_existing_task_before_acting(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'control.sqlite3'
    first_journal = ControlOperationJournal(path)
    await first_journal.open()
    operation = await first_journal.admit(
        lane='room-membership',
        kind='add',
        target_key='100',
        result={'requestedRoomId': 100, 'upload': False},
        steps=(
            # Resolve was durably completed before the process died.
            # The add side effect happened, but its CAS did not.
            # Recovery must observe the task instead of adding it twice.
            #
            # The explicit generation remains owned by the journal.
            ControlStepInput(key='resolve'),
            ControlStepInput(key='add'),
            ControlStepInput(key='desired-state'),
        ),
    )
    resolve_claim = await first_journal.claim_next('room-membership')
    assert resolve_claim is not None
    await first_journal.finish_step(
        resolve_claim,
        status='succeeded',
        result={'requestedRoomId': 100, 'resolvedRoomId': 100},
        operation_result={'resolvedRoomId': 100},
    )
    add_claim = await first_journal.claim_next('room-membership')
    assert add_claim is not None and add_claim.key == 'add'
    await first_journal.close()

    tasks = FakeTaskManager()
    tasks.tasks[100] = object()
    settings = make_settings_manager(Settings(tasks=[TaskSettings(room_id=100)]))
    reopened = ControlOperationJournal(path)
    await reopened.open()
    reconciler = RoomMembershipReconciler(
        reopened,
        settings,
        tasks,  # type: ignore[arg-type]
        FakeTaskControl(),  # type: ignore[arg-type]
        room_id_resolver=AsyncMock(side_effect=lambda room_id: room_id),
    )
    reconciler.start()
    try:
        await reconciler.wait_idle()
        final = await reopened.get(operation.id)
        assert final is not None and final.status == 'succeeded'
        assert tasks.add_calls == []
        settings.dump_settings.assert_not_awaited()  # type: ignore[attr-defined]
    finally:
        await reconciler.shutdown()
        await reopened.close()
