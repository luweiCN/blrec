import sqlite3
from pathlib import Path
from typing import AsyncIterator, Optional

import pytest
import pytest_asyncio

from blrec.bili_upload.account_lifecycle import (
    AccountLifecycle,
    AccountRemovalBlocked,
    AccountRemovalCommand,
    InvalidAccountReplacement,
    RemovalMode,
)
from blrec.bili_upload.database import BiliUploadDatabase


async def seed_account(
    database: BiliUploadDatabase, account_id: int, *, state: str = 'active'
) -> None:
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
        'state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (
            account_id,
            1000 + account_id,
            'account-{}'.format(account_id),
            b'credential',
            1,
            'key',
            state,
            1,
            1,
        ),
    )


async def seed_policy(
    database: BiliUploadDatabase,
    room_id: int,
    *,
    account_mode: str,
    account_id: Optional[int] = None,
) -> None:
    await database.execute(
        'INSERT INTO room_upload_policies('
        'room_id,account_mode,account_id,enabled,title_template,'
        'description_template,tid,tags,copyright,source,auto_comment,'
        'danmaku_backfill,filter_json,created_at,updated_at) '
        "VALUES(?,?,?,?,?,'description',17,'tag',1,'',0,0,'{}',1,1)",
        (room_id, account_mode, account_id, 1, 'title'),
    )


async def seed_job(
    database: BiliUploadDatabase,
    job_id: int,
    room_id: int,
    *,
    state: str = 'ready',
    submit_state: str = 'prepared',
    part_upload_state: str = None,
) -> None:
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at) VALUES(?,?,?,?,?)',
        (job_id, room_id, '{}:{}'.format(room_id, job_id), 'closed', 1),
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
        (job_id, job_id, 1, '{}', state, submit_state, 1, 1),
    )
    if part_upload_state is not None:
        await database.execute(
            'INSERT INTO upload_parts('
            'id,job_id,part_index,source_path,artifact_state,upload_state) '
            'VALUES(?,?,?,?,?,?)',
            (
                job_id,
                job_id,
                1,
                '/recordings/{}.flv'.format(job_id),
                'ready',
                part_upload_state,
            ),
        )


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_relationships_classify_rooms_and_jobs(database) -> None:
    await seed_account(database, 1)
    await seed_account(database, 2)
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )
    await seed_policy(database, 100, account_mode='fixed', account_id=1)
    await seed_policy(database, 200, account_mode='primary')
    await seed_job(database, 1, 301, state='waiting_artifacts')
    await seed_job(database, 2, 302, part_upload_state='prepared')
    await seed_job(database, 3, 303, part_upload_state='preupload')
    await seed_job(database, 4, 304, state='paused', part_upload_state='prepared')
    await database.execute(
        'INSERT INTO comment_items('
        'job_id,ordinal,kind,content,request_fingerprint,state) '
        "VALUES(4,0,'root','comment','fingerprint','confirmed')"
    )
    await seed_job(database, 5, 305, state='completed', submit_state='confirmed')
    await seed_job(database, 6, 306, state='rejected', submit_state='confirmed')

    relationships = await AccountLifecycle(database).relationships(1)
    standby_relationships = await AccountLifecycle(database).relationships(2)

    assert relationships.is_primary
    assert relationships.fixed_room_ids == (100,)
    assert relationships.follow_primary_room_ids == (200,)
    assert [job.id for job in relationships.reassignable_jobs] == [1, 2]
    assert [job.id for job in relationships.blocking_jobs] == [3, 4]
    assert relationships.historical_job_count == 2
    assert not standby_relationships.is_primary
    assert standby_relationships.follow_primary_room_ids == (200,)


@pytest.mark.asyncio
async def test_follow_primary_removal_rebinds_unstarted_jobs(database) -> None:
    await seed_account(database, 1)
    await seed_account(database, 2)
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )
    await seed_policy(database, 100, account_mode='fixed', account_id=1)
    await seed_policy(database, 200, account_mode='primary')
    await seed_job(database, 1, 301)
    await seed_job(database, 2, 302, state='completed', submit_state='confirmed')
    lifecycle = AccountLifecycle(database, clock=lambda: 123)

    result = await lifecycle.remove(
        1,
        AccountRemovalCommand(RemovalMode.FOLLOW_PRIMARY, new_primary_account_id=2),
        manager_subject='operator',
    )

    assert result.account_id == 1
    assert result.state == 'archived'
    assert (
        await database.scalar(
            'SELECT primary_account_id FROM bili_account_selection WHERE id=1'
        )
        == 2
    )
    policy = await database.fetchone(
        'SELECT account_mode,account_id FROM room_upload_policies WHERE room_id=100'
    )
    assert policy is not None
    assert dict(policy) == {'account_mode': 'primary', 'account_id': None}
    assert await database.scalar('SELECT account_id FROM upload_jobs WHERE id=1') == 2
    assert await database.scalar('SELECT account_id FROM upload_jobs WHERE id=2') == 1
    archived = await database.fetchone(
        'SELECT state,key_id,length(credential_ciphertext) AS credential_size,'
        'credential_expires_at FROM bili_accounts WHERE id=1'
    )
    assert archived is not None
    assert dict(archived) == {
        'state': 'archived',
        'key_id': 'archived',
        'credential_size': 0,
        'credential_expires_at': 0,
    }
    assert (
        await database.scalar(
            "SELECT COUNT(*) FROM management_audit WHERE action='remove_bili_account'"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_fixed_removal_rebinds_affected_rooms_to_explicit_account(
    database,
) -> None:
    await seed_account(database, 1)
    await seed_account(database, 2)
    await seed_account(database, 3)
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )
    await seed_policy(database, 100, account_mode='fixed', account_id=1)
    await seed_policy(database, 200, account_mode='primary')
    await seed_job(database, 1, 301)

    await AccountLifecycle(database).remove(
        1,
        AccountRemovalCommand(
            RemovalMode.FIXED, replacement_account_id=2, new_primary_account_id=3
        ),
        manager_subject='operator',
    )

    policies = await database.fetchall(
        'SELECT room_id,account_mode,account_id FROM room_upload_policies '
        'ORDER BY room_id'
    )
    assert [dict(row) for row in policies] == [
        {'room_id': 100, 'account_mode': 'fixed', 'account_id': 2},
        {'room_id': 200, 'account_mode': 'fixed', 'account_id': 2},
    ]
    assert await database.scalar('SELECT account_id FROM upload_jobs WHERE id=1') == 2
    assert (
        await database.scalar(
            'SELECT primary_account_id FROM bili_account_selection WHERE id=1'
        )
        == 3
    )


@pytest.mark.asyncio
async def test_disable_is_the_only_removal_mode_without_another_account(
    database,
) -> None:
    await seed_account(database, 1)
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )
    await seed_policy(database, 100, account_mode='fixed', account_id=1)
    await seed_policy(database, 200, account_mode='primary')
    await seed_job(database, 1, 301)
    lifecycle = AccountLifecycle(database)

    with pytest.raises(InvalidAccountReplacement):
        await lifecycle.remove(
            1,
            AccountRemovalCommand(RemovalMode.FOLLOW_PRIMARY),
            manager_subject='operator',
        )

    await lifecycle.remove(
        1, AccountRemovalCommand(RemovalMode.DISABLE), manager_subject='operator'
    )

    assert await database.scalar('SELECT COUNT(*) FROM bili_account_selection') == 0
    assert (
        await database.scalar(
            'SELECT COUNT(*) FROM room_upload_policies WHERE enabled=0'
        )
        == 2
    )
    job = await database.fetchone(
        'SELECT account_id,state,review_reason FROM upload_jobs WHERE id=1'
    )
    assert job is not None
    assert dict(job) == {
        'account_id': 1,
        'state': 'paused',
        'review_reason': 'upload account removed; select an account before resuming',
    }


@pytest.mark.asyncio
async def test_blocking_job_rejects_removal_without_partial_changes(database) -> None:
    await seed_account(database, 1)
    await seed_account(database, 2)
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )
    await seed_policy(database, 100, account_mode='fixed', account_id=1)
    await seed_job(database, 1, 301, part_upload_state='preupload')

    with pytest.raises(AccountRemovalBlocked) as captured:
        await AccountLifecycle(database).remove(
            1,
            AccountRemovalCommand(RemovalMode.FOLLOW_PRIMARY, new_primary_account_id=2),
            manager_subject='operator',
        )

    assert [job.id for job in captured.value.jobs] == [1]
    assert await database.scalar('SELECT state FROM bili_accounts WHERE id=1') == (
        'active'
    )
    assert (
        await database.scalar(
            'SELECT account_mode FROM room_upload_policies WHERE room_id=100'
        )
        == 'fixed'
    )
    assert await database.scalar('SELECT account_id FROM upload_jobs WHERE id=1') == 1


@pytest.mark.asyncio
async def test_removal_rolls_back_all_relationship_changes_on_database_error(
    database,
) -> None:
    await seed_account(database, 1)
    await seed_account(database, 2)
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )
    await seed_policy(database, 100, account_mode='fixed', account_id=1)
    await seed_job(database, 1, 301)
    await database.execute(
        "CREATE TRIGGER reject_archive BEFORE UPDATE OF state ON bili_accounts "
        "WHEN NEW.state='archived' BEGIN SELECT RAISE(ABORT,'reject'); END"
    )

    with pytest.raises(sqlite3.IntegrityError):
        await AccountLifecycle(database).remove(
            1,
            AccountRemovalCommand(RemovalMode.FOLLOW_PRIMARY, new_primary_account_id=2),
            manager_subject='operator',
        )

    assert (
        await database.scalar(
            'SELECT primary_account_id FROM bili_account_selection WHERE id=1'
        )
        == 1
    )
    assert (
        await database.scalar(
            'SELECT account_mode FROM room_upload_policies WHERE room_id=100'
        )
        == 'fixed'
    )
    assert await database.scalar('SELECT account_id FROM upload_jobs WHERE id=1') == 1
