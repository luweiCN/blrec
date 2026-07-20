from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from blrec.bili_upload.accounts import AccountWriteGate
from blrec.bili_upload.database import BiliUploadDatabase, LeaseClaim, LeaseLost
from blrec.bili_upload.deletion_worker import LocalDeletionWorker
from blrec.bili_upload.errors import (
    BiliApiError,
    DefinitelyNotSent,
    RemoteOutcomeUnknown,
)
from blrec.bili_upload.upload import UploadCoordinator
from blrec.bili_upload.upos import (
    FileIdentity,
    UposUploadDeferred,
    UposUploader,
    UposUploadPaused,
    UposUploadStopped,
)


class FakeSession:
    def __init__(
        self,
        file_name: str,
        remote_file_name: str = 'remote-video',
        biz_id: str = '12345',
    ) -> None:
        self.file_name = file_name
        self.remote_file_name = remote_file_name
        self.biz_id = biz_id


class FakeProtocol:
    def __init__(self) -> None:
        self.preupload_calls: List[Mapping[str, Any]] = []
        self.chunk_calls: List[int] = []
        self.chunk_bodies: List[bytes] = []
        self.complete_calls = 0
        self.crash_on_chunk: Optional[int] = None
        self.complete_error: Optional[BaseException] = None
        self.active_chunks = 0
        self.max_active_chunks = 0
        self.chunk_delay = 0.0
        self.preupload_error: Optional[BaseException] = None
        self.chunk_errors: List[BaseException] = []

    async def preupload(self, _bundle: Any, params: Mapping[str, Any]) -> Any:
        self.preupload_calls.append(params)
        if self.preupload_error is not None:
            error, self.preupload_error = self.preupload_error, None
            raise error
        return SimpleNamespace(
            payload={'chunk_size': 4}, session=FakeSession(str(params['name']))
        )

    def export_upos_session(self, session: FakeSession) -> Mapping[str, Any]:
        return {
            'file_name': session.file_name,
            'remote_file_name': session.remote_file_name,
        }

    def restore_upos_session(self, payload: Mapping[str, Any]) -> FakeSession:
        return FakeSession(str(payload['file_name']), str(payload['remote_file_name']))

    async def upload_chunk(
        self,
        _session: FakeSession,
        *,
        chunk_no: int,
        chunks: int,
        start: int,
        total: int,
        body: bytes,
    ) -> Mapping[str, Any]:
        del chunks, start, total
        self.chunk_calls.append(chunk_no)
        self.chunk_bodies.append(body)
        self.active_chunks += 1
        self.max_active_chunks = max(self.max_active_chunks, self.active_chunks)
        try:
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            if chunk_no == self.crash_on_chunk:
                raise RuntimeError('simulated process crash')
            if self.chunk_errors:
                raise self.chunk_errors.pop(0)
            return {'etag': 'etag-{}'.format(chunk_no)}
        finally:
            self.active_chunks -= 1

    async def complete_upload(
        self, _session: FakeSession, *, parts: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        self.complete_calls += 1
        if self.complete_error is not None:
            raise self.complete_error
        assert [part['partNumber'] for part in parts] == list(range(1, len(parts) + 1))
        return {'OK': 1}


async def prepared_part(
    database: BiliUploadDatabase,
    path: Path,
    *,
    stored_identity: Optional[FileIdentity] = None,
) -> int:
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,42,'u',X'00',1,'k','active',1,1)"
    )
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at) "
        "VALUES(1,100,'100:1','closed',1)"
    )
    await database.execute(
        "INSERT INTO upload_jobs("
        "id,session_id,account_id,policy_snapshot_json,state,submit_state,"
        "created_at,updated_at) "
        "VALUES(1,1,1,'{}','ready','prepared',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,final_path,file_identity,'
        "artifact_state,upload_state) VALUES(1,1,1,?,?,?,'ready','prepared')",
        (
            str(path),
            str(path),
            None if stored_identity is None else stored_identity.to_json(),
        ),
    )
    return 1


async def claim_job(database: BiliUploadDatabase, owner: str = 'worker') -> LeaseClaim:
    claim = await database.claim(
        'upload_jobs', ('ready', 'uploading'), owner, now=int(time.time())
    )
    assert claim is not None
    return claim


@pytest.mark.asyncio
async def test_file_identity_detects_replaced_final_file(tmp_path: Path) -> None:
    path = tmp_path / 'part.mp4'
    path.write_bytes(b'a' * (2 * 1024 * 1024))
    first = FileIdentity.from_path(str(path))
    path.write_bytes(b'b' * (2 * 1024 * 1024))
    second = FileIdentity.from_path(str(path))

    assert first != second
    assert FileIdentity.from_json(first.to_json()) == first


@pytest.mark.asyncio
async def test_restart_skips_confirmed_chunks(tmp_path: Path) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcdefghijkl')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        crashing_protocol = FakeProtocol()
        crashing_protocol.crash_on_chunk = 1
        uploader = UposUploader(
            database, crashing_protocol, chunk_size=4, concurrency=1
        )

        with pytest.raises(RuntimeError, match='simulated process crash'):
            await uploader.upload_part(part_id, bundle=object(), claim=claim)

        assert crashing_protocol.chunk_calls == [0, 1]
        assert (
            await database.scalar(
                'SELECT state FROM upload_chunks WHERE part_id=? AND chunk_no=0',
                (part_id,),
            )
            == 'confirmed'
        )

        resumed_protocol = FakeProtocol()
        resumed = UposUploader(database, resumed_protocol, chunk_size=4, concurrency=1)
        await resumed.upload_part(part_id, bundle=object(), claim=claim)

        assert resumed_protocol.preupload_calls == []
        assert resumed_protocol.chunk_calls == [1, 2]
        assert resumed_protocol.complete_calls == 1
        assert (
            await database.scalar(
                'SELECT upload_state FROM upload_parts WHERE id=?', (part_id,)
            )
            == 'confirmed'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_changed_file_identity_stops_before_network(tmp_path: Path) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'original')
    identity = FileIdentity.from_path(str(path))
    path.write_bytes(b'replaced')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, path, stored_identity=identity)
        claim = await claim_job(database)
        protocol = FakeProtocol()
        uploader = UposUploader(database, protocol, chunk_size=4, concurrency=2)

        with pytest.raises(UposUploadPaused, match='identity'):
            await uploader.upload_part(part_id, bundle=object(), claim=claim)

        assert protocol.preupload_calls == []
        assert protocol.chunk_calls == []
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'paused'
        )
        assert (
            await database.scalar(
                'SELECT artifact_state FROM upload_parts WHERE id=?', (part_id,)
            )
            == 'manual_review'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_chunk_reads_and_concurrency_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.bili_upload.upos.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcdefghijklmnopq')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        protocol = FakeProtocol()
        protocol.chunk_delay = 0.01
        uploader = UposUploader(database, protocol, chunk_size=4, concurrency=2)

        await uploader.upload_part(part_id, bundle=object(), claim=claim)

        assert protocol.max_active_chunks == 2
        assert max(map(len, protocol.chunk_bodies)) <= 4
        assert sum(map(len, protocol.chunk_bodies)) == path.stat().st_size
        assert any(
            event == 'upload_progress'
            and fields['percent'] == 100
            and fields['confirmed_bytes'] == path.stat().st_size
            for event, fields in events
        )
        assert any(event == 'upload_part_completed' for event, _fields in events)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_progress_milestones_use_whole_job_bytes_for_multiple_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.bili_upload.upos.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    first = tmp_path / 'p1.flv'
    second = tmp_path / 'p2.flv'
    first.write_bytes(b'abcd')
    second.write_bytes(b'x' * 16)
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, first)
        await database.execute(
            'INSERT INTO upload_parts('
            'id,job_id,part_index,source_path,final_path,file_identity,'
            "artifact_state,upload_state) VALUES(2,1,2,?,?,?,'ready','prepared')",
            (str(second), str(second), FileIdentity.from_path(str(second)).to_json()),
        )
        claim = await claim_job(database)
        uploader = UposUploader(database, FakeProtocol(), chunk_size=4, concurrency=1)

        await uploader.upload_part(part_id, bundle=object(), claim=claim)

        progress = [fields for event, fields in events if event == 'upload_progress']
        assert progress[-1]['percent'] == 20
        assert progress[-1]['confirmed_bytes'] == 4
        assert progress[-1]['total_bytes'] == 20
        assert all(fields['percent'] != 100 for fields in progress)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_preupload_admission_window_starts_at_one_and_grows_to_five(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        paths = []
        for index in range(1, 7):
            path = tmp_path / 'part-{}.flv'.format(index)
            path.write_bytes(('part-{}'.format(index)).encode('ascii'))
            paths.append(path)
        await prepared_part(database, paths[0])
        for index, path in enumerate(paths[1:], start=2):
            await database.execute(
                'INSERT INTO upload_parts('
                'id,job_id,part_index,source_path,final_path,artifact_state,'
                "upload_state) VALUES(?,1,?,?,?,'ready','prepared')",
                (index, index, str(path), str(path)),
            )
        claim = await claim_job(database)
        protocol = FakeProtocol()
        uploader = UposUploader(
            database, protocol, chunk_size=32, concurrency=1, clock=lambda: 1000
        )

        for part_id in range(1, 6):
            await uploader.upload_part(part_id, bundle=object(), claim=claim)
        with pytest.raises(RuntimeError, match='preupload.*deferred') as caught:
            await uploader.upload_part(6, bundle=object(), claim=claim)

        assert getattr(caught.value, 'retry_after_seconds', 0) == 60
        assert len(protocol.preupload_calls) == 5
        assert (
            await database.scalar('SELECT upload_state FROM upload_parts WHERE id=6')
            == 'prepared'
        )
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'ready'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_preupload_406_defers_without_pausing_job(tmp_path: Path) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcdefgh')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        protocol = FakeProtocol()
        protocol.preupload_error = BiliApiError(406, operation='preupload')
        uploader = UposUploader(
            database, protocol, chunk_size=4, concurrency=1, clock=lambda: 1000
        )

        with pytest.raises(RuntimeError, match='preupload.*deferred') as caught:
            await uploader.upload_part(part_id, bundle=object(), claim=claim)

        assert getattr(caught.value, 'retry_after_seconds', 0) >= 60
        assert (
            await database.scalar(
                'SELECT upload_state FROM upload_parts WHERE id=?', (part_id,)
            )
            == 'prepared'
        )
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'ready'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unknown_complete_result_is_terminal_and_never_retried(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcd')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        protocol = FakeProtocol()
        protocol.complete_error = RemoteOutcomeUnknown('complete_upload')
        uploader = UposUploader(database, protocol, chunk_size=4, concurrency=1)

        with pytest.raises(UposUploadPaused, match='outcome'):
            await uploader.upload_part(part_id, bundle=object(), claim=claim)
        protocol.complete_error = None
        with pytest.raises(UposUploadPaused, match='outcome'):
            await uploader.upload_part(part_id, bundle=object(), claim=claim)

        assert protocol.complete_calls == 1
        assert (
            await database.scalar(
                'SELECT upload_state FROM upload_parts WHERE id=?', (part_id,)
            )
            == 'unknown_outcome'
        )
        outcome = await database.fetchone(
            "SELECT outcome_state,outcome_json,acknowledged_at "
            "FROM owner_handoff_outcomes "
            "WHERE owner_kind='upos' AND owner_id=? "
            "AND side_effect_key='complete' AND source_generation=0",
            (part_id,),
        )
        assert outcome is not None
        assert dict(outcome) == {
            'outcome_state': 'unknown_terminal',
            'outcome_json': '{}',
            'acknowledged_at': outcome['acknowledged_at'],
        }
        assert outcome['acknowledged_at'] is not None
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'ready'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_chunk_success_after_deletion_is_handed_off_without_new_chunk(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcdefgh')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    request_started = asyncio.Event()
    release_response = asyncio.Event()

    class BlockingChunkProtocol(FakeProtocol):
        async def upload_chunk(
            self,
            session: FakeSession,
            *,
            chunk_no: int,
            chunks: int,
            start: int,
            total: int,
            body: bytes,
        ) -> Mapping[str, Any]:
            del session, chunks, start, total, body
            self.chunk_calls.append(chunk_no)
            request_started.set()
            await release_response.wait()
            return {'etag': 'etag-{}'.format(chunk_no)}

    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        protocol = BlockingChunkProtocol()
        uploader = UposUploader(database, protocol, chunk_size=4, concurrency=1)
        task = asyncio.create_task(
            uploader.upload_part(part_id, bundle=object(), claim=claim)
        )
        await asyncio.wait_for(request_started.wait(), timeout=1)
        await database.execute(
            "UPDATE recording_sessions SET deletion_state='requested',"
            'cancellation_generation=1 WHERE id=1'
        )
        release_response.set()

        with pytest.raises(UposUploadStopped, match='deletion'):
            await asyncio.wait_for(task, timeout=1)
        assert protocol.chunk_calls == [0]
        outcome = await database.fetchone(
            "SELECT outcome_state,outcome_json,acknowledged_at "
            "FROM owner_handoff_outcomes WHERE owner_kind='upos' "
            "AND owner_id=? AND side_effect_key='chunk:0' "
            'AND source_generation=0',
            (part_id,),
        )
        assert outcome is not None
        assert outcome['outcome_state'] == 'confirmed_success'
        assert json.loads(str(outcome['outcome_json'])) == {'etag': 'etag-0'}
        assert outcome['acknowledged_at'] is not None
    finally:
        release_response.set()
        if 'task' in locals():
            await asyncio.gather(task, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_completion_success_after_deletion_is_terminal_handoff(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcd')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    request_started = asyncio.Event()
    release_response = asyncio.Event()

    class BlockingCompletionProtocol(FakeProtocol):
        async def complete_upload(
            self, session: FakeSession, *, parts: Sequence[Mapping[str, Any]]
        ) -> Mapping[str, Any]:
            del session, parts
            self.complete_calls += 1
            request_started.set()
            await release_response.wait()
            return {'OK': 1}

    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        protocol = BlockingCompletionProtocol()
        uploader = UposUploader(database, protocol, chunk_size=4, concurrency=1)
        task = asyncio.create_task(
            uploader.upload_part(part_id, bundle=object(), claim=claim)
        )
        await asyncio.wait_for(request_started.wait(), timeout=1)
        await database.execute(
            "UPDATE recording_sessions SET deletion_state='requested',"
            'cancellation_generation=1 WHERE id=1'
        )
        release_response.set()

        with pytest.raises(UposUploadStopped, match='deletion'):
            await asyncio.wait_for(task, timeout=1)
        assert protocol.complete_calls == 1
        outcome = await database.fetchone(
            "SELECT outcome_state,outcome_json,acknowledged_at "
            "FROM owner_handoff_outcomes "
            "WHERE owner_kind='upos' AND owner_id=? "
            "AND side_effect_key='complete' AND source_generation=0",
            (part_id,),
        )
        assert outcome is not None
        assert outcome['outcome_state'] == 'confirmed_success'
        assert json.loads(str(outcome['outcome_json'])) == {
            'remote_filename': 'remote-video'
        }
        assert outcome['acknowledged_at'] is not None
        assert (
            await database.scalar(
                'SELECT upload_state FROM upload_parts WHERE id=?', (part_id,)
            )
            == 'completing'
        )
    finally:
        release_response.set()
        if 'task' in locals():
            await asyncio.gather(task, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_deletion_before_completion_prevents_remote_completion(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcd')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    before_completion = asyncio.Event()
    release_completion = asyncio.Event()
    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        protocol = FakeProtocol()
        uploader = UposUploader(database, protocol, chunk_size=4, concurrency=1)
        original_complete = uploader._complete

        async def complete_after_barrier(*args: Any, **kwargs: Any) -> str:
            before_completion.set()
            await release_completion.wait()
            return await original_complete(*args, **kwargs)

        uploader._complete = complete_after_barrier  # type: ignore
        task = asyncio.create_task(
            uploader.upload_part(part_id, bundle=object(), claim=claim)
        )
        await asyncio.wait_for(before_completion.wait(), timeout=1)
        await database.execute(
            "UPDATE recording_sessions SET deletion_state='requested',"
            'cancellation_generation=1 WHERE id=1'
        )
        release_completion.set()

        with pytest.raises(UposUploadStopped, match='deletion'):
            await asyncio.wait_for(task, timeout=1)
        assert protocol.complete_calls == 0
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM owner_handoff_outcomes "
                "WHERE owner_kind='upos' AND side_effect_key='complete'"
            )
            == 0
        )
    finally:
        release_completion.set()
        if 'task' in locals():
            await asyncio.gather(task, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_concurrent_chunks_all_acknowledge_deletion_before_owner_returns(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcdefgh')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    both_started = asyncio.Event()
    release_first = asyncio.Event()

    class SplitChunkProtocol(FakeProtocol):
        async def upload_chunk(
            self,
            session: FakeSession,
            *,
            chunk_no: int,
            chunks: int,
            start: int,
            total: int,
            body: bytes,
        ) -> Mapping[str, Any]:
            del session, chunks, start, total, body
            self.chunk_calls.append(chunk_no)
            if len(self.chunk_calls) == 2:
                both_started.set()
            if chunk_no == 0:
                await release_first.wait()
                return {'etag': 'etag-0'}
            await asyncio.Future()
            raise AssertionError('unreachable')

    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        protocol = SplitChunkProtocol()
        uploader = UposUploader(database, protocol, chunk_size=4, concurrency=2)
        task = asyncio.create_task(
            uploader.upload_part(part_id, bundle=object(), claim=claim)
        )
        await asyncio.wait_for(both_started.wait(), timeout=1)
        await database.execute(
            "UPDATE recording_sessions SET deletion_state='requested',"
            'cancellation_generation=1 WHERE id=1'
        )
        release_first.set()

        with pytest.raises(UposUploadStopped, match='deletion'):
            await asyncio.wait_for(task, timeout=1)
        outcomes = await database.fetchall(
            "SELECT side_effect_key,outcome_state FROM owner_handoff_outcomes "
            "WHERE owner_kind='upos' AND owner_id=? ORDER BY side_effect_key",
            (part_id,),
        )
        assert [(row['side_effect_key'], row['outcome_state']) for row in outcomes] == [
            ('chunk:0', 'confirmed_success'),
            ('chunk:1', 'unknown_terminal'),
        ]
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM owner_handoff_outcomes "
                "WHERE outcome_state='in_flight'"
            )
            == 1
        )
    finally:
        release_first.set()
        if 'task' in locals():
            await asyncio.gather(task, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('code', (406, 408, 425, 429))
async def test_transient_complete_rejection_is_deferred_without_pausing(
    tmp_path: Path, code: int
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcd')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        protocol = FakeProtocol()
        protocol.complete_error = BiliApiError(code, operation='complete_upload')
        uploader = UposUploader(database, protocol, chunk_size=4, concurrency=1)

        with pytest.raises(UposUploadDeferred):
            await uploader.upload_part(part_id, bundle=object(), claim=claim)

        assert (
            await database.scalar(
                'SELECT upload_state FROM upload_parts WHERE id=?', (part_id,)
            )
            == 'uploading'
        )
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'ready'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_transient_chunk_failures_defer_and_resume_without_pausing(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcd')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        protocol = FakeProtocol()
        protocol.chunk_errors = [
            RemoteOutcomeUnknown('upload_chunk'),
            RemoteOutcomeUnknown('upload_chunk'),
            RemoteOutcomeUnknown('upload_chunk'),
        ]
        uploader = UposUploader(
            database, protocol, chunk_size=4, concurrency=1, max_chunk_attempts=3
        )

        with pytest.raises(UposUploadDeferred):
            await uploader.upload_part(part_id, bundle=object(), claim=claim)
        await uploader.upload_part(part_id, bundle=object(), claim=claim)

        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'ready'
        )
        assert (
            await database.scalar(
                'SELECT upload_state FROM upload_parts WHERE id=?', (part_id,)
            )
            == 'confirmed'
        )
        assert protocol.chunk_calls == [0, 0, 0, 0]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_preupload_persists_bilibili_cid_for_submission_diagnostics(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcd')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        uploader = UposUploader(database, FakeProtocol(), chunk_size=4, concurrency=1)

        await uploader.upload_part(part_id, bundle=object(), claim=claim)

        assert (
            await database.scalar('SELECT cid FROM upload_parts WHERE id=?', (part_id,))
            == 12345
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_stale_lease_cannot_start_upload(tmp_path: Path) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcd')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, path)
        first = await database.claim('upload_jobs', ('ready',), 'old', now=1000)
        assert first is not None
        second = await database.claim('upload_jobs', ('ready',), 'new', now=1121)
        assert second is not None
        protocol = FakeProtocol()
        uploader = UposUploader(database, protocol, chunk_size=4, concurrency=1)

        with pytest.raises(LeaseLost):
            await uploader.upload_part(part_id, bundle=object(), claim=first)

        assert protocol.preupload_calls == []
        assert protocol.chunk_calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_upload_renews_lease_in_second_half_of_ttl(tmp_path: Path) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcd')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, path)
        claim = await database.claim('upload_jobs', ('ready',), 'worker', now=1000)
        assert claim is not None
        uploader = UposUploader(
            database, FakeProtocol(), chunk_size=4, concurrency=1, clock=lambda: 1060
        )

        await uploader.upload_part(part_id, bundle=object(), claim=claim)

        assert (
            await database.scalar('SELECT lease_until FROM upload_jobs WHERE id=1')
            == 1180
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_graceful_stop_happens_before_next_chunk(tmp_path: Path) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcdefgh')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        protocol = FakeProtocol()
        uploader = UposUploader(
            database, protocol, chunk_size=4, concurrency=1, stop_requested=lambda: True
        )

        with pytest.raises(UposUploadStopped):
            await uploader.upload_part(part_id, bundle=object(), claim=claim)

        assert len(protocol.preupload_calls) == 1
        assert protocol.chunk_calls == []
        assert (
            await database.scalar(
                'SELECT upload_state FROM upload_parts WHERE id=?', (part_id,)
            )
            == 'uploading'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cancelled_sibling_chunk_is_prepared_before_job_lease_can_release(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcdefgh')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    both_started = asyncio.Event()

    class SplitProtocol(FakeProtocol):
        async def upload_chunk(
            self,
            session: FakeSession,
            *,
            chunk_no: int,
            chunks: int,
            start: int,
            total: int,
            body: bytes,
        ) -> Mapping[str, Any]:
            del session, chunks, start, total, body
            self.chunk_calls.append(chunk_no)
            if len(self.chunk_calls) == 2:
                both_started.set()
            await both_started.wait()
            if chunk_no == 0:
                raise BiliApiError(408, operation='upload_chunk')
            await asyncio.Future()
            raise AssertionError('unreachable')

    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        protocol = SplitProtocol()
        uploader = UposUploader(
            database, protocol, chunk_size=4, concurrency=2, clock=lambda: 1000
        )

        with pytest.raises(UposUploadDeferred):
            await uploader.upload_part(part_id, bundle=object(), claim=claim)

        rows = await database.fetchall(
            'SELECT chunk_no,state FROM upload_chunks ORDER BY chunk_no'
        )
        assert [(int(row['chunk_no']), str(row['state'])) for row in rows] == [
            (0, 'prepared'),
            (1, 'prepared'),
        ]
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM owner_handoff_outcomes "
                "WHERE owner_kind='upos' AND outcome_state='in_flight'"
            )
            == 0
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_completion_not_sent_state_is_atomic_before_followup_work(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcd')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        part_id = await prepared_part(database, path)
        claim = await claim_job(database)
        protocol = FakeProtocol()
        protocol.complete_error = DefinitelyNotSent('complete_upload')
        uploader = UposUploader(database, protocol, chunk_size=4, concurrency=1)
        original_settle = uploader._settle_completion_failure

        async def settle_then_crash(*args: Any, **kwargs: Any) -> bool:
            await original_settle(*args, **kwargs)
            raise RuntimeError('simulated crash after completion settlement')

        uploader._settle_completion_failure = settle_then_crash  # type: ignore

        with pytest.raises(RuntimeError, match='after completion settlement'):
            await uploader.upload_part(part_id, bundle=object(), claim=claim)

        assert (
            await database.scalar(
                'SELECT upload_state FROM upload_parts WHERE id=?', (part_id,)
            )
            == 'uploading'
        )
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM owner_handoff_outcomes "
                "WHERE owner_kind='upos' AND owner_id=? "
                "AND side_effect_key='complete'",
                (part_id,),
            )
            == 0
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cancel_after_chunk_intent_commit_does_not_block_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcdefgh')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    intent_committed = threading.Event()
    release_database_thread = threading.Event()
    target_ready = asyncio.Event()
    peer_failed = asyncio.Event()
    original_write_sync = database._write_sync
    loop = asyncio.get_running_loop()

    def block_after_target_intent_commit(operation: Any) -> Any:
        result = original_write_sync(operation)
        if getattr(operation, '__name__', '') == 'begin':
            committed = (
                database._require_connection()
                .execute(
                    "SELECT 1 FROM owner_handoff_outcomes WHERE owner_kind='upos' "
                    "AND side_effect_key='chunk:1' AND outcome_state='in_flight'"
                )
                .fetchone()
            )
            if committed is not None and not intent_committed.is_set():
                intent_committed.set()
                loop.call_soon_threadsafe(target_ready.set)
                if not release_database_thread.wait(timeout=5):
                    raise RuntimeError('test chunk intent barrier timed out')
        return result

    monkeypatch.setattr(database, '_write_sync', block_after_target_intent_commit)
    try:
        await prepared_part(database, path)
        protocol = FakeProtocol()
        uploader = UposUploader(
            database, protocol, chunk_size=4, concurrency=2, clock=lambda: 1000
        )
        original_upload_chunk = uploader._upload_chunk

        async def fail_peer_before_intent(
            part_id: int,
            identity: FileIdentity,
            session: Any,
            session_json: str,
            claim: LeaseClaim,
            chunk: Any,
            chunks: int,
        ) -> None:
            if chunk.chunk_no == 0:
                await target_ready.wait()
                peer_failed.set()
                raise UposUploadDeferred(30, 'peer chunk deferred')
            await original_upload_chunk(
                part_id, identity, session, session_json, claim, chunk, chunks
            )

        uploader._upload_chunk = fail_peer_before_intent  # type: ignore

        async def load_bundle(account_id: int) -> object:
            assert account_id == 1
            return object()

        coordinator = UploadCoordinator(
            database,
            protocol,
            uploader,
            bundle_loader=load_bundle,
            account_gates=AccountWriteGate(database),
            cover_resolver=object(),
            worker_id='upload-test',
            clock=lambda: 1000,
        )
        process = asyncio.create_task(coordinator.run_once())
        committed = await loop.run_in_executor(None, intent_committed.wait, 2)
        assert committed
        await asyncio.wait_for(peer_failed.wait(), timeout=1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release_database_thread.set()

        assert await asyncio.wait_for(process, timeout=2) == 1
        chunks = await database.fetchall(
            'SELECT chunk_no,state FROM upload_chunks ORDER BY chunk_no'
        )
        assert [(int(row['chunk_no']), str(row['state'])) for row in chunks] == [
            (0, 'prepared'),
            (1, 'prepared'),
        ]
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM owner_handoff_outcomes "
                "WHERE owner_kind='upos' AND outcome_state='in_flight'"
            )
            == 0
        )

        deletion = LocalDeletionWorker(
            database, recording_root=tmp_path, clip_root=tmp_path / 'clips'
        )
        await deletion.request_session(1, manager_subject='manager')
        assert await deletion.run_once() == ('session', 1)
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
        assert not path.exists()
    finally:
        release_database_thread.set()
        if 'process' in locals():
            await asyncio.gather(process, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_cancel_before_chunk_success_commit_preserves_etag_and_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'abcdefgh')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    database_thread_blocked = threading.Event()
    release_database_thread = threading.Event()
    allow_target_response = asyncio.Event()
    deletion_write_queued = asyncio.Event()
    complete_write_queued = asyncio.Event()
    peer_failed = asyncio.Event()
    blocker_tasks: List[asyncio.Task[Any]] = []
    original_write = database.write
    loop = asyncio.get_running_loop()

    async def observe_chunk_complete(operation: Any) -> Any:
        operation_name = getattr(operation, '__name__', '')
        if operation_name == 'complete':
            complete_write_queued.set()
        elif operation_name == 'request':
            deletion_write_queued.set()
        return await original_write(operation)

    monkeypatch.setattr(database, 'write', observe_chunk_complete)

    class SuccessfulTargetProtocol(FakeProtocol):
        async def upload_chunk(
            self,
            session: FakeSession,
            *,
            chunk_no: int,
            chunks: int,
            start: int,
            total: int,
            body: bytes,
        ) -> Mapping[str, Any]:
            del session, chunks, start, total, body
            self.chunk_calls.append(chunk_no)

            def occupy_database(_connection: Any) -> None:
                database_thread_blocked.set()
                if not release_database_thread.wait(timeout=5):
                    raise RuntimeError('test chunk completion barrier timed out')

            blocker_tasks.append(asyncio.create_task(database.write(occupy_database)))
            blocked = await loop.run_in_executor(None, database_thread_blocked.wait, 2)
            assert blocked
            await allow_target_response.wait()
            return {'etag': 'etag-known-success'}

    try:
        await prepared_part(database, path)
        protocol = SuccessfulTargetProtocol()
        uploader = UposUploader(
            database, protocol, chunk_size=4, concurrency=2, clock=lambda: 1000
        )
        original_upload_chunk = uploader._upload_chunk

        async def fail_peer_before_intent(
            part_id: int,
            identity: FileIdentity,
            session: Any,
            session_json: str,
            claim: LeaseClaim,
            chunk: Any,
            chunks: int,
        ) -> None:
            if chunk.chunk_no == 0:
                await complete_write_queued.wait()
                peer_failed.set()
                raise UposUploadDeferred(30, 'peer chunk deferred')
            await original_upload_chunk(
                part_id, identity, session, session_json, claim, chunk, chunks
            )

        uploader._upload_chunk = fail_peer_before_intent  # type: ignore

        async def load_bundle(account_id: int) -> object:
            assert account_id == 1
            return object()

        coordinator = UploadCoordinator(
            database,
            protocol,
            uploader,
            bundle_loader=load_bundle,
            account_gates=AccountWriteGate(database),
            cover_resolver=object(),
            worker_id='upload-test',
            clock=lambda: 1000,
        )
        process = asyncio.create_task(coordinator.run_once())
        blocked = await loop.run_in_executor(None, database_thread_blocked.wait, 2)
        assert blocked
        deletion = LocalDeletionWorker(
            database, recording_root=tmp_path, clip_root=tmp_path / 'clips'
        )
        deletion_request = asyncio.create_task(
            deletion.request_session(1, manager_subject='manager')
        )
        await asyncio.wait_for(deletion_write_queued.wait(), timeout=1)
        allow_target_response.set()
        await asyncio.wait_for(peer_failed.wait(), timeout=2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release_database_thread.set()

        assert await asyncio.wait_for(deletion_request, timeout=2) == 1
        assert await asyncio.wait_for(process, timeout=2) == 1
        target = await database.fetchone(
            'SELECT state,etag FROM upload_chunks WHERE chunk_no=1'
        )
        assert target is not None
        assert dict(target) == {'state': 'confirmed', 'etag': 'etag-known-success'}
        outcome = await database.fetchone(
            'SELECT outcome_state,outcome_json,acknowledged_at '
            "FROM owner_handoff_outcomes WHERE owner_kind='upos' "
            "AND side_effect_key='chunk:1'"
        )
        assert outcome is not None
        assert outcome['outcome_state'] == 'confirmed_success'
        assert json.loads(str(outcome['outcome_json'])) == {
            'etag': 'etag-known-success'
        }
        assert outcome['acknowledged_at'] is not None

        assert await deletion.run_once() == ('session', 1)
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
        assert not path.exists()
    finally:
        allow_target_response.set()
        release_database_thread.set()
        if blocker_tasks:
            await asyncio.gather(*blocker_tasks, return_exceptions=True)
        if 'deletion_request' in locals():
            await asyncio.gather(deletion_request, return_exceptions=True)
        if 'process' in locals():
            await asyncio.gather(process, return_exceptions=True)
        await database.close()
