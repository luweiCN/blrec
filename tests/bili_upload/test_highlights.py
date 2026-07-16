from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.highlights import HighlightService


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


async def seed_timeline(database: BiliUploadDatabase, tmp_path: Path) -> None:
    first = tmp_path / 'p1.flv'
    second = tmp_path / 'p2.flv'
    first.write_bytes(b'first')
    second.write_bytes(b'second')
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at,title,anchor_name) "
        "VALUES(1,100,'100:900','open',900,'测试直播','主播')"
    )
    await database.execute(
        "INSERT INTO recording_runs(id,session_id,state,started_at) "
        "VALUES('run',1,'recording',900)"
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,final_path,'
        'record_start_time,timeline_start_at_ms,artifact_state,xml_completed,'
        'record_duration_seconds,created_at,updated_at) '
        "VALUES(1,1,'run',1,?,?,1000,1000000,'ready',1,90,1,1),"
        "(2,1,'run',2,?,NULL,1100,1100000,'recording',0,NULL,1,1)",
        (str(first), str(first), str(second)),
    )


@pytest.mark.asyncio
async def test_marker_uses_backend_clock_and_player_delay(database) -> None:
    service = HighlightService(database, clock=lambda: 1_100)

    marker = await service.create_marker(
        room_id=100,
        observed_at_ms=1_099_000,
        player_delay_ms=20_000,
        title='测试直播',
        anchor_name='主播',
        source='web',
    )

    assert marker.content_at_ms == 1_080_000
    assert marker.observed_at_ms == 1_099_000
    assert marker.player_delay_ms == 20_000
    assert marker.name.startswith('测试直播 高光 ')

    delayed = await service.create_marker(
        room_id=100,
        observed_at_ms=1_099_000,
        player_delay_ms=999_999,
        title='测试直播',
        anchor_name='主播',
        source='browser_extension',
    )
    assert delayed.id == marker.id + 1
    assert delayed.player_delay_ms == 300_000
    assert delayed.content_at_ms == 800_000

    updated = await service.update_marker(marker.id, '重命名高光', '剪这里')
    assert updated.name == '重命名高光'
    assert updated.note == '剪这里'

    await service.delete_marker(marker.id)
    await service.delete_marker(delayed.id)
    assert await database.scalar('SELECT COUNT(*) FROM highlight_markers') == 0


@pytest.mark.asyncio
async def test_marker_maps_normal_buffer_from_the_recording_first_byte_anchor(
    database, tmp_path: Path
) -> None:
    first_byte_ms = 1_000_000
    click_ms = first_byte_ms + 461_000
    video = tmp_path / 'active.flv'
    video.write_bytes(b'active')
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at,title,anchor_name) "
        "VALUES(1,100,'100:anchor','open',1000,'测试直播','主播')"
    )
    await database.execute(
        "INSERT INTO recording_runs(id,session_id,state,started_at) "
        "VALUES('run',1,'recording',1000)"
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,record_start_time,'
        'timeline_start_at_ms,artifact_state,created_at,updated_at) '
        "VALUES(1,1,'run',1,?,1000,?,'recording',1000,1000)",
        (str(video), first_byte_ms),
    )
    service = HighlightService(database, clock=lambda: click_ms / 1000)

    marker = await service.create_marker(
        room_id=100,
        observed_at_ms=click_ms - 50,
        player_delay_ms=0,
        current_time_ms=456_000,
        seekable_end_ms=461_000,
        raw_delay_ms=5_000,
        baseline_delay_ms=5_000,
        effective_rewind_ms=0,
        title='测试直播',
        anchor_name='主播',
        name='精彩操作',
        source='browser_extension',
    )

    assert marker.content_at_ms == click_ms
    assert marker.content_at_ms - first_byte_ms == 461_000
    assert marker.recording_part_id == 1
    assert marker.part_anchor_at_ms == first_byte_ms
    assert marker.raw_delay_ms == 5_000
    assert marker.baseline_delay_ms == 5_000
    assert marker.effective_rewind_ms == 0
    assert marker.name == '精彩操作'


@pytest.mark.asyncio
async def test_marker_subtracts_only_the_explicit_rewind(
    database, tmp_path: Path
) -> None:
    first_byte_ms = 1_000_000
    click_ms = first_byte_ms + 461_000
    video = tmp_path / 'active.flv'
    video.write_bytes(b'active')
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at) "
        "VALUES(1,100,'100:anchor','open',1000)"
    )
    await database.execute(
        "INSERT INTO recording_runs(id,session_id,state,started_at) "
        "VALUES('run',1,'recording',1000)"
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,record_start_time,'
        'timeline_start_at_ms,artifact_state,created_at,updated_at) '
        "VALUES(1,1,'run',1,?,1000,?,'recording',1000,1000)",
        (str(video), first_byte_ms),
    )
    marker = await HighlightService(
        database, clock=lambda: click_ms / 1000
    ).create_marker(
        room_id=100,
        observed_at_ms=click_ms,
        player_delay_ms=60_000,
        current_time_ms=396_000,
        seekable_end_ms=461_000,
        raw_delay_ms=65_000,
        baseline_delay_ms=5_000,
        effective_rewind_ms=60_000,
        title='',
        anchor_name='',
        name='',
        source='browser_extension',
    )

    assert marker.content_at_ms - first_byte_ms == 401_000


@pytest.mark.asyncio
async def test_timeline_preserves_gaps_and_only_maps_matching_markers(
    database, tmp_path: Path
) -> None:
    await seed_timeline(database, tmp_path)
    service = HighlightService(database, clock=lambda: 1_100)
    marker = await service.create_marker(
        room_id=100,
        observed_at_ms=1_100_000,
        player_delay_ms=20_000,
        title='测试直播',
        anchor_name='主播',
        source='web',
    )
    await database.execute(
        'INSERT INTO highlight_markers('
        'room_id,observed_at_ms,player_delay_ms,content_at_ms,title,anchor_name,'
        'name,note,source,created_at,updated_at) '
        "VALUES(100,900000,0,900000,'旧高光','主播','无法映射','',"
        "'browser_extension',900,900)"
    )

    timeline = await service.timeline(1, active_durations_ms={2: 120_000})

    assert timeline.parts[0].timeline_start_ms == 0
    assert timeline.parts[0].duration_ms == 90_000
    assert timeline.parts[1].timeline_start_ms == 100_000
    assert timeline.parts[1].stable_end_ms == 210_000
    assert [item.marker.id for item in timeline.markers] == [marker.id]
    assert timeline.markers[0].part_id == 1
    assert timeline.markers[0].local_offset_ms == 80_000
    assert timeline.markers[0].timeline_offset_ms == 80_000
    assert await database.scalar('SELECT COUNT(*) FROM highlight_markers') == 2


@pytest.mark.asyncio
async def test_timeline_falls_back_to_server_record_start_time(
    database, tmp_path: Path
) -> None:
    video = tmp_path / 'legacy.flv'
    video.write_bytes(b'legacy')
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at) "
        "VALUES(1,100,'100:legacy','closed',2000)"
    )
    await database.execute(
        "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
        "VALUES('legacy',1,'finished',2000,2030)"
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,final_path,'
        'record_start_time,artifact_state,xml_completed,record_duration_seconds,'
        'created_at,updated_at) '
        "VALUES(1,1,'legacy',1,?,?,2000,'ready',1,30,1,1)",
        (str(video), str(video)),
    )
    await database.execute(
        'INSERT INTO highlight_markers('
        'room_id,observed_at_ms,player_delay_ms,content_at_ms,title,anchor_name,'
        'name,note,source,created_at,updated_at) '
        "VALUES(100,2010000,0,2010000,'旧录像','主播','旧高光','',"
        "'web',2010,2010)"
    )

    timeline = await HighlightService(database).timeline(1, active_durations_ms={})

    assert timeline.parts[0].absolute_start_at_ms == 2_000_000
    assert timeline.markers[0].local_offset_ms == 10_000


@pytest.mark.asyncio
async def test_ready_clip_exposes_only_an_owned_existing_video(
    database, tmp_path: Path
) -> None:
    recording_root = tmp_path / 'recordings'
    video = recording_root / 'highlights' / '100' / 'highlight-1.mp4'
    video.parent.mkdir(parents=True)
    video.write_bytes(b'clip')
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,name,requested_start_ms,requested_end_ms,actual_start_ms,'
        'actual_end_ms,output_video_path,state,created_at,updated_at) '
        "VALUES(1,100,'高光',0,1000,0,1000,?,'ready',1,1)",
        (str(video),),
    )
    service = HighlightService(database, recording_root=recording_root)

    assert await service.clip_video_path(1) == video.resolve()

    await database.execute("UPDATE highlight_clips SET state='failed' WHERE id=1")
    with pytest.raises(ValueError, match='not ready'):
        await service.clip_video_path(1)
