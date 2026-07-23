import asyncio
import errno
import os
import threading
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import patch

import pytest

from blrec.bili_upload.artifact_recovery import RecoveredArtifact
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.media_library import (
    ImportPartRequest,
    MediaLibrary,
    MediaLibraryConflict,
)
from blrec.bili_upload.upos import FileIdentity


async def seed_recorded_session(database: BiliUploadDatabase, root: Path) -> None:
    source = root / 'room' / 'source.flv'
    final = root / 'room' / 'final.mp4'
    xml = root / 'room' / 'danmaku.xml'
    cover = root / 'room' / 'cover.jpg'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'source-video')
    final.write_bytes(b'final-video')
    xml.write_text('<i></i>', encoding='utf8')
    cover.write_bytes(b'cover')
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,42,'投稿账号',X'00',1,'k','active',1,1)"
    )
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,live_start_time,state,started_at,'
        'ended_at,title,cover_path,anchor_name,source_kind) '
        "VALUES(1,100,'100:1',1,'closed',1,10,'原直播标题',?,'主播','live')",
        (str(cover),),
    )
    await database.execute(
        "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
        "VALUES('run-1',1,'finished',1,10)"
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,final_path,xml_path,'
        'record_start_time,record_end_time,record_duration_seconds,'
        'file_size_bytes,artifact_state,xml_completed,created_at,updated_at,'
        'media_index_state) '
        "VALUES(1,1,'run-1',1,?,?,?,?,10,9,11,'ready',1,1,10,'ready')",
        (str(source), str(final), str(xml), 1),
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'aid,bvid,created_at,updated_at,upload_completed_at,submitted_at,'
        'approved_at) '
        "VALUES(1,1,1,'{}','completed','confirmed',101,'BVcurrent',1,10,8,9,10)"
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,final_path,xml_path,artifact_state,'
        'upload_state,file_identity,repair_original_path,'
        'repair_original_identity) '
        "VALUES(1,1,1,?,?,?,'ready','confirmed',?,?,?)",
        (
            str(source),
            str(final),
            str(xml),
            FileIdentity.from_path(str(final)).to_json(),
            str(source),
            FileIdentity.from_path(str(source)).to_json(),
        ),
    )
    await database.execute(
        'INSERT INTO upload_job_archives('
        'session_id,old_job_id,account_id,aid,bvid,state,submit_state,'
        'policy_snapshot_json,reason,archived_at) '
        "VALUES(1,1,1,100,'BVprevious','completed','confirmed','{}',"
        "'repost_as_new',5)"
    )
    await database.execute(
        'INSERT INTO event_journal('
        'id,event_type,room_id,run_id,path,payload_json,occurred_at) '
        "VALUES('event-1','video_completed',100,'run-1',?,'{}',10)",
        (str(final),),
    )


@pytest.mark.asyncio
async def test_favorite_moves_owned_files_and_keeps_submission_history(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'rec'
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_recorded_session(database, root)
        library = MediaLibrary(
            database, root, clock=lambda: 20, storage_key_factory=lambda: 'a' * 32
        )

        item = await library.favorite(1, manager_subject='manager')

        assert item.state == 'ready'
        assert item.session_id == 1
        assert item.display_name == '原直播标题'
        assert [part.part_index for part in item.parts] == [1]
        expected_root = root.parent / 'favorites' / ('a' * 32)
        row = await database.fetchone(
            'SELECT source_path,final_path,xml_path FROM recording_parts WHERE id=1'
        )
        assert row is not None
        assert Path(str(row['source_path'])).parent == expected_root
        assert Path(str(row['final_path'])).parent == expected_root
        assert Path(str(row['xml_path'])).parent == expected_root
        assert all(Path(str(row[column])).is_file() for column in row.keys())
        upload_part = await database.fetchone(
            'SELECT source_path,final_path,xml_path FROM upload_parts WHERE id=1'
        )
        assert upload_part is not None
        assert dict(upload_part) == dict(row)
        upload_identity = await database.fetchone(
            'SELECT file_identity,repair_original_path,repair_original_identity '
            'FROM upload_parts WHERE id=1'
        )
        assert upload_identity is not None
        assert FileIdentity.from_json(
            str(upload_identity['file_identity'])
        ).canonical_path == str(expected_root / 'part-0001.mp4')
        assert upload_identity['repair_original_path'] == str(
            expected_root / 'part-0001-source.flv'
        )
        assert FileIdentity.from_json(
            str(upload_identity['repair_original_identity'])
        ).canonical_path == str(expected_root / 'part-0001-source.flv')
        assert await database.scalar(
            'SELECT cover_path FROM recording_sessions WHERE id=1'
        ) == str(expected_root / 'cover.jpg')
        assert await database.scalar(
            "SELECT path FROM event_journal WHERE id='event-1'"
        ) == str(expected_root / 'part-0001.mp4')

        again = await library.favorite(1, manager_subject='manager')
        assert again.id == item.id
        assert await database.scalar('SELECT COUNT(*) FROM media_library_items') == 1

        history = await library.submission_history(item.id)
        assert [(entry.bvid, entry.current) for entry in history] == [
            ('BVcurrent', True),
            ('BVprevious', False),
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_favorite_recovers_after_cross_device_copy_precedes_source_unlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'rec'
    favorites = tmp_path / 'favorites'
    key = '9' * 32
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_recorded_session(database, root)

        def cross_device_replace(source: str, target: str) -> None:
            source_path = Path(source)
            target_path = Path(target)
            if root in source_path.parents and favorites in target_path.parents:
                raise OSError(errno.EXDEV, 'simulated cross-device move')
            os.replace(source, target)

        unlink_attempts = 0

        def interrupt_before_source_unlink(path: str) -> None:
            nonlocal unlink_attempts
            unlink_attempts += 1
            raise OSError('simulated interruption before source unlink')

        interrupted = MediaLibrary(
            database,
            root,
            clock=lambda: 20,
            storage_key_factory=lambda: key,
            replace_file=cross_device_replace,
            unlink_file=interrupt_before_source_unlink,
        )

        with pytest.raises(MediaLibraryConflict, match='移动失败'):
            await interrupted.favorite(1, manager_subject='manager')

        old_final = root / 'room' / 'final.mp4'
        copied_final = favorites / key / 'part-0001.mp4'
        assert unlink_attempts == 1
        assert old_final.read_bytes() == b'final-video'
        assert copied_final.read_bytes() == b'final-video'

        restarted = MediaLibrary(database, root, clock=lambda: 30)
        recovered = await restarted.favorite(1, manager_subject='manager')

        assert recovered.state == 'ready'
        assert not old_final.exists()
        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM media_library_file_moves '
                "WHERE item_id=? AND state!='ready'",
                (recovered.id,),
            )
            == 0
        )
        paths = await database.fetchone(
            'SELECT source_path,final_path,xml_path FROM recording_parts WHERE id=1'
        )
        assert paths is not None
        assert all(
            favorites / key in Path(str(paths[column])).parents
            for column in paths.keys()
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_favorite_rejects_an_open_session(tmp_path: Path) -> None:
    root = tmp_path / 'rec'
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_recorded_session(database, root)
        await database.execute("UPDATE recording_sessions SET state='open' WHERE id=1")
        library = MediaLibrary(database, root)

        with pytest.raises(MediaLibraryConflict, match='录制结束'):
            await library.favorite(1, manager_subject='manager')

        assert await database.scalar('SELECT COUNT(*) FROM media_library_items') == 0
    finally:
        await database.close()


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_import_uploads_multiple_parts_in_order_and_uses_safe_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'rec'
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        sizes = {b'part-one': 11, b'part-two-data': 22}

        def probe(path: str) -> RecoveredArtifact:
            content = Path(path).read_bytes()
            return RecoveredArtifact(path, len(content), sizes[content])

        library = MediaLibrary(
            database,
            root,
            clock=lambda: 100,
            storage_key_factory=lambda: 'b' * 32,
            artifact_probe=probe,
        )
        item = await library.create_import(
            kind='broadcast',
            display_name='外部直播',
            parts=(
                ImportPartRequest('../../第一段.mp4', len(b'part-one')),
                ImportPartRequest(
                    "第二段'; DROP TABLE x; --.mp4", len(b'part-two-data')
                ),
            ),
            tags=('精选', "x'); DROP TABLE x;--"),
            manager_subject='manager',
        )

        await library.upload_part(item.id, 1, chunks(b'part-', b'one'))
        await library.upload_part(item.id, 2, chunks(b'part-two-', b'data'))
        completed = await library.complete_import(item.id, manager_subject='manager')

        assert completed.state == 'ready'
        assert completed.room_id == 0
        assert completed.display_name == '外部直播'
        assert completed.tags == ('精选', "x'); DROP TABLE x;--")
        assert [part.part_index for part in completed.parts] == [1, 2]
        assert all(part.recording_part_id is not None for part in completed.parts)
        paths = [Path(part.storage_path) for part in completed.parts]
        assert [path.name for path in paths] == ['part-0001.mp4', 'part-0002.mp4']
        assert all(
            path.parent == root.parent / 'favorites' / ('b' * 32) for path in paths
        )
        assert all(path.is_file() for path in paths)
        rows = await database.fetchall(
            'SELECT part_index,record_duration_seconds FROM recording_parts '
            'WHERE session_id=? ORDER BY part_index',
            (completed.session_id,),
        )
        assert [dict(row) for row in rows] == [
            {'part_index': 1, 'record_duration_seconds': 11},
            {'part_index': 2, 'record_duration_seconds': 22},
        ]
        assert (
            await database.scalar(
                'SELECT state FROM recording_sessions WHERE id=?',
                (completed.session_id,),
            )
            == 'closed'
        )
        assert (
            int(
                await database.scalar(
                    'SELECT room_id FROM recording_sessions WHERE id=?',
                    (completed.session_id,),
                )
            )
            > 0
        )
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name='media_library_items'"
            )
            == 1
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_import_cannot_complete_until_every_part_is_uploaded(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        library = MediaLibrary(
            database, tmp_path / 'rec', storage_key_factory=lambda: 'c' * 32
        )
        item = await library.create_import(
            kind='broadcast',
            display_name='未完成直播',
            parts=(ImportPartRequest('one.mp4', 3), ImportPartRequest('two.mp4', 3)),
            manager_subject='manager',
        )
        await library.upload_part(item.id, 1, chunks(b'one'))

        with pytest.raises(MediaLibraryConflict, match='全部分 P'):
            await library.complete_import(item.id, manager_subject='manager')
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_import_rejects_upload_after_session_deletion_is_requested(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'rec'
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        library = MediaLibrary(database, root, storage_key_factory=lambda: 'd' * 32)
        item = await library.create_import(
            kind='clip',
            display_name='待删除片段',
            parts=(ImportPartRequest('clip.mp4', 3),),
            manager_subject='manager',
        )
        await database.execute(
            "UPDATE recording_sessions SET deletion_state='requested' WHERE id=?",
            (item.session_id,),
        )

        with pytest.raises(MediaLibraryConflict, match='正在删除'):
            await library.upload_part(item.id, 1, chunks(b'one'))

        part = (await library.get_item(item.id)).parts[0]
        assert part.state == 'pending'
        assert not Path(part.storage_path).exists()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_import_completion_marks_item_busy_while_probing(tmp_path: Path) -> None:
    root = tmp_path / 'rec'
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    probe_started = threading.Event()
    release_probe = threading.Event()
    try:

        def probe(path: str) -> RecoveredArtifact:
            probe_started.set()
            if not release_probe.wait(timeout=2):
                raise RuntimeError('probe was not released')
            return RecoveredArtifact(path, 3, 1)

        library = MediaLibrary(
            database, root, storage_key_factory=lambda: 'e' * 32, artifact_probe=probe
        )
        item = await library.create_import(
            kind='clip',
            display_name='校验中的片段',
            parts=(ImportPartRequest('clip.mp4', 3),),
            manager_subject='manager',
        )
        await library.upload_part(item.id, 1, chunks(b'one'))

        completion = asyncio.create_task(
            library.complete_import(item.id, manager_subject='manager')
        )
        loop = asyncio.get_running_loop()
        assert await loop.run_in_executor(None, probe_started.wait, 2)

        assert (
            await database.scalar(
                'SELECT state FROM media_library_items WHERE id=?', (item.id,)
            )
            == 'moving'
        )

        release_probe.set()
        completed = await completion
        assert completed.state == 'ready'
    finally:
        release_probe.set()
        await database.close()


@pytest.mark.asyncio
async def test_recovery_resets_interrupted_external_completion(tmp_path: Path) -> None:
    root = tmp_path / 'rec'
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        library = MediaLibrary(database, root, storage_key_factory=lambda: 'f' * 32)
        item = await library.create_import(
            kind='clip',
            display_name='中断的片段',
            parts=(ImportPartRequest('clip.mp4', 3),),
            manager_subject='manager',
        )
        await library.upload_part(item.id, 1, chunks(b'one'))
        await database.execute(
            "UPDATE media_library_items SET state='moving' WHERE id=?", (item.id,)
        )

        await library.recover_interrupted()

        assert (
            await database.scalar(
                'SELECT state FROM media_library_items WHERE id=?', (item.id,)
            )
            == 'uploading'
        )
        assert (await library.get_item(item.id)).parts[0].state == 'uploaded'
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_media_library_page_batches_parts_and_tags(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    keys = iter(('1' * 32, '2' * 32))
    try:
        library = MediaLibrary(
            database, tmp_path / 'rec', storage_key_factory=lambda: next(keys)
        )
        for name, tag in (('第一场', '访谈'), ('第二场', '游戏')):
            await library.create_import(
                kind='broadcast',
                display_name=name,
                parts=(ImportPartRequest('video.mp4', 3),),
                tags=(tag,),
                manager_subject='manager',
            )

        with patch.object(database, 'fetchall', wraps=database.fetchall) as fetchall:
            total, items = await library.list_items(kind='broadcast')

        assert total == 2
        assert {item.display_name: item.tags for item in items} == {
            '第一场': ('访谈',),
            '第二场': ('游戏',),
        }
        assert fetchall.await_count == 3
    finally:
        await database.close()
