from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from loguru import logger

from blrec.bili_upload.accounts import (
    AccountNotFound,
    AccountPaused,
    AccountWriteGate,
    CredentialVersionChanged,
)
from blrec.bili_upload.credentials import CredentialNotFound
from blrec.bili_upload.crypto import (
    CredentialBundle,
    InvalidCredentialBundle,
    InvalidCredentialKey,
)
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.errors import (
    BiliApiError,
    DefinitelyNotSent,
    ProtocolContractError,
    RemoteOutcomeUnknown,
)

from .catalog import hero_chinese_name
from .exclusions import EXCLUDED_TITLE_MARKER
from .repository import MatchRecord, VaingloryRepository

DESCRIPTION_BEGIN = '【对局战绩（自动识别）】'
DESCRIPTION_END = '【对局战绩结束】'
_COMMENT_LIMIT = 1000
_PICTURES_PER_COMMENT = 9
_CHAPTER_CONTENT_LIMIT = 16
_DESCRIPTION_TIMESTAMP = re.compile(
    r'^(第\d+局) (?:(?:\d+#)|(?:P\d+ ))(?:\d{2}:){1,2}\d{2}(｜)'
)
_GENERATED_SUMMARY = re.compile(r'^共 \d+ 局｜\d+ 胜 \d+ 负(?:｜\d+ 局结果未确认)?$')
_GENERATED_MATCH_LINE = re.compile(
    r'^(?:第\d+局(?: (?:(?:\d+#)|(?:P\d+ ))(?:\d{2}:){1,2}\d{2})?'
    r'(?: https://www\.bilibili\.com/video/BV[0-9A-Za-z]+'
    r'\?p=\d+&t=\d+)?｜(?:胜|负|结果未确认)｜|'
    r'[①-⑳㉑-㉟㊱-㊿]｜(?:胜　|负　|待定)｜)'
)
_GENERATED_LINK_LINE = re.compile(
    r'^第\d+局：https://www\.bilibili\.com/video/BV[0-9A-Za-z]+' r'\?p=\d+&t=\d+$'
)
_GENERATED_TRUNCATION = '…其余对局请见置顶评论'

_SESSION_ARCHIVES_COMPLETE = (
    'NOT EXISTS(SELECT 1 FROM vainglory_archive_imports source_import '
    'WHERE source_import.session_id=session.id AND ('
    "source_import.state!='ready' OR source_import.page_count<=0 OR "
    'source_import.completed_page_count!=source_import.page_count OR ('
    'SELECT COUNT(*) FROM vainglory_archive_parts source_part '
    'WHERE source_part.import_id=source_import.id '
    "AND source_part.state='ready')!=source_import.page_count))"
)
_PUBLICATION_READY_PREDICATE = (
    'EXISTS(SELECT 1 FROM vainglory_scan_jobs source_scan '
    'WHERE source_scan.session_id=session.id '
    "AND source_scan.state='ready') AND "
    + _SESSION_ARCHIVES_COMPLETE
    + " AND (publication.source_kind!='archive' OR EXISTS("
    'SELECT 1 FROM vainglory_archive_imports source_import '
    'WHERE source_import.session_id=session.id '
    'AND source_import.account_id=publication.account_id '
    'AND source_import.bvid=publication.bvid '
    "AND source_import.state='ready' AND source_import.page_count>0 "
    'AND source_import.completed_page_count=source_import.page_count '
    'AND (SELECT COUNT(*) FROM vainglory_archive_parts source_part '
    'WHERE source_part.import_id=source_import.id '
    "AND source_part.state='ready')=source_import.page_count)) "
    "AND (publication.source_kind!='upload' OR ("
    'publication.upload_job_id IS NOT NULL AND EXISTS('
    'SELECT 1 FROM upload_parts expected_upload '
    'WHERE expected_upload.job_id=publication.upload_job_id '
    'AND expected_upload.cid IS NOT NULL) AND NOT EXISTS('
    'SELECT 1 FROM upload_parts expected_upload '
    'WHERE expected_upload.job_id=publication.upload_job_id '
    'AND expected_upload.cid IS NOT NULL AND NOT EXISTS('
    'SELECT 1 FROM recording_parts expected_recording '
    'JOIN vainglory_part_jobs expected_analysis '
    'ON expected_analysis.part_id=expected_recording.id '
    'WHERE expected_recording.session_id=session.id '
    'AND expected_recording.part_index=expected_upload.part_index '
    "AND expected_analysis.state='ready'))))"
)


@dataclass(frozen=True)
class PublicationCommentPlan:
    content: str
    match_ids: Tuple[int, ...]


@dataclass(frozen=True)
class PublicationPlan:
    payload_hash: str
    description_block: str
    comments: Tuple[PublicationCommentPlan, ...]


@dataclass(frozen=True)
class _Candidate:
    account_id: int
    session_id: int
    upload_job_id: Optional[int]
    aid: int
    bvid: str
    source_kind: str


@dataclass(frozen=True)
class _ChapterPage:
    page: int
    cid: int
    duration_seconds: Optional[int]


def build_publication_plan(matches: Sequence[MatchRecord]) -> PublicationPlan:
    ordered = tuple(
        sorted(
            matches,
            key=lambda match: (
                match.archive_page or match.part_index,
                match.started_at_ms,
                match.result_at_ms,
                match.id,
            ),
        )
    )
    if not ordered:
        raise ValueError('publication needs at least one match')
    bvids = {match.bvid for match in ordered}
    if len(bvids) != 1 or None in bvids:
        raise ValueError('publication matches must belong to one archive')
    wins = sum(match.winner_color == 'teal' for match in ordered)
    losses = sum(match.winner_color == 'orange' for match in ordered)
    unknown = len(ordered) - wins - losses
    summary = '共 {} 局｜{} 胜 {} 负'.format(len(ordered), wins, losses)
    if unknown:
        summary += '｜{} 局结果未确认'.format(unknown)
    description_lines = tuple(
        _match_line(index, match, include_timestamp=False)
        for index, match in enumerate(ordered, 1)
    )
    description_links = tuple(
        line
        for index, match in enumerate(ordered, 1)
        for line in (_match_link_line(index, match),)
        if line is not None
    )
    comment_lines = tuple(
        _match_line(index, match, include_timestamp=True)
        for index, match in enumerate(ordered, 1)
    )
    identity = json.dumps(
        {
            'matches': [
                {
                    'id': match.id,
                    'page': match.archive_page,
                    'anchor': _match_anchor(match),
                    'start': match.started_at_ms,
                    'result': match.result_at_ms,
                    'winner': match.winner_color,
                    'game_mode': match.game_mode,
                    'players': [
                        [
                            player.side,
                            player.slot,
                            player.hero_label,
                            player.is_recorded_player,
                        ]
                        for player in match.players
                    ],
                }
                for match in ordered
            ],
            'summary': summary,
            'version': 10,
        },
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf8')
    payload_hash = hashlib.sha256(identity).hexdigest()
    description_parts = [summary]
    if description_links:
        description_parts.extend(('对局跳转', *description_links))
    description_parts.extend(('逐局对阵', *description_lines))
    description_block = '\n'.join(description_parts)
    comments = _comment_plans(summary, ordered, comment_lines)
    return PublicationPlan(payload_hash, description_block, comments)


def merge_archive_description(
    current: str, block: str, *, max_chars: int = 2000
) -> Optional[str]:
    if max_chars < 1:
        raise ValueError('description limit must be positive')
    visible_block = _visible_description_block(block)
    start = current.find(DESCRIPTION_BEGIN)
    end = current.find(DESCRIPTION_END, start + len(DESCRIPTION_BEGIN))
    if start >= 0 and end >= 0:
        end += len(DESCRIPTION_END)
        prefix = current[:start]
        suffix = current[end:]
        capacity = max_chars - len(prefix) - len(suffix)
        fitted = _fit_description_block(visible_block, capacity)
        return None if fitted is None else prefix + fitted + suffix
    generated_span = _generated_description_block_span(current)
    if generated_span is not None:
        start, end = generated_span
        prefix = current[:start]
        suffix = current[end:]
        capacity = max_chars - len(prefix) - len(suffix)
        fitted = _fit_description_block(visible_block, capacity)
        return None if fitted is None else prefix + fitted + suffix
    if _description_block_span(current, visible_block) is not None:
        return current
    separator = '' if not current else ('\n' if current.endswith('\n') else '\n\n')
    capacity = max_chars - len(current) - len(separator)
    fitted = _fit_description_block(visible_block, capacity)
    return None if fitted is None else current + separator + fitted


def description_contains_block(current: str, block: str) -> bool:
    return (
        _description_block_span(current, _visible_description_block(block)) is not None
    )


def remove_generated_description(current: str, block: str) -> str:
    start = current.find(DESCRIPTION_BEGIN)
    end = current.find(DESCRIPTION_END, start + len(DESCRIPTION_BEGIN))
    span: Optional[Tuple[int, int]] = None
    if start >= 0 and end >= 0:
        span = (start, end + len(DESCRIPTION_END))
    if span is None:
        span = _description_block_span(current, _visible_description_block(block))
    if span is None:
        span = _generated_description_block_span(current)
    if span is None:
        return current
    prefix = current[: span[0]].rstrip('\r\n')
    suffix = current[span[1] :].lstrip('\r\n')
    if prefix and suffix:
        return prefix + '\n\n' + suffix
    return prefix or suffix


class VaingloryPublicationService:
    _RETRYABLE_CODES = frozenset((-412, -352, 412, 429, 12015, 12051))
    _MISSING_REPLY_CODES = frozenset((-404, 404, 12002))
    _PERMANENT_CODES = frozenset(
        (-403, 403, 12002, 12003, 12009, 12016, 12025, 12035, 12045, 12052)
    )

    def __init__(
        self,
        database: BiliUploadDatabase,
        repository: VaingloryRepository,
        protocol: Any,
        *,
        bundle_loader: Callable[[int], Awaitable[CredentialBundle]],
        account_gates: AccountWriteGate,
        clock: Callable[[], float] = time.time,
        idle_poll_seconds: float = 10,
        action_interval_seconds: float = 10,
        picture_interval_seconds: float = 1,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if (
            idle_poll_seconds <= 0
            or action_interval_seconds < 0
            or picture_interval_seconds < 0
        ):
            raise ValueError('publication intervals are invalid')
        self._database = database
        self._repository = repository
        self._protocol = protocol
        self._bundle_loader = bundle_loader
        self._account_gates = account_gates
        self._clock = clock
        self._idle_poll_seconds = idle_poll_seconds
        self._action_interval_seconds = action_interval_seconds
        self._picture_interval_seconds = picture_interval_seconds
        self._sleeper = sleeper
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.recover_interrupted()
        self._wake.set()
        self._task = asyncio.create_task(self._run(), name='vainglory-publication')

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def recover_interrupted(self) -> int:
        now = self._now()

        def recover(connection: sqlite3.Connection) -> int:
            changed = connection.execute(
                "UPDATE vainglory_publication_comments SET state='unknown_outcome',"
                "error='进程中断后需要远端对账',updated_at=? "
                "WHERE state='in_flight'",
                (now,),
            ).rowcount
            changed += connection.execute(
                'UPDATE vainglory_publication_stale_comments '
                "SET state='unknown_outcome',"
                "error='进程中断后需要确认旧评论是否已删除',updated_at=? "
                "WHERE state='in_flight'",
                (now,),
            ).rowcount
            changed += connection.execute(
                "UPDATE vainglory_publications SET pin_state='prepared',"
                "error='进程中断后将重新确认置顶',updated_at=? "
                "WHERE pin_state='in_flight' AND state!='confirmed'",
                (now,),
            ).rowcount
            return changed

        return await self._database.write(recover)

    async def purge_excluded_remote(self) -> int:
        rows = await self._database.fetchall(
            'SELECT publication.*,account.state AS account_state,'
            'account.credential_version '
            'FROM vainglory_publications publication '
            'JOIN bili_accounts account ON account.id=publication.account_id '
            'JOIN recording_sessions session ON session.id=publication.session_id '
            'WHERE instr(COALESCE(session.title,\'\'),?)>0 OR EXISTS('
            'SELECT 1 FROM vainglory_archive_imports imported '
            'WHERE imported.session_id=publication.session_id '
            'AND instr(imported.title,?)>0) OR EXISTS('
            'SELECT 1 FROM archive_migration_items migration '
            'WHERE migration.session_id=publication.session_id '
            'AND instr(migration.title,?)>0) OR EXISTS('
            'SELECT 1 FROM upload_jobs source_job '
            'WHERE source_job.id=publication.upload_job_id '
            'AND instr(COALESCE(source_job.policy_snapshot_json,\'\'),?)>0) '
            'ORDER BY publication.id',
            (
                EXCLUDED_TITLE_MARKER,
                EXCLUDED_TITLE_MARKER,
                EXCLUDED_TITLE_MARKER,
                EXCLUDED_TITLE_MARKER,
            ),
        )
        cleaned = 0
        for index, raw in enumerate(rows):
            publication = dict(raw)
            if str(publication['account_state']) != 'active':
                logger.warning(
                    'Skipped remote exclusion cleanup for inactive account: bvid={}',
                    str(publication['bvid']),
                )
                continue
            try:
                gate = self._account_gates.for_account(int(publication['account_id']))
                async with gate.hold(int(publication['credential_version'])):
                    bundle = await self._bundle_loader(int(publication['account_id']))
                    await self._purge_excluded_archive(publication, bundle)
            except (
                AccountNotFound,
                AccountPaused,
                CredentialVersionChanged,
                CredentialNotFound,
                InvalidCredentialBundle,
                InvalidCredentialKey,
                BiliApiError,
                DefinitelyNotSent,
                RemoteOutcomeUnknown,
                ProtocolContractError,
            ) as error:
                logger.warning(
                    'Remote exclusion cleanup failed: bvid={} reason={!r}',
                    str(publication['bvid']),
                    error,
                )
                continue
            except Exception:
                logger.exception(
                    'Unexpected remote exclusion cleanup failure: bvid={}',
                    str(publication['bvid']),
                )
                continue
            await self._database.execute(
                'DELETE FROM vainglory_publications WHERE id=?',
                (int(publication['id']),),
            )
            cleaned += 1
            if self._action_interval_seconds and index + 1 < len(rows):
                await self._sleeper(self._action_interval_seconds)
        return cleaned

    async def _purge_excluded_archive(
        self, publication: Mapping[str, Any], bundle: CredentialBundle
    ) -> None:
        response = await self._protocol.archive_view(
            bundle,
            {
                'topic_grey': 1,
                'bvid': str(publication['bvid']),
                't': int(self._clock() * 1000),
            },
        )
        payload, current = self._edit_payload(
            response, aid=int(publication['aid']), bvid=str(publication['bvid'])
        )
        cleaned_description = remove_generated_description(
            current, str(publication['description_block'])
        )
        if cleaned_description != current:
            payload['desc'] = cleaned_description
            await self._protocol.edit_archive(bundle, payload)
        comments = await self._database.fetchall(
            'SELECT rpid FROM vainglory_publication_comments '
            'WHERE publication_id=? AND rpid IS NOT NULL ORDER BY ordinal',
            (int(publication['id']),),
        )
        for comment in comments:
            await self._protocol.delete_reply(
                bundle,
                {
                    'type': 1,
                    'oid': int(publication['aid']),
                    'rpid': int(comment['rpid']),
                },
            )

    async def run_once(self) -> bool:
        if await self._discover():
            return True
        work_query = (
            'SELECT publication.*,account.state AS account_state,'
            'account.credential_version,account.uid AS account_uid '
            'FROM vainglory_publications publication '
            'JOIN bili_accounts account ON account.id=publication.account_id '
            'JOIN recording_sessions session ON session.id=publication.session_id '
            "WHERE publication.state IN ('prepared','running','paused') "
            'AND publication.needs_refresh=0 '
            'AND instr(COALESCE(session.title,\'\'),?)=0 '
            'AND NOT EXISTS(SELECT 1 FROM vainglory_archive_imports imported '
            'WHERE imported.session_id=publication.session_id '
            'AND instr(imported.title,?)>0) '
            'AND NOT EXISTS(SELECT 1 FROM archive_migration_items migration '
            'WHERE migration.session_id=publication.session_id '
            'AND instr(migration.title,?)>0) '
            'AND NOT EXISTS(SELECT 1 FROM upload_jobs source_job '
            'WHERE source_job.id=publication.upload_job_id '
            'AND instr(COALESCE(source_job.policy_snapshot_json,\'\'),?)>0) '
            'AND '
            + _PUBLICATION_READY_PREDICATE
            + ' AND publication.next_attempt_at<=? '
            'ORDER BY CASE '
            "WHEN publication.source_kind='upload' AND NOT EXISTS("
            'SELECT 1 FROM archive_migration_items priority_item '
            'WHERE priority_item.upload_job_id=publication.upload_job_id) THEN 0 '
            "WHEN publication.source_kind='archive' THEN 1 ELSE 2 END,"
            'publication.created_at,publication.id LIMIT 1'
        )
        row = await self._database.fetchone(
            work_query,
            (
                EXCLUDED_TITLE_MARKER,
                EXCLUDED_TITLE_MARKER,
                EXCLUDED_TITLE_MARKER,
                EXCLUDED_TITLE_MARKER,
                self._now(),
            ),
        )
        if row is not None:
            await self._process(dict(row))
            return True
        return False

    async def _run(self) -> None:
        while True:
            try:
                progressed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Vainglory publication worker iteration failed')
                progressed = False
            if progressed:
                await self._sleeper(self._action_interval_seconds)
                continue
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._idle_poll_seconds
                )
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _discover(self) -> bool:
        candidate = await self._next_candidate()
        if candidate is None:
            return False
        selected = candidate
        matches = await self._session_matches(selected.session_id)
        matches = tuple(match for match in matches if match.bvid == selected.bvid)
        if not matches:
            return False
        plan = build_publication_plan(matches)
        now = self._now()

        def persist(connection: sqlite3.Connection) -> bool:
            current = connection.execute(
                'SELECT id,payload_hash FROM vainglory_publications '
                'WHERE account_id=? AND bvid=?',
                (selected.account_id, selected.bvid),
            ).fetchone()
            if current is not None:
                publication_id = int(current['id'])
                if str(current['payload_hash']) == plan.payload_hash:
                    connection.execute(
                        'UPDATE vainglory_publications SET needs_refresh=0,'
                        'updated_at=? WHERE id=?',
                        (now, publication_id),
                    )
                    return True
                connection.execute(
                    'INSERT INTO vainglory_publication_stale_comments('
                    'publication_id,ordinal,content,rpid,state,attempt_count,'
                    'next_attempt_at,error,created_at,updated_at) '
                    'SELECT publication_id,ordinal,content,rpid,'
                    "CASE WHEN rpid IS NULL THEN 'unknown_outcome' "
                    "ELSE 'prepared' END,0,0,NULL,?,? "
                    'FROM vainglory_publication_comments '
                    'WHERE publication_id=? AND ('
                    'rpid IS NOT NULL OR state IN ('
                    "'in_flight','unknown_outcome'))",
                    (now, now, publication_id),
                )
                connection.execute(
                    'DELETE FROM vainglory_publication_comments '
                    'WHERE publication_id=?',
                    (publication_id,),
                )
                connection.execute(
                    'UPDATE vainglory_publications SET session_id=?,'
                    'upload_job_id=?,aid=?,source_kind=?,payload_hash=?,'
                    "description_block=?,state='prepared',"
                    "chapter_state='prepared',description_state='prepared',"
                    "pin_state='prepared',"
                    'root_rpid=NULL,attempt_count=0,next_attempt_at=0,error=NULL,'
                    'needs_refresh=0,updated_at=? WHERE id=?',
                    (
                        selected.session_id,
                        selected.upload_job_id,
                        selected.aid,
                        selected.source_kind,
                        plan.payload_hash,
                        plan.description_block,
                        now,
                        publication_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    'INSERT INTO vainglory_publications('
                    'account_id,session_id,upload_job_id,aid,bvid,source_kind,'
                    'payload_hash,description_block,state,description_state,'
                    'pin_state,needs_refresh,created_at,updated_at) '
                    "VALUES(?,?,?,?,?,?,?,?,'prepared','prepared','prepared',0,?,?)",
                    (
                        selected.account_id,
                        selected.session_id,
                        selected.upload_job_id,
                        selected.aid,
                        selected.bvid,
                        selected.source_kind,
                        plan.payload_hash,
                        plan.description_block,
                        now,
                        now,
                    ),
                )
                publication_id = int(cursor.lastrowid)
            for ordinal, item in enumerate(plan.comments):
                connection.execute(
                    'INSERT INTO vainglory_publication_comments('
                    'publication_id,ordinal,content,match_ids_json,'
                    'uploaded_pictures_json,state,created_at,updated_at) '
                    "VALUES(?,?,?,?,?,'prepared',?,?)",
                    (
                        publication_id,
                        ordinal,
                        item.content,
                        json.dumps(item.match_ids, separators=(',', ':')),
                        '[]',
                        now,
                        now,
                    ),
                )
            return True

        return await self._database.write(persist)

    async def _next_candidate(self) -> Optional[_Candidate]:
        common = (
            'JOIN vainglory_scan_jobs scan ON scan.session_id=session.id '
            "JOIN bili_accounts account ON account.id={account}.account_id "
            "WHERE account.state='active' AND scan.state='ready' AND "
            + _SESSION_ARCHIVES_COMPLETE
            + ' '
            "AND instr(COALESCE(session.title,''),'直播剪辑')=0 "
            'AND scan.algorithm_version>=? AND EXISTS('
            'SELECT 1 FROM vainglory_matches match '
            'WHERE match.session_id=session.id) AND NOT EXISTS('
            'SELECT 1 FROM vainglory_matches match '
            'WHERE match.session_id=session.id '
            'AND match.hero_recognition_version<? AND EXISTS('
            'SELECT 1 FROM vainglory_match_players player '
            'WHERE player.match_id=match.id '
            'AND player.hero_id IS NULL)) AND NOT EXISTS('
            'SELECT 1 FROM vainglory_matches match '
            'WHERE match.session_id=session.id '
            'AND match.recorded_player_detection_version<?) '
            'AND (NOT EXISTS('
            'SELECT 1 FROM vainglory_publications publication '
            'WHERE publication.account_id={account}.account_id '
            'AND publication.bvid={account}.bvid) OR EXISTS('
            'SELECT 1 FROM vainglory_publications publication '
            'WHERE publication.account_id={account}.account_id '
            'AND publication.bvid={account}.bvid '
            'AND publication.needs_refresh=1)) '
        )
        row = await self._database.fetchone(
            'SELECT job.account_id,job.session_id,job.id AS upload_job_id,'
            "job.aid,job.bvid,'upload' AS source_kind "
            'FROM upload_jobs job '
            'JOIN recording_sessions session ON session.id=job.session_id '
            + common.format(account='job')
            + "AND job.state IN ('waiting_review','approved','completed') "
            "AND job.submit_state='confirmed' AND job.aid>0 AND job.bvid<>'' "
            'AND EXISTS(SELECT 1 FROM upload_parts expected_upload '
            'WHERE expected_upload.job_id=job.id '
            'AND expected_upload.cid IS NOT NULL) '
            'AND NOT EXISTS(SELECT 1 FROM upload_parts expected_upload '
            'WHERE expected_upload.job_id=job.id '
            'AND expected_upload.cid IS NOT NULL AND NOT EXISTS('
            'SELECT 1 FROM recording_parts expected_recording '
            'JOIN vainglory_part_jobs expected_analysis '
            'ON expected_analysis.part_id=expected_recording.id '
            'WHERE expected_recording.session_id=session.id '
            'AND expected_recording.part_index=expected_upload.part_index '
            "AND expected_analysis.state='ready')) "
            'ORDER BY CASE WHEN EXISTS('
            'SELECT 1 FROM archive_migration_items priority_item '
            'WHERE priority_item.upload_job_id=job.id) THEN 1 ELSE 0 END,'
            'session.started_at DESC,job.id DESC LIMIT 1',
            (
                VaingloryRepository.ALGORITHM_VERSION,
                VaingloryRepository.HERO_RECOGNITION_VERSION,
                VaingloryRepository.RECORDED_PLAYER_DETECTION_VERSION,
            ),
        )
        if row is None:
            row = await self._database.fetchone(
                'SELECT imported.account_id,imported.session_id,'
                "NULL AS upload_job_id,imported.aid,imported.bvid,'archive' "
                'AS source_kind FROM vainglory_archive_imports imported '
                'JOIN recording_sessions session '
                'ON session.id=imported.session_id '
                + common.format(account='imported')
                + "AND imported.state='ready' AND imported.page_count>0 "
                'AND imported.completed_page_count=imported.page_count '
                'AND imported.session_id IS NOT NULL '
                'AND NOT EXISTS(SELECT 1 FROM upload_jobs job '
                'WHERE job.session_id=imported.session_id) '
                'ORDER BY imported.published_at DESC,imported.id DESC LIMIT 1',
                (
                    VaingloryRepository.ALGORITHM_VERSION,
                    VaingloryRepository.HERO_RECOGNITION_VERSION,
                    VaingloryRepository.RECORDED_PLAYER_DETECTION_VERSION,
                ),
            )
        if row is None:
            return None
        return _Candidate(
            account_id=int(row['account_id']),
            session_id=int(row['session_id']),
            upload_job_id=(
                None if row['upload_job_id'] is None else int(row['upload_job_id'])
            ),
            aid=int(row['aid']),
            bvid=str(row['bvid']),
            source_kind=str(row['source_kind']),
        )

    async def _session_matches(self, session_id: int) -> Tuple[MatchRecord, ...]:
        offset = 0
        matches: List[MatchRecord] = []
        while True:
            page = await self._repository.list_matches(
                session_id=session_id, limit=100, offset=offset
            )
            matches.extend(page.items)
            offset += len(page.items)
            if offset >= page.total or not page.items:
                return tuple(matches)

    async def _process(self, publication: Mapping[str, Any]) -> None:
        publication_id = int(publication['id'])
        if not await self._publication_ready(publication_id):
            return
        if str(publication['account_state']) != 'active':
            await self._retry_publication(
                publication_id, int(publication['attempt_count']), '投稿账号当前不可用'
            )
            return
        try:
            gate = self._account_gates.for_account(int(publication['account_id']))
            async with gate.hold(int(publication['credential_version'])):
                bundle = await self._bundle_loader(int(publication['account_id']))
                await self._database.execute(
                    "UPDATE vainglory_publications SET state='running',"
                    'attempt_count=attempt_count+1,error=NULL,updated_at=? WHERE id=?',
                    (self._now(), publication_id),
                )
                stale_comment = await self._next_stale_comment(publication_id)
                if stale_comment is not None:
                    if int(stale_comment['next_attempt_at']) > self._now():
                        await self._database.execute(
                            'UPDATE vainglory_publications SET next_attempt_at=? '
                            'WHERE id=?',
                            (int(stale_comment['next_attempt_at']), publication_id),
                        )
                        return
                    await self._remove_stale_comment(publication, stale_comment, bundle)
                    return
                if str(publication['chapter_state']) not in ('confirmed', 'skipped'):
                    await self._publish_chapters(publication, bundle)
                    return
                if str(publication['description_state']) not in (
                    'confirmed',
                    'skipped_no_room',
                ):
                    await self._publish_description(publication, bundle)
                    return
                comment = await self._next_comment(publication_id)
                if comment is not None:
                    if int(comment['next_attempt_at']) > self._now():
                        await self._database.execute(
                            'UPDATE vainglory_publications SET next_attempt_at=? '
                            'WHERE id=?',
                            (int(comment['next_attempt_at']), publication_id),
                        )
                        return
                    await self._publish_comment(publication, comment, bundle)
                    return
                if str(publication['pin_state']) != 'confirmed':
                    await self._publish_pin(publication, bundle)
                    return
                await self._database.execute(
                    "UPDATE vainglory_publications SET state='confirmed',"
                    "needs_refresh=0,error=CASE WHEN chapter_state='skipped' "
                    'THEN error ELSE NULL END,updated_at=? WHERE id=?',
                    (self._now(), publication_id),
                )
        except (
            AccountNotFound,
            AccountPaused,
            CredentialVersionChanged,
            CredentialNotFound,
            InvalidCredentialBundle,
            InvalidCredentialKey,
        ):
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                '投稿账号或凭据在发布期间发生变化',
            )

    async def _publication_ready(self, publication_id: int) -> bool:
        return bool(
            await self._database.scalar(
                'SELECT EXISTS(SELECT 1 FROM vainglory_publications publication '
                'JOIN recording_sessions session '
                'ON session.id=publication.session_id '
                'WHERE publication.id=? AND ' + _PUBLICATION_READY_PREDICATE + ')',
                (int(publication_id),),
            )
        )

    async def _publish_chapters(
        self, publication: Mapping[str, Any], bundle: CredentialBundle
    ) -> None:
        publication_id = int(publication['id'])
        try:
            response = await self._protocol.archive_view(
                bundle,
                {
                    'topic_grey': 1,
                    'bvid': str(publication['bvid']),
                    't': int(self._clock() * 1000),
                },
            )
            pages = _chapter_pages(response)
            if not pages or any(page.duration_seconds is None for page in pages):
                public = await self._protocol.public_archive_view(
                    bundle, bvid=str(publication['bvid'])
                )
                pages = _merge_chapter_pages(pages, _chapter_pages(public))
            matches = await self._session_matches(int(publication['session_id']))
            matches = tuple(
                match for match in matches if match.bvid == str(publication['bvid'])
            )
            targets = _chapter_targets(matches, pages)
            if not targets:
                await self._set_chapter_state(
                    publication_id,
                    'skipped',
                    error='没有可写入 B 站章节的有效对局时间点',
                )
                return
            for target_index, (page, cards) in enumerate(targets):
                current = await self._protocol.archive_cards(
                    bundle, aid=int(publication['aid']), cid=page.cid
                )
                existing = _existing_chapter_cards(current)
                if existing and not _automatic_chapter_cards(existing):
                    logger.info(
                        'Preserved custom Bilibili chapters: bvid={} page={} cid={}',
                        str(publication['bvid']),
                        page.page,
                        page.cid,
                    )
                    continue
                if _same_chapter_cards(existing, cards):
                    continue
                await self._protocol.submit_archive_chapters(
                    bundle,
                    aid=int(publication['aid']),
                    cid=page.cid,
                    cards=cards,
                    permanent=True,
                )
                if self._picture_interval_seconds and target_index + 1 < len(targets):
                    await self._sleeper(self._picture_interval_seconds)
        except DefinitelyNotSent:
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                'B 站章节请求未发出，将自动重试',
            )
        except RemoteOutcomeUnknown:
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                'B 站章节更新结果未知，下次先远端对账',
            )
        except BiliApiError as error:
            if error.code in self._RETRYABLE_CODES:
                await self._handle_api_error(publication_id, publication, error)
            else:
                logger.warning(
                    'Skipped unsupported Bilibili chapters: bvid={} code={}',
                    str(publication['bvid']),
                    error.code,
                )
                await self._set_chapter_state(
                    publication_id,
                    'skipped',
                    error='B 站章节接口拒绝写入（{}）{}'.format(
                        error.code,
                        (
                            ''
                            if not error.public_message
                            else '：{}'.format(error.public_message)
                        ),
                    ),
                )
        except ProtocolContractError:
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                'B 站章节数据结构异常，将自动重试',
                minimum_delay=3600,
            )
        else:
            await self._set_chapter_state(publication_id, 'confirmed')

    async def _publish_description(
        self, publication: Mapping[str, Any], bundle: CredentialBundle
    ) -> None:
        publication_id = int(publication['id'])
        try:
            response = await self._protocol.archive_view(
                bundle,
                {
                    'topic_grey': 1,
                    'bvid': str(publication['bvid']),
                    't': int(self._clock() * 1000),
                },
            )
            payload, current = self._edit_payload(
                response, aid=int(publication['aid']), bvid=str(publication['bvid'])
            )
        except (DefinitelyNotSent, RemoteOutcomeUnknown, BiliApiError):
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                '读取稿件简介失败，将自动重试',
            )
            return
        except ProtocolContractError:
            await self._fail(publication_id, '稿件详情结构不符合预期')
            return
        block = str(publication['description_block'])
        if description_contains_block(current, block):
            await self._set_description_state(publication_id, 'confirmed')
            return
        merged = merge_archive_description(current, block)
        if merged is None:
            await self._set_description_state(publication_id, 'skipped_no_room')
            return
        if merged == current:
            await self._set_description_state(publication_id, 'confirmed')
            return
        payload['desc'] = merged
        await self._set_description_state(publication_id, 'in_flight')
        try:
            await self._protocol.edit_archive(bundle, payload)
        except DefinitelyNotSent:
            await self._set_description_state(publication_id, 'prepared')
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                '简介更新请求未发出，将自动重试',
            )
        except RemoteOutcomeUnknown:
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                '简介更新结果未知，下次先远端对账',
            )
        except BiliApiError as error:
            await self._handle_api_error(publication_id, publication, error)
        except ProtocolContractError:
            await self._fail(publication_id, '简介更新响应不符合预期')
        else:
            await self._set_description_state(publication_id, 'confirmed')

    async def _next_stale_comment(
        self, publication_id: int
    ) -> Optional[Mapping[str, Any]]:
        row = await self._database.fetchone(
            'SELECT * FROM vainglory_publication_stale_comments '
            'WHERE publication_id=? ORDER BY ordinal DESC,id LIMIT 1',
            (publication_id,),
        )
        return None if row is None else dict(row)

    async def _remove_stale_comment(
        self,
        publication: Mapping[str, Any],
        comment: Mapping[str, Any],
        bundle: CredentialBundle,
    ) -> None:
        if str(comment['state']) == 'unknown_outcome':
            await self._reconcile_stale_comment(publication, comment, bundle)
            return
        rpid = _positive_int(comment.get('rpid'))
        if rpid is None:
            await self._forget_stale_comment(int(comment['id']))
            return
        await self._database.execute(
            'UPDATE vainglory_publication_stale_comments '
            "SET state='in_flight',attempt_count=attempt_count+1,error=NULL,"
            'updated_at=? WHERE id=?',
            (self._now(), int(comment['id'])),
        )
        try:
            await self._protocol.delete_reply(
                bundle, {'type': 1, 'oid': int(publication['aid']), 'rpid': rpid}
            )
        except DefinitelyNotSent:
            await self._retry_stale_comment(
                publication,
                comment,
                '旧评论删除请求未发出，将自动重试',
                state='prepared',
            )
        except RemoteOutcomeUnknown:
            await self._retry_stale_comment(
                publication,
                comment,
                '旧评论删除结果未知，下次先远端确认',
                state='unknown_outcome',
            )
        except BiliApiError as error:
            if error.code in self._MISSING_REPLY_CODES:
                await self._forget_stale_comment(int(comment['id']))
            else:
                await self._handle_api_error(int(publication['id']), publication, error)
        except ProtocolContractError:
            await self._retry_stale_comment(
                publication,
                comment,
                '旧评论删除响应异常，将自动重试',
                state='unknown_outcome',
                minimum_delay=3600,
            )
        else:
            await self._forget_stale_comment(int(comment['id']))

    async def _reconcile_stale_comment(
        self,
        publication: Mapping[str, Any],
        comment: Mapping[str, Any],
        bundle: CredentialBundle,
    ) -> None:
        rpid = _positive_int(comment.get('rpid'))
        try:
            if rpid is not None:
                response = await self._protocol.reply_detail(
                    bundle,
                    {
                        'type': 1,
                        'oid': int(publication['aid']),
                        'root': rpid,
                        'pn': 1,
                        'ps': 20,
                    },
                )
                data = response.get('data')
                root = data.get('root') if isinstance(data, Mapping) else None
                if root is None:
                    await self._forget_stale_comment(int(comment['id']))
                    return
                if (
                    not isinstance(root, Mapping)
                    or _positive_int(root.get('rpid')) != rpid
                ):
                    raise ProtocolContractError('old comment identity is inconsistent')
            else:
                response = await self._protocol.list_replies(
                    bundle,
                    {
                        'type': 1,
                        'oid': int(publication['aid']),
                        'mode': 2,
                        'next': 0,
                        'ps': 20,
                    },
                )
                matches = [
                    reply
                    for reply in _reply_entries(response)
                    if _reply_matches(
                        reply,
                        content=str(comment['content']),
                        account_uid=int(publication['account_uid']),
                        aid=int(publication['aid']),
                        root_rpid=None,
                        is_root=True,
                    )
                ]
                if len(matches) != 1:
                    await self._retry_stale_comment(
                        publication,
                        comment,
                        '无法唯一确认旧评论是否存在，已停止发布新评论',
                        state='unknown_outcome',
                        minimum_delay=3600,
                    )
                    return
                rpid = _positive_int(matches[0].get('rpid'))
                if rpid is None:
                    raise ProtocolContractError('old comment has no RPID')
        except BiliApiError as error:
            if error.code in self._MISSING_REPLY_CODES:
                await self._forget_stale_comment(int(comment['id']))
            else:
                await self._handle_api_error(int(publication['id']), publication, error)
            return
        except (DefinitelyNotSent, RemoteOutcomeUnknown):
            await self._retry_stale_comment(
                publication,
                comment,
                '旧评论远端确认失败，将自动重试',
                state='unknown_outcome',
            )
            return
        except ProtocolContractError:
            await self._retry_stale_comment(
                publication,
                comment,
                '旧评论远端数据异常，将自动重试',
                state='unknown_outcome',
                minimum_delay=3600,
            )
            return
        await self._database.execute(
            'UPDATE vainglory_publication_stale_comments '
            "SET rpid=?,state='prepared',error=NULL,next_attempt_at=0,"
            'updated_at=? WHERE id=?',
            (rpid, self._now(), int(comment['id'])),
        )

    async def _retry_stale_comment(
        self,
        publication: Mapping[str, Any],
        comment: Mapping[str, Any],
        message: str,
        *,
        state: str,
        minimum_delay: int = 5,
    ) -> None:
        delay = min(
            3600, max(minimum_delay, 2 ** min(int(comment['attempt_count']) + 1, 11))
        )
        now = self._now()
        next_attempt_at = now + delay

        def retry(connection: sqlite3.Connection) -> None:
            connection.execute(
                'UPDATE vainglory_publication_stale_comments '
                'SET state=?,next_attempt_at=?,error=?,updated_at=? WHERE id=?',
                (state, next_attempt_at, message[:500], now, int(comment['id'])),
            )
            connection.execute(
                "UPDATE vainglory_publications SET state='paused',"
                'next_attempt_at=?,error=?,updated_at=? WHERE id=?',
                (next_attempt_at, message[:500], now, int(publication['id'])),
            )

        await self._database.write(retry)

    async def _forget_stale_comment(self, comment_id: int) -> None:
        await self._database.execute(
            'DELETE FROM vainglory_publication_stale_comments WHERE id=?', (comment_id,)
        )

    async def _next_comment(self, publication_id: int) -> Optional[Mapping[str, Any]]:
        row = await self._database.fetchone(
            'SELECT * FROM vainglory_publication_comments '
            "WHERE publication_id=? AND state!='confirmed' "
            'ORDER BY ordinal LIMIT 1',
            (publication_id,),
        )
        return None if row is None else dict(row)

    async def _publish_comment(
        self,
        publication: Mapping[str, Any],
        comment: Mapping[str, Any],
        bundle: CredentialBundle,
    ) -> None:
        publication_id = int(publication['id'])
        if str(comment['state']) == 'unknown_outcome':
            await self._reconcile_comment(publication, comment, bundle)
            return
        try:
            pictures = await self._upload_comment_pictures(comment, bundle)
        except (DefinitelyNotSent, RemoteOutcomeUnknown):
            await self._retry_comment(comment, '结算图上传未完成，将自动重试')
            return
        except BiliApiError as error:
            await self._handle_api_error(publication_id, publication, error)
            return
        except ProtocolContractError:
            await self._fail(publication_id, '结算图无法上传')
            return
        ordinal = int(comment['ordinal'])
        params: Dict[str, Any] = {
            'type': 1,
            'oid': int(publication['aid']),
            'message': str(comment['content']),
            'plat': 1,
        }
        visible_pictures = [picture for picture in pictures if picture is not None]
        if visible_pictures:
            params.update(
                {
                    'pictures': json.dumps(
                        visible_pictures, ensure_ascii=False, separators=(',', ':')
                    ),
                    'gaia_source': 'main_web',
                }
            )
        await self._database.execute(
            "UPDATE vainglory_publication_comments SET state='in_flight',"
            'attempt_count=attempt_count+1,error=NULL,updated_at=? WHERE id=?',
            (self._now(), int(comment['id'])),
        )
        try:
            response = await self._protocol.add_reply(bundle, params)
        except DefinitelyNotSent:
            await self._retry_comment(comment, '评论请求未发出，将自动重试')
            return
        except RemoteOutcomeUnknown:
            await self._database.execute(
                "UPDATE vainglory_publication_comments SET state='unknown_outcome',"
                "error='评论可能已发出，下次先远端对账',"
                'updated_at=? WHERE id=?',
                (self._now(), int(comment['id'])),
            )
            return
        except BiliApiError as error:
            if error.code == 12051:
                await self._database.execute(
                    "UPDATE vainglory_publication_comments "
                    "SET state='unknown_outcome',error='远端提示重复评论，需要对账',"
                    'updated_at=? WHERE id=?',
                    (self._now(), int(comment['id'])),
                )
            else:
                await self._database.execute(
                    "UPDATE vainglory_publication_comments SET state='prepared',"
                    'error=?,updated_at=? WHERE id=?',
                    (
                        'B 站评论接口返回错误（{}）'.format(error.code),
                        self._now(),
                        int(comment['id']),
                    ),
                )
                await self._handle_api_error(publication_id, publication, error)
            return
        except ProtocolContractError:
            await self._fail(publication_id, '评论响应不符合预期')
            return
        rpid = _positive_int(
            response.get('data', {}).get('rpid')
            if isinstance(response.get('data'), Mapping)
            else None
        )
        if rpid is None:
            await self._database.execute(
                "UPDATE vainglory_publication_comments SET state='unknown_outcome',"
                "error='评论接口没有返回 RPID，需要对账',updated_at=? "
                'WHERE id=?',
                (self._now(), int(comment['id'])),
            )
            return
        if visible_pictures:
            try:
                picture_count = await self._remote_comment_picture_count(
                    publication, bundle, rpid
                )
            except (BiliApiError, DefinitelyNotSent, RemoteOutcomeUnknown):
                await self._database.execute(
                    "UPDATE vainglory_publication_comments "
                    "SET state='unknown_outcome',rpid=?,"
                    "error='评论已发出，等待确认结算图',updated_at=? WHERE id=?",
                    (rpid, self._now(), int(comment['id'])),
                )
                await self._retry_publication(
                    publication_id,
                    int(publication['attempt_count']),
                    '等待确认评论中的结算图',
                )
                return
            except ProtocolContractError:
                await self._fail(publication_id, '评论图片确认响应不符合预期')
                return
            if picture_count < len(visible_pictures):
                await self._remove_incomplete_comment(
                    publication, comment, bundle, rpid
                )
                return
        await self._confirm_comment(publication_id, int(comment['id']), ordinal, rpid)

    async def _upload_comment_pictures(
        self, comment: Mapping[str, Any], bundle: CredentialBundle
    ) -> List[Optional[Mapping[str, Any]]]:
        match_ids = _json_positive_ints(comment['match_ids_json'])
        pictures = _json_pictures(comment['uploaded_pictures_json'])
        if len(pictures) > len(match_ids):
            raise ProtocolContractError('stored comment pictures are invalid')
        for match_id in match_ids[len(pictures) :]:
            path = await self._repository.result_frame_path(match_id)
            if path is None:
                pictures.append(None)
            else:
                try:
                    content = await asyncio.get_running_loop().run_in_executor(
                        None, Path(path).read_bytes
                    )
                except OSError:
                    logger.warning(
                        'Skipped unreadable Vainglory result frame: '
                        'match_id={} path={}',
                        match_id,
                        path,
                    )
                    pictures.append(None)
                else:
                    picture = await self._protocol.upload_comment_picture(
                        bundle,
                        filename=path.name,
                        mime_type='image/png',
                        content=content,
                    )
                    pictures.append(dict(picture))
                    if self._picture_interval_seconds and len(pictures) < len(
                        match_ids
                    ):
                        await self._sleeper(self._picture_interval_seconds)
            await self._database.execute(
                'UPDATE vainglory_publication_comments '
                'SET uploaded_pictures_json=?,updated_at=? WHERE id=?',
                (
                    json.dumps(pictures, ensure_ascii=False, separators=(',', ':')),
                    self._now(),
                    int(comment['id']),
                ),
            )
        return pictures

    async def _reconcile_comment(
        self,
        publication: Mapping[str, Any],
        comment: Mapping[str, Any],
        bundle: CredentialBundle,
    ) -> None:
        try:
            stored_rpid = _positive_int(comment.get('rpid'))
            if stored_rpid is not None:
                response = await self._protocol.reply_detail(
                    bundle,
                    {
                        'type': 1,
                        'oid': int(publication['aid']),
                        'root': stored_rpid,
                        'pn': 1,
                        'ps': 20,
                    },
                )
                entries = (_reply_root(response),)
            else:
                response = await self._protocol.list_replies(
                    bundle,
                    {
                        'type': 1,
                        'oid': int(publication['aid']),
                        'mode': 2,
                        'next': 0,
                        'ps': 20,
                    },
                )
                entries = _reply_entries(response)
            matches = [
                reply
                for reply in entries
                if _reply_matches(
                    reply,
                    content=str(comment['content']),
                    account_uid=int(publication['account_uid']),
                    aid=int(publication['aid']),
                    root_rpid=None,
                    is_root=True,
                )
            ]
        except (BiliApiError, DefinitelyNotSent, RemoteOutcomeUnknown):
            await self._retry_publication(
                int(publication['id']),
                int(publication['attempt_count']),
                '评论远端对账失败，将自动重试',
            )
            return
        except ProtocolContractError:
            await self._fail(int(publication['id']), '评论对账响应不符合预期')
            return
        if len(matches) != 1:
            await self._retry_publication(
                int(publication['id']),
                int(publication['attempt_count']),
                '无法唯一确认评论是否已发送',
                minimum_delay=3600,
            )
            return
        rpid = _positive_int(matches[0].get('rpid'))
        if rpid is None:
            await self._fail(int(publication['id']), '评论对账缺少 RPID')
            return
        expected_pictures = len(_json_positive_ints(comment['match_ids_json']))
        if expected_pictures and _reply_picture_count(matches[0]) < expected_pictures:
            await self._remove_incomplete_comment(publication, comment, bundle, rpid)
            return
        await self._confirm_comment(
            int(publication['id']), int(comment['id']), int(comment['ordinal']), rpid
        )

    async def _remote_comment_picture_count(
        self, publication: Mapping[str, Any], bundle: CredentialBundle, rpid: int
    ) -> int:
        response = await self._protocol.reply_detail(
            bundle,
            {
                'type': 1,
                'oid': int(publication['aid']),
                'root': rpid,
                'pn': 1,
                'ps': 20,
            },
        )
        root = _reply_root(response)
        if _positive_int(root.get('rpid')) != rpid:
            raise ProtocolContractError('comment detail root is inconsistent')
        return _reply_picture_count(root)

    async def _remove_incomplete_comment(
        self,
        publication: Mapping[str, Any],
        comment: Mapping[str, Any],
        bundle: CredentialBundle,
        rpid: int,
    ) -> None:
        publication_id = int(publication['id'])
        if int(comment['attempt_count']) >= 3:
            await self._fail(publication_id, 'B 站连续未附加结算图，已停止重复发布')
            return
        try:
            await self._protocol.delete_reply(
                bundle, {'type': 1, 'oid': int(publication['aid']), 'rpid': rpid}
            )
        except (BiliApiError, DefinitelyNotSent, RemoteOutcomeUnknown):
            await self._database.execute(
                "UPDATE vainglory_publication_comments "
                "SET state='unknown_outcome',rpid=?,"
                "error='结算图缺失，等待删除文字评论',updated_at=? WHERE id=?",
                (rpid, self._now(), int(comment['id'])),
            )
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                '结算图缺失，等待删除文字评论后重发',
            )
            return
        now = self._now()

        def reset(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE vainglory_publication_comments SET rpid=NULL,"
                "uploaded_pictures_json='[]',updated_at=? WHERE id=?",
                (now, int(comment['id'])),
            )
            if int(comment['ordinal']) == 0:
                connection.execute(
                    'UPDATE vainglory_publications SET root_rpid=NULL,'
                    "pin_state='prepared',updated_at=? WHERE id=?",
                    (now, publication_id),
                )

        await self._database.write(reset)
        await self._retry_comment(
            comment,
            'B 站未附加结算图，已删除文字评论并重新上传图片后重发',
            minimum_delay=60,
        )

    async def _publish_pin(
        self, publication: Mapping[str, Any], bundle: CredentialBundle
    ) -> None:
        publication_id = int(publication['id'])
        root_rpid = await self._root_rpid(publication_id)
        if root_rpid is None:
            await self._retry_publication(
                publication_id, int(publication['attempt_count']), '等待根评论发布完成'
            )
            return
        await self._database.execute(
            "UPDATE vainglory_publications SET pin_state='in_flight',"
            'updated_at=? WHERE id=?',
            (self._now(), publication_id),
        )
        try:
            await self._protocol.top_reply(
                bundle,
                {
                    'type': 1,
                    'oid': int(publication['aid']),
                    'rpid': root_rpid,
                    'action': 1,
                },
            )
        except (DefinitelyNotSent, RemoteOutcomeUnknown):
            await self._database.execute(
                "UPDATE vainglory_publications SET pin_state='prepared' WHERE id=?",
                (publication_id,),
            )
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                '置顶结果未确认，将对同一条评论重试',
            )
        except BiliApiError as error:
            await self._handle_api_error(publication_id, publication, error)
        except ProtocolContractError:
            await self._fail(publication_id, '置顶响应不符合预期')
        else:
            await self._database.execute(
                "UPDATE vainglory_publications SET pin_state='confirmed',"
                'error=NULL,updated_at=? WHERE id=?',
                (self._now(), publication_id),
            )

    async def _confirm_comment(
        self, publication_id: int, comment_id: int, ordinal: int, rpid: int
    ) -> None:
        now = self._now()

        def confirm(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE vainglory_publication_comments SET state='confirmed',"
                'rpid=?,error=NULL,updated_at=? WHERE id=?',
                (rpid, now, comment_id),
            )
            if ordinal == 0:
                connection.execute(
                    'UPDATE vainglory_publications SET root_rpid=?,updated_at=? '
                    'WHERE id=?',
                    (rpid, now, publication_id),
                )

        await self._database.write(confirm)

    async def _root_rpid(self, publication_id: int) -> Optional[int]:
        value = await self._database.scalar(
            'SELECT root_rpid FROM vainglory_publications WHERE id=?', (publication_id,)
        )
        return _positive_int(value)

    async def _retry_comment(
        self, comment: Mapping[str, Any], message: str, *, minimum_delay: int = 5
    ) -> None:
        attempt = int(comment['attempt_count'])
        delay = min(3600, max(minimum_delay, 2 ** min(attempt + 1, 11)))
        now = self._now()
        next_attempt_at = now + delay

        def retry(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE vainglory_publication_comments SET state='prepared',"
                'next_attempt_at=?,error=?,updated_at=? WHERE id=?',
                (next_attempt_at, message[:500], now, int(comment['id'])),
            )
            connection.execute(
                "UPDATE vainglory_publications SET state='paused',"
                'next_attempt_at=?,error=?,updated_at=? WHERE id=?',
                (next_attempt_at, message[:500], now, int(comment['publication_id'])),
            )

        await self._database.write(retry)

    async def _retry_publication(
        self, publication_id: int, attempt: int, message: str, *, minimum_delay: int = 5
    ) -> None:
        delay = max(minimum_delay, min(6 * 3600, 2 ** min(attempt + 1, 14)))
        await self._database.execute(
            "UPDATE vainglory_publications SET state='paused',"
            'next_attempt_at=?,error=?,updated_at=? WHERE id=?',
            (self._now() + delay, message[:500], self._now(), publication_id),
        )

    async def _handle_api_error(
        self, publication_id: int, publication: Mapping[str, Any], error: BiliApiError
    ) -> None:
        if error.code in self._PERMANENT_CODES:
            await self._fail(
                publication_id, 'B 站拒绝稿件更新或评论（{}）'.format(error.code)
            )
            return
        delay = 6 * 3600 if error.code in self._RETRYABLE_CODES else 3600
        await self._retry_publication(
            publication_id,
            int(publication['attempt_count']),
            'B 站接口返回错误（{}），将自动重试'.format(error.code),
            minimum_delay=delay,
        )

    async def _fail(self, publication_id: int, message: str) -> None:
        await self._database.execute(
            "UPDATE vainglory_publications SET state='failed',error=?,updated_at=? "
            'WHERE id=?',
            (message[:500], self._now(), publication_id),
        )

    async def _set_description_state(self, publication_id: int, state: str) -> None:
        await self._database.execute(
            'UPDATE vainglory_publications SET description_state=?,updated_at=? '
            'WHERE id=?',
            (state, self._now(), publication_id),
        )

    async def _set_chapter_state(
        self, publication_id: int, state: str, *, error: Optional[str] = None
    ) -> None:
        await self._database.execute(
            'UPDATE vainglory_publications SET chapter_state=?,error=?,updated_at=? '
            'WHERE id=?',
            (
                state,
                None if error is None else error[:500],
                self._now(),
                publication_id,
            ),
        )

    @staticmethod
    def _edit_payload(
        response: Mapping[str, Any], *, aid: int, bvid: str
    ) -> Tuple[Dict[str, Any], str]:
        data = response.get('data')
        if not isinstance(data, Mapping):
            raise ProtocolContractError('archive detail response is incomplete')
        archive = data.get('archive')
        videos = data.get('videos')
        if not isinstance(videos, list):
            videos = data.get('Videos')
        if not isinstance(archive, Mapping) or not isinstance(videos, list):
            raise ProtocolContractError('archive detail response is incomplete')
        remote_aid = _positive_int(archive.get('aid'))
        remote_bvid = archive.get('bvid')
        if remote_aid != aid or remote_bvid != bvid:
            raise ProtocolContractError('archive identity does not match publication')
        current = archive.get('desc')
        title = archive.get('title')
        tag = archive.get('tag')
        tid = _positive_int(archive.get('tid'))
        copyright_value = _positive_int(archive.get('copyright'))
        if (
            not isinstance(current, str)
            or not isinstance(title, str)
            or not title
            or not isinstance(tag, str)
            or tid is None
            or copyright_value not in (1, 2, 3)
        ):
            raise ProtocolContractError('archive metadata is incomplete')
        normalized_videos = []
        for raw in videos:
            if not isinstance(raw, Mapping):
                raise ProtocolContractError('archive videos are incomplete')
            filename = raw.get('filename')
            part_title = raw.get('title')
            if not isinstance(filename, str) or not filename:
                raise ProtocolContractError('archive videos are incomplete')
            video: Dict[str, Any] = {
                'filename': filename,
                'title': part_title if isinstance(part_title, str) else '',
                'desc': raw.get('desc') if isinstance(raw.get('desc'), str) else '',
            }
            cid = _positive_int(raw.get('cid'))
            if cid is not None:
                video['cid'] = cid
            normalized_videos.append(video)
        if not normalized_videos:
            raise ProtocolContractError('archive has no videos')
        creation_statement = archive.get('creation_statement')
        if not isinstance(creation_statement, Mapping):
            creation_statement = {'id': -2 if copyright_value == 2 else -1}
        reply = data.get('reply')
        if not isinstance(reply, Mapping):
            reply = {}
        subtitle = data.get('subtitle')
        if not isinstance(subtitle, Mapping):
            subtitle = archive.get('subtitle')
        if not isinstance(subtitle, Mapping):
            subtitle = {'open': 0, 'lan': ''}
        payload: Dict[str, Any] = {
            'aid': aid,
            'cover': archive.get('cover') or archive.get('pic') or '',
            'title': title,
            'copyright': copyright_value,
            'tid': tid,
            'tag': tag,
            'desc_format_id': int(archive.get('desc_format_id') or 0),
            'desc': current,
            'recreate': -1,
            'source': archive.get('source') or '',
            'dynamic': archive.get('dynamic') or '',
            'interactive': _flag(archive.get('interactive')),
            'videos': normalized_videos,
            'act_reserve_create': _flag(data.get('act_reserve_create')),
            'no_disturbance': _flag(data.get('no_disturbance')),
            'no_reprint': _flag(archive.get('no_reprint')),
            'is_only_self': _flag(archive.get('is_only_self')),
            'open_elec': _flag(archive.get('open_elec')),
            'subtitle': dict(subtitle),
            'dolby': _flag(archive.get('is_dolby')),
            'lossless_music': _flag(archive.get('lossless_music')),
            'up_selection_reply': bool(_flag(reply.get('up_selection'))),
            'up_close_reply': _flag(reply.get('state')) != 0,
            'up_close_danmu': bool(_flag(archive.get('up_close_danmu'))),
            'creation_statement': dict(creation_statement),
            'topic_grey': 1,
            'web_os': 1,
        }
        for name in ('mission_id', 'order_id'):
            value = _positive_int(archive.get(name))
            if value is not None:
                payload[name] = value
        return payload, current

    def _now(self) -> int:
        return max(1, int(self._clock()))


def _chapter_pages(response: Mapping[str, Any]) -> Tuple[_ChapterPage, ...]:
    data = response.get('data')
    if not isinstance(data, Mapping):
        return ()
    raw_pages = data.get('videos')
    if not isinstance(raw_pages, list):
        raw_pages = data.get('Videos')
    if not isinstance(raw_pages, list):
        raw_pages = data.get('pages')
    if not isinstance(raw_pages, list):
        return ()
    pages = []
    for index, raw in enumerate(raw_pages, 1):
        if not isinstance(raw, Mapping):
            continue
        cid = _positive_int(raw.get('cid'))
        if cid is None:
            continue
        page = _positive_int(raw.get('page')) or index
        pages.append(
            _ChapterPage(
                page=page, cid=cid, duration_seconds=_positive_int(raw.get('duration'))
            )
        )
    return tuple(pages)


def _merge_chapter_pages(
    primary: Sequence[_ChapterPage], fallback: Sequence[_ChapterPage]
) -> Tuple[_ChapterPage, ...]:
    fallback_by_page = {page.page: page for page in fallback}
    merged = []
    seen = set()
    for page in primary:
        other = fallback_by_page.get(page.page)
        merged.append(
            _ChapterPage(
                page=page.page,
                cid=page.cid,
                duration_seconds=(
                    page.duration_seconds
                    if page.duration_seconds is not None
                    else None if other is None else other.duration_seconds
                ),
            )
        )
        seen.add(page.page)
    merged.extend(page for page in fallback if page.page not in seen)
    return tuple(sorted(merged, key=lambda page: page.page))


def _chapter_targets(
    matches: Sequence[MatchRecord], pages: Sequence[_ChapterPage]
) -> Tuple[Tuple[_ChapterPage, Tuple[Mapping[str, Any], ...]], ...]:
    ordered = tuple(
        sorted(
            matches,
            key=lambda match: (
                match.archive_page or match.part_index,
                match.started_at_ms,
                match.result_at_ms,
                match.id,
            ),
        )
    )
    anchors: Dict[int, List[Tuple[int, int, MatchRecord]]] = {}
    for index, match in enumerate(ordered, 1):
        anchor = _match_anchor(match)
        if anchor is None:
            continue
        page, seconds = anchor
        anchors.setdefault(page, []).append((seconds, index, match))
    targets = []
    for page in sorted(pages, key=lambda item: item.page):
        if page.duration_seconds is None:
            continue
        cards = _build_chapter_cards(page.duration_seconds, anchors.get(page.page, ()))
        if cards:
            targets.append((page, cards))
    return tuple(targets)


def _build_chapter_cards(
    duration_seconds: int, anchors: Sequence[Tuple[int, int, MatchRecord]]
) -> Tuple[Mapping[str, Any], ...]:
    duration = max(0, int(duration_seconds))
    unique = []
    seen_seconds = set()
    for seconds, index, match in sorted(anchors, key=lambda item: (item[0], item[1])):
        start = max(0, int(seconds))
        if start >= duration or start in seen_seconds:
            continue
        seen_seconds.add(start)
        unique.append((start, _chapter_content(index, match)))
    if not unique:
        return ()
    starts: List[Tuple[int, str]] = []
    if unique[0][0] > 0:
        starts.append((0, '直播开始'))
    for start, content in unique:
        if starts:
            previous_start = starts[-1][0]
            minimum = 1 if len(starts) == 1 else 5
            if start - previous_start < minimum:
                continue
        starts.append((start, content))
    while starts:
        minimum = 1 if len(starts) == 1 else 5
        if duration - starts[-1][0] >= minimum:
            break
        starts.pop()
    if len(starts) < 2:
        return ()
    return tuple(
        {
            'from': start,
            'to': starts[index + 1][0] if index + 1 < len(starts) else duration,
            'content': content,
        }
        for index, (start, content) in enumerate(starts)
    )


def _chapter_content(index: int, match: MatchRecord) -> str:
    result = {'teal': '胜', 'orange': '负'}.get(match.winner_color, '未定')
    parts = ['第{}局'.format(_chinese_number(index)), result]
    recorded = next(
        (
            player
            for player in match.players
            if player.is_recorded_player
            and (
                (player.side == 'left' and match.left_color == 'teal')
                or (player.side == 'right' and match.right_color == 'teal')
            )
        ),
        None,
    )
    if recorded is not None and recorded.hero_label:
        parts.append(hero_chinese_name(recorded.hero_label))
    mode = {'3v3': '3V3', '5v5': '5V5', 'aram': '大乱斗'}.get(match.game_mode)
    if mode is not None:
        parts.append(mode)
    while parts:
        content = '｜'.join(parts)
        if len(content) <= _CHAPTER_CONTENT_LIMIT:
            return content
        if len(parts) > 3:
            parts.pop(2)
        else:
            parts.pop()
    return result


def _chinese_number(value: int) -> str:
    if value <= 0 or value > 99:
        return str(value)
    digits = '零一二三四五六七八九'
    if value < 10:
        return digits[value]
    tens, ones = divmod(value, 10)
    prefix = '十' if tens == 1 else digits[tens] + '十'
    return prefix if ones == 0 else prefix + digits[ones]


def _existing_chapter_cards(
    response: Mapping[str, Any]
) -> Tuple[Mapping[str, Any], ...]:
    data = response.get('data')
    if not isinstance(data, Mapping):
        return ()
    catalog = data.get('catalog')
    if not isinstance(catalog, list):
        return ()
    for entry in catalog:
        if not isinstance(entry, Mapping):
            continue
        card_type = entry.get('type')
        if card_type not in (2, '2', 'chapter', 'chapters'):
            continue
        cards = entry.get('cards')
        if not isinstance(cards, list):
            return ()
        return tuple(card for card in cards if isinstance(card, Mapping))
    return ()


def _automatic_chapter_cards(cards: Sequence[Mapping[str, Any]]) -> bool:
    return all(
        str(card.get('content') or '') == '直播开始'
        or re.fullmatch(
            r'(?:第(?:\d+|[一二三四五六七八九十]{1,3})局'
            r'(?:[|｜](?:胜|负|未定)'
            r'(?:[|｜][^|｜\r\n]{1,8})?(?:[|｜](?:3V3|5V5|大乱斗))?)?'
            r'|\d+(?:胜|负|未定)'
            r'(?:(?:\[[^\[\]\r\n]{1,8}\])|(?:\|[^|\r\n]{1,8}))?)',
            str(card.get('content') or ''),
        )
        is not None
        for card in cards
    )


def _same_chapter_cards(
    left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]
) -> bool:
    def normalized(
        cards: Sequence[Mapping[str, Any]]
    ) -> Tuple[Tuple[int, int, str], ...]:
        values = []
        for card in cards:
            try:
                start = int(float(card.get('from', 0)))
                end = int(float(card.get('to', 0)))
            except (TypeError, ValueError):
                return ()
            values.append((start, end, str(card.get('content') or '')))
        return tuple(values)

    return normalized(left) == normalized(right)


def _match_line(index: int, match: MatchRecord, *, include_timestamp: bool) -> str:
    result = {'teal': '胜　', 'orange': '负　'}.get(match.winner_color, '待定')
    recorded = '、'.join(_heroes_for_color(match, 'teal')) or '未识别'
    opponent = '、'.join(_heroes_for_color(match, 'orange')) or '未识别'
    label = _circled_match_number(index)
    line = '{}｜{}｜{} vs {}'.format(label, result, recorded, opponent)
    timestamp = _native_timestamp(match) if include_timestamp else None
    return line if timestamp is None else '{}｜{}'.format(line, timestamp)


def _circled_match_number(index: int) -> str:
    if 1 <= index <= 20:
        return chr(0x2460 + index - 1)
    if 21 <= index <= 35:
        return chr(0x3251 + index - 21)
    if 36 <= index <= 50:
        return chr(0x32B1 + index - 36)
    digits = str(index).translate(str.maketrans('0123456789', '０１２３４５６７８９'))
    return '〔{}〕'.format(digits)


def _match_link_line(index: int, match: MatchRecord) -> Optional[str]:
    link = _match_link(match)
    return None if link is None else '第{}局：{}'.format(index, link)


def _heroes_for_color(match: MatchRecord, color: str) -> Tuple[str, ...]:
    side = (
        'left'
        if match.left_color == color
        else 'right' if match.right_color == color else ''
    )
    players = sorted(
        (player for player in match.players if player.side == side),
        key=lambda player: player.slot,
    )
    values = []
    for player in players:
        name = (
            hero_chinese_name(player.hero_label) if player.hero_label else '英雄未识别'
        )
        values.append('［{}］'.format(name) if player.is_recorded_player else name)
    return tuple(values)


def _native_timestamp(match: MatchRecord) -> Optional[str]:
    anchor = _match_anchor(match)
    if anchor is None:
        return None
    page, seconds = anchor
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    timestamp = (
        '{:02d}:{:02d}:{:02d}'.format(hours, minutes, seconds)
        if hours
        else '{:02d}:{:02d}'.format(minutes, seconds)
    )
    return '{}#{}'.format(page, timestamp)


def _match_link(match: MatchRecord) -> Optional[str]:
    anchor = _match_anchor(match)
    if anchor is None or not match.bvid:
        return None
    page, seconds = anchor
    return 'https://www.bilibili.com/video/{}?p={}&t={}'.format(
        match.bvid, page, seconds
    )


def _match_anchor(match: MatchRecord) -> Optional[Tuple[int, int]]:
    if (
        not match.bvid
        or match.archive_page is None
        or match.archive_page <= 0
        or match.duration_seconds is None
        or match.duration_seconds <= 0
        or match.result_at_ms <= 0
    ):
        return None
    if match.started_at_ms > 0:
        measured = (match.result_at_ms - match.started_at_ms) / 1000
        if abs(measured - match.duration_seconds) <= 30:
            return match.archive_page, match.started_at_ms // 1000
        return None
    segments = match.previous_archive_segments
    if not segments and (
        match.previous_archive_page is not None
        and match.previous_archive_page > 0
        and match.previous_archive_duration_seconds is not None
        and match.previous_archive_duration_seconds > 0
    ):
        segments = (
            (match.previous_archive_page, match.previous_archive_duration_seconds),
        )
    remaining_ms = match.duration_seconds * 1_000 - match.result_at_ms
    if remaining_ms <= 0:
        return None
    for page, duration_seconds in segments:
        duration_ms = duration_seconds * 1_000
        if remaining_ms <= duration_ms + 30_000:
            return page, max(0, duration_ms - remaining_ms) // 1_000
        remaining_ms -= duration_ms
    return None


def _comment_plans(
    summary: str, matches: Sequence[MatchRecord], lines: Sequence[str]
) -> Tuple[PublicationCommentPlan, ...]:
    content = '\n'.join((summary, *lines))
    if len(content) > _COMMENT_LIMIT:
        compact_lines = tuple(
            _compact_result_line(index, match) for index, match in enumerate(matches, 1)
        )
        content = '\n'.join((summary, *compact_lines))
    if len(content) > _COMMENT_LIMIT:
        outcomes = ''.join(
            {'teal': '胜', 'orange': '负'}.get(match.winner_color, '未')
            for match in matches
        )
        content = '\n'.join((summary, '逐局（按顺序）：' + outcomes))
    if len(content) > _COMMENT_LIMIT:
        content = content[: _COMMENT_LIMIT - 1] + '…'
    picture_ids = tuple(match.id for match in matches if match.has_result_frame)
    plans = [PublicationCommentPlan(content, picture_ids[:_PICTURES_PER_COMMENT])]
    for continuation, offset in enumerate(
        range(_PICTURES_PER_COMMENT, len(picture_ids), _PICTURES_PER_COMMENT), 1
    ):
        plans.append(
            PublicationCommentPlan(
                '结算截图（续 {}）'.format(continuation),
                picture_ids[offset : offset + _PICTURES_PER_COMMENT],
            )
        )
    return tuple(plans)


def _compact_result_line(index: int, match: MatchRecord) -> str:
    result = {'teal': '胜', 'orange': '负'}.get(match.winner_color, '未确认')
    timestamp = _native_timestamp(match)
    return '第{}局{}｜{}'.format(
        index, '' if timestamp is None else ' ' + timestamp, result
    )


def _fit_description_block(block: str, capacity: int) -> Optional[str]:
    if len(block) <= capacity:
        return block
    lines = block.splitlines()
    if not lines:
        return None
    suffix = _GENERATED_TRUNCATION
    kept: List[str] = []
    for line in lines:
        candidate = '\n'.join((*kept, line, suffix))
        if len(candidate) > capacity:
            break
        kept.append(line)
    fitted = '\n'.join((*kept, suffix))
    return fitted if len(fitted) <= capacity else None


def _visible_description_block(block: str) -> str:
    lines = block.splitlines()
    if lines and lines[0] == DESCRIPTION_BEGIN and lines[-1] == DESCRIPTION_END:
        lines = lines[1:-1]
    return '\n'.join(_DESCRIPTION_TIMESTAMP.sub(r'\1\2', line) for line in lines)


def _description_block_span(current: str, block: str) -> Optional[Tuple[int, int]]:
    if not block:
        return None
    start = current.find(block)
    while start >= 0:
        end = start + len(block)
        before_is_boundary = start == 0 or current[start - 1] == '\n'
        after_is_boundary = end == len(current) or current[end] == '\n'
        if before_is_boundary and after_is_boundary:
            return start, end
        start = current.find(block, start + 1)
    return None


def _generated_description_block_span(current: str) -> Optional[Tuple[int, int]]:
    lines = current.splitlines(keepends=True)
    offset = 0
    for index, line in enumerate(lines):
        content = line.rstrip('\r\n')
        if not _GENERATED_SUMMARY.fullmatch(content):
            offset += len(line)
            continue
        position = index + 1
        cursor = offset + len(line)
        end = offset + len(content)
        item_count = 0
        headings = set()
        while position < len(lines):
            generated = lines[position].rstrip('\r\n')
            if (
                generated in ('逐局战绩', '逐局对阵', '对局跳转')
                and generated not in headings
            ):
                headings.add(generated)
            elif _GENERATED_MATCH_LINE.match(
                generated
            ) or _GENERATED_LINK_LINE.fullmatch(generated):
                item_count += 1
            elif generated != _GENERATED_TRUNCATION:
                break
            end = cursor + len(generated)
            cursor += len(lines[position])
            position += 1
        if item_count == 0:
            offset += len(line)
            continue
        return offset, end
    return None


def _json_positive_ints(value: Any) -> List[int]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        raise ProtocolContractError('stored match IDs are invalid') from None
    if not isinstance(parsed, list):
        raise ProtocolContractError('stored match IDs are invalid')
    values = [_positive_int(item) for item in parsed]
    if any(item is None for item in values):
        raise ProtocolContractError('stored match IDs are invalid')
    return [int(item) for item in values if item is not None]


def _json_pictures(value: Any) -> List[Optional[Mapping[str, Any]]]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        raise ProtocolContractError('stored comment pictures are invalid') from None
    if not isinstance(parsed, list):
        raise ProtocolContractError('stored comment pictures are invalid')
    pictures: List[Optional[Mapping[str, Any]]] = []
    for picture in parsed:
        if picture is None:
            pictures.append(None)
        elif isinstance(picture, Mapping):
            pictures.append(dict(picture))
        else:
            raise ProtocolContractError('stored comment pictures are invalid')
    return pictures


def _reply_entries(response: Mapping[str, Any]) -> Tuple[Mapping[str, Any], ...]:
    data = response.get('data')
    if not isinstance(data, Mapping):
        raise ProtocolContractError('comment list response is incomplete')
    entries: List[Mapping[str, Any]] = []
    for name in ('replies', 'top_replies'):
        value = data.get(name)
        if isinstance(value, list):
            entries.extend(entry for entry in value if isinstance(entry, Mapping))
    upper = data.get('upper')
    if isinstance(upper, Mapping):
        entries.extend(entry for entry in upper.values() if isinstance(entry, Mapping))
    return tuple(entries)


def _reply_root(response: Mapping[str, Any]) -> Mapping[str, Any]:
    data = response.get('data')
    root = data.get('root') if isinstance(data, Mapping) else None
    if not isinstance(root, Mapping):
        raise ProtocolContractError('comment detail response is incomplete')
    return root


def _reply_picture_count(reply: Mapping[str, Any]) -> int:
    content = reply.get('content')
    pictures = content.get('pictures') if isinstance(content, Mapping) else None
    return len(pictures) if isinstance(pictures, list) else 0


def _reply_matches(
    reply: Mapping[str, Any],
    *,
    content: str,
    account_uid: int,
    aid: int,
    root_rpid: Optional[int],
    is_root: bool,
) -> bool:
    body = reply.get('content')
    if not isinstance(body, Mapping) or body.get('message') != content:
        return False
    owner_uid = _positive_int(reply.get('mid'))
    if owner_uid is None:
        member = reply.get('member')
        owner_uid = (
            _positive_int(member.get('mid')) if isinstance(member, Mapping) else None
        )
    if owner_uid != account_uid:
        return False
    remote_aid = _positive_int(reply.get('oid'))
    if remote_aid is not None and remote_aid != aid:
        return False
    remote_root = _positive_int(reply.get('root'))
    return remote_root is None if is_root else remote_root == root_rpid


def _positive_int(value: Any) -> Optional[int]:
    if type(value) is int:
        result = value
    elif isinstance(value, str) and value.isdigit():
        result = int(value)
    else:
        return None
    return result if result > 0 else None


def _flag(value: Any) -> int:
    if value in (1, True, '1'):
        return 1
    return 0
