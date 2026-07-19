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


@pytest.mark.asyncio
async def test_clip_library_accepts_a_dedicated_root(database, tmp_path: Path) -> None:
    clip_root = tmp_path / 'clips'
    video = clip_root / '100' / 'highlight-1.mp4'
    video.parent.mkdir(parents=True)
    video.write_bytes(b'clip')
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,name,requested_start_ms,requested_end_ms,actual_start_ms,'
        'actual_end_ms,output_video_path,state,created_at,updated_at) '
        "VALUES(1,100,'高光',0,1000,0,1000,?,'ready',1,1)",
        (str(video),),
    )

    service = HighlightService(database, clip_root=clip_root)

    assert await service.clip_video_path(1) == video.resolve()


@pytest.mark.asyncio
async def test_legacy_clip_outputs_move_to_the_dedicated_library(
    database, tmp_path: Path
) -> None:
    recording_root = tmp_path / 'rec'
    clip_root = tmp_path / 'clips'
    old_video = recording_root / 'highlights' / '100' / 'highlight-1.mp4'
    old_xml = recording_root / 'highlights' / '100' / 'highlight-1.xml'
    old_video.parent.mkdir(parents=True)
    old_video.write_bytes(b'video')
    old_xml.write_text('<i/>', encoding='utf8')
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,name,requested_start_ms,requested_end_ms,actual_start_ms,'
        'actual_end_ms,output_video_path,output_xml_path,state,created_at,updated_at) '
        "VALUES(1,100,'高光',0,1000,0,1000,?,?,'ready',1,1)",
        (str(old_video), str(old_xml)),
    )
    service = HighlightService(database, clip_root=clip_root)

    migrated = await service.migrate_legacy_outputs(recording_root)

    assert migrated == 1
    assert old_video.exists()
    assert old_xml.exists()
    assert (clip_root / '100' / old_video.name).read_bytes() == b'video'
    assert (clip_root / '100' / old_xml.name).read_text(encoding='utf8') == '<i/>'
    assert (
        await service.clip_video_path(1)
        == (clip_root / '100' / old_video.name).resolve()
    )


@pytest.mark.asyncio
async def test_legacy_clip_migration_updates_its_local_upload_paths(
    database, tmp_path: Path
) -> None:
    recording_root = tmp_path / 'rec'
    clip_root = tmp_path / 'clips'
    old_video = recording_root / 'highlights' / '100' / 'highlight-1.mp4'
    old_xml = recording_root / 'highlights' / '100' / 'highlight-1.xml'
    old_video.parent.mkdir(parents=True)
    old_video.write_bytes(b'video')
    old_xml.write_text('<i/>', encoding='utf8')
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at,source_kind) '
        "VALUES(2,100,'highlight:1','closed',2,'highlight')"
    )
    await database.execute(
        "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
        "VALUES('clip',2,'finished',2,2)"
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,final_path,xml_path,'
        'record_start_time,artifact_state,created_at,updated_at) '
        "VALUES(2,2,'clip',1,?,?,?,2,'ready',2,2)",
        (str(old_video), str(old_video), str(old_xml)),
    )
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,1000,'投稿账号',X'00',1,'test','active',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'created_at,updated_at) '
        "VALUES(7,2,1,'{}','paused','prepared',2,2)"
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,final_path,xml_path,file_identity,'
        'artifact_state,upload_state) '
        "VALUES(8,7,1,?,?,?,'legacy-identity','ready','confirmed')",
        (str(old_video), str(old_video), str(old_xml)),
    )
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,upload_session_id,name,requested_start_ms,requested_end_ms,'
        'actual_start_ms,actual_end_ms,output_video_path,output_xml_path,state,'
        'created_at,updated_at) '
        "VALUES(1,100,2,'高光',0,1000,0,1000,?,?,'ready',1,1)",
        (str(old_video), str(old_xml)),
    )

    migrated = await HighlightService(
        database, clip_root=clip_root
    ).migrate_legacy_outputs(recording_root)

    new_video = str((clip_root / '100' / old_video.name).resolve())
    new_xml = str((clip_root / '100' / old_xml.name).resolve())
    assert migrated == 1
    recording_part = await database.fetchone(
        'SELECT source_path,final_path,xml_path FROM recording_parts WHERE id=2'
    )
    assert recording_part is not None
    assert dict(recording_part) == {
        'source_path': new_video,
        'final_path': new_video,
        'xml_path': new_xml,
    }
    upload_part = await database.fetchone(
        'SELECT source_path,final_path,xml_path,file_identity '
        'FROM upload_parts WHERE id=8'
    )
    assert upload_part is not None
    assert dict(upload_part) == {
        'source_path': new_video,
        'final_path': new_video,
        'xml_path': new_xml,
        'file_identity': None,
    }
    assert old_video.exists()
    assert old_xml.exists()


@pytest.mark.asyncio
async def test_failed_clip_can_be_queued_for_retry(database, tmp_path: Path) -> None:
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,name,requested_start_ms,requested_end_ms,state,error_message,'
        'next_attempt_at,created_at,updated_at) '
        "VALUES(1,100,'失败片段',0,1000,'failed','ffprobe failed',99,1,1)"
    )
    service = HighlightService(database, recording_root=tmp_path)

    clip = await service.retry_clip(1)

    assert clip.state == 'queued'
    assert clip.error_message is None
    assert (
        await database.scalar('SELECT next_attempt_at FROM highlight_clips WHERE id=1')
        == 0
    )


@pytest.mark.asyncio
async def test_list_clips_restores_upload_progress_for_a_recording(
    database, tmp_path: Path
) -> None:
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at,source_kind) "
        "VALUES(1,100,'100:live','closed',1,'live'),"
        "(2,100,'highlight:1','closed',2,'highlight')"
    )
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,1000,'投稿账号',X'00',1,'test','active',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'created_at,updated_at) '
        "VALUES(7,2,1,'{}','uploading','prepared',2,2)"
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,artifact_state) '
        "VALUES(8,7,1,'/rec/highlight.mp4','ready')"
    )
    await database.execute(
        'INSERT INTO upload_chunks('
        'part_id,chunk_no,offset,size,state,attempt) '
        "VALUES(8,0,0,25,'confirmed',1),(8,1,25,75,'prepared',0)"
    )
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,source_session_id,upload_session_id,name,'
        'requested_start_ms,requested_end_ms,state,created_at,updated_at) '
        "VALUES(1,100,1,2,'第二段',2000,3000,'ready',2,2),"
        "(2,100,1,NULL,'第一段',0,1000,'ready',1,1)"
    )

    clips = await HighlightService(database).list_clips(1)

    assert [clip.name for clip in clips] == ['第一段', '第二段']
    assert clips[0].upload_job_id is None
    assert clips[1].upload_job_id == 7
    assert clips[1].upload_state == 'uploading'
    assert clips[1].upload_percent == 25.0


@pytest.mark.asyncio
async def test_global_clip_library_is_newest_first_and_includes_source_metadata(
    database, tmp_path: Path
) -> None:
    first = tmp_path / 'first.mp4'
    second = tmp_path / 'second.mp4'
    first.write_bytes(b'a' * 10)
    second.write_bytes(b'b' * 20)
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at,title,anchor_name,'
        'source_kind) '
        "VALUES(1,100,'100:1','closed',1,'第一场','主播甲','live'),"
        "(2,200,'200:2','closed',2,'第二场','主播乙','live')"
    )
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,source_session_id,name,requested_start_ms,requested_end_ms,'
        'actual_start_ms,actual_end_ms,output_video_path,state,created_at,updated_at) '
        "VALUES(1,100,1,'第一段',0,10000,0,10000,?,'ready',1,1),"
        "(2,200,2,'第二段',5000,20000,5000,20000,?,'ready',2,2)",
        (str(first), str(second)),
    )

    total, clips = await HighlightService(database).list_all_clips(limit=20, offset=0)

    assert total == 2
    assert [clip.name for clip in clips] == ['第二段', '第一段']
    assert clips[0].source_anchor_name == '主播乙'
    assert clips[0].source_title == '第二场'
    assert clips[0].duration_ms == 15_000
    assert clips[0].file_size_bytes == 20


@pytest.mark.asyncio
async def test_delete_clip_removes_its_local_upload_task(
    database, tmp_path: Path
) -> None:
    clip_root = tmp_path / 'clips'
    video = clip_root / '100' / 'highlight-1.mp4'
    xml = clip_root / '100' / 'highlight-1.xml'
    video.parent.mkdir(parents=True)
    video.write_bytes(b'clip')
    xml.write_text('<i/>', encoding='utf8')
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at,source_kind) "
        "VALUES(1,100,'100:live','closed',1,'live'),"
        "(2,100,'highlight:1','closed',2,'highlight')"
    )
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,1000,'投稿账号',X'00',1,'test','active',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'created_at,updated_at) '
        "VALUES(7,2,1,'{}','uploading','prepared',2,2)"
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,final_path,xml_path,artifact_state) '
        "VALUES(8,7,1,?,?,?,'ready')",
        (str(video), str(video), str(xml)),
    )
    await database.execute(
        'INSERT INTO upload_chunks('
        'part_id,chunk_no,offset,size,state,attempt) '
        "VALUES(8,0,0,4,'confirmed',1)"
    )
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,source_session_id,upload_session_id,name,'
        'requested_start_ms,requested_end_ms,actual_start_ms,actual_end_ms,'
        'output_video_path,output_xml_path,state,created_at,updated_at) '
        "VALUES(1,100,1,2,'待删除片段',0,1000,0,1000,?,?,'ready',2,2)",
        (str(video), str(xml)),
    )

    result = await HighlightService(database, clip_root=clip_root).delete_clip(1)

    assert result == 'deleted'
    assert not video.exists()
    assert not xml.exists()
    assert await database.scalar('SELECT COUNT(*) FROM highlight_clips') == 0
    assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
    assert await database.scalar('SELECT COUNT(*) FROM upload_parts') == 0
    assert await database.scalar('SELECT COUNT(*) FROM upload_chunks') == 0
    assert (
        await database.scalar(
            "SELECT COUNT(*) FROM recording_sessions WHERE source_kind='highlight'"
        )
        == 0
    )
