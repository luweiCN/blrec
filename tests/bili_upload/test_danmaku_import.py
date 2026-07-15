import json
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from blrec.bili_upload.danmaku_import import DanmakuFilter, DanmakuImporter
from blrec.bili_upload.database import BiliUploadDatabase


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


def write_xml(path: Path, elements: str) -> Path:
    path.write_text('<i>{}</i>'.format(elements), encoding='utf8')
    return path


async def seed_part(
    database: BiliUploadDatabase,
    xml_path: Path,
    *,
    filters: object = None,
    branch_state: str = 'importing',
) -> None:
    snapshot = json.dumps({'filters': filters or {}}, ensure_ascii=False)
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,state,'
        'created_at,updated_at) '
        "VALUES(1,42,'投稿账号',X'00',1,'key','active',1,1)"
    )
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at) '
        "VALUES(1,100,'100:1','closed',1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'comment_branch_state,danmaku_branch_state,aid,bvid,created_at,updated_at) '
        "VALUES(1,1,1,?,'approved','confirmed','disabled',?,303,'BVtest',1,1)",
        (snapshot, branch_state),
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,final_path,xml_path,artifact_state,'
        'upload_state,danmaku_import_state,remote_filename,cid) '
        "VALUES(1,1,1,'/rec/p1.flv','/rec/p1.mp4',?,'ready','confirmed',"
        "'pending','remote-p1',1001)",
        (str(xml_path),),
    )


def test_parse_keeps_old_rows_and_applies_only_explicit_filters(tmp_path: Path) -> None:
    xml = write_xml(
        tmp_path / 'filter.xml',
        '<d p="1,1,25,1,1,0,h,1">same text</d>'
        '<d p="2,1,25,2,2,0,h,2">same text</d>'
        '<d p="3,1,25,3,3,0,h,3" is_lottery="true">lottery</d>'
        '<d p="4,1,25,4,4,0,h,4" is_system="true">system</d>'
        '<d p="5,1,25,5,5,0,h,5">blocked phrase</d>'
        '<d p="6,1,25,6,6,0,h,6" source_event_id="event-1">first</d>'
        '<d p="7,1,25,7,7,0,h,7" source_event_id="event-1">duplicate</d>'
        '<d p="8,1,25,8,8,0,h,8" user_level="1">low level</d>',
    )
    filters = DanmakuFilter(blocked_phrases=('blocked',), minimum_user_level=5)

    rows = list(DanmakuImporter.parse(xml, filters))
    texts = [row.content for row in rows]

    assert texts[:2] == ['same text', 'same text']
    assert 'lottery' not in texts
    assert 'system' not in texts
    assert 'blocked phrase' not in texts
    assert 'low level' not in texts
    assert [row.source_event_id for row in rows].count('event-1') == 1
    assert DanmakuFilter().minimum_user_level is None
    assert DanmakuFilter().minimum_fan_medal_level is None


def test_sc_and_guard_become_priority_top_danmaku(tmp_path: Path) -> None:
    xml = write_xml(
        tmp_path / 'priority.xml',
        '<sc ts="1.5" user="甲" price="30000">留言</sc>'
        '<guard ts="2" user="乙" giftname="舰长" count="3" />',
    )

    rows = list(DanmakuImporter.parse(xml, DanmakuFilter()))

    assert [(row.progress_ms, row.mode, row.priority) for row in rows] == [
        (1500, 5, 100),
        (2000, 5, 100),
    ]
    assert rows[0].content == '甲发送了30元留言：留言'
    assert rows[1].content == '乙开通了3个月舰长'


@pytest.mark.asyncio
async def test_import_has_no_daily_or_per_part_cap(
    database: BiliUploadDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xml = write_xml(
        tmp_path / 'large.xml',
        ''.join(
            '<d p="{0},1,25,16777215,{0},0,h,{0}">弹幕 {0}</d>'.format(index)
            for index in range(1201)
        ),
    )
    await seed_part(database, xml)
    importer = DanmakuImporter(
        database,
        insert_batch_size=500,
        import_high_watermark=10_000,
        space_threshold_bytes=0,
    )
    batch_sizes = []
    original = importer._insert_batch

    async def track_batch(*args: object, **kwargs: object):
        rows = args[-1]
        batch_sizes.append(len(rows))  # type: ignore[arg-type]
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importer, '_insert_batch', track_batch)

    imported = await importer.import_part(1, str(xml))

    assert imported == 1201
    assert await database.scalar('SELECT COUNT(*) FROM danmaku_items') == 1201
    assert max(batch_sizes) == 500
    assert (
        await database.scalar(
            'SELECT danmaku_import_state FROM upload_parts WHERE id=1'
        )
        == 'completed'
    )


@pytest.mark.asyncio
async def test_import_resumes_after_high_watermark_without_deleting_xml(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    xml = write_xml(
        tmp_path / 'capacity.xml',
        '<d p="1,1,25,1,1,0,h,1">一</d>' '<d p="2,1,25,2,2,0,h,2">二</d>',
    )
    await seed_part(database, xml)
    importer = DanmakuImporter(
        database, import_high_watermark=1, space_threshold_bytes=0
    )

    assert await importer.import_part(1, str(xml)) == 1
    assert (
        await database.scalar(
            'SELECT danmaku_import_state FROM upload_parts WHERE id=1'
        )
        == 'waiting_capacity'
    )
    assert xml.exists()

    await database.execute("UPDATE danmaku_items SET state='confirmed'")
    assert await importer.run_once() == 1
    assert await database.scalar('SELECT COUNT(*) FROM danmaku_items') == 2
    assert (
        await database.scalar(
            'SELECT danmaku_import_state FROM upload_parts WHERE id=1'
        )
        == 'completed'
    )


@pytest.mark.asyncio
async def test_create_marks_missing_xml_without_affecting_other_branches(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    missing = tmp_path / 'missing.xml'
    await seed_part(database, missing, branch_state='pending')

    await DanmakuImporter(database, space_threshold_bytes=0).create(1)

    row = await database.fetchone(
        'SELECT danmaku_branch_state,comment_branch_state FROM upload_jobs WHERE id=1'
    )
    assert dict(row) == {
        'danmaku_branch_state': 'skipped_source_missing',
        'comment_branch_state': 'disabled',
    }
    assert (
        await database.scalar(
            'SELECT danmaku_import_state FROM upload_parts WHERE id=1'
        )
        == 'missing_source'
    )


@pytest.mark.asyncio
async def test_create_uses_frozen_policy_filters_and_queues_for_publish(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    xml = write_xml(
        tmp_path / 'policy.xml',
        '<d p="1,1,25,1,1,0,h,1">保留</d>' '<d p="2,1,25,2,2,0,h,2">过滤词</d>',
    )
    await seed_part(
        database, xml, filters={'blockedWords': ['过滤']}, branch_state='pending'
    )

    await DanmakuImporter(database, space_threshold_bytes=0).create(1)

    assert await database.scalar('SELECT COUNT(*) FROM danmaku_items') == 1
    assert await database.scalar('SELECT content FROM danmaku_items') == '保留'
    assert (
        await database.scalar('SELECT danmaku_branch_state FROM upload_jobs WHERE id=1')
        == 'publishing'
    )


@pytest.mark.asyncio
async def test_low_disk_pauses_import_and_keeps_source(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    xml = write_xml(tmp_path / 'low-space.xml', '<d p="1,1,25,1,1,0,h,1">弹幕</d>')
    await seed_part(database, xml)
    importer = DanmakuImporter(
        database, space_threshold_bytes=100, free_space=lambda _path: 100
    )

    assert await importer.import_part(1, str(xml)) == 0
    assert xml.exists()
    assert (
        await database.scalar(
            'SELECT danmaku_import_state FROM upload_parts WHERE id=1'
        )
        == 'waiting_capacity'
    )


@pytest.mark.asyncio
async def test_importer_processes_pending_branch_without_global_switch(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    xml = write_xml(tmp_path / 'switch.xml', '<d p="1,1,25,1,1,0,h,1">弹幕</d>')
    await seed_part(database, xml, branch_state='pending')
    importer = DanmakuImporter(database, space_threshold_bytes=0)

    await importer.create(1)
    assert await database.scalar('SELECT COUNT(*) FROM danmaku_items') == 1
    assert (
        await database.scalar('SELECT danmaku_branch_state FROM upload_jobs WHERE id=1')
        == 'publishing'
    )
