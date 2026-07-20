import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest
import pytest_asyncio

import blrec.bili_upload.journal as journal_module
from blrec.bili_upload.artifact_recovery import RecoveredArtifact
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.deletion_worker import LocalDeletionWorker
from blrec.bili_upload.journal import RecordingJournalBridge, RecordingJournalListener
from blrec.web.routers import recording_sessions


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


async def seed_upload_policy(
    database: BiliUploadDatabase, *, room_id: int = 100, enabled: bool = True
) -> None:
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,42,'投稿账号',X'00',1,'k','active',1,1)"
    )
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )
    await database.execute(
        'INSERT INTO room_upload_policies('
        'room_id,account_mode,account_id,enabled,title_template,'
        'description_template,part_title_template,dynamic_template,tid,tags,'
        'creation_statement_id,original_authorization,copyright,source,'
        'is_only_self,publish_dynamic,no_reprint,up_selection_reply,'
        'up_close_reply,up_close_danmu,auto_comment,danmaku_backfill,'
        'filter_json,created_at,updated_at) '
        "VALUES(?,'primary',NULL,?,'{{ title }} 录播','',"
        "'P{{ part_index }}','',17,'直播,录播',-1,1,1,'',0,0,1,0,0,0,0,0,"
        "'{}',1,1)",
        (room_id, int(enabled)),
    )


def _seed_summary_sessions(
    connection: sqlite3.Connection, sessions: Sequence[Tuple[int, int]]
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,42,'summary-account',X'00',1,'k','active',1,1)"
    )
    connection.executemany(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,live_start_time,state,started_at,ended_at,'
        'title,cover_url,anchor_uid,anchor_name,area_id,area_name,parent_area_id,'
        'parent_area_name,live_end_time,upload_decision,upload_resolution_state) '
        "VALUES(?,?,?,?,'closed',?,?,?,?,?,?,?,?,?,?,?,?, 'job_created')",
        [
            (
                session_id,
                10_000 + session_id,
                'summary-{}'.format(session_id),
                started_at - 100,
                started_at,
                started_at + 60,
                'session {}'.format(session_id),
                'https://example.invalid/{}.jpg'.format(session_id),
                20_000 + session_id,
                'anchor {}'.format(session_id),
                1,
                'area',
                2,
                'parent area',
                started_at + 60,
                'follow_room',
            )
            for session_id, started_at in sessions
        ],
    )
    connection.executemany(
        'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
        "VALUES(?,?,'finished',?,?)",
        [
            (
                'summary-run-{}'.format(session_id),
                session_id,
                started_at,
                started_at + 60,
            )
            for session_id, started_at in sessions
        ],
    )
    recording_parts = [
        (
            session_id * 10 + part_index,
            session_id,
            'summary-run-{}'.format(session_id),
            part_index,
            '/rec/{}/p{}.flv'.format(session_id, part_index),
            '/rec/{}/p{}.mp4'.format(session_id, part_index),
            '/rec/{}/p{}.xml'.format(session_id, part_index),
            started_at + (part_index - 1) * 10,
            started_at + part_index * 10,
            part_index * 10,
            part_index * 1_000,
            part_index + 2,
            'ready',
            1,
            started_at,
            started_at,
        )
        for session_id, started_at in sessions
        for part_index in (1, 2)
    ]
    connection.executemany(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,final_path,xml_path,'
        'record_start_time,record_end_time,record_duration_seconds,file_size_bytes,'
        'danmaku_count,artifact_state,xml_completed,created_at,updated_at) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        recording_parts,
    )
    connection.executemany(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'comment_branch_state,danmaku_branch_state,repair_state,operator_paused,'
        'preupload_finalized,created_at,updated_at) '
        "VALUES(?,?,1,?,'uploading','prepared','disabled','disabled','idle',0,1,?,?)",
        [
            (
                session_id,
                session_id,
                '{"title":"upload %s"}' % session_id,
                started_at,
                started_at,
            )
            for session_id, started_at in sessions
        ],
    )
    upload_parts = [
        (
            session_id * 10 + part_index,
            session_id,
            part_index,
            '/upload/{}/p{}.mp4'.format(session_id, part_index),
            'ready',
            'confirmed' if part_index == 1 else 'uploading',
            'disabled',
            'remote-{}-{}'.format(session_id, part_index),
            session_id * 10 + part_index,
        )
        for session_id, _started_at in sessions
        for part_index in (1, 2)
    ]
    connection.executemany(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,artifact_state,upload_state,'
        'danmaku_import_state,remote_filename,cid) '
        'VALUES(?,?,?,?,?,?,?,?,?)',
        upload_parts,
    )
    connection.executemany(
        'INSERT INTO upload_chunks('
        'part_id,chunk_no,offset,size,state,attempt) VALUES(?,?,?,?,?,0)',
        [
            (
                session_id * 10 + part_index,
                chunk_no,
                chunk_no * 100,
                (chunk_no + 1) * 100,
                'confirmed' if part_index == 1 else 'prepared',
            )
            for session_id, _started_at in sessions
            for part_index in (1, 2)
            for chunk_no in (0, 1)
        ],
    )
    connection.executemany(
        'INSERT INTO danmaku_items('
        'part_id,xml_identity,original_index,progress_ms,mode,fontsize,color,'
        'content,request_fingerprint,state) VALUES(?,?,?,?,?,?,?,?,?,?)',
        [
            (
                session_id * 10 + part_index,
                'xml-{}'.format(part_index),
                item_index,
                item_index * 1_000,
                1,
                25,
                16_777_215,
                'danmaku',
                'request-{}-{}-{}'.format(session_id, part_index, item_index),
                (
                    'confirmed'
                    if part_index == 1
                    else 'prepared' if item_index == 0 else 'failed_permanent'
                ),
            )
            for session_id, _started_at in sessions
            for part_index in (1, 2)
            for item_index in (0, 1)
        ],
    )


def _summary_child_accesses(
    plan: Sequence[Mapping[str, object]]
) -> Dict[str, Tuple[Tuple[str, Optional[str]], ...]]:
    aliases = (
        'recording_part',
        'recording_part_match',
        'upload_part',
        'upload_chunk',
        'danmaku_item',
    )
    result: Dict[str, List[Tuple[str, Optional[str]]]] = {
        alias: [] for alias in aliases
    }
    for row in plan:
        tokens = [token.strip('`"[]') for token in str(row['detail']).split()]
        if len(tokens) < 2 or tokens[0] not in ('SCAN', 'SEARCH'):
            continue
        relation_index = 1 if tokens[1] != 'TABLE' else 2
        if len(tokens) <= relation_index:
            continue
        relation = tokens[relation_index]
        alias_marker = relation_index + 1
        if len(tokens) > alias_marker + 1 and tokens[alias_marker] == 'AS':
            relation = tokens[alias_marker + 1]
        if relation in result:
            index_name = None
            if 'INDEX' in tokens:
                index_position = tokens.index('INDEX') + 1
                if index_position < len(tokens):
                    index_name = tokens[index_position]
            result[relation].append((tokens[0], index_name))
    return {alias: tuple(accesses) for alias, accesses in result.items()}


@pytest.mark.parametrize(
    ('detail', 'alias', 'index_name'),
    (
        (
            'SEARCH TABLE recording_parts AS recording_part USING INDEX '
            'recording_parts_session_idx (session_id=?)',
            'recording_part',
            'recording_parts_session_idx',
        ),
        (
            'SEARCH upload_chunk USING COVERING INDEX upload_chunks_part_idx '
            '(part_id=?)',
            'upload_chunk',
            'upload_chunks_part_idx',
        ),
    ),
    ids=('sqlite-3.22', 'modern-sqlite'),
)
def test_summary_child_accesses_parse_old_and_new_sqlite_plans(
    detail: str, alias: str, index_name: str
) -> None:
    accesses = _summary_child_accesses(({'detail': detail},))

    assert accesses[alias] == (('SEARCH', index_name),)


@pytest.mark.asyncio
async def test_list_session_summary_bounds_child_work_to_selected_page(
    database: BiliUploadDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_history = tuple((value, 1_000 + value) for value in range(1, 501))
    selected_page = tuple((value, 10_000 + value) for value in range(501, 521))
    await database.write(
        lambda connection: _seed_summary_sessions(
            connection, initial_history + selected_page
        )
    )
    journal = RecordingJournalBridge(database, clock=lambda: 20_000)
    original_fetchall = database.fetchall
    original_scalar = database.scalar
    database_calls: List[Tuple[str, str, Tuple[object, ...]]] = []
    filesystem_calls: List[str] = []

    async def counting_fetchall(
        sql: str, parameters: Sequence[object] = ()
    ) -> List[sqlite3.Row]:
        database_calls.append(('fetchall', sql, tuple(parameters)))
        return await original_fetchall(sql, parameters)

    async def counting_scalar(sql: str, parameters: Sequence[object] = ()) -> object:
        database_calls.append(('scalar', sql, tuple(parameters)))
        return await original_scalar(sql, parameters)

    def unexpected_filesystem_call(*_args: object, **_kwargs: object) -> None:
        filesystem_calls.append('called')
        raise AssertionError('recording summary lists must not inspect files')

    monkeypatch.setattr(database, 'fetchall', counting_fetchall)
    monkeypatch.setattr(database, 'scalar', counting_scalar)
    with monkeypatch.context() as filesystem:
        filesystem.setattr('os.path.exists', unexpected_filesystem_call)
        filesystem.setattr('os.path.getsize', unexpected_filesystem_call)
        filesystem.setattr(Path, 'stat', unexpected_filesystem_call)
        summaries = await journal.list_session_summaries(
            limit=20, offset=0, scope='uploads', sort_order='newest'
        )

    assert [summary.id for summary in summaries] == list(range(520, 500, -1))
    assert len(database_calls) == 1
    assert database_calls[0][0] == 'fetchall'
    assert filesystem_calls == []
    assert summaries[0].part_count == 2
    assert summaries[0].danmaku_count == 7
    assert summaries[0].total_file_size_bytes == 3_000
    assert summaries[0].record_duration_seconds == 30
    assert summaries[0].upload_job is not None
    assert summaries[0].upload_job.total_bytes == 600
    assert summaries[0].upload_job.confirmed_bytes == 300
    assert summaries[0].upload_job.percent == 50.0
    assert summaries[0].upload_job.current_part_index == 2
    assert summaries[0].upload_job.discovered_part_count == 2
    assert summaries[0].upload_job.confirmed_part_count == 1
    assert (
        summaries[0].upload_job.danmaku_total,
        summaries[0].upload_job.danmaku_confirmed,
        summaries[0].upload_job.danmaku_pending,
        summaries[0].upload_job.danmaku_unknown,
        summaries[0].upload_job.danmaku_failed,
    ) == (4, 2, 1, 0, 1)
    assert summaries[0].upload_job.can_pause is True
    assert summaries[0].upload_job.can_delete is True
    assert summaries[0].upload_job.can_edit is False
    assert summaries[0].upload_job.can_skip is False
    forbidden_fields = {
        'broadcast_session_key',
        'cover_path',
        'source_path',
        'final_path',
        'xml_path',
        'parts',
        'unknown_danmaku_items',
        'policy_snapshot_json',
        'submission_verification',
    }
    assert forbidden_fields.isdisjoint(summaries[0].__dataclass_fields__)
    assert forbidden_fields.isdisjoint(summaries[0].upload_job.__dataclass_fields__)

    summary_sql = database_calls[0][1]
    summary_parameters = database_calls[0][2]
    normalized_sql = ' '.join(summary_sql.split())
    selected_sql, aggregate_sql = normalized_sql.split('),part_summary AS', 1)
    assert selected_sql.startswith(
        'WITH selected_sessions AS (SELECT session.id AS session_id,' 'job.id AS job_id'
    )
    assert 'WHERE job.id IS NOT NULL' in selected_sql
    assert (
        'ORDER BY session.started_at DESC,session.id DESC LIMIT ? OFFSET ?'
        in selected_sql
    )
    assert 'LIMIT' not in aggregate_sql
    assert 'AS MATERIALIZED' not in normalized_sql
    assert normalized_sql.index('LIMIT ? OFFSET ?') < normalized_sql.index(
        'recording_parts recording_part'
    )
    for selected_join in (
        'selected_sessions selected CROSS JOIN recording_parts recording_part',
        'selected_sessions selected CROSS JOIN upload_parts upload_part',
        'selected_upload_parts selected_upload_part '
        'LEFT JOIN upload_chunks upload_chunk',
        'selected_upload_parts selected_upload_part '
        'CROSS JOIN danmaku_items danmaku_item',
    ):
        assert selected_join in normalized_sql
    explained_plan = await original_fetchall(
        'EXPLAIN QUERY PLAN ' + summary_sql, summary_parameters
    )
    child_accesses = _summary_child_accesses(explained_plan)
    assert all(child_accesses.values())
    child_tables = {
        'recording_part': 'recording_parts',
        'recording_part_match': 'recording_parts',
        'upload_part': 'upload_parts',
        'upload_chunk': 'upload_chunks',
        'danmaku_item': 'danmaku_items',
    }
    child_indexes = {
        alias: {
            str(row['name'])
            for row in await original_fetchall(
                'PRAGMA index_list({})'.format(table_name)
            )
        }
        for alias, table_name in child_tables.items()
    }
    assert all(
        operation == 'SEARCH' and index_name in child_indexes[alias]
        for alias, accesses in child_accesses.items()
        for operation, index_name in accesses
    ), '{}\n{}'.format(
        child_accesses, '\n'.join(str(row['detail']) for row in explained_plan)
    )

    doubled_history = tuple((value, value - 520) for value in range(521, 1_021))
    await database.write(
        lambda connection: _seed_summary_sessions(connection, doubled_history)
    )
    database_calls.clear()
    with monkeypatch.context() as filesystem:
        filesystem.setattr('os.path.exists', unexpected_filesystem_call)
        filesystem.setattr('os.path.getsize', unexpected_filesystem_call)
        filesystem.setattr(Path, 'stat', unexpected_filesystem_call)
        doubled_summaries = await journal.list_session_summaries(
            limit=20, offset=0, scope='uploads', sort_order='newest'
        )

    assert doubled_summaries == summaries
    assert len(database_calls) == 1
    assert database_calls[0][0] == 'fetchall'
    doubled_plan = await original_fetchall(
        'EXPLAIN QUERY PLAN ' + database_calls[0][1], database_calls[0][2]
    )
    assert _summary_child_accesses(doubled_plan) == child_accesses


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('filters', 'expected_index'),
    (
        ({'scope': 'recordings'}, 'recording_sessions_source_started_idx'),
        ({'upload_state': 'paused'}, 'upload_jobs_state_session_idx'),
    ),
)
async def test_session_summary_query_uses_proven_list_index(
    database: BiliUploadDatabase,
    monkeypatch: pytest.MonkeyPatch,
    filters: dict,
    expected_index: str,
) -> None:
    journal = RecordingJournalBridge(database)
    original_fetchall = database.fetchall
    statements = []

    async def capture_fetchall(sql, parameters=()):
        statements.append((sql, tuple(parameters)))
        return await original_fetchall(sql, parameters)

    monkeypatch.setattr(database, 'fetchall', capture_fetchall)
    await journal.list_session_summaries(limit=20, offset=0, **filters)

    assert len(statements) == 1
    plan = await original_fetchall(
        'EXPLAIN QUERY PLAN ' + statements[0][0], statements[0][1]
    )
    details = [str(row['detail']) for row in plan]
    assert any(expected_index in detail for detail in details), '\n'.join(details)


@pytest.mark.asyncio
async def test_session_summary_dto_matches_full_projection_scalars_and_actions(
    database: BiliUploadDatabase,
) -> None:
    await database.write(
        lambda connection: _seed_summary_sessions(
            connection,
            tuple((session_id, session_id * 1_000) for session_id in range(1, 7)),
        )
    )

    def configure_representative_states(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE upload_jobs SET state='paused',operator_paused=1 WHERE id=1"
        )
        connection.execute(
            "UPDATE upload_parts SET upload_state='prepared',remote_filename=NULL "
            'WHERE job_id=1'
        )
        connection.execute(
            "UPDATE upload_jobs SET state='approved',submit_state='confirmed',"
            "aid=2,bvid='BVrepair' WHERE id=2"
        )
        connection.execute(
            "UPDATE upload_parts SET transcode_state='failed' WHERE job_id=2 "
            'AND part_index=1'
        )
        connection.execute(
            "UPDATE upload_jobs SET state='approved',submit_state='confirmed',"
            "aid=3,bvid='BVbackfill',danmaku_branch_state='disabled' WHERE id=3"
        )
        connection.execute(
            "UPDATE upload_jobs SET state='waiting_artifacts',preupload_finalized=0 "
            'WHERE id=4'
        )
        connection.execute(
            "UPDATE upload_parts SET upload_state='confirmed' WHERE job_id=4"
        )
        connection.execute('DELETE FROM danmaku_items WHERE part_id IN (51,52)')
        connection.execute('DELETE FROM upload_chunks WHERE part_id IN (51,52)')
        connection.execute('DELETE FROM upload_parts WHERE job_id=5')
        connection.execute(
            "UPDATE upload_jobs SET state='waiting_artifacts',preupload_finalized=0 "
            'WHERE id=5'
        )
        connection.execute(
            "UPDATE recording_sessions SET source_kind='highlight',"
            "title='local highlight' WHERE id=6"
        )
        connection.execute(
            'UPDATE upload_jobs SET policy_snapshot_json=? WHERE id=6',
            ('{"title":"final highlight title"}',),
        )

    await database.write(configure_representative_states)
    journal = RecordingJournalBridge(database, clock=lambda: 20_000)

    full_sessions = await journal.list_sessions(sort_order='oldest')
    full_jobs = await journal.upload_jobs_for_sessions(
        tuple(session.id for session in full_sessions)
    )
    summaries = await journal.list_session_summaries(sort_order='oldest')

    full_by_id = {session.id: session for session in full_sessions}
    summary_by_id = {summary.id: summary for summary in summaries}
    for session_id in range(1, 7):
        full_payload = recording_sessions._session_response(
            full_by_id[session_id], full_jobs[session_id]
        ).dict()
        for field in ('broadcast_session_key', 'cover_path', 'parts'):
            full_payload.pop(field)
        full_upload_job = full_payload['upload_job']
        assert isinstance(full_upload_job, dict)
        for field in ('parts', 'unknown_danmaku_items', 'submission_verification'):
            full_upload_job.pop(field)
        summary_payload = recording_sessions._session_summary_response(
            summary_by_id[session_id]
        ).dict()
        assert summary_payload == full_payload

    assert summary_by_id[1].upload_job is not None
    assert summary_by_id[1].upload_job.can_retry is True
    assert summary_by_id[1].upload_job.can_resume is True
    assert summary_by_id[1].upload_job.can_edit is True
    assert summary_by_id[2].upload_job is not None
    assert summary_by_id[2].upload_job.can_repair is True
    assert (
        'backfill_danmaku'
        in recording_sessions._session_summary_response(
            summary_by_id[3]
        ).available_actions
    )
    assert summary_by_id[4].upload_job is not None
    assert summary_by_id[4].upload_job.display_state == 'preuploaded_waiting'
    assert summary_by_id[5].upload_job is not None
    assert summary_by_id[5].upload_job.discovered_part_count == 0
    assert summary_by_id[5].upload_job.display_state == 'preuploading'
    assert summary_by_id[6].title == 'final highlight title'


@pytest.mark.parametrize('xml_path', ('', None), ids=('empty', 'missing'))
@pytest.mark.asyncio
async def test_session_summary_unusable_xml_path_matches_full_backfill_action(
    database: BiliUploadDatabase, xml_path: Optional[str]
) -> None:
    await database.write(
        lambda connection: _seed_summary_sessions(connection, ((1, 1_000),))
    )
    await database.execute(
        "UPDATE upload_jobs SET state='approved',submit_state='confirmed',"
        "aid=1,bvid='BVxml',danmaku_branch_state='disabled' WHERE id=1"
    )
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=1 WHERE session_id=1',
        (xml_path,),
    )
    journal = RecordingJournalBridge(database, clock=lambda: 20_000)

    full_session = (await journal.list_sessions())[0]
    full_job = (await journal.upload_jobs_for_sessions((1,)))[1]
    summary = (await journal.list_session_summaries())[0]
    full_actions = recording_sessions._session_response(
        full_session, full_job
    ).available_actions
    summary_actions = recording_sessions._session_summary_response(
        summary
    ).available_actions

    assert summary_actions == full_actions
    assert 'backfill_danmaku' not in summary_actions


@pytest.mark.asyncio
async def test_get_session_keeps_full_recording_detail(database) -> None:
    await database.write(
        lambda connection: _seed_summary_sessions(connection, ((1, 1_000),))
    )

    session = await RecordingJournalBridge(database).get_session(1)

    assert session.broadcast_session_key == 'summary-1'
    assert session.cover_path is None
    assert [part.source_path for part in session.parts] == [
        '/rec/1/p1.flv',
        '/rec/1/p2.flv',
    ]


@pytest.mark.asyncio
async def test_get_session_rejects_an_unknown_id(database) -> None:
    with pytest.raises(ValueError, match="unknown recording session '404'"):
        await RecordingJournalBridge(database).get_session(404)


@pytest.mark.asyncio
async def test_recording_start_does_not_freeze_room_upload_policy(database) -> None:
    await seed_upload_policy(database)
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)

    await journal.recording_started(100, live_start_time=900)
    await database.execute(
        'UPDATE room_upload_policies SET enabled=0 WHERE room_id=100'
    )
    await journal.recording_started(100, live_start_time=901)

    rows = await database.fetchall(
        'SELECT upload_intent,upload_decision FROM recording_sessions ORDER BY id'
    )
    assert [tuple(row) for row in rows] == [
        ('none', 'follow_room'),
        ('none', 'follow_room'),
    ]


@pytest.mark.asyncio
async def test_list_sessions_derives_current_upload_intent(database) -> None:
    await seed_upload_policy(database)
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    session = await journal.session_for_run(run_id)

    assert (await journal.list_sessions())[0].upload_intent == 'auto'

    await database.execute(
        "UPDATE recording_sessions SET upload_decision='upload' WHERE id=?",
        (session.id,),
    )
    assert (await journal.list_sessions())[0].upload_intent == 'upload'

    await database.execute(
        "UPDATE recording_sessions SET upload_decision='skip' WHERE id=?", (session.id,)
    )
    assert (await journal.list_sessions())[0].upload_intent == 'skip'

    await database.execute(
        "UPDATE recording_sessions SET upload_decision='follow_room' WHERE id=?",
        (session.id,),
    )
    await database.execute(
        'UPDATE room_upload_policies SET enabled=0 WHERE room_id=100'
    )
    assert (await journal.list_sessions())[0].upload_intent == 'none'

    await database.execute(
        'UPDATE room_upload_policies SET enabled=1 WHERE room_id=100'
    )
    await database.execute(
        'INSERT INTO upload_suppressions('
        'session_id,reason,manager_subject,created_at) VALUES(?,?,?,?)',
        (session.id, 'operator', 'owner', 1_001),
    )
    assert (await journal.list_sessions())[0].upload_intent == 'skip'


@pytest.mark.asyncio
async def test_video_created_records_local_media_timeline_anchor(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000.250)
    run_id = await journal.recording_started(100, live_start_time=900)

    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=990)

    row = await database.fetchone(
        'SELECT record_start_time,timeline_start_at_ms '
        'FROM recording_parts WHERE run_id=?',
        (run_id,),
    )
    assert row is not None
    assert dict(row) == {'record_start_time': 990, 'timeline_start_at_ms': 1_000_250}


@pytest.mark.asyncio
async def test_part_order_is_creation_order_not_completion_order(
    database, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = []
    monkeypatch.setattr(
        'blrec.bili_upload.journal.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    await journal.video_created(run_id, '/rec/p2.flv', record_start_time=902)

    await journal.video_completed(run_id, '/rec/p2.flv')
    await journal.video_completed(run_id, '/rec/p1.flv')

    parts = await journal.parts_for_run(run_id)
    assert [(part.part_index, part.source_path) for part in parts] == [
        (1, '/rec/p1.flv'),
        (2, '/rec/p2.flv'),
    ]
    names = [event for event, _fields in events]
    assert names == [
        'recording_started',
        'recording_part_created',
        'recording_part_created',
        'recording_part_completed',
        'recording_part_completed',
    ]


@pytest.mark.asyncio
async def test_restart_of_same_live_reuses_session_and_continues_part_numbers(
    database,
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    first_run = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(first_run, '/rec/p1.flv', record_start_time=901)
    await journal.video_completed(first_run, '/rec/p1.flv')
    await journal.video_postprocessed(first_run, '/rec/p1.flv', '/rec/p1.flv')
    await journal.recording_cancelled(first_run)

    restarted_run = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(restarted_run, '/rec/p2.flv', record_start_time=902)

    first_session = await journal.session_for_run(first_run)
    restarted_session = await journal.session_for_run(restarted_run)
    restarted_parts = await journal.parts_for_run(restarted_run)
    assert restarted_session.id == first_session.id
    assert restarted_session.state == 'open'
    assert [(part.part_index, part.source_path) for part in restarted_parts] == [
        (2, '/rec/p2.flv')
    ]


@pytest.mark.asyncio
async def test_deleting_session_is_not_reopened_by_a_late_recording_start(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    first_run = await journal.recording_started(100, live_start_time=900)
    await journal.recording_cancelled(first_run)
    first_session = await journal.session_for_run(first_run)
    deletion = LocalDeletionWorker(
        database,
        recording_root=tmp_path,
        clip_root=tmp_path / 'clips',
        clock=lambda: 1_001,
    )
    await deletion.request_session(first_session.id, manager_subject='manager')

    restarted_run = await journal.recording_started(100, live_start_time=900)
    restarted_session = await journal.session_for_run(restarted_run)
    frozen = await database.fetchone(
        'SELECT state,deletion_state,cancellation_generation '
        'FROM recording_sessions WHERE id=?',
        (first_session.id,),
    )

    assert restarted_session.id != first_session.id
    assert restarted_session.broadcast_session_key.startswith('100:900:continuation:')
    assert frozen is not None
    assert dict(frozen) == {
        'state': 'cancelled',
        'deletion_state': 'requested',
        'cancellation_generation': 1,
    }


@pytest.mark.asyncio
async def test_late_recording_events_handoff_owned_paths_without_reviving_session(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'late.flv'
    final = tmp_path / 'late.mp4'
    xml = tmp_path / 'late.xml'
    cover = tmp_path / 'cover.jpg'
    source.write_bytes(b'source')
    final.write_bytes(b'final')
    xml.write_text('<i><d>late</d></i>', encoding='utf8')
    cover.write_bytes(b'cover')
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    session = await journal.session_for_run(run_id)
    deletion = LocalDeletionWorker(
        database,
        recording_root=tmp_path,
        clip_root=tmp_path / 'clips',
        clock=lambda: 1_001,
    )
    await deletion.request_session(session.id, manager_subject='manager')

    await journal.cover_downloaded(run_id, str(cover))
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))
    await journal.video_postprocessed(run_id, str(source), str(final))
    await journal.danmaku_completed(run_id, str(xml))
    await journal.recording_finished(run_id)

    frozen = await database.fetchone(
        'SELECT state,deletion_state,cancellation_generation,cover_path '
        'FROM recording_sessions WHERE id=?',
        (session.id,),
    )
    part = await database.fetchone(
        'SELECT artifact_state,source_path,final_path,xml_path,xml_completed '
        'FROM recording_parts WHERE session_id=?',
        (session.id,),
    )
    outcomes = await database.fetchall(
        'SELECT side_effect_key,source_generation,outcome_state '
        "FROM owner_handoff_outcomes WHERE owner_kind='recorder' "
        'AND owner_id=? ORDER BY id',
        (session.id,),
    )
    assert frozen is not None
    assert dict(frozen) == {
        'state': 'open',
        'deletion_state': 'requested',
        'cancellation_generation': 1,
        'cover_path': str(cover),
    }
    assert part is not None
    assert dict(part) == {
        'artifact_state': 'failed',
        'source_path': str(source),
        'final_path': str(final),
        'xml_path': str(xml),
        'xml_completed': 0,
    }
    assert len(outcomes) == 5
    assert {str(row['outcome_state']) for row in outcomes} == {'cancelled_local'}
    assert {int(row['source_generation']) for row in outcomes} == {0}
    assert (
        await database.scalar(
            "SELECT COUNT(*) FROM recording_runs WHERE id=? AND state='finished'",
            (run_id,),
        )
        == 1
    )

    assert await deletion.run_once() == ('session', session.id)
    assert not any(path.exists() for path in (source, final, xml, cover))
    assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0


@pytest.mark.asyncio
async def test_late_recovery_and_cancel_are_terminal_local_handoffs(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'recovered.flv'
    source.write_bytes(b'source')
    journal = RecordingJournalBridge(
        database,
        clock=lambda: 1_000,
        artifact_probe=lambda path: RecoveredArtifact(path, 6, 1),
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    session = await journal.session_for_run(run_id)
    deletion = LocalDeletionWorker(
        database,
        recording_root=tmp_path,
        clip_root=tmp_path / 'clips',
        clock=lambda: 1_001,
    )
    await deletion.request_session(session.id, manager_subject='manager')

    await journal.video_postprocessing_failed(run_id, str(source), RuntimeError('boom'))
    await journal.recording_cancelled(run_id)

    frozen = await database.fetchone(
        'SELECT state,deletion_state FROM recording_sessions WHERE id=?', (session.id,)
    )
    part = await database.fetchone(
        'SELECT artifact_state,final_path FROM recording_parts WHERE session_id=?',
        (session.id,),
    )
    assert frozen is not None
    assert dict(frozen) == {'state': 'open', 'deletion_state': 'requested'}
    assert part is not None
    assert dict(part) == {'artifact_state': 'failed', 'final_path': str(source)}
    assert (
        await database.scalar(
            "SELECT COUNT(*) FROM owner_handoff_outcomes "
            "WHERE owner_kind='recorder' AND owner_id=? "
            "AND outcome_state='cancelled_local'",
            (session.id,),
        )
        == 2
    )
    assert (
        await database.scalar(
            "SELECT COUNT(*) FROM recording_runs WHERE id=? AND state='cancelled'",
            (run_id,),
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'terminal_event', ('recording_finished', 'recording_cancelled')
)
async def test_deletion_waits_for_postprocessor_after_run_becomes_terminal(
    database: BiliUploadDatabase, tmp_path: Path, terminal_event: str
) -> None:
    source = tmp_path / 'pending.flv'
    final = tmp_path / 'pending.mp4'
    source.write_bytes(b'source')
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))
    session = await journal.session_for_run(run_id)
    deletion = LocalDeletionWorker(
        database,
        recording_root=tmp_path,
        clip_root=tmp_path / 'clips',
        clock=lambda: 1_001,
    )
    await deletion.request_session(session.id, manager_subject='manager')
    await getattr(journal, terminal_event)(run_id)

    assert await deletion.run_once() == ('session', session.id)
    assert source.exists()
    assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 1

    final.write_bytes(b'final')
    await journal.video_postprocessed(run_id, str(source), str(final))
    part = await database.fetchone(
        'SELECT artifact_state,final_path FROM recording_parts WHERE session_id=?',
        (session.id,),
    )
    assert part is not None
    assert dict(part) == {'artifact_state': 'failed', 'final_path': str(final)}
    assert (
        await database.scalar(
            "SELECT outcome_state FROM owner_handoff_outcomes "
            "WHERE owner_kind='recorder' AND owner_id=? "
            "AND side_effect_key LIKE '%:video_postprocessed:%'",
            (session.id,),
        )
        == 'cancelled_local'
    )

    assert await deletion.run_once() == ('session', session.id)
    assert not source.exists()
    assert not final.exists()
    assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0


@pytest.mark.asyncio
async def test_deletion_intent_before_video_completed_still_blocks_for_postprocessor(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'late-completed.flv'
    final = tmp_path / 'late-completed.mp4'
    source.write_bytes(b'source')
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    session = await journal.session_for_run(run_id)
    deletion = LocalDeletionWorker(
        database,
        recording_root=tmp_path,
        clip_root=tmp_path / 'clips',
        clock=lambda: 1_001,
    )
    await deletion.request_session(session.id, manager_subject='manager')

    await journal.video_completed(run_id, str(source))
    await journal.recording_finished(run_id)

    assert (
        await database.scalar(
            'SELECT artifact_state FROM recording_parts WHERE session_id=?',
            (session.id,),
        )
        == 'postprocessing'
    )
    assert await deletion.run_once() == ('session', session.id)
    assert source.exists()
    assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 1

    final.write_bytes(b'final')
    await journal.video_postprocessed(run_id, str(source), str(final))
    assert await deletion.run_once() == ('session', session.id)
    assert not source.exists()
    assert not final.exists()


@pytest.mark.asyncio
async def test_startup_recovery_releases_deleted_session_postprocessor_blocker(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    source = tmp_path / 'crashed.flv'
    source.write_bytes(b'source')
    journal = RecordingJournalBridge(
        database,
        clock=lambda: 1_000,
        artifact_probe=lambda path: RecoveredArtifact(path, 6, 1),
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))
    session = await journal.session_for_run(run_id)
    deletion = LocalDeletionWorker(
        database,
        recording_root=tmp_path,
        clip_root=tmp_path / 'clips',
        clock=lambda: 1_001,
    )
    await deletion.request_session(session.id, manager_subject='manager')
    await journal.recording_finished(run_id)

    restarted = RecordingJournalBridge(
        database,
        clock=lambda: 1_002,
        artifact_probe=lambda path: RecoveredArtifact(path, 6, 1),
    )
    await restarted.reconcile_open_sessions()

    part = await database.fetchone(
        'SELECT artifact_state,final_path FROM recording_parts WHERE session_id=?',
        (session.id,),
    )
    assert part is not None
    assert dict(part) == {'artifact_state': 'failed', 'final_path': str(source)}
    assert (
        await database.scalar(
            "SELECT outcome_state FROM owner_handoff_outcomes "
            "WHERE owner_kind='recorder' AND owner_id=?",
            (session.id,),
        )
        == 'cancelled_local'
    )
    assert await deletion.run_once() == ('session', session.id)
    assert not source.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('recoverable', (True, False))
async def test_startup_recovery_owns_derived_mp4_when_source_was_removed(
    database: BiliUploadDatabase, tmp_path: Path, recoverable: bool
) -> None:
    source = tmp_path / 'remuxed.flv'
    final = tmp_path / 'remuxed.mp4'
    source.write_bytes(b'source')

    def probe(path: str) -> Optional[RecoveredArtifact]:
        candidate = Path(path)
        if not candidate.is_file():
            return None
        if not recoverable:
            return None
        return RecoveredArtifact(path, candidate.stat().st_size, 1)

    journal = RecordingJournalBridge(
        database, clock=lambda: 1_000, artifact_probe=probe
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))
    session = await journal.session_for_run(run_id)
    deletion = LocalDeletionWorker(
        database,
        recording_root=tmp_path,
        clip_root=tmp_path / 'clips',
        clock=lambda: 1_001,
    )
    await deletion.request_session(session.id, manager_subject='manager')
    await journal.recording_finished(run_id)
    final.write_bytes(b'remuxed')
    source.unlink()

    restarted = RecordingJournalBridge(
        database, clock=lambda: 1_002, artifact_probe=probe
    )
    await restarted.reconcile_open_sessions()

    part = await database.fetchone(
        'SELECT artifact_state,source_path,final_path '
        'FROM recording_parts WHERE session_id=?',
        (session.id,),
    )
    assert part is not None
    assert dict(part) == {
        'artifact_state': 'failed',
        'source_path': str(source),
        'final_path': str(final),
    }
    assert await deletion.run_once() == ('session', session.id)
    assert not final.exists()
    assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0


@pytest.mark.asyncio
async def test_legacy_started_payload_uses_original_generation_for_handoff(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    session = await journal.session_for_run(run_id)
    await database.execute(
        "UPDATE event_journal SET payload_json=? WHERE run_id=? "
        "AND event_type='recording_started'",
        ('{"live_start_time":900}', run_id),
    )
    deletion = LocalDeletionWorker(
        database,
        recording_root=tmp_path,
        clip_root=tmp_path / 'clips',
        clock=lambda: 1_001,
    )
    await deletion.request_session(session.id, manager_subject='manager')

    await journal.recording_finished(run_id)

    assert (
        await database.scalar(
            "SELECT source_generation FROM owner_handoff_outcomes "
            "WHERE owner_kind='recorder' AND owner_id=?",
            (session.id,),
        )
        == 0
    )


@pytest.mark.asyncio
async def test_restart_of_frozen_live_creates_continuation_session(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    first_run = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(first_run, '/rec/p1.flv', record_start_time=901)
    await journal.video_completed(first_run, '/rec/p1.flv')
    await journal.video_postprocessed(first_run, '/rec/p1.flv', '/rec/p1.flv')
    await journal.recording_finished(first_run)

    restarted_run = await journal.recording_started(100, live_start_time=900)

    first_session = await journal.session_for_run(first_run)
    restarted_session = await journal.session_for_run(restarted_run)
    assert restarted_session.id != first_session.id
    assert restarted_session.broadcast_session_key.startswith('100:900:continuation:')
    assert first_session.state == 'closed'


@pytest.mark.asyncio
async def test_list_sessions_supports_offset_and_total(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    for index in range(3):
        await journal.recording_started(100 + index, live_start_time=900 + index)

    sessions = await journal.list_sessions(limit=1, offset=1)

    assert await journal.count_sessions() == 3
    assert len(sessions) == 1
    assert sessions[0].room_id == 101


@pytest.mark.asyncio
async def test_list_sessions_identifies_derived_highlight_media(database) -> None:
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at,source_kind) "
        "VALUES(1,100,'100:live','closed',1,'live'),"
        "(2,100,'highlight:7','closed',2,'highlight')"
    )
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,source_session_id,upload_session_id,name,requested_start_ms,'
        'requested_end_ms,state,created_at,updated_at) '
        "VALUES(7,100,1,2,'高光',0,1000,'ready',1,1)"
    )

    sessions = await RecordingJournalBridge(database).list_sessions(sort_order='oldest')

    assert [(item.source_kind, item.highlight_clip_id) for item in sessions] == [
        ('live', None),
        ('highlight', 7),
    ]

    recordings = await RecordingJournalBridge(database).list_sessions(
        scope='recordings'
    )
    assert [item.id for item in recordings] == [1]

    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
        'state,created_at,updated_at) '
        "VALUES(1,42,'账号',X'00',1,'key','active',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'session_id,account_id,policy_snapshot_json,state,submit_state,'
        'created_at,updated_at) '
        "VALUES(2,1,'{\"title\":\"最终投稿标题\"}','paused','prepared',1,1)"
    )
    journal = RecordingJournalBridge(database)
    uploads = await journal.list_sessions(scope='uploads')
    assert [item.id for item in uploads] == [2]
    jobs = await journal.upload_jobs_for_sessions((2,))
    assert jobs[2].title == '最终投稿标题'
    summaries = await journal.list_session_summaries(scope='uploads')
    assert summaries[0].title == '最终投稿标题'


@pytest.mark.asyncio
async def test_list_sessions_rejects_negative_offset(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)

    with pytest.raises(ValueError, match='offset must not be negative'):
        await journal.list_sessions(offset=-1)


@pytest.mark.asyncio
async def test_list_sessions_filters_upload_state_time_and_fuzzy_text(database) -> None:
    now = [1_000]
    journal = RecordingJournalBridge(database, clock=lambda: now[0])
    first_run = await journal.recording_started(
        100,
        live_start_time=900,
        metadata=SimpleNamespace(
            title='深夜游戏直播',
            cover_url='',
            anchor_uid=10,
            anchor_name='甲主播',
            area_id=1,
            area_name='单机游戏',
            parent_area_id=2,
            parent_area_name='游戏',
        ),
    )
    now[0] = 2_000
    second_run = await journal.recording_started(
        200,
        live_start_time=1_900,
        metadata=SimpleNamespace(
            title='白天学习直播',
            cover_url='',
            anchor_uid=20,
            anchor_name='乙主播',
            area_id=3,
            area_name='教育学习',
            parent_area_id=4,
            parent_area_name='知识',
        ),
    )
    first = await journal.session_for_run(first_run)
    second = await journal.session_for_run(second_run)
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,state,'
        'created_at,updated_at) '
        "VALUES(1,10,'游戏投稿账号',X'00',1,'k','active',1,1),"
        "(2,20,'学习投稿账号',X'00',1,'k','active',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'session_id,account_id,policy_snapshot_json,state,submit_state,'
        'created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
        (first.id, 1, '{}', 'paused', 'prepared', 1_000, 1_000),
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'session_id,account_id,policy_snapshot_json,state,submit_state,aid,bvid,'
        'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (second.id, 2, '{}', 'approved', 'confirmed', 123, 'BV1approved', 2_000, 2_000),
    )

    sessions = await journal.list_sessions(
        query='学习投稿',
        upload_state='approved',
        started_from=1_500,
        started_to=2_500,
        sort_order='oldest',
    )

    assert [session.id for session in sessions] == [second.id]
    assert (
        await journal.count_sessions(
            query='学习投稿',
            upload_state='approved',
            started_from=1_500,
            started_to=2_500,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_missing_live_start_time_reuses_open_surrogate_session(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)

    first_run = await journal.recording_started(100, live_start_time=0)
    restarted_run = await journal.recording_started(100, live_start_time=0)

    first_session = await journal.session_for_run(first_run)
    restarted_session = await journal.session_for_run(restarted_run)
    assert restarted_session.id == first_session.id
    assert (
        restarted_session.broadcast_session_key == first_session.broadcast_session_key
    )
    assert restarted_session.broadcast_session_key.startswith('100:local:')


@pytest.mark.asyncio
async def test_reconcile_recovers_crash_interrupted_file_without_manual_review(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'interrupted.flv'
    source.write_bytes(b'partial recording')
    journal = RecordingJournalBridge(
        database,
        clock=lambda: 1_000,
        artifact_probe=lambda path: RecoveredArtifact(path, 17, 12),
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)

    await journal.reconcile_open_sessions()

    session = await journal.session_for_run(run_id)
    part = (await journal.parts_for_run(run_id))[0]
    assert session.state == 'cancelled'
    assert part.artifact_state == 'ready'
    assert part.final_path == str(source)
    assert part.file_size_bytes == 17
    assert part.record_duration_seconds == 12
    assert part.record_end_time == 913
    assert part.error_message == '录制异常中断，已自动恢复原始文件'
    assert (
        await database.scalar(
            'SELECT COUNT(*) FROM recording_runs '
            "WHERE id=? AND state='cancelled' AND ended_at IS NOT NULL",
            (run_id,),
        )
        == 1
    )


@pytest.mark.asyncio
async def test_reconcile_excludes_unreadable_interrupted_file(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'broken.flv'
    source.write_bytes(b'broken')
    journal = RecordingJournalBridge(
        database, clock=lambda: 1_000, artifact_probe=lambda _path: None
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)

    await journal.reconcile_open_sessions()

    part = (await journal.parts_for_run(run_id))[0]
    assert part.artifact_state == 'failed'
    assert part.final_path is None
    assert part.error_message == '录制异常中断，文件无法解析，已自动排除'


@pytest.mark.asyncio
async def test_reconcile_falls_back_when_existing_ready_artifact_is_unreadable(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'source.flv'
    final = tmp_path / 'broken.mp4'
    source.write_bytes(b'video')
    final.write_bytes(b'broken')

    def probe(path: str):
        if path == str(source):
            return RecoveredArtifact(path, 5, 20)
        return None

    journal = RecordingJournalBridge(
        database, clock=lambda: 1_000, artifact_probe=probe
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))
    await journal.video_postprocessed(run_id, str(source), str(final))
    await database.execute(
        "UPDATE recording_sessions SET state='manual_review' "
        'WHERE id=(SELECT session_id FROM recording_runs WHERE id=?)',
        (run_id,),
    )

    await journal.reconcile_open_sessions()

    part = (await journal.parts_for_run(run_id))[0]
    assert part.artifact_state == 'ready'
    assert part.final_path == str(source)
    assert part.error_message == '录制异常中断，已自动恢复原始文件'


@pytest.mark.asyncio
async def test_cancelled_session_is_finalized_after_resume_grace(
    database, tmp_path: Path
) -> None:
    now = [1_000]
    source = tmp_path / 'resumable.flv'
    source.write_bytes(b'video')
    journal = RecordingJournalBridge(database, clock=lambda: now[0])
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))
    await journal.video_postprocessed(run_id, str(source), str(source))
    await journal.recording_cancelled(run_id)

    now[0] = 1_599
    assert await journal.finalize_cancelled_sessions(grace_seconds=600) == 0
    assert (await journal.session_for_run(run_id)).state == 'cancelled'

    now[0] = 1_600
    assert await journal.finalize_cancelled_sessions(grace_seconds=600) == 1
    assert (await journal.session_for_run(run_id)).state == 'closed'


@pytest.mark.asyncio
async def test_cancelled_session_with_only_broken_parts_is_skipped(database) -> None:
    now = [1_000]
    journal = RecordingJournalBridge(
        database, clock=lambda: now[0], artifact_probe=lambda _path: None
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/broken.flv', record_start_time=901)
    await journal.video_completed(run_id, '/rec/broken.flv')
    await journal.video_postprocessing_failed(
        run_id, '/rec/broken.flv', RuntimeError('invalid FLV')
    )
    await journal.recording_cancelled(run_id)

    now[0] = 1_600
    assert await journal.finalize_cancelled_sessions(grace_seconds=600) == 1
    assert (await journal.session_for_run(run_id)).state == 'skipped'


@pytest.mark.asyncio
async def test_reconcile_consumes_legacy_manual_review_state(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'legacy.flv'
    source.write_bytes(b'video')
    journal = RecordingJournalBridge(
        database,
        clock=lambda: 1_000,
        artifact_probe=lambda path: RecoveredArtifact(path, 5, 9),
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await database.execute(
        "UPDATE recording_runs SET state='finished',ended_at=910 WHERE id=?", (run_id,)
    )
    await database.execute(
        "UPDATE recording_parts SET artifact_state='manual_review' WHERE run_id=?",
        (run_id,),
    )
    await database.execute(
        "UPDATE recording_sessions SET state='manual_review' "
        'WHERE id=(SELECT session_id FROM recording_runs WHERE id=?)',
        (run_id,),
    )

    await journal.reconcile_open_sessions()

    session = await journal.session_for_run(run_id)
    part = (await journal.parts_for_run(run_id))[0]
    assert session.state == 'closed'
    assert part.artifact_state == 'ready'


@pytest.mark.asyncio
async def test_postprocessing_failure_uses_valid_source_as_upload_artifact(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'source.flv'
    source.write_bytes(b'video')
    journal = RecordingJournalBridge(
        database,
        clock=lambda: 1_000,
        artifact_probe=lambda path: RecoveredArtifact(path, 5, 20),
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))

    await journal.video_postprocessing_failed(
        run_id, str(source), RuntimeError('remux failed')
    )

    part = (await journal.parts_for_run(run_id))[0]
    assert part.artifact_state == 'ready'
    assert part.final_path == str(source)
    assert part.file_size_bytes == 5
    assert (
        part.error_message
        == '后处理失败，已自动使用原始录制文件：RuntimeError: remux failed'
    )


@pytest.mark.asyncio
async def test_remux_path_becomes_final_only_after_postprocess(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    final = tmp_path / 'part.mp4'
    source.write_bytes(b'source')
    final.write_bytes(b'final')
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))

    part = (await journal.parts_for_run(run_id))[0]
    assert part.final_path is None
    assert part.artifact_state == 'postprocessing'

    source.unlink()
    await journal.video_postprocessed(run_id, str(source), str(final))

    part = (await journal.parts_for_run(run_id))[0]
    assert part.final_path == str(final)
    assert part.artifact_state == 'ready'
    assert part.source_exists is False


@pytest.mark.asyncio
async def test_session_closes_only_after_recording_and_postprocessing_finish(
    database,
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    await journal.video_completed(run_id, '/rec/p1.flv')

    await journal.recording_finished(run_id)
    assert (await journal.session_for_run(run_id)).state == 'open'

    await journal.video_postprocessed(run_id, '/rec/p1.flv', '/rec/p1.flv')
    assert (await journal.session_for_run(run_id)).state == 'closed'


@pytest.mark.asyncio
async def test_unrecoverable_postprocessing_failure_skips_empty_session(
    database,
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    await journal.video_completed(run_id, '/rec/p1.flv')
    await journal.recording_finished(run_id)

    await journal.video_postprocessing_failed(
        run_id, '/rec/p1.flv', RuntimeError('invalid FLV')
    )

    session = await journal.session_for_run(run_id)
    assert session.state == 'skipped'
    assert session.parts[0].artifact_state == 'failed'
    assert session.parts[0].error_message == 'RuntimeError: invalid FLV'


@pytest.mark.asyncio
async def test_replayed_event_is_idempotent(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(
        100, live_start_time=900, event_id='recording-started'
    )

    await journal.video_created(
        run_id, '/rec/p1.flv', record_start_time=901, event_id='video-created'
    )
    await journal.video_created(
        run_id, '/rec/p1.flv', record_start_time=901, event_id='video-created'
    )

    assert len(await journal.parts_for_run(run_id)) == 1
    assert (
        await database.scalar(
            "SELECT COUNT(*) FROM event_journal WHERE id='video-created'"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_completed_danmaku_is_bound_to_matching_part(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'p1.flv'
    xml = tmp_path / 'p1.xml'
    source.write_bytes(b'source')
    xml.write_text('<i><d>one</d></i>')
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)

    await journal.danmaku_completed(run_id, str(xml))

    part = (await journal.parts_for_run(run_id))[0]
    assert part.xml_path == str(xml)
    assert part.xml_completed is True
    assert part.danmaku_count == 1


@pytest.mark.asyncio
async def test_session_snapshot_and_part_metrics_are_persisted(
    database, tmp_path: Path
) -> None:
    now = [1_000]
    source = tmp_path / 'part.flv'
    final = tmp_path / 'part.mp4'
    xml = tmp_path / 'part.xml'
    cover = tmp_path / 'cover.jpg'
    source.write_bytes(b'source')
    final.write_bytes(b'final-video')
    xml.write_text('<i><d>one</d><gift>ignore</gift><d>two</d></i>')
    cover.write_bytes(b'cover')
    journal = RecordingJournalBridge(database, clock=lambda: now[0])

    run_id = await journal.recording_started(
        100,
        live_start_time=900,
        metadata=SimpleNamespace(
            title='开播标题',
            cover_url='https://example.invalid/cover.jpg',
            anchor_uid=42,
            anchor_name='主播',
            area_id=1,
            area_name='单机游戏',
            parent_area_id=2,
            parent_area_name='游戏',
        ),
    )
    await journal.cover_downloaded(run_id, str(cover))
    await journal.video_created(run_id, str(source), record_start_time=901)
    now[0] = 911
    await journal.video_completed(run_id, str(source))
    await journal.video_postprocessed(run_id, str(source), str(final))
    await journal.danmaku_completed(run_id, str(xml))
    now[0] = 912
    await journal.recording_finished(run_id)

    session = await journal.session_for_run(run_id)
    part = session.parts[0]
    assert session.title == '开播标题'
    assert session.cover_url == 'https://example.invalid/cover.jpg'
    assert session.cover_path == str(cover)
    assert session.anchor_uid == 42
    assert session.anchor_name == '主播'
    assert session.area_name == '单机游戏'
    assert session.parent_area_name == '游戏'
    assert session.live_end_time == 912
    assert session.part_count == 1
    assert session.danmaku_count == 2
    assert session.total_file_size_bytes == len(b'final-video')
    assert session.record_duration_seconds == 10
    assert part.record_end_time == 911
    assert part.record_duration_seconds == 10
    assert part.file_size_bytes == len(b'final-video')
    assert part.danmaku_count == 2


@pytest.mark.asyncio
async def test_upload_progress_is_joined_to_its_recording_session(database) -> None:
    now = [1_000.0]
    journal = RecordingJournalBridge(database, clock=lambda: now[0])
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    session = await journal.session_for_run(run_id)
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,state,'
        'pause_reason,created_at,updated_at,avatar_url,credential_expires_at) '
        "VALUES(7,42,'投稿账号',?,1,'key','active',NULL,900,900,'',0)",
        (b'encrypted',),
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'comment_branch_state,danmaku_branch_state,aid,bvid,review_reason,'
        'attempt,next_attempt_at,created_at,updated_at) '
        "VALUES(9,?,7,'{}','waiting_review','confirmed','pending','pending',"
        "123,'BV1test','等待 B 站审核',2,1100,1001,1050)",
        (session.id,),
    )
    await database.execute(
        "UPDATE upload_jobs SET submission_verification_state='partial',"
        "submission_verified_at=1040,submission_verification_json='"
        '{"state":"partial","checked":["title"],'
        '"missing":["up_selection_reply"],"mismatches":[]}'
        "' WHERE id=9"
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,final_path,xml_path,artifact_state,'
        'upload_state,danmaku_import_state,remote_filename,cid) '
        "VALUES(10,9,1,'/rec/p1.flv','/rec/p1.mp4','/rec/p1.xml','ready',"
        "'confirmed','pending','remote-p1',NULL)"
    )
    await database.execute(
        'INSERT INTO upload_chunks('
        'part_id,chunk_no,offset,size,state,attempt) VALUES'
        "(10,0,0,4,'confirmed',1),(10,1,4,4,'prepared',0)"
    )
    for index, state in enumerate(
        ('confirmed', 'prepared', 'in_flight', 'unknown_outcome', 'failed_permanent')
    ):
        await database.execute(
            'INSERT INTO danmaku_items('
            'part_id,xml_identity,original_index,progress_ms,mode,fontsize,color,'
            'content,priority,request_fingerprint,state,error_message) '
            'VALUES(10,?,?,?,?,?,?,?,?,?,?,?)',
            (
                'xml-1',
                index,
                index * 1000,
                1,
                25,
                16_777_215,
                '弹幕 {}'.format(index),
                0,
                'fingerprint-{}'.format(index),
                state,
                '远端结果未知' if state == 'unknown_outcome' else None,
            ),
        )

    jobs = await journal.upload_jobs_for_sessions((session.id, 999))

    assert set(jobs) == {session.id}
    job = jobs[session.id]
    assert (job.state, job.submit_state, job.account_display_name) == (
        'waiting_review',
        'confirmed',
        '投稿账号',
    )
    assert (job.parts[0].part_index, job.parts[0].upload_state) == (1, 'confirmed')
    assert job.parts[0].remote_filename == 'remote-p1'
    assert job.parts[0].cid is None
    assert job.parts[0].confirmed_bytes == 4
    assert job.parts[0].total_bytes == 8
    assert job.confirmed_bytes == 4
    assert job.total_bytes == 8
    assert job.percent == 50.0
    assert job.current_part_index == 1
    assert job.bytes_per_second is None
    assert job.eta_seconds is None
    assert job.can_repair is False
    assert job.submission_verification_state == 'partial'
    assert job.submission_verified_at == 1040
    assert job.submission_verification == {
        'state': 'partial',
        'checked': ['title'],
        'missing': ['up_selection_reply'],
        'mismatches': [],
    }
    assert (
        job.danmaku_total,
        job.danmaku_confirmed,
        job.danmaku_pending,
        job.danmaku_unknown,
        job.danmaku_failed,
    ) == (5, 1, 2, 1, 1)
    assert len(job.unknown_danmaku_items) == 1
    assert job.unknown_danmaku_items[0].content == '弹幕 3'
    assert job.unknown_danmaku_items[0].part_index == 1

    now[0] = 1_002.0
    await database.execute(
        "UPDATE upload_chunks SET state='confirmed' " 'WHERE part_id=10 AND chunk_no=1'
    )
    progressed = (await journal.upload_jobs_for_sessions((session.id,)))[session.id]
    assert progressed.confirmed_bytes == 8
    assert progressed.percent == 100.0
    assert progressed.bytes_per_second == 2.0
    assert progressed.eta_seconds == 0

    await database.execute(
        "UPDATE upload_parts SET transcode_state='failed' WHERE id=10"
    )
    failed_jobs = await journal.upload_jobs_for_sessions((session.id,))
    assert failed_jobs[session.id].can_repair is True


@pytest.mark.asyncio
async def test_realtime_upload_progress_returns_active_job_bytes(database) -> None:
    now = [1_000.0]
    journal = RecordingJournalBridge(database, clock=lambda: now[0])
    run_id = await journal.recording_started(100, live_start_time=900)
    session = await journal.session_for_run(run_id)
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) VALUES(1,42,'账号',X'00',1,'k','active',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'preupload_finalized,created_at,updated_at) '
        "VALUES(3,?,1,'{}','uploading','prepared',0,1,1000)",
        (session.id,),
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,artifact_state,upload_state) '
        "VALUES(4,3,1,'/rec/p1.flv','ready','uploading')"
    )
    await database.execute(
        'INSERT INTO upload_chunks('
        'part_id,chunk_no,offset,size,state,attempt) '
        "VALUES(4,0,0,4,'confirmed',1),(4,1,4,4,'prepared',0)"
    )

    progress = await journal.realtime_upload_progress()

    assert progress == [
        {
            'jobId': 3,
            'sessionId': session.id,
            'state': 'uploading',
            'submitState': 'prepared',
            'preuploadFinalized': False,
            'displayState': 'preuploading',
            'aid': None,
            'bvid': None,
            'confirmedBytes': 4,
            'totalBytes': 8,
            'percent': 50.0,
            'bytesPerSecond': None,
            'etaSeconds': None,
            'currentPartIndex': 1,
            'confirmedPartCount': 0,
            'discoveredPartCount': 1,
        }
    ]

    now[0] = 1_002.0
    await database.execute(
        "UPDATE upload_chunks SET state='confirmed' " 'WHERE part_id=4 AND chunk_no=1'
    )

    progressed = (await journal.realtime_upload_progress())[0]

    assert progressed['confirmedBytes'] == 8
    assert progressed['totalBytes'] == 8
    assert progressed['percent'] == 100.0
    assert progressed['bytesPerSecond'] == 2.0
    assert progressed['etaSeconds'] == 0
    assert progressed['currentPartIndex'] == 1
    assert progressed['confirmedPartCount'] == 0
    assert progressed['discoveredPartCount'] == 1


@pytest.mark.asyncio
async def test_realtime_upload_progress_bounds_nas_shaped_history(
    database, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_000
    historical_job_count = 43
    confirmed_chunks_per_job = 24
    active_job_id = historical_job_count + 1

    def seed(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO bili_accounts("
            "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
            "state,created_at,updated_at) VALUES(1,42,'账号',X'00',1,'k','active',1,1)"
        )
        connection.executemany(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at) '
            "VALUES(?,?,?,'closed',1)",
            (
                (session_id, 10_000 + session_id, 'history-{}'.format(session_id))
                for session_id in range(1, active_job_id + 1)
            ),
        )
        terminal_jobs = [
            (
                job_id,
                job_id,
                1,
                '{}',
                'approved',
                'confirmed',
                'completed',
                'completed',
                'completed',
                'completed',
                1,
                1,
                1,
            )
            for job_id in range(1, active_job_id)
        ]
        active_job = (
            active_job_id,
            active_job_id,
            1,
            '{}',
            'uploading',
            'prepared',
            'disabled',
            'disabled',
            'disabled',
            'idle',
            0,
            1,
            now,
        )
        connection.executemany(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'comment_branch_state,danmaku_branch_state,collection_branch_state,'
            'repair_state,preupload_finalized,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            terminal_jobs + [active_job],
        )
        connection.executemany(
            'INSERT INTO upload_parts('
            'id,job_id,part_index,source_path,artifact_state,upload_state) '
            'VALUES(?,?,1,?,?,?)',
            [
                (
                    job_id,
                    job_id,
                    '/rec/history-{}.flv'.format(job_id),
                    'ready',
                    'confirmed',
                )
                for job_id in range(1, active_job_id)
            ]
            + [(active_job_id, active_job_id, '/rec/active.flv', 'ready', 'uploading')],
        )
        connection.executemany(
            'INSERT INTO upload_chunks('
            'part_id,chunk_no,offset,size,state,attempt) VALUES(?,?,?,?,?,?)',
            [
                (job_id, chunk_no, chunk_no, 1, 'confirmed', 1)
                for job_id in range(1, active_job_id)
                for chunk_no in range(confirmed_chunks_per_job)
            ]
            + [
                (active_job_id, 0, 0, 4, 'confirmed', 1),
                (active_job_id, 1, 4, 4, 'prepared', 0),
            ],
        )

    await database.write(seed)
    fetchall = database.fetchall
    fetchall_calls: List[Tuple[str, Tuple[object, ...]]] = []

    async def counting_fetchall(
        sql: str, parameters: Sequence[object] = ()
    ) -> List[sqlite3.Row]:
        fetchall_calls.append((sql, tuple(parameters)))
        return await fetchall(sql, parameters)

    monkeypatch.setattr(database, 'fetchall', counting_fetchall)
    journal = RecordingJournalBridge(database, clock=lambda: now)

    progress = await journal.realtime_upload_progress()

    assert [item['jobId'] for item in progress] == [active_job_id]
    assert len(fetchall_calls) <= 2
    assert len(fetchall_calls) == 2
    job_sql = ' '.join(fetchall_calls[0][0].split())
    assert (
        "job.state IN ('waiting_artifacts','ready','uploading','submitting',"
        "'waiting_review')" in job_sql
    )
    assert (
        "job.repair_state IN ('queued','checking','reuploading','editing',"
        "'waiting_review')" in job_sql
    )
    assert "job.comment_branch_state IN ('pending','running')" in job_sql
    assert "job.danmaku_branch_state IN ('pending','importing','publishing')" in job_sql
    assert "job.collection_branch_state IN ('pending','running')" in job_sql
    assert 'job.updated_at>=?' in job_sql
    assert fetchall_calls[0][1] == (now - 300,)
    aggregate_sql = ' '.join(fetchall_calls[1][0].split())
    assert 'part.job_id IN (?)' in aggregate_sql
    assert 'part.job_id IN ({})'.format(active_job_id) not in aggregate_sql
    assert fetchall_calls[1][1] == (active_job_id,)
    realtime_sql = ' '.join(sql for sql, _parameters in fetchall_calls).lower()
    for forbidden in (
        'bili_accounts',
        'policy_snapshot_json',
        'danmaku_items',
        'unknown_outcome',
        'submission_verification',
    ):
        assert forbidden not in realtime_sql

    await database.execute(
        "UPDATE upload_jobs SET state='approved',submit_state='confirmed',"
        "comment_branch_state='completed',danmaku_branch_state='completed',"
        "collection_branch_state='completed',repair_state='completed',"
        'preupload_finalized=1,updated_at=1 WHERE id=?',
        (active_job_id,),
    )
    fetchall_calls.clear()

    assert await journal.realtime_upload_progress() == []
    assert len(fetchall_calls) == 1
    assert 'upload_chunks' not in fetchall_calls[0][0]


class FakeEmitter:
    def __init__(self) -> None:
        self.listeners: List[object] = []

    def add_listener(self, listener: object) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener: object) -> None:
        self.listeners.remove(listener)


@pytest.mark.asyncio
async def test_listener_persists_recorder_and_postprocessor_lifecycle(
    database, tmp_path: Path
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    source = tmp_path / 'p1.flv'
    final = tmp_path / 'p1.mp4'
    xml = tmp_path / 'p1.xml'
    cover = tmp_path / 'cover.jpg'
    source.write_bytes(b'source')
    final.write_bytes(b'final')
    xml.write_text('<i><d>one</d></i>')
    cover.write_bytes(b'cover')
    recorder = FakeEmitter()
    recorder.live = SimpleNamespace(
        room_id=100,
        room_info=SimpleNamespace(
            room_id=100,
            live_start_time=900,
            title='直播标题',
            cover='https://example.invalid/cover.jpg',
            area_id=1,
            area_name='单机游戏',
            parent_area_id=2,
            parent_area_name='游戏',
        ),
        user_info=SimpleNamespace(uid=42, name='主播'),
    )
    recorder.record_start_time = 901
    postprocessor = FakeEmitter()
    listener = RecordingJournalListener(
        journal,
        recorder,  # type: ignore[arg-type]
        postprocessor,  # type: ignore[arg-type]
    )

    await listener.on_recording_started(recorder)  # type: ignore[arg-type]
    await listener.on_video_file_created(  # type: ignore[arg-type]
        recorder, str(source)
    )
    await listener.on_video_file_completed(  # type: ignore[arg-type]
        recorder, str(source)
    )
    await listener.on_danmaku_file_completed(  # type: ignore[arg-type]
        recorder, str(xml)
    )
    await listener.on_cover_image_downloaded(  # type: ignore[arg-type]
        recorder, str(cover)
    )
    await listener.on_video_postprocessing_result(  # type: ignore[arg-type]
        postprocessor, str(source), str(final)
    )
    assert listener._source_runs == {}
    await listener.on_recording_finished(recorder)  # type: ignore[arg-type]

    sessions = await journal.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].state == 'closed'
    assert sessions[0].title == '直播标题'
    assert sessions[0].anchor_name == '主播'
    assert sessions[0].cover_path == str(cover)
    assert sessions[0].parts[0].final_path == str(final)
    assert sessions[0].parts[0].xml_path == str(xml)
    assert sessions[0].parts[0].danmaku_count == 1

    listener.close()
    assert recorder.listeners == []
    assert postprocessor.listeners == []


@pytest.mark.asyncio
async def test_active_part_for_session_returns_only_the_latest_unfinished_part(
    database: BiliUploadDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    await journal.video_completed(run_id, '/rec/p1.flv')
    await journal.video_postprocessed(run_id, '/rec/p1.flv', '/rec/p1.mp4')
    await journal.video_created(run_id, '/rec/p2.flv', record_start_time=902)
    await journal.video_completed(run_id, '/rec/p2.flv')
    await journal.video_created(run_id, '/rec/p3.flv', record_start_time=903)
    session_id = (await journal.list_sessions())[0].id
    exists_calls = []

    def forbidden_exists(path: str) -> bool:
        exists_calls.append(path)
        raise AssertionError('active part projection must not touch the filesystem')

    monkeypatch.setattr(journal_module.os.path, 'exists', forbidden_exists)
    heartbeats = []

    async def heartbeat() -> None:
        for index in range(5):
            await asyncio.sleep(0)
            heartbeats.append(index)

    heartbeat_task = asyncio.create_task(heartbeat())

    active = await journal.active_part_for_session(session_id)
    await heartbeat_task

    assert active is not None
    assert active.part_index == 3
    assert active.artifact_state == 'recording'
    assert exists_calls == []
    assert heartbeats == [0, 1, 2, 3, 4]

    await journal.video_completed(run_id, '/rec/p3.flv')
    await journal.video_postprocessed(run_id, '/rec/p3.flv', '/rec/p3.mp4')
    await journal.video_postprocessed(run_id, '/rec/p2.flv', '/rec/p2.mp4')

    assert await journal.active_part_for_session(session_id) is None
