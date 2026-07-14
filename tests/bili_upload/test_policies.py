from pathlib import Path

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.policies import (
    InvalidRoomUploadPolicy,
    RoomUploadPolicyCommand,
    RoomUploadPolicyManager,
)


async def seed_accounts(database: BiliUploadDatabase) -> None:
    for account_id, state in ((1, 'active'), (2, 'active'), (3, 'paused')):
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) VALUES(?,?,?,X\'00\',1,\'k\',?,?,?)',
            (account_id, 40 + account_id, '账号{}'.format(account_id), state, 1, 1),
        )
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )


def command(*, account_mode: str = 'primary', account_id=None, **overrides):
    values = dict(
        account_mode=account_mode,
        account_id=account_id,
        enabled=True,
        title_template='{{ title }} 录播',
        description_template='主播：{{ anchor_name }}',
        part_title_template='第 {{ part_index }} P',
        dynamic_template='{{ title }}｜{{ anchor_name }}',
        tid=17,
        tags='直播,录播',
        copyright=1,
        source='',
        is_only_self=False,
        publish_dynamic=True,
        no_reprint=True,
        up_selection_reply=False,
        up_close_reply=False,
        up_close_danmu=False,
        auto_comment=False,
        danmaku_backfill=False,
        filters={'blockedWords': []},
    )
    values.update(overrides)
    return RoomUploadPolicyCommand(**values)


@pytest.mark.asyncio
async def test_primary_policy_follows_primary_account_until_job_creation(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        manager = RoomUploadPolicyManager(database, clock=lambda: 1000)

        created = await manager.upsert(100, command())
        await database.execute(
            'UPDATE bili_account_selection SET primary_account_id=2 WHERE id=1'
        )
        changed = (await manager.list())[0]

        assert created.account_mode == 'primary'
        assert created.account_id is None
        assert created.resolved_account_id == 1
        assert changed.resolved_account_id == 2
        assert changed.resolved_account_name == '账号2'
        assert changed.blocked_reason is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_fixed_policy_stays_bound_to_selected_account(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        manager = RoomUploadPolicyManager(database, clock=lambda: 1000)

        await manager.upsert(100, command(account_mode='fixed', account_id=2))
        await database.execute(
            'UPDATE bili_account_selection SET primary_account_id=1 WHERE id=1'
        )
        policy = await manager.get(100)

        assert policy.account_id == 2
        assert policy.resolved_account_id == 2
        assert policy.resolved_account_name == '账号2'
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_policy_rejects_inactive_or_mismatched_account_selection(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        manager = RoomUploadPolicyManager(database)

        with pytest.raises(InvalidRoomUploadPolicy, match='active'):
            await manager.upsert(100, command(account_mode='fixed', account_id=3))
        with pytest.raises(InvalidRoomUploadPolicy, match='accountId'):
            await manager.upsert(100, command(account_mode='primary', account_id=1))
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_policy_delete_only_affects_future_upload_jobs(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        manager = RoomUploadPolicyManager(database)
        await manager.upsert(100, command())

        await manager.delete(100)

        assert await manager.list() == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_policy_round_trips_archive_submission_settings(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        manager = RoomUploadPolicyManager(database, clock=lambda: 1000)

        policy = await manager.upsert(
            100,
            command(
                is_only_self=True,
                publish_dynamic=False,
                no_reprint=False,
                up_selection_reply=True,
            ),
        )

        assert policy.part_title_template == '第 {{ part_index }} P'
        assert policy.dynamic_template == '{{ title }}｜{{ anchor_name }}'
        assert policy.is_only_self is True
        assert policy.publish_dynamic is False
        assert policy.no_reprint is False
        assert policy.up_selection_reply is True
        assert policy.up_close_reply is False
        assert policy.up_close_danmu is False
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('overrides', 'message'),
    (
        ({'auto_comment': True, 'up_close_reply': True}, 'comments must remain open'),
        (
            {'danmaku_backfill': True, 'up_close_danmu': True},
            'danmaku must remain open',
        ),
        (
            {'up_selection_reply': True, 'up_close_reply': True},
            'selected comments require open comments',
        ),
    ),
)
async def test_policy_rejects_conflicting_interaction_settings(
    tmp_path: Path, overrides, message: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        manager = RoomUploadPolicyManager(database)

        with pytest.raises(InvalidRoomUploadPolicy, match=message):
            await manager.upsert(100, command(**overrides))
    finally:
        await database.close()
