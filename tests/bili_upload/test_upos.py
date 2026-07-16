from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from blrec.bili_upload.database import BiliUploadDatabase, LeaseClaim, LeaseLost
from blrec.bili_upload.errors import RemoteOutcomeUnknown
from blrec.bili_upload.upos import (
    FileIdentity,
    UposUploader,
    UposUploadPaused,
    UposUploadStopped,
)


class FakeSession:
    def __init__(self, file_name: str, remote_file_name: str = 'remote-video') -> None:
        self.file_name = file_name
        self.remote_file_name = remote_file_name


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

    async def preupload(self, _bundle: Any, params: Mapping[str, Any]) -> Any:
        self.preupload_calls.append(params)
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
async def test_unknown_complete_result_is_paused_and_never_repeated(
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

        with pytest.raises(UposUploadPaused, match='unknown'):
            await uploader.upload_part(part_id, bundle=object(), claim=claim)
        with pytest.raises(UposUploadPaused, match='unknown'):
            await uploader.upload_part(part_id, bundle=object(), claim=claim)

        assert protocol.complete_calls == 1
        assert (
            await database.scalar(
                'SELECT upload_state FROM upload_parts WHERE id=?', (part_id,)
            )
            == 'unknown_outcome'
        )
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'paused'
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
