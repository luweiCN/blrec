from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.policies import RoomUploadPolicyCommand, RoomUploadPolicyManager
from blrec.bili_upload.session_submission import (
    SessionSubmissionLocked,
    SessionSubmissionManager,
)


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


def command(**overrides) -> RoomUploadPolicyCommand:
    values = dict(
        account_mode='primary',
        account_id=None,
        enabled=True,
        title_template='{{ title }} 录播',
        description_template='主播：{{ anchor_name }}',
        part_title_template='P{{ part_index }}',
        dynamic_template='{{ title }}',
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


async def seed(database: BiliUploadDatabase) -> None:
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
        'state,created_at,updated_at) '
        "VALUES(1,42,'投稿账号',X'00',1,'key','active',1,1)"
    )
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at) '
        "VALUES(1,100,'100:1','open',1)"
    )


@pytest.mark.asyncio
async def test_session_submission_inherits_room_policy_until_explicit_save(
    database: BiliUploadDatabase,
) -> None:
    await seed(database)
    policy_manager = RoomUploadPolicyManager(database, clock=lambda: 10)
    await policy_manager.upsert(100, command(title_template='房间标题'))
    manager = SessionSubmissionManager(
        database, policy_manager=policy_manager, clock=lambda: 20
    )

    inherited = await manager.get(1)
    assert inherited.decision == 'follow_room'
    assert inherited.inherited is True
    assert inherited.settings.title_template == '房间标题'

    saved = await manager.save_override(
        1,
        command(title_template='本场标题', is_only_self=True),
        manager_subject='administrator',
    )
    assert saved.inherited is False
    assert saved.settings.title_template == '本场标题'
    assert saved.settings.is_only_self is True

    restored = await manager.clear_override(1, manager_subject='administrator')
    assert restored.inherited is True
    assert restored.settings.title_template == '房间标题'

    actions = await database.fetchall('SELECT action FROM management_audit ORDER BY id')
    assert [str(row['action']) for row in actions] == [
        'save_session_submission_override',
        'clear_session_submission_override',
    ]


@pytest.mark.asyncio
async def test_media_library_submission_is_always_manual_deletion(
    database: BiliUploadDatabase,
) -> None:
    await seed(database)
    await database.execute(
        'INSERT INTO media_library_items('
        'session_id,kind,origin,storage_key,display_name,state,created_at,'
        "updated_at) VALUES(1,'broadcast','upload',?,'永久直播','ready',1,1)",
        ('f' * 32,),
    )
    policy_manager = RoomUploadPolicyManager(database, clock=lambda: 10)
    await policy_manager.upsert(
        100, command(retention_mode='approved', retention_days=30)
    )
    manager = SessionSubmissionManager(
        database, policy_manager=policy_manager, clock=lambda: 20
    )

    inherited = await manager.get(1)
    saved = await manager.save_override(
        1,
        command(retention_mode='submitted', retention_days=7),
        manager_subject='administrator',
    )

    assert inherited.settings.retention_mode == 'never'
    assert inherited.settings.retention_days == 0
    assert saved.settings.retention_mode == 'never'
    assert saved.settings.retention_days == 0


@pytest.mark.asyncio
async def test_session_submission_decision_can_be_changed_before_job_creation(
    database: BiliUploadDatabase,
) -> None:
    await seed(database)
    manager = SessionSubmissionManager(
        database, policy_manager=RoomUploadPolicyManager(database), clock=lambda: 20
    )

    upload = await manager.set_decision(1, 'upload', manager_subject='administrator')
    skipped = await manager.set_decision(1, 'skip', manager_subject='administrator')

    assert upload.decision == 'upload'
    assert skipped.decision == 'skip'
    assert skipped.resolution_state == 'pending'


@pytest.mark.asyncio
async def test_session_submission_is_immutable_after_upload_job_creation(
    database: BiliUploadDatabase,
) -> None:
    await seed(database)
    await database.execute(
        "UPDATE recording_sessions SET state='closed',"
        "upload_resolution_state='job_created' WHERE id=1"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'session_id,account_id,policy_snapshot_json,state,submit_state,'
        'created_at,updated_at) '
        "VALUES(1,1,'{}','waiting_artifacts','prepared',1,1)"
    )
    manager = SessionSubmissionManager(
        database, policy_manager=RoomUploadPolicyManager(database)
    )

    with pytest.raises(SessionSubmissionLocked):
        await manager.set_decision(1, 'skip', manager_subject='administrator')


@pytest.mark.asyncio
async def test_preupload_submission_can_change_until_finalized(
    database: BiliUploadDatabase,
) -> None:
    await seed(database)
    await database.execute(
        "UPDATE recording_sessions SET upload_resolution_state='job_created' "
        'WHERE id=1'
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'session_id,account_id,policy_snapshot_json,state,submit_state,'
        'preupload_finalized,created_at,updated_at) '
        "VALUES(1,1,'{}','waiting_artifacts','prepared',0,1,1)"
    )
    await database.execute(
        'INSERT INTO upload_suppressions('
        'session_id,reason,manager_subject,created_at) '
        "VALUES(1,'manager_skipped','administrator',1)"
    )
    manager = SessionSubmissionManager(
        database, policy_manager=RoomUploadPolicyManager(database)
    )

    changed = await manager.set_decision(1, 'upload', manager_subject='administrator')
    saved = await manager.save_override(
        1, command(title_template='最终标题'), manager_subject='administrator'
    )

    assert changed.decision == 'upload'
    assert saved.settings.title_template == '最终标题'
    assert (
        await database.scalar(
            'SELECT COUNT(*) FROM upload_suppressions WHERE session_id=1'
        )
        == 0
    )
