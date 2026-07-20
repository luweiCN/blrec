import asyncio
import builtins
import os
import threading
from io import BytesIO
from pathlib import Path
from typing import AsyncIterator, Optional, Tuple

import pytest
import pytest_asyncio

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.journal import RecordingJournalBridge
from blrec.bili_upload.recording_content import (
    DanmakuPage,
    FlvMediaSnapshot,
    RecordingContentCursorStale,
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


async def _seed_active_xml_part(
    database: BiliUploadDatabase, tmp_path: Path, name: str, body: str
) -> Tuple[int, Path]:
    source = tmp_path / '{}.flv'.format(name)
    part_id = await _seed_part(database, source)
    xml = tmp_path / '{}.xml'.format(name)
    xml.write_text(body, encoding='utf8')
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
        (str(xml), part_id),
    )
    return part_id, xml


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
async def test_media_descriptor_keeps_final_first_without_touching_filesystem(
    database: BiliUploadDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'part.flv'
    final = tmp_path / 'part.mp4'
    part_id = await _seed_part(database, source, final)

    def forbidden_size(_path: str) -> Optional[int]:
        raise AssertionError('media descriptor must not stat files')

    monkeypatch.setattr(
        RecordingContentReader, '_regular_file_size', staticmethod(forbidden_size)
    )

    descriptor = await RecordingContentReader(database).media_descriptor(part_id)

    assert descriptor.part_id == part_id
    assert descriptor.room_id == 100
    assert descriptor.part_index == 1
    assert descriptor.index_state == 'pending'
    assert [candidate.path for candidate in descriptor.candidates] == [
        str(final),
        str(source),
    ]
    assert [candidate.content_type for candidate in descriptor.candidates] == [
        'video/mp4',
        'video/x-flv',
    ]
    assert [candidate.recording for candidate in descriptor.candidates] == [
        False,
        False,
    ]


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
async def test_danmaku_first_page_does_not_parse_the_whole_file(
    database: BiliUploadDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'many.xml'
    xml.write_text(
        '<i>'
        + ''.join(
            '<d p="{},1,25,1">{}</d>'.format(index, index) for index in range(100)
        )
        + '</i>',
        encoding='utf8',
    )
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=1 WHERE id=?',
        (str(xml), part_id),
    )
    original = RecordingContentReader._danmaku_line
    parsed = []

    def counting(index, element):
        parsed.append(index)
        return original(index, element)

    monkeypatch.setattr(RecordingContentReader, '_danmaku_line', staticmethod(counting))

    page = await RecordingContentReader(database).danmaku(part_id, cursor=0, limit=2)

    assert [item.content for item in page.items] == ['0', '1']
    assert page.next_cursor == 2
    assert parsed == [0, 1, 2]


@pytest.mark.asyncio
async def test_danmaku_cursor_zero_reopens_after_a_zero_item_page(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'active-header.xml'
    xml.write_text(
        '<i><metadata>{}'.format('x' * RecordingContentReader._DANMAKU_READ_BYTES),
        encoding='utf8',
    )
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
        (str(xml), part_id),
    )
    reader = RecordingContentReader(database)

    first = await reader.danmaku(part_id, cursor=0, limit=1)
    first_stream = next(iter(reader._danmaku_streams.values()))
    second = await reader.danmaku(part_id, cursor=0, limit=1)
    second_stream = next(iter(reader._danmaku_streams.values()))

    assert first.items == second.items == ()
    assert first.next_cursor == second.next_cursor == 0
    assert first_stream is not second_stream
    assert first_stream.file.closed is True
    assert first_stream.read_offset == second_stream.read_offset


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ('non_d_prefix', 'unfinished_d'))
async def test_danmaku_closes_parser_input_that_exceeds_the_memory_cap(
    database: BiliUploadDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'oversized.xml'
    prefix = '<i><metadata>' if kind == 'non_d_prefix' else '<i><d p="1,1,25,1">'
    xml.write_text(
        prefix + 'x' * (RecordingContentReader._DANMAKU_PENDING_BYTES + 1),
        encoding='utf8',
    )
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
        (str(xml), part_id),
    )
    reader = RecordingContentReader(database)
    opened = []
    new_stream = reader._new_danmaku_stream

    def capture_stream(*args, **kwargs):
        stream = new_stream(*args, **kwargs)
        opened.append(stream)
        return stream

    monkeypatch.setattr(reader, '_new_danmaku_stream', capture_stream)

    with pytest.raises(RecordingContentCursorStale):
        await reader.danmaku(part_id, cursor=0, limit=1)

    assert len(opened) == 1
    assert opened[0].read_offset <= RecordingContentReader._DANMAKU_PENDING_BYTES
    assert opened[0].file.closed is True
    assert reader._danmaku_streams == {}


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
        await reader.danmaku(1, cursor=0, limit=501)


@pytest.mark.asyncio
async def test_danmaku_rejects_an_unknown_cursor_without_scanning(
    database: BiliUploadDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'many.xml'
    xml.write_text(
        '<i>'
        + ''.join(
            '<d p="{},1,25,1">{}</d>'.format(index, index) for index in range(100)
        )
        + '</i>',
        encoding='utf8',
    )
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=1 WHERE id=?',
        (str(xml), part_id),
    )
    parsed = []
    original = RecordingContentReader._danmaku_line

    def counting(index, element):
        parsed.append(index)
        return original(index, element)

    monkeypatch.setattr(RecordingContentReader, '_danmaku_line', staticmethod(counting))

    with pytest.raises(RecordingContentCursorStale):
        await RecordingContentReader(database).danmaku(part_id, cursor=100_000, limit=2)

    assert parsed == []


@pytest.mark.asyncio
async def test_danmaku_continues_after_append_to_the_same_active_file(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'active.xml'
    xml.write_text('<i><d p="1,1,25,1">第一条</d>', encoding='utf8')
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
        (str(xml), part_id),
    )
    reader = RecordingContentReader(database)

    first = await reader.danmaku(part_id, cursor=0, limit=10)
    with xml.open('a', encoding='utf8') as file:
        file.write('<d p="2,1,25,1">第二条</d>')
    second = await reader.danmaku(part_id, cursor=first.next_cursor or 0, limit=10)

    assert [item.content for item in first.items] == ['第一条']
    assert first.next_cursor == 1
    assert [item.content for item in second.items] == ['第二条']
    assert second.next_cursor == 2


@pytest.mark.asyncio
async def test_danmaku_accepts_same_inode_growth_between_stat_and_fstat(
    database: BiliUploadDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'active.xml'
    xml.write_text('<i><d p="1,1,25,1">第一条</d>', encoding='utf8')
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
        (str(xml), part_id),
    )
    original_fstat = os.fstat
    first_fstat = True

    def grow_before_fstat(fd):
        nonlocal first_fstat
        if first_fstat:
            first_fstat = False
            with xml.open('a', encoding='utf8') as file:
                file.write('<d p="2,1,25,1">第二条</d>')
        return original_fstat(fd)

    monkeypatch.setattr(os, 'fstat', grow_before_fstat)

    page = await RecordingContentReader(database).danmaku(part_id, cursor=0, limit=10)

    assert [item.content for item in page.items] == ['第一条', '第二条']


@pytest.mark.asyncio
async def test_danmaku_closes_an_open_file_when_fstat_fails(
    database: BiliUploadDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'active.xml'
    xml.write_text('<i>', encoding='utf8')
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
        (str(xml), part_id),
    )
    original_open = builtins.open
    opened = []

    def tracking_open(path, *args, **kwargs):
        file = original_open(path, *args, **kwargs)
        if str(path) == str(xml):
            opened.append(file)
        return file

    def failing_fstat(_fd):
        raise OSError('injected fstat failure')

    monkeypatch.setattr(builtins, 'open', tracking_open)
    monkeypatch.setattr(os, 'fstat', failing_fstat)

    with pytest.raises(RecordingContentUnavailable):
        await RecordingContentReader(database).danmaku(part_id, cursor=0, limit=1)

    assert len(opened) == 1
    assert opened[0].closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ('truncate', 'replace'))
async def test_danmaku_rejects_a_shrunk_or_replaced_active_file(
    database: BiliUploadDatabase, tmp_path: Path, change: str
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'active.xml'
    xml.write_text('<i><d p="1,1,25,1">第一条</d>', encoding='utf8')
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
        (str(xml), part_id),
    )
    reader = RecordingContentReader(database)
    first = await reader.danmaku(part_id, cursor=0, limit=10)

    if change == 'truncate':
        xml.write_text('<i>', encoding='utf8')
    else:
        replacement = tmp_path / 'replacement.xml'
        replacement.write_text('<i><d p="2,1,25,1">替换内容</d>', encoding='utf8')
        replacement.replace(xml)

    with pytest.raises(RecordingContentCursorStale):
        await reader.danmaku(part_id, cursor=first.next_cursor or 0, limit=10)


@pytest.mark.asyncio
async def test_danmaku_detects_shrink_before_the_unread_tail(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'large-active.xml'
    xml.write_text(
        '<i>'
        + ''.join(
            '<d p="{},1,25,1">{}</d>'.format(index, 'x' * 100) for index in range(2_000)
        ),
        encoding='utf8',
    )
    original_size = xml.stat().st_size
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
        (str(xml), part_id),
    )
    reader = RecordingContentReader(database)
    first = await reader.danmaku(part_id, cursor=0, limit=1)
    with xml.open('r+b') as file:
        file.truncate(original_size - 1)

    with pytest.raises(RecordingContentCursorStale):
        await reader.danmaku(part_id, cursor=first.next_cursor or 0, limit=1)


@pytest.mark.asyncio
async def test_danmaku_cache_evicts_and_closes_the_oldest_of_three_streams(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    reader = RecordingContentReader(database)
    part_ids = []
    for index in range(3):
        source = tmp_path / 'part-{}.flv'.format(index)
        part_id = await _seed_part(database, source)
        xml = tmp_path / 'part-{}.xml'.format(index)
        xml.write_text('<i><d p="1,1,25,1">{}</d>'.format(index), encoding='utf8')
        await database.execute(
            'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
            (str(xml), part_id),
        )
        part_ids.append(part_id)

    first_page = await reader.danmaku(part_ids[0], cursor=0, limit=10)
    first_stream = next(iter(reader._danmaku_streams.values()))
    await reader.danmaku(part_ids[1], cursor=0, limit=10)
    await reader.danmaku(part_ids[2], cursor=0, limit=10)

    assert first_stream.file.closed is True
    assert len(reader._danmaku_streams) == 2
    with pytest.raises(RecordingContentCursorStale):
        await reader.danmaku(part_ids[0], cursor=first_page.next_cursor or 0, limit=10)


@pytest.mark.asyncio
async def test_danmaku_reserves_global_input_budget_before_feeding_second_handle(
    database: BiliUploadDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = '<i><metadata>{}'.format('x' * (200 * 1024))
    first_id, _first_xml = await _seed_active_xml_part(
        database, tmp_path, 'first-budget', body
    )
    second_id, _second_xml = await _seed_active_xml_part(
        database, tmp_path, 'second-budget', body
    )
    reader = RecordingContentReader(database)
    next_danmaku = reader._next_danmaku
    peak_bytes = 0

    def measure_peak(*args, **kwargs):
        nonlocal peak_bytes
        result = next_danmaku(*args, **kwargs)
        peak_bytes = max(
            peak_bytes,
            sum(
                stream.unreleased_input_bytes
                for stream in reader._danmaku_streams.values()
            ),
        )
        return result

    monkeypatch.setattr(reader, '_next_danmaku', measure_peak)

    await reader.danmaku(first_id, cursor=0, limit=1)
    first_stream = next(iter(reader._danmaku_streams.values()))
    await reader.danmaku(second_id, cursor=0, limit=1)

    assert peak_bytes <= RecordingContentReader._DANMAKU_PENDING_BYTES
    assert first_stream.file.closed is True


@pytest.mark.asyncio
async def test_concurrent_danmaku_handles_never_exceed_global_input_budget(
    database: BiliUploadDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = '<i><metadata>{}'.format('x' * (180 * 1024))
    first_id, _first_xml = await _seed_active_xml_part(
        database, tmp_path, 'first-concurrent', body
    )
    second_id, _second_xml = await _seed_active_xml_part(
        database, tmp_path, 'second-concurrent', body
    )
    reader = RecordingContentReader(database)
    reserve = reader._reserve_danmaku_input
    rounds = threading.Barrier(2)
    counts = {}
    peak_bytes = 0
    state_lock = threading.Lock()
    opened = []
    new_stream = reader._new_danmaku_stream

    def capture_stream(*args, **kwargs):
        stream = new_stream(*args, **kwargs)
        with state_lock:
            opened.append(stream)
        return stream

    def reserve_in_lockstep(stream, requested):
        nonlocal peak_bytes
        with state_lock:
            count = counts.get(stream.identity, 0)
            counts[stream.identity] = count + 1
        if count < 3:
            rounds.wait(timeout=2)
        reserved = reserve(stream, requested)
        with state_lock:
            peak_bytes = max(peak_bytes, reader._danmaku_reserved_bytes)
        return reserved

    monkeypatch.setattr(reader, '_new_danmaku_stream', capture_stream)
    monkeypatch.setattr(reader, '_reserve_danmaku_input', reserve_in_lockstep)

    results = await asyncio.gather(
        reader.danmaku(first_id, cursor=0, limit=1),
        reader.danmaku(second_id, cursor=0, limit=1),
        return_exceptions=True,
    )

    assert any(isinstance(result, RecordingContentCursorStale) for result in results)
    assert peak_bytes <= RecordingContentReader._DANMAKU_PENDING_BYTES
    reader.close()
    assert reader._danmaku_reserved_bytes == 0
    assert len(opened) == 2
    assert all(stream.file.closed for stream in opened)


@pytest.mark.asyncio
async def test_danmaku_serializes_concurrent_consumers_without_duplicates(
    database: BiliUploadDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'active.xml'
    xml.write_text(
        '<i><d p="1,1,25,1">第一条</d><d p="2,1,25,1">第二条</d>', encoding='utf8'
    )
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
        (str(xml), part_id),
    )
    reader = RecordingContentReader(database)
    first = await reader.danmaku(part_id, cursor=0, limit=1)
    selected = threading.Barrier(2)
    select_stream = reader._select_danmaku_stream

    def select_with_barrier(*args, **kwargs):
        stream = select_stream(*args, **kwargs)
        if kwargs['cursor'] > 0:
            selected.wait(timeout=2)
        return stream

    monkeypatch.setattr(reader, '_select_danmaku_stream', select_with_barrier)

    results = await asyncio.gather(
        reader.danmaku(part_id, cursor=first.next_cursor or 0, limit=1),
        reader.danmaku(part_id, cursor=first.next_cursor or 0, limit=1),
        return_exceptions=True,
    )

    pages = [result for result in results if isinstance(result, DanmakuPage)]
    stale = [
        result for result in results if isinstance(result, RecordingContentCursorStale)
    ]
    assert [[item.index for item in page.items] for page in pages] == [[1]]
    assert len(stale) == 1


@pytest.mark.asyncio
async def test_danmaku_reader_close_releases_cached_files(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'active.xml'
    xml.write_text('<i><d p="1,1,25,1">第一条</d>', encoding='utf8')
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
        (str(xml), part_id),
    )
    reader = RecordingContentReader(database)
    await reader.danmaku(part_id, cursor=0, limit=1)
    stream = next(iter(reader._danmaku_streams.values()))

    reader.close()

    assert stream.file.closed is True
    assert reader._danmaku_streams == {}


@pytest.mark.asyncio
async def test_closed_danmaku_reader_does_not_open_new_streams(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    part_id = await _seed_part(database, source)
    xml = tmp_path / 'active.xml'
    xml.write_text('<i>', encoding='utf8')
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
        (str(xml), part_id),
    )
    reader = RecordingContentReader(database)
    reader.close()

    with pytest.raises(RecordingContentUnavailable):
        await reader.danmaku(part_id, cursor=0, limit=1)

    assert reader._danmaku_streams == {}
