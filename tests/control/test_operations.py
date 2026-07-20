from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from blrec.control.operations import (
    ControlJournalClosed,
    ControlLaneSaturated,
    ControlOperationJournal,
    ControlStepInput,
)


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
