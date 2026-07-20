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
        creation_statement_id=-1,
        original_authorization=True,
        source='',
        is_only_self=False,
        publish_dynamic=True,
        up_selection_reply=False,
        up_close_reply=False,
        up_close_danmu=False,
        auto_comment=False,
        danmaku_backfill=False,
        filters={'blockedWords': []},
    )
    values.update(overrides)
    return RoomUploadPolicyCommand(**values)


def track_database_reads(database: BiliUploadDatabase, monkeypatch):
    calls = {'fetchall': 0, 'fetchone': 0}
    fetchall = database.fetchall
    fetchone = database.fetchone

    async def tracked_fetchall(*args, **kwargs):
        calls['fetchall'] += 1
        return await fetchall(*args, **kwargs)

    async def tracked_fetchone(*args, **kwargs):
        calls['fetchone'] += 1
        return await fetchone(*args, **kwargs)

    monkeypatch.setattr(database, 'fetchall', tracked_fetchall)
    monkeypatch.setattr(database, 'fetchone', tracked_fetchone)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize('policy_count', (1, 20, 100))
async def test_policy_list_resolves_accounts_with_constant_query_budget(
    tmp_path: Path, monkeypatch, policy_count: int
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        manager = RoomUploadPolicyManager(database, clock=lambda: 1000)
        await manager.upsert(1, command())
        for room_id in range(2, policy_count + 1):
            await manager.upsert(room_id, command(account_mode='fixed', account_id=2))
        calls = track_database_reads(database, monkeypatch)

        policies = await manager.list()

        assert len(policies) == policy_count
        assert policies[0].resolved_account_name == '账号1'
        assert policies[-1].resolved_account_name == (
            '账号1' if policy_count == 1 else '账号2'
        )
        assert calls == {'fetchall': 1, 'fetchone': 0}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_policy_get_resolves_account_with_one_query(
    tmp_path: Path, monkeypatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        manager = RoomUploadPolicyManager(database, clock=lambda: 1000)
        await manager.upsert(100, command(account_mode='fixed', account_id=2))
        calls = track_database_reads(database, monkeypatch)

        policy = await manager.get(100)

        assert policy.resolved_account_id == 2
        assert policy.resolved_account_name == '账号2'
        assert calls == {'fetchall': 0, 'fetchone': 1}
    finally:
        await database.close()


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
async def test_primary_policy_remains_visible_without_a_primary_selection(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        manager = RoomUploadPolicyManager(database, clock=lambda: 1000)
        await manager.upsert(100, command())
        await database.execute('DELETE FROM bili_account_selection WHERE id=1')

        listed = (await manager.list())[0]
        fetched = await manager.get(100)

        for policy in (listed, fetched):
            assert policy.resolved_account_id is None
            assert policy.resolved_account_name is None
            assert policy.blocked_reason == '未找到可用的投稿账号'
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('account_mode', 'account_id', 'resolved_account_id', 'state'),
    (('primary', None, 1, 'paused'), ('fixed', 2, 2, 'archived')),
)
async def test_policy_keeps_resolved_account_identity_when_account_becomes_unavailable(
    tmp_path: Path, account_mode: str, account_id, resolved_account_id: int, state: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        manager = RoomUploadPolicyManager(database, clock=lambda: 1000)
        await manager.upsert(
            100, command(account_mode=account_mode, account_id=account_id)
        )
        await database.execute(
            'UPDATE bili_accounts SET state=? WHERE id=?', (state, resolved_account_id)
        )

        policy = await manager.get(100)

        assert policy.resolved_account_id == resolved_account_id
        assert policy.resolved_account_name == '账号{}'.format(resolved_account_id)
        assert policy.blocked_reason == '投稿账号当前不可用'
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('filter_json', ('{', '[]'))
async def test_corrupt_policy_filters_take_precedence_over_account_state(
    tmp_path: Path, filter_json: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        manager = RoomUploadPolicyManager(database, clock=lambda: 1000)
        await manager.upsert(100, command(account_mode='fixed', account_id=2))
        await database.execute("UPDATE bili_accounts SET state='paused' WHERE id=2")
        await database.execute(
            'UPDATE room_upload_policies SET filter_json=? WHERE room_id=100',
            (filter_json,),
        )

        policy = await manager.get(100)

        assert policy.resolved_account_id == 2
        assert policy.filters == {}
        assert policy.blocked_reason == '过滤设置损坏'
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
                original_authorization=False,
                up_selection_reply=True,
            ),
        )

        assert policy.part_title_template == '第 {{ part_index }} P'
        assert policy.dynamic_template == '{{ title }}｜{{ anchor_name }}'
        assert policy.is_only_self is True
        assert policy.publish_dynamic is False
        assert policy.creation_statement_id == -1
        assert policy.original_authorization is False
        assert policy.copyright == 3
        assert policy.no_reprint is False
        assert policy.up_selection_reply is True
        assert policy.up_close_reply is False
        assert policy.up_close_danmu is False
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_policy_round_trips_collection_cover_and_schedule_settings(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        await database.execute(
            'INSERT INTO cover_assets('
            'id,sha256,storage_path,filename,mime_type,width,height,byte_size,'
            'created_at,updated_at) VALUES(7,?,?,?,?,?,?,?,?,?)',
            ('a' * 64, '/covers/a.jpg', '封面.jpg', 'image/jpeg', 1600, 1000, 10, 1, 1),
        )
        manager = RoomUploadPolicyManager(database, clock=lambda: 1000)

        policy = await manager.upsert(
            100,
            command(
                collection_season_id=20,
                collection_section_id=21,
                cover_mode='custom',
                cover_asset_id=7,
                publish_delay_seconds=7200,
                retention_mode='approved',
                retention_days=14,
            ),
        )

        assert policy.collection_season_id == 20
        assert policy.collection_section_id == 21
        assert policy.cover_mode == 'custom'
        assert policy.cover_asset_id == 7
        assert policy.publish_delay_seconds == 7200
        assert policy.retention_mode == 'approved'
        assert policy.retention_days == 14
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('overrides', 'message'),
    (
        ({'collection_season_id': 20}, 'collection'),
        ({'collection_section_id': 21}, 'collection'),
        ({'cover_mode': 'custom', 'cover_asset_id': None}, 'cover'),
        ({'cover_mode': 'live', 'cover_asset_id': 7}, 'cover'),
        ({'publish_delay_seconds': 3600}, 'publish delay'),
        ({'publish_delay_seconds': 15 * 24 * 60 * 60 + 1}, 'publish delay'),
        ({'retention_mode': 'invalid'}, 'retention mode'),
        ({'retention_days': -1}, 'retention days'),
        ({'retention_days': 3651}, 'retention days'),
    ),
)
async def test_policy_rejects_invalid_collection_cover_and_schedule_settings(
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('overrides', 'message'),
    (
        ({'creation_statement_id': -2, 'source': ''}, 'source is required'),
        (
            {
                'creation_statement_id': -2,
                'source': 'https://live.bilibili.com/100',
                'original_authorization': True,
            },
            'mutually exclusive',
        ),
    ),
)
async def test_policy_rejects_invalid_creation_statement_combinations(
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
