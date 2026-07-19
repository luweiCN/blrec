from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import time
from collections import deque
from dataclasses import asdict, dataclass
from functools import partial
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

from blrec.logging.audit import audit

from .crypto import CredentialBundle
from .database import BiliUploadDatabase, LeaseClaim, LeaseLost
from .errors import (
    BiliApiError,
    DefinitelyNotSent,
    ProtocolContractError,
    RemoteOutcomeUnknown,
)

__all__ = (
    'FileIdentity',
    'UposUploader',
    'UposUploadDeferred',
    'UposUploadPaused',
    'UposUploadStopped',
)


class UposUploadDeferred(RuntimeError):
    def __init__(self, retry_after_seconds: int, reason: str) -> None:
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        self.reason = reason
        super().__init__('preupload deferred: {}'.format(reason))


class UposUploadPaused(RuntimeError):
    pass


class UposUploadStopped(RuntimeError):
    pass


class _SessionExpired(RuntimeError):
    pass


class _PreuploadAdmissionWindow:
    _WINDOW_SECONDS = 60
    _MAX_CAPACITY = 5
    _MAX_COOLDOWN_SECONDS = 15 * 60

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._capacity = 1
        self._starts: Deque[float] = deque()
        self._cooldown_until = 0.0
        self._consecutive_rate_limits = 0

    def reserve(self) -> int:
        now = float(self._clock())
        if now < self._cooldown_until:
            return max(1, int(math.ceil(self._cooldown_until - now)))
        cutoff = now - self._WINDOW_SECONDS
        while self._starts and self._starts[0] <= cutoff:
            self._starts.popleft()
        if len(self._starts) >= self._capacity:
            return max(1, int(math.ceil(self._starts[0] + self._WINDOW_SECONDS - now)))
        self._starts.append(now)
        return 0

    def succeeded(self) -> None:
        self._capacity = min(self._MAX_CAPACITY, self._capacity + 1)
        self._consecutive_rate_limits = 0

    def rate_limited(self) -> int:
        self._capacity = max(1, self._capacity // 2)
        self._consecutive_rate_limits += 1
        delay = min(
            self._MAX_COOLDOWN_SECONDS,
            60 * (2 ** min(self._consecutive_rate_limits - 1, 4)),
        )
        self._cooldown_until = max(self._cooldown_until, float(self._clock()) + delay)
        return delay


@dataclass(frozen=True)
class FileIdentity:
    canonical_path: str
    size: int
    mtime_ns: int
    head_digest: str
    tail_digest: str

    _FIELDS = frozenset(
        ('canonical_path', 'size', 'mtime_ns', 'head_digest', 'tail_digest')
    )

    @classmethod
    def from_path(cls, path: str, sample_size: int = 1024 * 1024) -> 'FileIdentity':
        canonical = os.path.realpath(path)
        file_stat = os.stat(canonical)
        with open(canonical, 'rb') as file:
            head = file.read(sample_size)
            file.seek(max(0, file_stat.st_size - sample_size))
            tail = file.read(sample_size)
        return cls(
            canonical_path=canonical,
            size=file_stat.st_size,
            mtime_ns=file_stat.st_mtime_ns,
            head_digest=hashlib.blake2b(head, digest_size=16).hexdigest(),
            tail_digest=hashlib.blake2b(tail, digest_size=16).hexdigest(),
        )

    def to_json(self) -> str:
        return json.dumps(
            asdict(self), ensure_ascii=False, separators=(',', ':'), sort_keys=True
        )

    @classmethod
    def from_json(cls, value: str) -> 'FileIdentity':
        try:
            payload = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            raise ValueError('invalid file identity') from None
        if not isinstance(payload, dict) or set(payload) != cls._FIELDS:
            raise ValueError('invalid file identity')
        if (
            not isinstance(payload['canonical_path'], str)
            or not payload['canonical_path']
            or type(payload['size']) is not int
            or payload['size'] < 0
            or type(payload['mtime_ns']) is not int
            or payload['mtime_ns'] < 0
            or not isinstance(payload['head_digest'], str)
            or not isinstance(payload['tail_digest'], str)
        ):
            raise ValueError('invalid file identity')
        return cls(**payload)


@dataclass(frozen=True)
class _Part:
    id: int
    job_id: int
    path: str
    file_identity: Optional[str]
    artifact_state: str
    upload_state: str
    remote_filename: Optional[str]
    upload_session_json: Optional[str]


@dataclass(frozen=True)
class _Chunk:
    chunk_no: int
    offset: int
    size: int
    state: str
    attempt: int


class UposUploader:
    _SESSION_FIELDS = frozenset(('format_version', 'renewal_count', 'session'))

    def __init__(
        self,
        database: BiliUploadDatabase,
        protocol: Any,
        *,
        chunk_size: int,
        concurrency: int,
        max_chunk_attempts: int = 3,
        clock: Callable[[], float] = time.time,
        stop_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError('chunk size must be positive')
        if concurrency < 1 or concurrency > 3:
            raise ValueError('chunk concurrency must be between 1 and 3')
        if max_chunk_attempts < 1:
            raise ValueError('chunk attempts must be positive')
        self._database = database
        self._protocol = protocol
        self._chunk_size = chunk_size
        self._concurrency = concurrency
        self._max_chunk_attempts = max_chunk_attempts
        self._clock = clock
        self._stop_requested = stop_requested
        self._progress_milestones: Dict[int, int] = {}
        self._preupload_admission = _PreuploadAdmissionWindow(clock)

    async def upload_part(
        self, part_id: int, *, bundle: CredentialBundle, claim: LeaseClaim
    ) -> str:
        await self._renew_claim(claim)
        part = await self._load_part(part_id, claim)
        if part.upload_state == 'confirmed':
            if not part.remote_filename:
                raise UposUploadPaused('confirmed UPOS part has no remote filename')
            return part.remote_filename
        if part.upload_state in ('unknown_outcome', 'completing'):
            await self._update_part(part_id, claim, {'upload_state': 'uploading'})
        if part.upload_state == 'failed':
            raise UposUploadPaused('UPOS part requires manual retry')
        if part.artifact_state != 'ready':
            raise UposUploadPaused('recording artifact is not ready')

        identity = await self._load_or_store_identity(part, claim)
        if identity.size <= 0:
            await self._pause_identity(part_id, claim, 'recording file is empty')
        audit(
            'upload_part_started',
            job_id=claim.id,
            part_id=part_id,
            total_bytes=identity.size,
            resumed=part.upload_session_json is not None,
        )

        session, renewal_count = self._restore_session(part, claim)
        while True:
            if session is None:
                session = await self._start_session(
                    part_id, identity, bundle, claim, renewal_count
                )
            session_json = await self._session_json(part_id, claim)
            try:
                await self._upload_chunks(
                    part_id, identity, session, session_json, claim
                )
                return await self._complete(
                    part_id, identity, session, session_json, claim
                )
            except _SessionExpired:
                if renewal_count >= 1:
                    await self._pause(
                        part_id,
                        claim,
                        reason='UPOS session expired repeatedly',
                        upload_state='failed',
                    )
                    raise UposUploadPaused('UPOS session expired repeatedly')
                renewal_count += 1
                await self._discard_session(part_id, claim, renewal_count)
                session = None

    async def _load_or_store_identity(
        self, part: _Part, claim: LeaseClaim
    ) -> FileIdentity:
        try:
            current = await self._file_identity(part.path)
        except OSError:
            await self._pause_identity(part.id, claim, 'recording file is missing')
            raise AssertionError('unreachable')
        if part.file_identity is None:
            await self._update_part(
                part.id, claim, {'file_identity': current.to_json()}
            )
            return current
        try:
            stored = FileIdentity.from_json(part.file_identity)
        except ValueError:
            await self._pause_identity(
                part.id, claim, 'stored file identity is invalid'
            )
            raise AssertionError('unreachable')
        if current != stored:
            await self._pause_identity(part.id, claim, 'file identity changed')
            raise AssertionError('unreachable')
        return stored

    async def _start_session(
        self,
        part_id: int,
        identity: FileIdentity,
        bundle: CredentialBundle,
        claim: LeaseClaim,
        renewal_count: int,
    ) -> Any:
        await self._verify_identity(part_id, identity, claim)
        retry_after = self._preupload_admission.reserve()
        if retry_after:
            audit(
                'upload_preupload_deferred',
                level='DEBUG',
                job_id=claim.id,
                part_id=part_id,
                retry_after_seconds=retry_after,
                reason='admission_window',
            )
            raise UposUploadDeferred(retry_after, 'admission window')
        await self._update_part(part_id, claim, {'upload_state': 'preupload'})
        try:
            prepared = await self._protocol.preupload(
                bundle,
                {
                    'r': 'upos',
                    'profile': 'ugcupos/bup',
                    'ssl': 0,
                    'version': '2.8.12',
                    'build': 2081200,
                    'name': os.path.basename(identity.canonical_path),
                    'size': identity.size,
                },
            )
        except BiliApiError as error:
            if error.code not in (406, 429):
                raise
            retry_after = self._preupload_admission.rate_limited()
            await self._update_part(part_id, claim, {'upload_state': 'prepared'})
            audit(
                'upload_preupload_rate_limited',
                level='WARNING',
                job_id=claim.id,
                part_id=part_id,
                error_code=error.code,
                retry_after_seconds=retry_after,
            )
            raise UposUploadDeferred(retry_after, 'rate limited') from None
        self._preupload_admission.succeeded()
        exported = self._protocol.export_upos_session(prepared.session)
        session_json = self._encode_session(exported, renewal_count)
        remote_filename = getattr(prepared.session, 'remote_file_name', None)
        if not isinstance(remote_filename, str) or not remote_filename:
            raise ProtocolContractError('UPOS session has no remote filename')
        cid = self._positive_int(getattr(prepared.session, 'biz_id', None))
        await self._initialize_session(
            part_id, identity.size, session_json, remote_filename, cid, claim
        )
        audit(
            'upload_session_started',
            job_id=claim.id,
            part_id=part_id,
            total_bytes=identity.size,
            chunks=int(math.ceil(identity.size / self._chunk_size)),
            renewal_count=renewal_count,
        )
        return prepared.session

    def _restore_session(
        self, part: _Part, claim: LeaseClaim
    ) -> Tuple[Optional[Any], int]:
        del claim
        if part.upload_session_json is None:
            return None, 0
        payload = self._decode_session(part.upload_session_json)
        renewal_count = int(payload['renewal_count'])
        session_payload = payload['session']
        if session_payload is None:
            return None, renewal_count
        assert isinstance(session_payload, dict)
        return self._protocol.restore_upos_session(session_payload), renewal_count

    async def _upload_chunks(
        self,
        part_id: int,
        identity: FileIdentity,
        session: Any,
        session_json: str,
        claim: LeaseClaim,
    ) -> None:
        chunks = await self._chunks(part_id, identity.size, session_json, claim)
        pending = [chunk for chunk in chunks if chunk.state != 'confirmed']
        if not pending:
            return
        semaphore = asyncio.Semaphore(self._concurrency)

        async def upload(chunk: _Chunk) -> None:
            async with semaphore:
                await self._upload_chunk(
                    part_id, identity, session, session_json, claim, chunk, len(chunks)
                )

        tasks = [asyncio.ensure_future(upload(chunk)) for chunk in pending]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _upload_chunk(
        self,
        part_id: int,
        identity: FileIdentity,
        session: Any,
        session_json: str,
        claim: LeaseClaim,
        chunk: _Chunk,
        chunks: int,
    ) -> None:
        attempt = chunk.attempt
        while attempt < self._max_chunk_attempts:
            if self._stop_requested():
                raise UposUploadStopped('UPOS upload stopped at a chunk boundary')
            await self._verify_identity(part_id, identity, claim)
            body = await self._read_chunk(
                identity.canonical_path, chunk.offset, chunk.size
            )
            if len(body) != chunk.size:
                await self._pause_identity(
                    part_id, claim, 'recording file changed while reading'
                )
            attempt += 1
            await self._update_chunk(
                part_id,
                chunk.chunk_no,
                claim,
                session_json,
                state='in_flight',
                attempt=attempt,
            )
            try:
                response = await self._protocol.upload_chunk(
                    session,
                    chunk_no=chunk.chunk_no,
                    chunks=chunks,
                    start=chunk.offset,
                    total=identity.size,
                    body=body,
                )
            except BiliApiError as error:
                if error.code in (401, 403):
                    raise _SessionExpired() from None
                if error.code in (406, 408, 425, 429):
                    retry_after = (
                        self._preupload_admission.rate_limited()
                        if error.code in (406, 429)
                        else 30
                    )
                    await self._defer_chunk(
                        part_id,
                        chunk.chunk_no,
                        claim,
                        session_json,
                        reason='UPOS chunk request was temporarily rejected',
                        retry_after_seconds=retry_after,
                    )
                await self._fail_chunk(part_id, chunk.chunk_no, claim, session_json)
                raise UposUploadPaused('UPOS chunk was rejected') from None
            except (DefinitelyNotSent, RemoteOutcomeUnknown):
                if attempt < self._max_chunk_attempts:
                    await self._update_chunk(
                        part_id,
                        chunk.chunk_no,
                        claim,
                        session_json,
                        state='prepared',
                        attempt=attempt,
                    )
                    continue
                await self._defer_chunk(
                    part_id,
                    chunk.chunk_no,
                    claim,
                    session_json,
                    reason='UPOS chunk retry window was exhausted',
                )
            except ProtocolContractError:
                await self._fail_chunk(part_id, chunk.chunk_no, claim, session_json)
                raise UposUploadPaused('UPOS chunk response is invalid') from None
            await self._verify_identity(part_id, identity, claim)
            etag = response.get('etag')
            if not isinstance(etag, str) or not etag:
                etag = 'etag'
            await self._update_chunk(
                part_id,
                chunk.chunk_no,
                claim,
                session_json,
                state='confirmed',
                attempt=attempt,
                etag=etag,
            )
            audit(
                'upload_chunk_confirmed',
                level='DEBUG',
                job_id=claim.id,
                part_id=part_id,
                chunk_no=chunk.chunk_no,
                chunk_bytes=chunk.size,
                attempt=attempt,
            )
            await self._audit_progress(part_id, identity.size, claim.id)
            return

        await self._defer_chunk(
            part_id,
            chunk.chunk_no,
            claim,
            session_json,
            reason='UPOS chunk retry window was exhausted',
        )

    async def _complete(
        self,
        part_id: int,
        identity: FileIdentity,
        session: Any,
        session_json: str,
        claim: LeaseClaim,
    ) -> str:
        if self._stop_requested():
            raise UposUploadStopped('UPOS upload stopped before completion')
        await self._verify_identity(part_id, identity, claim)
        rows = await self._database.fetchall(
            'SELECT chunk_no,etag,state FROM upload_chunks '
            'WHERE part_id=? ORDER BY chunk_no',
            (part_id,),
        )
        if not rows or any(str(row['state']) != 'confirmed' for row in rows):
            raise UposUploadPaused('UPOS chunks are incomplete')
        parts = []
        for row in rows:
            etag = row['etag']
            if not isinstance(etag, str) or not etag:
                raise UposUploadPaused('UPOS chunk confirmation is incomplete')
            parts.append({'partNumber': int(row['chunk_no']) + 1, 'eTag': etag})
        await self._update_part(
            part_id,
            claim,
            {'upload_state': 'completing'},
            expected_session_json=session_json,
        )
        try:
            await self._protocol.complete_upload(session, parts=parts)
        except DefinitelyNotSent:
            await self._update_part(
                part_id,
                claim,
                {'upload_state': 'uploading'},
                expected_session_json=session_json,
            )
            raise
        except RemoteOutcomeUnknown:
            await self._update_part(
                part_id,
                claim,
                {'upload_state': 'uploading'},
                expected_session_json=session_json,
            )
            audit(
                'upload_completion_deferred',
                level='WARNING',
                job_id=claim.id,
                part_id=part_id,
                retry_after_seconds=60,
            )
            raise UposUploadDeferred(60, 'completion outcome unknown') from None
        except BiliApiError as error:
            if error.code in (401, 403):
                raise _SessionExpired() from None
            if error.code in (406, 408, 425, 429):
                delay = (
                    self._preupload_admission.rate_limited()
                    if error.code in (406, 429)
                    else 60
                )
                await self._update_part(
                    part_id,
                    claim,
                    {'upload_state': 'uploading'},
                    expected_session_json=session_json,
                )
                audit(
                    'upload_completion_deferred',
                    level='WARNING',
                    job_id=claim.id,
                    part_id=part_id,
                    error_code=error.code,
                    retry_after_seconds=delay,
                )
                raise UposUploadDeferred(
                    delay, 'UPOS completion temporarily rejected'
                ) from None
            await self._pause(
                part_id,
                claim,
                reason='UPOS completion was rejected',
                upload_state='failed',
            )
            raise UposUploadPaused('UPOS completion was rejected') from None
        except ProtocolContractError:
            await self._pause(
                part_id,
                claim,
                reason='UPOS completion response is invalid',
                upload_state='failed',
            )
            raise UposUploadPaused('UPOS completion response is invalid') from None
        await self._verify_identity(part_id, identity, claim)
        await self._update_part(
            part_id,
            claim,
            {'upload_state': 'confirmed'},
            expected_session_json=session_json,
        )
        audit(
            'upload_part_completed',
            job_id=claim.id,
            part_id=part_id,
            total_bytes=identity.size,
        )
        self._progress_milestones.pop(part_id, None)
        remote_filename = getattr(session, 'remote_file_name', None)
        if not isinstance(remote_filename, str) or not remote_filename:
            raise ProtocolContractError('UPOS session has no remote filename')
        return remote_filename

    async def _fail_chunk(
        self, part_id: int, chunk_no: int, claim: LeaseClaim, session_json: str
    ) -> None:
        await self._update_chunk(
            part_id, chunk_no, claim, session_json, state='failed', attempt=None
        )
        await self._pause(
            part_id, claim, reason='UPOS chunk upload failed', upload_state='failed'
        )

    async def _defer_chunk(
        self,
        part_id: int,
        chunk_no: int,
        claim: LeaseClaim,
        session_json: str,
        *,
        reason: str,
        retry_after_seconds: int = 30,
    ) -> None:
        await self._update_chunk(
            part_id, chunk_no, claim, session_json, state='prepared', attempt=0
        )
        audit(
            'upload_chunk_deferred',
            level='WARNING',
            job_id=claim.id,
            part_id=part_id,
            chunk_no=chunk_no,
            retry_after_seconds=retry_after_seconds,
            reason=reason,
        )
        raise UposUploadDeferred(retry_after_seconds, reason)

    async def _verify_identity(
        self, part_id: int, expected: FileIdentity, claim: LeaseClaim
    ) -> None:
        await self._assert_claim(part_id, claim)
        try:
            current = await self._file_identity(expected.canonical_path)
        except OSError:
            await self._pause_identity(part_id, claim, 'recording file is missing')
            raise AssertionError('unreachable')
        if current != expected:
            await self._pause_identity(part_id, claim, 'file identity changed')

    async def _pause_identity(
        self, part_id: int, claim: LeaseClaim, reason: str
    ) -> None:
        await self._pause(
            part_id,
            claim,
            reason=reason,
            upload_state='failed',
            artifact_state='manual_review',
        )
        raise UposUploadPaused(reason)

    async def _load_part(self, part_id: int, claim: LeaseClaim) -> _Part:
        now = int(self._clock())
        row = await self._database.fetchone(
            'SELECT part.id,part.job_id,part.source_path,part.final_path,'
            'part.file_identity,part.artifact_state,part.upload_state,'
            'part.remote_filename,part.upload_session_json '
            'FROM upload_parts part JOIN upload_jobs job ON job.id=part.job_id '
            'WHERE part.id=? AND job.id=? AND job.lease_owner=? '
            'AND job.lease_generation=? AND job.lease_until>?',
            (part_id, claim.id, claim.lease_owner, claim.lease_generation, now),
        )
        if row is None:
            raise LeaseLost('upload job lease was lost')
        final_path = row['final_path']
        path = str(final_path) if final_path else str(row['source_path'])
        return _Part(
            id=int(row['id']),
            job_id=int(row['job_id']),
            path=path,
            file_identity=(
                None if row['file_identity'] is None else str(row['file_identity'])
            ),
            artifact_state=str(row['artifact_state']),
            upload_state=str(row['upload_state']),
            remote_filename=(
                None if row['remote_filename'] is None else str(row['remote_filename'])
            ),
            upload_session_json=(
                None
                if row['upload_session_json'] is None
                else str(row['upload_session_json'])
            ),
        )

    async def _assert_claim(self, part_id: int, claim: LeaseClaim) -> None:
        await self._renew_claim(claim)
        now = int(self._clock())
        matched = await self._database.scalar(
            'SELECT COUNT(*) FROM upload_parts part '
            'JOIN upload_jobs job ON job.id=part.job_id '
            'WHERE part.id=? AND job.id=? AND job.lease_owner=? '
            'AND job.lease_generation=? AND job.lease_until>?',
            (part_id, claim.id, claim.lease_owner, claim.lease_generation, now),
        )
        if matched != 1:
            raise LeaseLost('upload job lease was lost')

    async def _renew_claim(self, claim: LeaseClaim) -> None:
        await self._database.renew(claim, now=int(self._clock()))

    async def _session_json(self, part_id: int, claim: LeaseClaim) -> str:
        row = await self._load_part(part_id, claim)
        if row.upload_session_json is None:
            raise ProtocolContractError('UPOS session was not persisted')
        return row.upload_session_json

    async def _initialize_session(
        self,
        part_id: int,
        total_size: int,
        session_json: str,
        remote_filename: str,
        cid: Optional[int],
        claim: LeaseClaim,
    ) -> None:
        chunk_size = self._chunk_size

        def initialize(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                'UPDATE upload_parts SET upload_state=?,upload_session_json=?,'
                'remote_filename=?,cid=COALESCE(?,cid) WHERE id=? AND EXISTS('
                'SELECT 1 FROM upload_jobs job WHERE job.id=upload_parts.job_id '
                'AND job.id=? AND job.lease_owner=? AND job.lease_generation=?)',
                (
                    'uploading',
                    session_json,
                    remote_filename,
                    cid,
                    part_id,
                    claim.id,
                    claim.lease_owner,
                    claim.lease_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLost('upload job lease was lost')
            connection.execute('DELETE FROM upload_chunks WHERE part_id=?', (part_id,))
            chunks = int(math.ceil(total_size / chunk_size))
            for chunk_no in range(chunks):
                offset = chunk_no * chunk_size
                size = min(chunk_size, total_size - offset)
                connection.execute(
                    'INSERT INTO upload_chunks('
                    'part_id,chunk_no,offset,size,state,attempt) '
                    "VALUES(?,?,?,?,'prepared',0)",
                    (part_id, chunk_no, offset, size),
                )

        await self._database.write(initialize)

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return None
        return normalized if normalized > 0 else None

    async def _discard_session(
        self, part_id: int, claim: LeaseClaim, renewal_count: int
    ) -> None:
        empty_session = self._encode_session(None, renewal_count)

        def discard(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                'UPDATE upload_parts SET upload_state=?,upload_session_json=?,'
                'remote_filename=NULL WHERE id=? AND EXISTS('
                'SELECT 1 FROM upload_jobs job WHERE job.id=upload_parts.job_id '
                'AND job.id=? AND job.lease_owner=? AND job.lease_generation=?)',
                (
                    'preupload',
                    empty_session,
                    part_id,
                    claim.id,
                    claim.lease_owner,
                    claim.lease_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLost('upload job lease was lost')
            connection.execute('DELETE FROM upload_chunks WHERE part_id=?', (part_id,))

        await self._database.write(discard)

    async def _chunks(
        self, part_id: int, total_size: int, session_json: str, claim: LeaseClaim
    ) -> List[_Chunk]:
        await self._normalize_in_flight(part_id, session_json, claim)
        rows = await self._database.fetchall(
            'SELECT chunk_no,offset,size,state,attempt FROM upload_chunks '
            'WHERE part_id=? ORDER BY chunk_no',
            (part_id,),
        )
        expected_count = int(math.ceil(total_size / self._chunk_size))
        if len(rows) != expected_count:
            await self._pause(
                part_id,
                claim,
                reason='UPOS chunk journal is inconsistent',
                upload_state='failed',
            )
            raise UposUploadPaused('UPOS chunk journal is inconsistent')
        chunks = []
        for index, row in enumerate(rows):
            offset = index * self._chunk_size
            size = min(self._chunk_size, total_size - offset)
            if (
                int(row['chunk_no']) != index
                or int(row['offset']) != offset
                or int(row['size']) != size
            ):
                await self._pause(
                    part_id,
                    claim,
                    reason='UPOS chunk journal is inconsistent',
                    upload_state='failed',
                )
                raise UposUploadPaused('UPOS chunk journal is inconsistent')
            chunks.append(
                _Chunk(
                    chunk_no=index,
                    offset=offset,
                    size=size,
                    state=str(row['state']),
                    attempt=int(row['attempt']),
                )
            )
        return chunks

    async def _normalize_in_flight(
        self, part_id: int, session_json: str, claim: LeaseClaim
    ) -> None:
        def normalize(connection: sqlite3.Connection) -> None:
            self._require_claim(connection, part_id, claim, session_json)
            connection.execute(
                "UPDATE upload_chunks SET state='prepared' "
                "WHERE part_id=? AND state='in_flight'",
                (part_id,),
            )

        await self._database.write(normalize)

    async def _update_part(
        self,
        part_id: int,
        claim: LeaseClaim,
        values: Mapping[str, Any],
        *,
        expected_session_json: Optional[str] = None,
    ) -> None:
        allowed = {
            'artifact_state',
            'file_identity',
            'remote_filename',
            'upload_session_json',
            'upload_state',
        }
        if not values or not set(values) <= allowed:
            raise ValueError('invalid upload part update')
        assignments = ','.join('{}=?'.format(column) for column in values)
        parameters: List[Any] = list(values.values())
        sql = (
            'UPDATE upload_parts SET {} WHERE id=? AND EXISTS('
            'SELECT 1 FROM upload_jobs job WHERE job.id=upload_parts.job_id '
            'AND job.id=? AND job.lease_owner=? AND job.lease_generation=?)'
        ).format(assignments)
        parameters.extend(
            (part_id, claim.id, claim.lease_owner, claim.lease_generation)
        )
        if expected_session_json is not None:
            sql += ' AND upload_session_json=?'
            parameters.append(expected_session_json)
        updated = await self._database.execute(sql, parameters)
        if updated != 1:
            raise LeaseLost('upload job lease was lost')

    async def _update_chunk(
        self,
        part_id: int,
        chunk_no: int,
        claim: LeaseClaim,
        session_json: str,
        *,
        state: str,
        attempt: Optional[int],
        etag: Optional[str] = None,
    ) -> None:
        assignments = ['state=?']
        parameters: List[Any] = [state]
        if attempt is not None:
            assignments.append('attempt=?')
            parameters.append(attempt)
        if etag is not None:
            assignments.append('etag=?')
            parameters.append(etag)
        parameters.extend(
            (
                part_id,
                chunk_no,
                claim.id,
                claim.lease_owner,
                claim.lease_generation,
                session_json,
            )
        )
        updated = await self._database.execute(
            'UPDATE upload_chunks SET {} WHERE part_id=? AND chunk_no=? '
            'AND EXISTS(SELECT 1 FROM upload_parts part '
            'JOIN upload_jobs job ON job.id=part.job_id '
            'WHERE part.id=upload_chunks.part_id AND job.id=? '
            'AND job.lease_owner=? AND job.lease_generation=? '
            'AND part.upload_session_json=?)'.format(','.join(assignments)),
            parameters,
        )
        if updated != 1:
            raise LeaseLost('upload job lease was lost')

    async def _pause(
        self,
        part_id: int,
        claim: LeaseClaim,
        *,
        reason: str,
        upload_state: str,
        artifact_state: Optional[str] = None,
    ) -> None:
        now = int(self._clock())

        def pause(connection: sqlite3.Connection) -> None:
            assignments = ['upload_state=?']
            parameters: List[Any] = [upload_state]
            if artifact_state is not None:
                assignments.append('artifact_state=?')
                parameters.append(artifact_state)
            parameters.extend(
                (part_id, claim.id, claim.lease_owner, claim.lease_generation)
            )
            part_cursor = connection.execute(
                'UPDATE upload_parts SET {} WHERE id=? AND EXISTS('
                'SELECT 1 FROM upload_jobs job WHERE job.id=upload_parts.job_id '
                'AND job.id=? AND job.lease_owner=? AND job.lease_generation=?)'.format(
                    ','.join(assignments)
                ),
                parameters,
            )
            if part_cursor.rowcount != 1:
                raise LeaseLost('upload job lease was lost')
            job_cursor = connection.execute(
                "UPDATE upload_jobs SET state='paused',review_reason=?,updated_at=? "
                'WHERE id=? AND lease_owner=? AND lease_generation=?',
                (reason, now, claim.id, claim.lease_owner, claim.lease_generation),
            )
            if job_cursor.rowcount != 1:
                raise LeaseLost('upload job lease was lost')

        await self._database.write(pause)
        audit(
            'upload_part_paused',
            level='WARNING',
            job_id=claim.id,
            part_id=part_id,
            upload_state=upload_state,
            artifact_state=artifact_state,
            reason=reason,
        )

    async def _audit_progress(
        self, part_id: int, total_bytes: int, job_id: int
    ) -> None:
        confirmed = int(
            await self._database.scalar(
                'SELECT COALESCE(SUM(chunk.size),0) FROM upload_chunks chunk '
                'JOIN upload_parts part ON part.id=chunk.part_id '
                "WHERE part.job_id=? AND chunk.state='confirmed'",
                (job_id,),
            )
        )
        rows = await self._database.fetchall(
            'SELECT file_identity,COALESCE(final_path,source_path) AS path '
            'FROM upload_parts WHERE job_id=?',
            (job_id,),
        )
        job_total_bytes = 0
        for row in rows:
            identity_json = row['file_identity']
            if identity_json is not None:
                try:
                    job_total_bytes += FileIdentity.from_json(str(identity_json)).size
                    continue
                except ValueError:
                    pass
            try:
                job_total_bytes += os.path.getsize(str(row['path']))
            except OSError:
                pass
        if job_total_bytes <= 0:
            job_total_bytes = total_bytes
        percent = min(100, int(confirmed * 100 / job_total_bytes))
        milestone = min(100, (percent // 5) * 5)
        if milestone <= self._progress_milestones.get(job_id, -1):
            return
        self._progress_milestones[job_id] = milestone
        audit(
            'upload_progress',
            job_id=job_id,
            part_id=part_id,
            percent=milestone,
            confirmed_bytes=confirmed,
            total_bytes=job_total_bytes,
        )

    @staticmethod
    def _require_claim(
        connection: sqlite3.Connection,
        part_id: int,
        claim: LeaseClaim,
        session_json: str,
    ) -> None:
        row = connection.execute(
            'SELECT 1 FROM upload_parts part '
            'JOIN upload_jobs job ON job.id=part.job_id '
            'WHERE part.id=? AND job.id=? AND job.lease_owner=? '
            'AND job.lease_generation=? AND part.upload_session_json=?',
            (
                part_id,
                claim.id,
                claim.lease_owner,
                claim.lease_generation,
                session_json,
            ),
        ).fetchone()
        if row is None:
            raise LeaseLost('upload job lease was lost')

    @classmethod
    def _encode_session(
        cls, session: Optional[Mapping[str, Any]], renewal_count: int
    ) -> str:
        return json.dumps(
            {'format_version': 1, 'renewal_count': renewal_count, 'session': session},
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )

    @classmethod
    def _decode_session(cls, value: str) -> Dict[str, Any]:
        try:
            payload = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            raise ProtocolContractError('invalid persisted UPOS session') from None
        if not isinstance(payload, dict) or set(payload) != cls._SESSION_FIELDS:
            raise ProtocolContractError('invalid persisted UPOS session')
        if payload['format_version'] != 1:
            raise ProtocolContractError('invalid persisted UPOS session')
        renewal_count = payload['renewal_count']
        if type(renewal_count) is not int or renewal_count < 0 or renewal_count > 1:
            raise ProtocolContractError('invalid persisted UPOS session')
        session = payload['session']
        if session is not None and not isinstance(session, dict):
            raise ProtocolContractError('invalid persisted UPOS session')
        return payload

    @staticmethod
    async def _file_identity(path: str) -> FileIdentity:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, FileIdentity.from_path, path)

    @staticmethod
    async def _read_chunk(path: str, offset: int, size: int) -> bytes:
        def read() -> bytes:
            with open(path, 'rb') as file:
                file.seek(offset)
                return file.read(size)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, partial(read))
