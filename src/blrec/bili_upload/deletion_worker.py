from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Tuple

from blrec.logging.audit import audit

from .database import BiliUploadDatabase

__all__ = ('LocalDeletionWorker', 'LocalDeletionRejected')


class LocalDeletionRejected(ValueError):
    pass


class LocalDeletionWorker:
    """Serial, restart-safe deletion of files owned by BLREC.

    Request methods only persist cancellation intent.  File and child-row work is
    deliberately kept in ``run_once`` so HTTP callers never wait for NAS I/O or
    another domain owner.
    """

    QUANTUM = 128
    _FILE_SUFFIXES = frozenset(
        (
            '.flv',
            '.mp4',
            '.ts',
            '.m4s',
            '.m3u8',
            '.mkv',
            '.mov',
            '.webm',
            '.xml',
            '.jpg',
            '.jpeg',
            '.png',
            '.webp',
        )
    )

    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        recording_root: Path,
        clip_root: Path,
        active_session_canceller: Optional[Callable[[int], Awaitable[None]]] = None,
        clock: Callable[[], float] = time.time,
        unlink: Callable[[Path], None] = lambda path: path.unlink(),
    ) -> None:
        self._database = database
        self._recording_root = self._resolve_root(recording_root)
        self._clip_root = self._resolve_root(clip_root)
        self._active_session_canceller = active_session_canceller
        self._clock = clock
        self._unlink = unlink
        self._run_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()
        self._wake_generation = 0
        self._accepting = True

    async def request_session(self, session_id: int, *, manager_subject: str) -> int:
        if not self._accepting:
            raise LocalDeletionRejected('本地删除服务正在停止')
        if not manager_subject:
            raise LocalDeletionRejected('管理员身份不能为空')
        now = int(self._clock())

        def request(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                'SELECT deletion_state,cancellation_generation '
                'FROM recording_sessions WHERE id=?',
                (session_id,),
            ).fetchone()
            if row is None:
                raise LocalDeletionRejected('录制场次不存在')
            generation = int(row['cancellation_generation']) + 1
            connection.execute(
                "UPDATE recording_sessions SET deletion_state='requested',"
                'deletion_error=NULL,deletion_requested_at=?,'
                'cancellation_generation=? WHERE id=?',
                (now, generation, session_id),
            )
            connection.execute(
                'INSERT INTO management_audit('
                'manager_subject,action,target_type,target_id,old_state,new_state,'
                'reason,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (
                    manager_subject,
                    'request_session_deletion',
                    'recording_session',
                    str(session_id),
                    str(row['deletion_state']),
                    'requested',
                    '管理员请求删除本地场次及归属文件',
                    now,
                ),
            )
            return generation

        generation = await self._database.write(request)
        self.wake()
        return generation

    async def request_clip(self, clip_id: int) -> int:
        if not self._accepting:
            raise LocalDeletionRejected('本地删除服务正在停止')
        now = int(self._clock())

        def request(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                'SELECT deletion_state,cancellation_generation,upload_session_id '
                'FROM highlight_clips WHERE id=?',
                (clip_id,),
            ).fetchone()
            if row is None:
                raise LocalDeletionRejected('高光片段不存在')
            generation = int(row['cancellation_generation']) + 1
            connection.execute(
                "UPDATE highlight_clips SET deletion_state='requested',"
                'deletion_error=NULL,deletion_requested_at=?,'
                'cancellation_generation=? WHERE id=?',
                (now, generation, clip_id),
            )
            upload_session_id = row['upload_session_id']
            if upload_session_id is not None:
                connection.execute(
                    "UPDATE recording_sessions SET deletion_state='requested',"
                    'deletion_error=NULL,deletion_requested_at=?,'
                    'cancellation_generation=cancellation_generation+1 '
                    'WHERE id=?',
                    (now, int(upload_session_id)),
                )
            return generation

        generation = await self._database.write(request)
        self.wake()
        return generation

    def wake(self) -> None:
        self._wake_generation += 1
        self._wake_event.set()

    def stop_admission(self) -> None:
        self._accepting = False
        self.wake()

    async def recover_interrupted(self) -> None:
        def recover(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE recording_sessions SET deletion_state='requested',"
                'deletion_error=NULL,deletion_requested_at=?,'
                'cancellation_generation=cancellation_generation+1 '
                "WHERE deletion_state='none' AND EXISTS("
                'SELECT 1 FROM highlight_clips clip '
                'WHERE clip.upload_session_id=recording_sessions.id '
                "AND clip.deletion_state!='none')",
                (int(self._clock()),),
            )
            connection.execute(
                'UPDATE recording_sessions SET cancellation_generation=1 '
                "WHERE deletion_state!='none' AND cancellation_generation=0"
            )
            connection.execute(
                "UPDATE local_deletion_items SET state='pending',error=NULL "
                "WHERE state='deleting'"
            )
            connection.execute(
                "UPDATE recording_sessions SET deletion_state='requested',"
                'deletion_error=NULL '
                "WHERE deletion_state IN ('deleting','failed')"
            )
            connection.execute(
                "UPDATE highlight_clips SET deletion_state='requested',"
                'deletion_error=NULL '
                "WHERE deletion_state IN ('quiescing','deleting','failed')"
            )
            connection.execute(
                "UPDATE owner_handoff_outcomes SET "
                "outcome_state='unknown_terminal',outcome_json='{}',"
                'acknowledged_at=? '
                "WHERE outcome_state='in_flight'",
                (int(self._clock()),),
            )

        await self._database.write(recover)
        self.wake()

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            wake_generation = self._wake_generation
            try:
                processed = await self.run_once(stop_event=stop_event)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                processed = None
                audit(
                    'local_deletion_worker_iteration_failed',
                    level='ERROR',
                    error_type=type(error).__name__,
                    result='will_retry',
                )
            if processed is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
                continue
            self._wake_event.clear()
            if stop_event.is_set():
                return
            if self._wake_generation != wake_generation:
                continue
            wake_task = asyncio.create_task(self._wake_event.wait())
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                (wake_task, stop_task), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stop_task in done and stop_task.result():
                return

    async def run_once(
        self, *, stop_event: Optional[asyncio.Event] = None
    ) -> Optional[Tuple[str, int]]:
        async with self._run_lock:
            owner = await self._next_owner()
            if owner is None:
                return None
            owner_kind, owner_id, generation = owner
            if stop_event is not None and stop_event.is_set():
                return owner_kind, owner_id
            try:
                ready = await self._quiesce(owner_kind, owner_id, generation)
                if not ready:
                    return owner_kind, owner_id
                await self._snapshot_items(owner_kind, owner_id, generation)
                await self._delete_quantum(
                    owner_kind, owner_id, generation, stop_event=stop_event
                )
                await self._finish_if_empty(owner_kind, owner_id, generation)
            except LocalDeletionRejected as error:
                await self._fail_owner(owner_kind, owner_id, generation, str(error))
            except OSError as error:
                await self._fail_owner(
                    owner_kind,
                    owner_id,
                    generation,
                    'unlink_{}'.format(type(error).__name__),
                )
            return owner_kind, owner_id

    async def _next_owner(self) -> Optional[Tuple[str, int, int]]:
        session = await self._database.fetchone(
            'SELECT session.id,session.cancellation_generation '
            'FROM recording_sessions session '
            "WHERE session.deletion_state IN ('requested','deleting') "
            'AND NOT EXISTS(SELECT 1 FROM highlight_clips clip '
            'WHERE clip.upload_session_id=session.id '
            "AND clip.deletion_state!='none') "
            'ORDER BY session.deletion_requested_at,session.id LIMIT 1'
        )
        if session is not None:
            return (
                'session',
                int(session['id']),
                int(session['cancellation_generation']),
            )
        clip = await self._database.fetchone(
            'SELECT id,cancellation_generation FROM highlight_clips '
            "WHERE deletion_state IN ('requested','quiescing','deleting') "
            'ORDER BY deletion_requested_at,id LIMIT 1'
        )
        if clip is None:
            return None
        return 'clip', int(clip['id']), int(clip['cancellation_generation'])

    async def _quiesce(self, owner_kind: str, owner_id: int, generation: int) -> bool:
        if owner_kind == 'clip':
            return await self._quiesce_clip(owner_id, generation)
        row = await self._database.fetchone(
            'SELECT room_id FROM recording_sessions WHERE id=? '
            'AND cancellation_generation=?',
            (owner_id, generation),
        )
        if row is None:
            return False
        active = await self._database.scalar(
            "SELECT COUNT(*) FROM recording_runs WHERE session_id=? "
            "AND state='recording'",
            (owner_id,),
        )
        if active:
            if self._active_session_canceller is not None:
                try:
                    await self._active_session_canceller(int(row['room_id']))
                except Exception as error:
                    audit(
                        'local_deletion_recorder_cancel_failed',
                        level='WARNING',
                        owner_kind=owner_kind,
                        owner_id=owner_id,
                        error_type=type(error).__name__,
                    )
            return False
        blockers = await self._session_blockers(owner_id)
        if blockers:
            audit(
                'local_deletion_waiting_for_owner',
                owner_kind=owner_kind,
                owner_id=owner_id,
                blockers=','.join(blockers),
            )
            return False
        await self._cancel_idle_local_work(owner_id, generation)
        return True

    async def _quiesce_clip(self, clip_id: int, generation: int) -> bool:
        def cancel_local(connection: sqlite3.Connection) -> bool:
            current = connection.execute(
                'SELECT state,lease_owner,upload_session_id FROM highlight_clips '
                'WHERE id=? AND cancellation_generation=?',
                (clip_id, generation),
            ).fetchone()
            if current is None:
                return False
            in_flight_handoff = connection.execute(
                'SELECT 1 FROM owner_handoff_outcomes '
                "WHERE owner_kind='highlight' AND owner_id=? "
                "AND outcome_state='in_flight' LIMIT 1",
                (clip_id,),
            ).fetchone()
            if in_flight_handoff is not None:
                return False
            if current['lease_owner'] is not None:
                return False
            upload_session_id = current['upload_session_id']
            if upload_session_id is not None:
                session_id = int(upload_session_id)
                connection.execute(
                    "UPDATE recording_sessions SET deletion_state='requested',"
                    'deletion_error=NULL,deletion_requested_at=?,'
                    'cancellation_generation=cancellation_generation+1 '
                    "WHERE id=? AND deletion_state='none'",
                    (int(self._clock()), session_id),
                )
                blockers = self._session_blockers_in_transaction(connection, session_id)
                if blockers:
                    return False
                session = connection.execute(
                    'SELECT cancellation_generation FROM recording_sessions '
                    'WHERE id=?',
                    (session_id,),
                ).fetchone()
                if session is None:
                    return False
                self._cancel_idle_local_work_in_transaction(
                    connection,
                    session_id,
                    int(session['cancellation_generation']),
                    int(self._clock()),
                )
            connection.execute(
                "UPDATE highlight_clips SET deletion_state='quiescing',"
                'lease_owner=NULL,lease_until=NULL,next_attempt_at=0,updated_at=? '
                'WHERE id=? AND cancellation_generation=?',
                (int(self._clock()), clip_id, generation),
            )
            return True

        return await self._database.write(cancel_local)

    async def _session_blockers(self, session_id: int) -> Tuple[str, ...]:
        return await self._database.read(
            lambda connection: self._session_blockers_in_transaction(
                connection, session_id
            )
        )

    @staticmethod
    def _session_blockers_in_transaction(
        connection: sqlite3.Connection, session_id: int
    ) -> Tuple[str, ...]:
        row = connection.execute(
            'SELECT job.id,job.lease_owner,job.submit_state,'
            'job.collection_branch_state,job.danmaku_branch_state,'
            'job.repair_state FROM upload_jobs job WHERE job.session_id=?',
            (session_id,),
        ).fetchone()
        blockers: List[str] = []
        if row is not None:
            job_id = int(row['id'])
            if row['lease_owner'] is not None:
                blockers.append('upload')
            elif str(row['submit_state']) == 'in_flight':
                blockers.append('upload_submit')
            if str(row['collection_branch_state']) == 'running':
                blockers.append('collection')
            if str(row['danmaku_branch_state']) == 'importing':
                blockers.append('danmaku_import')
            if str(row['repair_state']) in ('checking', 'reuploading', 'editing'):
                blockers.append('repair')
            in_flight_chunks = connection.execute(
                'SELECT COUNT(*) FROM upload_chunks WHERE part_id IN('
                'SELECT id FROM upload_parts WHERE job_id=?) '
                "AND state='in_flight'",
                (job_id,),
            ).fetchone()[0]
            if in_flight_chunks:
                blockers.append('upos')
            remote_handoffs = connection.execute(
                'SELECT COUNT(*) FROM owner_handoff_outcomes outcome '
                "WHERE outcome.outcome_state='in_flight' AND (("
                "outcome.owner_kind='upload' AND outcome.owner_id=?) OR ("
                "outcome.owner_kind='upos' AND outcome.owner_id IN("
                'SELECT id FROM upload_parts WHERE job_id=?)) OR ('
                "outcome.owner_kind='repair' AND outcome.owner_id=?) OR ("
                "outcome.owner_kind='comment' AND outcome.owner_id IN("
                'SELECT id FROM comment_items WHERE job_id=?)) OR ('
                "outcome.owner_kind='danmaku' AND outcome.owner_id IN("
                'SELECT item.id FROM danmaku_items item '
                'JOIN upload_parts part ON part.id=item.part_id '
                'WHERE part.job_id=?)))',
                (job_id, job_id, job_id, job_id, job_id),
            ).fetchone()[0]
            if int(remote_handoffs):
                blockers.append('remote_handoff')
            comment = connection.execute(
                'SELECT COUNT(*) FROM comment_items WHERE job_id=? '
                'AND lease_owner IS NOT NULL',
                (job_id,),
            ).fetchone()[0]
            if int(comment):
                blockers.append('comment')
            danmaku = connection.execute(
                'SELECT COUNT(*) FROM danmaku_items WHERE part_id IN('
                'SELECT id FROM upload_parts WHERE job_id=?) '
                'AND lease_owner IS NOT NULL',
                (job_id,),
            ).fetchone()[0]
            if int(danmaku):
                blockers.append('danmaku')
        local_handoffs = connection.execute(
            'SELECT COUNT(*) FROM owner_handoff_outcomes outcome '
            "WHERE outcome.outcome_state='in_flight' AND (("
            "outcome.owner_kind='recorder' AND outcome.owner_id=?) OR ("
            "outcome.owner_kind='media_index' AND outcome.owner_id IN("
            'SELECT id FROM recording_parts WHERE session_id=?)))',
            (session_id, session_id),
        ).fetchone()[0]
        if int(local_handoffs) and 'remote_handoff' not in blockers:
            blockers.append('remote_handoff')
        media = connection.execute(
            'SELECT COUNT(*) FROM recording_parts WHERE session_id=? '
            "AND media_index_state='indexing' AND media_index_owner IS NOT NULL",
            (session_id,),
        ).fetchone()[0]
        if int(media):
            blockers.append('media_index')
        postprocessor = connection.execute(
            'SELECT COUNT(*) FROM recording_parts WHERE session_id=? '
            "AND artifact_state='postprocessing'",
            (session_id,),
        ).fetchone()[0]
        if int(postprocessor):
            blockers.append('postprocessor')
        highlight = connection.execute(
            'SELECT COUNT(*) FROM highlight_clips clip '
            'JOIN highlight_clip_sources source ON source.clip_id=clip.id '
            'JOIN recording_parts part ON part.id=source.part_id '
            "WHERE part.session_id=? AND clip.state='processing' "
            'AND clip.lease_owner IS NOT NULL',
            (session_id,),
        ).fetchone()[0]
        if int(highlight):
            blockers.append('highlight')
        return tuple(blockers)

    async def _cancel_idle_local_work(self, session_id: int, generation: int) -> None:
        now = int(self._clock())

        def cancel(connection: sqlite3.Connection) -> None:
            self._cancel_idle_local_work_in_transaction(
                connection, session_id, generation, now
            )

        await self._database.write(cancel)

    def _cancel_idle_local_work_in_transaction(
        self, connection: sqlite3.Connection, session_id: int, generation: int, now: int
    ) -> None:
        current = connection.execute(
            'SELECT cancellation_generation FROM recording_sessions WHERE id=?',
            (session_id,),
        ).fetchone()
        if current is None or int(current['cancellation_generation']) != generation:
            return
        job = connection.execute(
            'SELECT id,submit_state FROM upload_jobs WHERE session_id=?', (session_id,)
        ).fetchone()
        if job is None:
            return
        job_id = int(job['id'])
        if str(
            job['submit_state']
        ) == 'unknown_outcome' and not self._has_terminal_handoff(
            connection, 'upload', job_id, 'archive_submit'
        ):
            self._record_terminal_handoff(
                connection,
                owner_kind='upload',
                owner_id=job_id,
                side_effect_key='archive_submit',
                source_generation=generation - 1,
                now=now,
            )
        unknown_parts = connection.execute(
            "SELECT id FROM upload_parts WHERE job_id=? "
            "AND upload_state='unknown_outcome'",
            (job_id,),
        ).fetchall()
        for part in unknown_parts:
            part_id = int(part['id'])
            if not self._has_terminal_handoff(connection, 'upos', part_id, 'complete'):
                self._record_terminal_handoff(
                    connection,
                    owner_kind='upos',
                    owner_id=part_id,
                    side_effect_key='complete',
                    source_generation=generation - 1,
                    now=now,
                )
        unknown_comments = connection.execute(
            "SELECT id FROM comment_items WHERE job_id=? "
            "AND state='unknown_outcome'",
            (job_id,),
        ).fetchall()
        for item in unknown_comments:
            self._record_terminal_handoff(
                connection,
                owner_kind='comment',
                owner_id=int(item['id']),
                side_effect_key='publish',
                source_generation=generation - 1,
                now=now,
            )
        unknown_danmaku = connection.execute(
            "SELECT item.id FROM danmaku_items item "
            'JOIN upload_parts part ON part.id=item.part_id '
            "WHERE part.job_id=? AND item.state='unknown_outcome'",
            (job_id,),
        ).fetchall()
        for item in unknown_danmaku:
            self._record_terminal_handoff(
                connection,
                owner_kind='danmaku',
                owner_id=int(item['id']),
                side_effect_key='publish',
                source_generation=generation - 1,
                now=now,
            )
        connection.execute(
            "UPDATE upload_jobs SET state='paused',operator_paused=1,"
            "operator_resume_state=NULL,review_reason='任务正在删除',"
            "repair_state=CASE WHEN repair_state IN "
            "('queued','checking','reuploading') THEN 'failed' "
            'ELSE repair_state END,'
            "repair_error=CASE WHEN repair_state IN "
            "('queued','checking','reuploading') THEN '任务正在删除' "
            'ELSE repair_error END,'
            "comment_branch_state=CASE WHEN comment_branch_state IN "
            "('pending','running') THEN 'paused' ELSE comment_branch_state END,"
            "danmaku_branch_state=CASE WHEN danmaku_branch_state IN "
            "('pending','publishing') THEN 'paused' ELSE danmaku_branch_state END,"
            'updated_at=? WHERE id=?',
            (now, job_id),
        )

    @staticmethod
    def _has_terminal_handoff(
        connection: sqlite3.Connection,
        owner_kind: str,
        owner_id: int,
        side_effect_key: str,
    ) -> bool:
        return (
            connection.execute(
                'SELECT 1 FROM owner_handoff_outcomes WHERE owner_kind=? '
                'AND owner_id=? AND side_effect_key=? '
                "AND outcome_state!='in_flight' LIMIT 1",
                (owner_kind, owner_id, side_effect_key),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _record_terminal_handoff(
        connection: sqlite3.Connection,
        *,
        owner_kind: str,
        owner_id: int,
        side_effect_key: str,
        source_generation: int,
        now: int,
    ) -> None:
        connection.execute(
            'INSERT OR IGNORE INTO owner_handoff_outcomes('
            'owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,outcome_json,acknowledged_at) '
            "VALUES(?,?,?,?,'unknown_terminal','{}',?)",
            (owner_kind, owner_id, side_effect_key, max(0, source_generation), now),
        )

    async def _snapshot_items(
        self, owner_kind: str, owner_id: int, generation: int
    ) -> None:
        if generation <= 0:
            raise LocalDeletionRejected('invalid_cancellation_generation')
        if owner_kind == 'clip':
            row = await self._database.fetchone(
                'SELECT output_video_path,output_xml_path FROM highlight_clips '
                'WHERE id=? AND cancellation_generation=?',
                (owner_id, generation),
            )
            raw_paths = (
                ()
                if row is None
                else tuple(
                    str(row[column])
                    for column in ('output_video_path', 'output_xml_path')
                    if row[column]
                )
            )
            root = self._clip_root
        else:
            session = await self._database.fetchone(
                'SELECT cover_path FROM recording_sessions '
                'WHERE id=? AND cancellation_generation=?',
                (owner_id, generation),
            )
            rows = await self._database.fetchall(
                'SELECT source_path,final_path,xml_path FROM recording_parts '
                'WHERE session_id=? UNION ALL '
                'SELECT part.source_path,part.final_path,part.xml_path '
                'FROM upload_parts part JOIN upload_jobs job ON job.id=part.job_id '
                'WHERE job.session_id=?',
                (owner_id, owner_id),
            )
            collected: List[str] = []
            for row in rows:
                collected.extend(
                    str(row[column])
                    for column in ('source_path', 'final_path', 'xml_path')
                    if row[column]
                )
            if session is not None and session['cover_path']:
                collected.append(str(session['cover_path']))
            raw_paths = tuple(collected)
            root = self._recording_root
        paths = tuple(
            dict.fromkeys(self._owned_path(value, root) for value in raw_paths)
        )

        def snapshot(connection: sqlite3.Connection) -> None:
            table = (
                'recording_sessions' if owner_kind == 'session' else 'highlight_clips'
            )
            current = connection.execute(
                'SELECT cancellation_generation FROM {} WHERE id=?'.format(table),
                (owner_id,),
            ).fetchone()
            if current is None or int(current['cancellation_generation']) != generation:
                return
            connection.execute(
                "UPDATE {} SET deletion_state='deleting',deletion_error=NULL "
                'WHERE id=? AND cancellation_generation=?'.format(table),
                (owner_id, generation),
            )
            connection.execute(
                'DELETE FROM local_deletion_items WHERE owner_kind=? AND owner_id=? '
                'AND cancellation_generation<>?',
                (owner_kind, owner_id, generation),
            )
            connection.executemany(
                'INSERT INTO local_deletion_items('
                'owner_kind,owner_id,cancellation_generation,path,state) '
                "VALUES(?,?,?,?,'pending') ON CONFLICT("
                'owner_kind,owner_id,cancellation_generation,path) DO NOTHING',
                ((owner_kind, owner_id, generation, str(path)) for path in paths),
            )

        await self._database.write(snapshot)

    async def _delete_quantum(
        self,
        owner_kind: str,
        owner_id: int,
        generation: int,
        *,
        stop_event: Optional[asyncio.Event],
    ) -> None:
        rows = await self._database.fetchall(
            'SELECT id,path FROM local_deletion_items '
            'WHERE owner_kind=? AND owner_id=? AND cancellation_generation=? '
            "AND state IN ('pending','deleting','failed') ORDER BY id LIMIT ?",
            (owner_kind, owner_id, generation, self.QUANTUM),
        )
        for row in rows:
            if stop_event is not None and stop_event.is_set():
                return
            item_id = int(row['id'])
            changed = await self._database.execute(
                "UPDATE local_deletion_items SET state='deleting',error=NULL "
                'WHERE id=? AND cancellation_generation=?',
                (item_id, generation),
            )
            if changed != 1:
                continue
            path = Path(str(row['path']))
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._unlink_if_present, path
                )
            except OSError as error:
                await self._database.execute(
                    "UPDATE local_deletion_items SET state='failed',error=? "
                    'WHERE id=?',
                    (type(error).__name__, item_id),
                )
                raise
            await self._database.execute(
                "UPDATE local_deletion_items SET state='done',error=NULL "
                'WHERE id=? AND cancellation_generation=?',
                (item_id, generation),
            )

    async def _finish_if_empty(
        self, owner_kind: str, owner_id: int, generation: int
    ) -> None:
        remaining = await self._database.scalar(
            'SELECT COUNT(*) FROM local_deletion_items WHERE owner_kind=? '
            "AND owner_id=? AND cancellation_generation=? AND state!='done'",
            (owner_kind, owner_id, generation),
        )
        if remaining:
            return
        if owner_kind == 'clip':
            await self._finish_clip(owner_id, generation)
        else:
            await self._finish_session(owner_id, generation)

    async def _finish_session(self, session_id: int, generation: int) -> None:
        def finish(connection: sqlite3.Connection) -> bool:
            current = connection.execute(
                'SELECT deletion_state,cancellation_generation '
                'FROM recording_sessions WHERE id=?',
                (session_id,),
            ).fetchone()
            if (
                current is None
                or int(current['cancellation_generation']) != generation
                or str(current['deletion_state']) != 'deleting'
            ):
                return False
            if self._session_blockers_in_transaction(connection, session_id):
                return False
            job = connection.execute(
                'SELECT id FROM upload_jobs WHERE session_id=?', (session_id,)
            ).fetchone()
            if job is not None:
                self._delete_job_children(connection, int(job['id']))
                connection.execute(
                    'DELETE FROM upload_jobs WHERE id=?', (int(job['id']),)
                )
            connection.execute(
                'DELETE FROM event_journal WHERE run_id IN('
                'SELECT id FROM recording_runs WHERE session_id=?)',
                (session_id,),
            )
            self._delete_session_handoffs(connection, session_id)
            connection.execute(
                'DELETE FROM recording_parts WHERE session_id=?', (session_id,)
            )
            connection.execute(
                'DELETE FROM recording_runs WHERE session_id=?', (session_id,)
            )
            connection.execute(
                'DELETE FROM upload_suppressions WHERE session_id=?', (session_id,)
            )
            connection.execute(
                'DELETE FROM upload_job_archives WHERE session_id=?', (session_id,)
            )
            connection.execute(
                'DELETE FROM recording_sessions WHERE id=?', (session_id,)
            )
            connection.execute(
                'DELETE FROM local_deletion_items WHERE owner_kind=\'session\' '
                'AND owner_id=? AND cancellation_generation=?',
                (session_id, generation),
            )
            return True

        if await self._database.write(finish):
            audit(
                'local_deletion_completed',
                owner_kind='session',
                owner_id=session_id,
                generation=generation,
                result='deleted_local_only',
            )

    async def _finish_clip(self, clip_id: int, generation: int) -> None:
        def finish(connection: sqlite3.Connection) -> bool:
            current = connection.execute(
                'SELECT upload_session_id,deletion_state,cancellation_generation '
                'FROM highlight_clips WHERE id=?',
                (clip_id,),
            ).fetchone()
            if (
                current is None
                or int(current['cancellation_generation']) != generation
                or str(current['deletion_state']) != 'deleting'
            ):
                return False
            in_flight_handoff = connection.execute(
                'SELECT 1 FROM owner_handoff_outcomes '
                "WHERE owner_kind='highlight' AND owner_id=? "
                "AND outcome_state='in_flight' LIMIT 1",
                (clip_id,),
            ).fetchone()
            if in_flight_handoff is not None:
                return False
            session_id = current['upload_session_id']
            if session_id is not None:
                if self._session_blockers_in_transaction(connection, int(session_id)):
                    return False
                job = connection.execute(
                    'SELECT id FROM upload_jobs WHERE session_id=?', (int(session_id),)
                ).fetchone()
                if job is not None:
                    self._delete_job_children(connection, int(job['id']))
                    connection.execute(
                        'DELETE FROM upload_jobs WHERE id=?', (int(job['id']),)
                    )
            connection.execute(
                "DELETE FROM owner_handoff_outcomes WHERE owner_kind='highlight' "
                'AND owner_id=?',
                (clip_id,),
            )
            connection.execute('DELETE FROM highlight_clips WHERE id=?', (clip_id,))
            if session_id is not None:
                connection.execute(
                    'DELETE FROM event_journal WHERE run_id IN('
                    'SELECT id FROM recording_runs WHERE session_id=?)',
                    (int(session_id),),
                )
                self._delete_session_handoffs(connection, int(session_id))
                connection.execute(
                    'DELETE FROM recording_parts WHERE session_id=?', (int(session_id),)
                )
                connection.execute(
                    'DELETE FROM recording_runs WHERE session_id=?', (int(session_id),)
                )
                connection.execute(
                    'DELETE FROM recording_sessions WHERE id=? '
                    "AND source_kind='highlight' "
                    'AND NOT EXISTS(SELECT 1 FROM upload_jobs WHERE session_id=?)',
                    (int(session_id), int(session_id)),
                )
            connection.execute(
                'DELETE FROM local_deletion_items WHERE owner_kind=\'clip\' '
                'AND owner_id=? AND cancellation_generation=?',
                (clip_id, generation),
            )
            return True

        if await self._database.write(finish):
            audit(
                'local_deletion_completed',
                owner_kind='clip',
                owner_id=clip_id,
                generation=generation,
                result='deleted_local_only',
            )

    async def _fail_owner(
        self, owner_kind: str, owner_id: int, generation: int, error: str
    ) -> None:
        table = 'recording_sessions' if owner_kind == 'session' else 'highlight_clips'
        await self._database.execute(
            "UPDATE {} SET deletion_state='failed',deletion_error=? "
            'WHERE id=? AND cancellation_generation=?'.format(table),
            (error[:200], owner_id, generation),
        )
        audit(
            'local_deletion_failed',
            level='WARNING',
            owner_kind=owner_kind,
            owner_id=owner_id,
            generation=generation,
            error_code=error[:200],
            result='retryable',
        )

    def _owned_path(self, raw_path: str, root: Path) -> Path:
        path = Path(os.path.abspath(os.path.expanduser(raw_path)))
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            raise LocalDeletionRejected('path_ownership_violation') from None
        if path.suffix.lower() not in self._FILE_SUFFIXES:
            raise LocalDeletionRejected('path_suffix_not_allowed')
        return path

    def _unlink_if_present(self, path: Path) -> None:
        try:
            path.lstat()
        except FileNotFoundError:
            return
        if not path.is_file() and not path.is_symlink():
            raise OSError('owned path is not a file')
        self._unlink(path)

    @staticmethod
    def _resolve_root(path: Path) -> Path:
        return Path(os.path.abspath(os.path.expanduser(str(path)))).resolve()

    @staticmethod
    def _delete_job_children(connection: sqlite3.Connection, job_id: int) -> None:
        connection.execute(
            'DELETE FROM owner_handoff_outcomes WHERE '
            "(owner_kind IN ('upload','repair','collection') AND owner_id=?) OR "
            "(owner_kind='upos' AND owner_id IN("
            'SELECT id FROM upload_parts WHERE job_id=?)) OR '
            "(owner_kind='comment' AND owner_id IN("
            'SELECT id FROM comment_items WHERE job_id=?)) OR '
            "(owner_kind='danmaku' AND owner_id IN("
            'SELECT item.id FROM danmaku_items item '
            'JOIN upload_parts part ON part.id=item.part_id '
            'WHERE part.job_id=?))',
            (job_id, job_id, job_id, job_id),
        )
        connection.execute(
            'DELETE FROM danmaku_items WHERE part_id IN('
            'SELECT id FROM upload_parts WHERE job_id=?)',
            (job_id,),
        )
        connection.execute(
            'DELETE FROM upload_chunks WHERE part_id IN('
            'SELECT id FROM upload_parts WHERE job_id=?)',
            (job_id,),
        )
        connection.execute('DELETE FROM comment_items WHERE job_id=?', (job_id,))
        connection.execute('DELETE FROM upload_parts WHERE job_id=?', (job_id,))

    @staticmethod
    def _delete_session_handoffs(
        connection: sqlite3.Connection, session_id: int
    ) -> None:
        connection.execute(
            'DELETE FROM owner_handoff_outcomes WHERE '
            "(owner_kind='recorder' AND owner_id=?) OR "
            "(owner_kind='media_index' AND owner_id IN("
            'SELECT id FROM recording_parts WHERE session_id=?))',
            (session_id, session_id),
        )
