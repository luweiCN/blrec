from io import BytesIO
from pathlib import Path
from typing import AsyncIterator, Optional

import pytest
import pytest_asyncio

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.journal import RecordingJournalBridge
from blrec.bili_upload.recording_content import (
    FlvMediaSnapshot,
    RecordingContentInvalid,
    RecordingContentReader,
    RecordingContentUnavailable,
)
from blrec.flv.common import create_metadata_tag, parse_metadata
from blrec.flv.io import FlvReader, FlvWriter
from blrec.flv.models import FlvHeader


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


async def _seed_part(
    database: BiliUploadDatabase, source: Path, final: Optional[Path] = None
) -> int:
    source.write_bytes(b'source-video')
    if final is None:
        final = source
    elif not final.exists():
        final.write_bytes(b'final-video')
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))
    await journal.video_postprocessed(run_id, str(source), str(final))
    return (await journal.parts_for_run(run_id))[0].id


@pytest.mark.asyncio
async def test_media_prefers_existing_final_file(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    final = tmp_path / 'part.mp4'
    part_id = await _seed_part(database, source, final)

    resource = await RecordingContentReader(database).media(part_id)

    assert resource.path == str(final)
    assert resource.size == len(b'final-video')
    assert resource.content_type == 'video/mp4'
    assert resource.recording is False
    assert resource.part_index == 1
    assert resource.playback_mode == 'seekable'
    assert resource.index_state == 'pending'


@pytest.mark.asyncio
async def test_media_falls_back_to_growing_source_file(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    final = tmp_path / 'part.mp4'
    part_id = await _seed_part(database, source, final)
    final.unlink()
    await database.execute(
        "UPDATE recording_parts SET artifact_state='recording' WHERE id=?", (part_id,)
    )

    resource = await RecordingContentReader(database).media(part_id)

    assert resource.path == str(source)
    assert resource.size == len(b'source-video')
    assert resource.content_type == 'video/x-flv'
    assert resource.recording is True
    assert resource.room_id == 100
    assert resource.playback_mode == 'active_snapshot'


@pytest.mark.asyncio
async def test_completed_flv_is_sequential_until_its_index_is_ready(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'interrupted.flv'
    part_id = await _seed_part(database, source)
    reader = RecordingContentReader(database)

    pending = await reader.media(part_id)
    await database.execute(
        "UPDATE recording_parts SET media_index_state='ready' WHERE id=?", (part_id,)
    )
    indexed = await reader.media(part_id)

    assert pending.playback_mode == 'sequential'
    assert pending.index_state == 'pending'
    assert indexed.playback_mode == 'seekable'
    assert indexed.index_state == 'ready'


def test_flv_snapshot_exposes_duration_and_maps_virtual_ranges(tmp_path: Path) -> None:
    source = tmp_path / 'recording.flv'
    original = BytesIO()
    writer = FlvWriter(original)
    writer.write_header(FlvHeader('FLV', 1, 5, 9))
    writer.write_tag(
        create_metadata_tag({'duration': 0.0, 'filesize': 0.0, 'Title': '直播'})
    )
    source_tail_start = original.tell()
    tail = b'video-tag-0' + b'video-tag-1' + b'video-tag-2'
    original.write(tail)
    source.write_bytes(original.getvalue())

    snapshot = FlvMediaSnapshot.create(
        str(source),
        source.stat().st_size,
        {
            'duration': 12.5,
            'filesize': float(source.stat().st_size),
            'lasttimestamp': 12.5,
            'keyframes': {
                'times': [0.0, 5.0, 10.0],
                'filepositions': [
                    float(source_tail_start),
                    float(source_tail_start + 11),
                    float(source_tail_start + 22),
                ],
            },
        },
    )

    prefix_reader = FlvReader(BytesIO(snapshot.prefix))
    prefix_reader.read_header()
    metadata = parse_metadata(prefix_reader.read_tag())
    offset = len(snapshot.prefix) - source_tail_start
    assert metadata['duration'] == 12.5
    assert metadata['filesize'] == snapshot.size
    assert metadata['keyframes']['filepositions'] == [
        float(source_tail_start + offset),
        float(source_tail_start + 11 + offset),
        float(source_tail_start + 22 + offset),
    ]
    assert snapshot.duration_ms == 12_500
    assert b''.join(snapshot.iter_range(0, snapshot.size)) == snapshot.prefix + tail

    range_start = len(snapshot.prefix) - 4
    assert b''.join(snapshot.iter_range(range_start, 12)) == (
        snapshot.prefix[-4:] + tail[:8]
    )


@pytest.mark.asyncio
async def test_media_reports_remote_fallback_only_after_approval(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'missing.flv'
    part_id = await _seed_part(database, source)
    source.unlink()
    session_id = await database.scalar(
        'SELECT session_id FROM recording_parts WHERE id=?', (part_id,)
    )
    await database.execute(
        'INSERT INTO bili_accounts('
        'uid,display_name,credential_ciphertext,credential_version,key_id,state,'
        'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
        (42, '投稿账号', b'ciphertext', 1, 'test', 'active', 1_000, 1_000),
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'session_id,account_id,policy_snapshot_json,state,submit_state,'
        'comment_branch_state,danmaku_branch_state,bvid,created_at,updated_at) '
        "VALUES(?,1,'{}','approved','confirmed','disabled','disabled',?,?,?)",
        (session_id, 'BV1test', 1_000, 1_000),
    )

    resource = await RecordingContentReader(database).media(part_id)

    assert resource.path is None
    assert resource.remote_available is True
    assert resource.bvid == 'BV1test'


@pytest.mark.asyncio
async def test_media_rejects_a_database_path_that_is_not_a_regular_file(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    directory = tmp_path / 'not-a-file.mp4'
    directory.mkdir()
    source.unlink()
    await database.execute(
        'UPDATE recording_parts SET final_path=? WHERE id=?', (str(directory), part_id)
    )

    with pytest.raises(RecordingContentUnavailable):
        await RecordingContentReader(database).media(part_id)


@pytest.mark.asyncio
async def test_danmaku_pages_completed_xml(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'part.xml'
    xml.write_text(
        '<i>'
        '<d p="1.25,1,25,16777215,0,0,0,0" user="主播" uid="42">第一条</d>'
        '<d p="2.5,4,18,255,0,0,0,0">第二条</d>'
        '<d p="3.75,5,30,65280,0,0,0,0" user=" " uid="invalid">第三条</d>'
        '</i>',
        encoding='utf8',
    )
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=1 WHERE id=?',
        (str(xml), part_id),
    )
    reader = RecordingContentReader(database)

    first = await reader.danmaku(part_id, cursor=0, limit=2)
    second = await reader.danmaku(part_id, cursor=first.next_cursor or 0, limit=2)

    assert [item.content for item in first.items] == ['第一条', '第二条']
    assert first.items[0].progress_ms == 1_250
    assert first.items[0].mode == 1
    assert first.items[0].font_size == 25
    assert first.items[0].color == 16_777_215
    assert first.items[0].user == '主播'
    assert first.items[0].uid == 42
    assert first.items[1].user is None
    assert first.items[1].uid is None
    assert first.next_cursor == 2
    assert [item.content for item in second.items] == ['第三条']
    assert second.items[0].user is None
    assert second.items[0].uid is None
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_danmaku_rejects_malformed_xml(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'broken.xml'
    xml.write_text('<i><d p="1,1,25,1">broken', encoding='utf8')
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=1 WHERE id=?',
        (str(xml), part_id),
    )

    with pytest.raises(RecordingContentInvalid, match='弹幕文件格式无效'):
        await RecordingContentReader(database).danmaku(part_id, cursor=0, limit=100)


@pytest.mark.asyncio
async def test_danmaku_never_expands_external_entities(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    secret = tmp_path / 'secret.txt'
    secret.write_text('DO-NOT-EXPAND', encoding='utf8')
    xml = tmp_path / 'entity.xml'
    xml.write_text(
        '<!DOCTYPE i [<!ENTITY xxe SYSTEM "{}">]>'
        '<i><d p="1,1,25,1">&xxe;</d></i>'.format(secret.as_uri()),
        encoding='utf8',
    )
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=1 WHERE id=?',
        (str(xml), part_id),
    )

    with pytest.raises(RecordingContentInvalid) as error:
        await RecordingContentReader(database).danmaku(part_id, cursor=0, limit=100)
    assert 'DO-NOT-EXPAND' not in str(error.value)


@pytest.mark.asyncio
async def test_danmaku_validates_cursor_and_limit(database: BiliUploadDatabase) -> None:
    reader = RecordingContentReader(database)

    with pytest.raises(ValueError, match='cursor'):
        await reader.danmaku(1, cursor=-1, limit=100)
    with pytest.raises(ValueError, match='limit'):
        await reader.danmaku(1, cursor=0, limit=101)
