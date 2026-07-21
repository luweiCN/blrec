from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from blrec.control.operations import (
    ControlJournalClosed,
    ControlJournalError,
    ControlLaneSaturated,
    ControlOperationJournal,
    ControlStepInput,
)


@pytest.mark.asyncio
async def test_journal_wraps_storage_errors(tmp_path: Path) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()

    def fail_queued_count(_lane: str) -> int:
        raise sqlite3.OperationalError('database unavailable')

    journal._queued_count_sync = fail_queued_count  # type: ignore[method-assign]
    try:
        with pytest.raises(ControlJournalError) as raised:
            await journal.queued_count('task-state')
        assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_membership_remove_waits_for_older_short_room_add(tmp_path: Path) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    older = await journal.admit(
        lane='room-membership',
        kind='add',
        target_key='6',
        result={'requestedRoomId': 6},
        steps=[
            ControlStepInput(key='resolve'),
            ControlStepInput(key='add'),
            ControlStepInput(key='desired-state'),
        ],
    )
    later = await journal.admit(
        lane='room-membership',
        kind='remove',
        target_key='3582149',
        result={'roomIds': [3582149]},
        steps=[
            ControlStepInput(key='desired-absent'),
            ControlStepInput(key='teardown:3582149'),
            ControlStepInput(key='settings'),
        ],
    )
    try:
        resolve = await journal.claim_next('room-membership')
        assert resolve is not None
        assert resolve.operation_id == older.id and resolve.key == 'resolve'
        assert await journal.claim_next('room-membership') is None

        await journal.finish_step(
            resolve,
            status='succeeded',
            result={'requestedRoomId': 6, 'resolvedRoomId': 3582149},
            operation_result={'resolvedRoomId': 3582149},
        )
        add = await journal.claim_next('room-membership')
        assert add is not None
        assert add.operation_id == older.id and add.key == 'add'
        assert await journal.claim_next('room-membership') is None

        await journal.finish_step(add, status='succeeded')
        desired = await journal.claim_next('room-membership')
        assert desired is not None
        assert desired.operation_id == older.id and desired.key == 'desired-state'
        assert await journal.claim_next('room-membership') is None

        await journal.finish_step(desired, status='succeeded')
        removal = await journal.claim_next('room-membership')
        assert removal is not None
        assert removal.operation_id == later.id and removal.key == 'desired-absent'
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_membership_keeps_different_resolved_rooms_concurrent(
    tmp_path: Path,
) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    older = await journal.admit(
        lane='room-membership',
        kind='add',
        target_key='6',
        result={'requestedRoomId': 6},
        steps=[ControlStepInput(key='resolve'), ControlStepInput(key='add')],
    )
    later = await journal.admit(
        lane='room-membership',
        kind='remove',
        target_key='200',
        result={'roomIds': [200]},
        steps=[ControlStepInput(key='desired-absent')],
    )
    try:
        resolve = await journal.claim_next('room-membership')
        assert resolve is not None
        await journal.finish_step(
            resolve,
            status='succeeded',
            result={'requestedRoomId': 6, 'resolvedRoomId': 100},
            operation_result={'resolvedRoomId': 100},
        )

        add = await journal.claim_next('room-membership')
        removal = await journal.claim_next('room-membership')
        assert add is not None and add.operation_id == older.id
        assert removal is not None and removal.operation_id == later.id
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_reopen_backfills_durable_admission_order(tmp_path: Path) -> None:
    path = tmp_path / 'control.sqlite3'
    journal = ControlOperationJournal(path)
    await journal.open()
    membership = await journal.admit(
        lane='room-membership',
        kind='collect',
        target_key='100:0',
        steps=[ControlStepInput(key='desired-state')],
    )
    await journal.admit(
        lane='task-state',
        kind='stop',
        target_key='100',
        steps=[ControlStepInput(key='100')],
    )
    await journal.close()

    with sqlite3.connect(str(path)) as connection:
        connection.execute('DROP TABLE control_operation_admissions')

    reopened = ControlOperationJournal(path)
    await reopened.open()
    try:
        assert await reopened.has_later_task_state_intent(membership.id, 100)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_revision_operation_chases_a_new_desired_revision(tmp_path: Path) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    try:
        first = await journal.submit_revision(
            lane='settings-apply',
            kind='apply',
            target_key='settings:header',
            action='apply',
        )
        claim = await journal.claim_next('settings-apply')
        assert claim is not None
        revision = await journal.get_revision('settings-apply', 'settings:header')
        assert revision is not None and revision.desired_revision == 1

        second = await journal.submit_revision(
            lane='settings-apply',
            kind='apply',
            target_key='settings:header',
            action='apply',
        )
        assert second.id == first.id
        assert not await journal.finish_revision_step(claim, applied_revision=1)

        pending = await journal.get(first.id)
        assert pending is not None and pending.status == 'accepted'
        second_claim = await journal.claim_next('settings-apply')
        assert second_claim is not None
        revision = await journal.get_revision('settings-apply', 'settings:header')
        assert revision is not None and revision.desired_revision == 2
        assert await journal.finish_revision_step(second_claim, applied_revision=2)

        complete = await journal.get(first.id)
        assert complete is not None and complete.status == 'succeeded'
        revision = await journal.get_revision('settings-apply', 'settings:header')
        assert revision is not None and revision.applied_revision == 2
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_revision_gap_is_recovered_after_restart(tmp_path: Path) -> None:
    path = tmp_path / 'control.sqlite3'
    journal = ControlOperationJournal(path)
    await journal.open()
    operation = await journal.submit_revision(
        lane='settings-apply',
        kind='apply',
        target_key='settings:live_monitor',
        action='apply',
    )
    claim = await journal.claim_next('settings-apply')
    assert claim is not None
    await journal.close()

    reopened = ControlOperationJournal(path)
    await reopened.open()
    try:
        recovered = await reopened.recover_revision_gaps(
            lane='settings-apply', kind='apply'
        )
        assert [item.id for item in recovered] == [operation.id]
        recovered_claim = await reopened.claim_next('settings-apply')
        assert recovered_claim is not None
        revision = await reopened.get_revision(
            'settings-apply', 'settings:live_monitor'
        )
        assert revision is not None and revision.desired_revision == 1
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_failed_revision_attempt_keeps_gap_for_recovery(tmp_path: Path) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    try:
        first = await journal.submit_revision(
            lane='settings-apply',
            kind='apply',
            target_key='settings:logging',
            action='apply',
        )
        claim = await journal.claim_next('settings-apply')
        assert claim is not None
        await journal.finish_step(
            claim, status='failed', error_code='SETTINGS_APPLY_FAILED'
        )

        recovered = await journal.recover_revision_gaps(
            lane='settings-apply', kind='apply'
        )

        assert len(recovered) == 1
        assert recovered[0].id != first.id
        revision = await journal.get_revision('settings-apply', 'settings:logging')
        assert revision is not None
        assert revision.desired_revision == 1
        assert revision.applied_revision == 0
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_unassigned_gap_recovery_does_not_retry_a_failed_apply(
    tmp_path: Path,
) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    try:
        operation = await journal.submit_revision(
            lane='settings-apply',
            kind='apply',
            target_key='settings:logging',
            action='apply',
        )
        claim = await journal.claim_next('settings-apply')
        assert claim is not None
        await journal.finish_step(
            claim, status='failed', error_code='SETTINGS_APPLY_FAILED'
        )

        recovered = await journal.recover_revision_gaps(
            lane='settings-apply', kind='apply', unassigned_only=True
        )

        assert recovered == ()
        assert await journal.get(operation.id) is not None
        assert await journal.claim_next('settings-apply') is None
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_reserved_revisions_survive_lane_saturation_and_recover_in_order(
    tmp_path: Path,
) -> None:
    journal = ControlOperationJournal(
        tmp_path / 'control.sqlite3', max_nonterminal_per_lane=1
    )
    await journal.open()
    try:
        blocker = await journal.admit(
            lane='settings-apply',
            kind='blocker',
            target_key='blocker',
            steps=[ControlStepInput(key='blocker')],
        )

        reserved = await journal.reserve_revisions(
            lane='settings-apply',
            kind='apply',
            revisions=(
                ('settings:header', 'apply'),
                ('settings:live_monitor', 'apply'),
            ),
        )

        assert [item.desired_revision for item in reserved] == [1, 1]
        assert [item.operation_id for item in reserved] == [None, None]
        assert (
            await journal.recover_revision_gaps(lane='settings-apply', kind='apply')
            == ()
        )

        blocker_claim = await journal.claim_next('settings-apply')
        assert blocker_claim is not None
        assert blocker_claim.operation_id == blocker.id
        await journal.finish_step(blocker_claim, status='succeeded')

        recovered = await journal.recover_revision_gaps(
            lane='settings-apply', kind='apply'
        )
        assert len(recovered) == 1
        first_claim = await journal.claim_next('settings-apply')
        assert first_claim is not None
        first_revision = await journal.get_revision('settings-apply', first_claim.key)
        assert first_revision is not None
        assert await journal.finish_revision_step(
            first_claim, applied_revision=first_revision.desired_revision
        )

        recovered = await journal.recover_revision_gaps(
            lane='settings-apply', kind='apply'
        )
        assert len(recovered) == 1
        second_claim = await journal.claim_next('settings-apply')
        assert second_claim is not None
        assert second_claim.key != first_claim.key
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_journal_is_private_durable_and_uses_full_delete_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'control.sqlite3'
    journal = ControlOperationJournal(path)

    await journal.open()
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert await journal.pragma('journal_mode') == 'delete'
        assert await journal.pragma('synchronous') == 2
        operation = await journal.admit(
            lane='task-state',
            kind='start',
            target_key='100',
            steps=[ControlStepInput(key='100')],
        )
    finally:
        await journal.close()

    reopened = ControlOperationJournal(path)
    await reopened.open()
    try:
        recovered = await reopened.get(operation.id)
        assert recovered is not None
        assert recovered.status == 'accepted'
        assert recovered.steps[0].status == 'queued'
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_journal_deduplicates_nonterminal_and_retries_failed_as_new_attempt(
    tmp_path: Path,
) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    try:
        first = await journal.admit(
            lane='task-state',
            kind='start',
            target_key='100',
            steps=[ControlStepInput(key='100')],
        )
        duplicate = await journal.admit(
            lane='task-state',
            kind='start',
            target_key='100',
            steps=[ControlStepInput(key='100')],
        )
        assert duplicate.id == first.id

        claim = await journal.claim_next('task-state')
        assert claim is not None
        await journal.finish_step(
            claim, status='failed', error_code='TASK_LIFECYCLE_FAILED'
        )
        failed = await journal.get(first.id)
        assert failed is not None
        assert failed.status == 'failed'

        retry = await journal.admit(
            lane='task-state',
            kind='start',
            target_key='100',
            steps=[ControlStepInput(key='100')],
        )
        assert retry.id != first.id
        assert retry.attempt == 2
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_journal_can_idempotently_admit_a_cross_database_operation_id(
    tmp_path: Path,
) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    try:
        first = await journal.admit(
            operation_id='upload-retry-operation-1',
            lane='upload-retry',
            kind='retry-failed',
            target_key='upload-retry-operation-1',
            steps=[ControlStepInput(key='quantum:0')],
            result={'processed': 0, 'total': 201},
        )
        duplicate = await journal.admit(
            operation_id='upload-retry-operation-1',
            lane='upload-retry',
            kind='retry-failed',
            target_key='upload-retry-operation-1',
            steps=[ControlStepInput(key='quantum:0')],
            result={'processed': 0, 'total': 201},
        )

        assert first.id == duplicate.id == 'upload-retry-operation-1'
        assert await journal.queued_count('upload-retry') == 1
        with pytest.raises(ValueError, match='different control operation'):
            await journal.admit(
                operation_id='upload-retry-operation-1',
                lane='other-lane',
                kind='retry-failed',
                target_key='upload-retry-operation-1',
                steps=[ControlStepInput(key='quantum:0')],
            )
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_journal_limits_each_lane_to_one_hundred_nonterminal_operations(
    tmp_path: Path,
) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    try:
        for room_id in range(100):
            await journal.admit(
                lane='task-state',
                kind='start',
                target_key=str(room_id),
                steps=[ControlStepInput(key=str(room_id))],
            )

        with pytest.raises(ControlLaneSaturated):
            await journal.admit(
                lane='task-state',
                kind='start',
                target_key='overflow',
                steps=[ControlStepInput(key='overflow')],
            )
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_journal_preserves_rejected_items_and_terminal_results(
    tmp_path: Path,
) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    try:
        operation = await journal.admit(
            lane='task-state',
            kind='start',
            target_key='batch:100,404',
            steps=[
                ControlStepInput(key='100'),
                ControlStepInput(
                    key='404', status='rejected', error_code='TASK_NOT_FOUND'
                ),
            ],
        )
        claim = await journal.claim_next('task-state')
        assert claim is not None
        await journal.finish_step(
            claim, status='succeeded', result={'roomId': 100, 'changed': True}
        )

        final = await journal.get(operation.id)
        assert final is not None
        assert final.status == 'failed'
        assert [(step.key, step.status, step.error_code) for step in final.steps] == [
            ('100', 'succeeded', None),
            ('404', 'rejected', 'TASK_NOT_FOUND'),
        ]
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_running_steps_are_requeued_after_restart(tmp_path: Path) -> None:
    path = tmp_path / 'control.sqlite3'
    journal = ControlOperationJournal(path)
    await journal.open()
    operation = await journal.admit(
        lane='task-state',
        kind='start',
        target_key='100',
        steps=[ControlStepInput(key='100')],
    )
    claim = await journal.claim_next('task-state')
    assert claim is not None
    await journal.close()

    reopened = ControlOperationJournal(path)
    await reopened.open()
    try:
        recovered_claim = await reopened.claim_next('task-state')
        assert recovered_claim is not None
        assert recovered_claim.operation_id == operation.id
        assert recovered_claim.generation == claim.generation
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_closed_admission_rejects_new_operations(tmp_path: Path) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    journal.close_admission()
    try:
        with pytest.raises(ControlJournalClosed):
            await journal.admit(
                lane='task-state',
                kind='start',
                target_key='100',
                steps=[ControlStepInput(key='100')],
            )
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_failed_preparation_terminates_unclaimed_operation_durably(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'control.sqlite3'
    journal = ControlOperationJournal(path, max_nonterminal_per_lane=1)
    await journal.open()
    operation = await journal.admit(
        lane='task-state',
        kind='start',
        target_key='100,404',
        steps=[
            ControlStepInput(key='100'),
            ControlStepInput(key='404', status='rejected', error_code='TASK_NOT_FOUND'),
        ],
    )

    try:
        assert await journal.fail_unclaimed_operation(
            operation.id, error_code='SETTINGS_PERSIST_FAILED'
        )
        assert await journal.queued_count('task-state') == 0
        assert await journal.claim_next('task-state') is None

        failed = await journal.get(operation.id)
        assert failed is not None
        assert failed.status == 'failed'
        assert failed.error_code == 'SETTINGS_PERSIST_FAILED'
        assert [(step.key, step.status, step.error_code) for step in failed.steps] == [
            ('100', 'failed', 'SETTINGS_PERSIST_FAILED'),
            ('404', 'rejected', 'TASK_NOT_FOUND'),
        ]
        replacement = await journal.admit(
            lane='task-state',
            kind='start',
            target_key='200',
            steps=[ControlStepInput(key='200')],
        )
        assert replacement.status == 'accepted'
        assert await journal.fail_unclaimed_operation(
            replacement.id, error_code='TEST_CLEANUP'
        )
    finally:
        await journal.close()

    reopened = ControlOperationJournal(path)
    await reopened.open()
    try:
        assert await reopened.queued_count('task-state') == 0
        assert await reopened.claim_next('task-state') is None
        failed = await reopened.get(operation.id)
        assert failed is not None
        assert failed.status == 'failed'
        assert failed.error_code == 'SETTINGS_PERSIST_FAILED'
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_reopen_prunes_only_expired_terminal_operations(tmp_path: Path) -> None:
    now = [1.0]
    path = tmp_path / 'control.sqlite3'
    journal = ControlOperationJournal(path, clock=lambda: now[0])
    await journal.open()
    terminal = await journal.admit(
        lane='task-state',
        kind='start',
        target_key='terminal',
        steps=[ControlStepInput(key='100')],
    )
    claim = await journal.claim_next('task-state')
    assert claim is not None
    await journal.finish_step(claim, status='succeeded')
    pending = await journal.admit(
        lane='task-state',
        kind='start',
        target_key='pending',
        steps=[ControlStepInput(key='200')],
    )
    await journal.close()

    now[0] += 31 * 24 * 60 * 60
    reopened = ControlOperationJournal(path, clock=lambda: now[0])
    await reopened.open()
    try:
        assert await reopened.get(terminal.id) is None
        assert await reopened.get(pending.id) is not None
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_failed_step_and_dependents_commit_in_one_durable_transaction(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'control.sqlite3'
    journal = ControlOperationJournal(path)
    await journal.open()
    operation = await journal.admit(
        lane='room-membership',
        kind='collect',
        target_key='100:1',
        steps=[
            ControlStepInput(key='add'),
            ControlStepInput(key='desired-state'),
            ControlStepInput(key='policy'),
        ],
    )
    claim = await journal.claim_next('room-membership')
    assert claim is not None and claim.key == 'add'

    assert await journal.fail_step_and_dependents(
        claim, error_code='TASK_ADD_FAILED', dependent_error_code='DEPENDENCY_FAILED'
    )
    await journal.close()

    reopened = ControlOperationJournal(path)
    await reopened.open()
    try:
        failed = await reopened.get(operation.id)
        assert failed is not None and failed.status == 'failed'
        assert [(step.key, step.status, step.error_code) for step in failed.steps] == [
            ('add', 'failed', 'TASK_ADD_FAILED'),
            ('desired-state', 'failed', 'DEPENDENCY_FAILED'),
            ('policy', 'failed', 'DEPENDENCY_FAILED'),
        ]
        assert await reopened.claim_next('room-membership') is None
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_membership_claim_waits_for_the_previous_step(tmp_path: Path) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    try:
        await journal.admit(
            lane='room-membership',
            kind='collect',
            target_key='100:0',
            steps=[
                ControlStepInput(key='resolve'),
                ControlStepInput(key='add'),
                ControlStepInput(key='desired-state'),
            ],
        )

        resolve = await journal.claim_next('room-membership')

        assert resolve is not None and resolve.key == 'resolve'
        assert await journal.claim_next('room-membership') is None
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_reopen_terminalizes_a_legacy_split_membership_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'control.sqlite3'
    journal = ControlOperationJournal(path)
    await journal.open()
    operation = await journal.admit(
        lane='room-membership',
        kind='add',
        target_key='100',
        steps=[ControlStepInput(key='add'), ControlStepInput(key='desired-state')],
    )
    claim = await journal.claim_next('room-membership')
    assert claim is not None
    await journal.finish_step(claim, status='failed', error_code='TASK_ADD_FAILED')
    await journal.close()

    reopened = ControlOperationJournal(path)
    await reopened.open()
    try:
        failed = await reopened.get(operation.id)
        assert failed is not None and failed.status == 'failed'
        assert [(step.status, step.error_code) for step in failed.steps] == [
            ('failed', 'TASK_ADD_FAILED'),
            ('failed', 'DEPENDENCY_FAILED'),
        ]
        assert await reopened.claim_next('room-membership') is None
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_reopen_still_prunes_expired_terminal_membership_operations(
    tmp_path: Path,
) -> None:
    now = [1.0]
    path = tmp_path / 'control.sqlite3'
    journal = ControlOperationJournal(path, clock=lambda: now[0])
    await journal.open()
    operation = await journal.admit(
        lane='room-membership',
        kind='add',
        target_key='100',
        steps=[ControlStepInput(key='add')],
    )
    claim = await journal.claim_next('room-membership')
    assert claim is not None
    await journal.fail_step_and_dependents(
        claim, error_code='TASK_ADD_FAILED', dependent_error_code='DEPENDENCY_FAILED'
    )
    await journal.close()

    now[0] += 31 * 24 * 60 * 60
    reopened = ControlOperationJournal(path, clock=lambda: now[0])
    await reopened.open()
    try:
        assert await reopened.get(operation.id) is None
    finally:
        await reopened.close()
