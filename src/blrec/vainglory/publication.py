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
_WAITING_DESCRIPTION = '等待对局分析完成后生成发布内容'
_LEGACY_CHAPTER_TIMING_ERROR = '部分对局缺少有效时间点，视频分段不会跳过并将自动重试'
_WAITING_PUBLICATION_ERROR = '稿件尚未公开，公开可访问后自动处理简介、评论和视频分段'

_PUBLICATION_ANALYSIS_READY_PREDICATE = (
    'EXISTS(SELECT 1 FROM vainglory_scan_jobs source_scan '
    'WHERE source_scan.session_id=session.id '
    "AND source_scan.state='ready')"
)
_PUBLICATION_SKIPPED_ARCHIVE_PREDICATE = (
    'EXISTS(SELECT 1 FROM vainglory_archive_imports empty_import '
    'WHERE empty_import.session_id=session.id '
    'AND empty_import.account_id=publication.account_id '
    'AND empty_import.bvid=publication.bvid '
    "AND empty_import.state='skipped')"
)
_PUBLICATION_ARCHIVE_READY_PREDICATE = (
    'EXISTS(SELECT 1 FROM vainglory_archive_imports ready_import '
    'WHERE ready_import.session_id=session.id '
    'AND ready_import.account_id=publication.account_id '
    'AND ready_import.bvid=publication.bvid '
    "AND ready_import.state='ready' AND ready_import.page_count>0 "
    'AND ready_import.completed_page_count=ready_import.page_count '
    'AND (SELECT COUNT(*) FROM vainglory_archive_parts ready_part '
    'WHERE ready_part.import_id=ready_import.id '
    "AND ready_part.state='ready')=ready_import.page_count)"
)
_PUBLICATION_TERMINAL_EMPTY_PREDICATE = (
    '(' + _PUBLICATION_SKIPPED_ARCHIVE_PREDICATE + ') AND NOT EXISTS('
    'SELECT 1 FROM recording_parts local_part '
    'WHERE local_part.session_id=session.id '
    "AND local_part.artifact_state='ready' "
    'AND local_part.video_deleted_at IS NULL)'
)
_PUBLICATION_READY_PREDICATE = (
    "((publication.source_kind='upload' AND "
    + _PUBLICATION_ANALYSIS_READY_PREDICATE
    + ") OR (publication.source_kind='archive' AND "
    + _PUBLICATION_ANALYSIS_READY_PREDICATE
    + ' AND '
    + _PUBLICATION_ARCHIVE_READY_PREDICATE
    + ') OR ('
    + _PUBLICATION_TERMINAL_EMPTY_PREDICATE
    + '))'
)
_PUBLICATION_UPLOAD_APPROVED_PREDICATE = (
    "(publication.source_kind!='upload' OR EXISTS("
    'SELECT 1 FROM upload_jobs approved_upload '
    'WHERE approved_upload.id=publication.upload_job_id '
    "AND approved_upload.state IN ('approved','completed')))"
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
    match_count: int
    analysis_snapshot_json: str
    comments_json: str


@dataclass(frozen=True)
class PublicationTaskStatus:
    session_id: int
    code: str
    label: str
    detail: Optional[str]
    recommended_action: str
    next_attempt_at: Optional[int]
    plan_state: str
    upload_state: Optional[str]
    scan_state: Optional[str]
    operator_paused: bool


@dataclass(frozen=True)
class PublicationAuditStatus:
    total_count: int
    verified_count: int
    stale_count: int
    pending_count: int
    failed_count: int
    oldest_verified_at: Optional[int]


@dataclass(frozen=True)
class _Candidate:
    account_id: int
    session_id: int
    upload_job_id: Optional[int]
    aid: int
    bvid: str
    source_kind: str
    analysis_ready: bool


@dataclass(frozen=True)
class _ChapterPage:
    page: int
    cid: int
    duration_seconds: Optional[int]


def _publication_task_status(row: Mapping[str, Any]) -> PublicationTaskStatus:
    session_id = int(row['session_id'])
    publication_state = str(row['publication_state'])
    plan_state = str(row['plan_state'])
    source_kind = str(row['source_kind'])
    error = None if row.get('error') is None else str(row['error'])
    upload_state = None if row.get('upload_state') is None else str(row['upload_state'])
    scan_state = None if row.get('scan_state') is None else str(row['scan_state'])
    operator_paused = bool(row.get('operator_paused'))
    next_attempt = int(row.get('next_attempt_at') or 0)
    remote_verified = row.get('remote_verified_at') is not None

    code = 'queued'
    label = '发布队列中'
    detail = error
    action = 'wait'
    if operator_paused:
        code = 'operator_paused'
        label = '历史稿件迁移已人工暂停'
        detail = '恢复历史稿件迁移后，对局分析和发布会自动继续。'
        action = 'resume_migration'
    elif plan_state == 'waiting_analysis' and not (
        source_kind == 'upload' and upload_state in (None, 'rejected', 'paused')
    ):
        if scan_state == 'failed':
            code = 'analysis_failed'
            label = '对局分析失败'
            detail = (
                str(row['scan_error'])
                if row.get('scan_error') is not None
                else '请使用新算法重新分析这场直播。'
            )
            action = 'reanalyze'
        else:
            code = 'waiting_analysis'
            label = '等待对局分析'
            detail = error or '分析完成后会自动生成简介、评论和视频分段。'
            action = 'wait'
    elif source_kind == 'upload' and upload_state is None:
        code = 'upload_missing'
        label = '投稿任务记录缺失'
        detail = '无法确认稿件是否已通过审核，发布 worker 不会冒险写入。'
        action = 'check_upload'
    elif source_kind == 'upload' and upload_state == 'rejected':
        code = 'review_rejected'
        label = '稿件审核未通过'
        detail = (
            str(row['review_reason'])
            if row.get('review_reason') is not None
            else '请先处理 B 站稿件审核问题。'
        )
        action = 'check_upload'
    elif source_kind == 'upload' and upload_state == 'paused':
        code = 'upload_paused'
        label = '投稿任务已暂停'
        detail = '这是投稿任务本身的暂停，不是简介、评论或分段的状态。'
        action = 'check_upload'
    elif source_kind == 'upload' and upload_state == 'waiting_review':
        code = 'waiting_review'
        label = '等待稿件审核'
        detail = '审核通过后，发布 worker 会自动处理简介、评论和视频分段。'
        action = 'wait'
    elif source_kind == 'upload' and upload_state not in ('approved', 'completed'):
        code = 'waiting_upload'
        label = '等待稿件发布'
        detail = '稿件还在上传或提交阶段，尚不能回填发布内容。'
        action = 'wait'
    elif error == _WAITING_PUBLICATION_ERROR:
        code = 'waiting_publication'
        label = '等待稿件公开'
        detail = _WAITING_PUBLICATION_ERROR
        action = 'wait'
    elif error == _LEGACY_CHAPTER_TIMING_ERROR:
        code = 'legacy_chapter_timing'
        label = '旧版分段时间待重算'
        detail = '旧算法没有正确使用结算图 OCR 时长；新版可以直接重算。'
        action = 'retry_chapter'
    elif error is not None and '识别结果缺少' in error:
        code = 'analysis_data_invalid'
        label = '识别数据需重新分析'
        detail = error
        action = 'reanalyze'
    elif publication_state == 'confirmed' and remote_verified:
        code = 'confirmed'
        label = '发布内容已全部完成'
        detail = None
        action = 'none'
    elif publication_state == 'confirmed':
        code = 'remote_verification_pending'
        label = '等待公开内容复核'
        detail = error or '尚未确认匿名访客可以看到简介、评论和视频分段。'
        action = 'wait'
    elif publication_state == 'running':
        code = 'running'
        label = '正在发布'
        detail = error
        action = 'wait'
    elif publication_state == 'failed':
        code = 'failed'
        label = '发布任务失败'
        detail = error
        action = 'retry'
    elif publication_state == 'paused':
        code = 'retry_scheduled'
        label = '等待自动重试'
        detail = error or '发布 worker 已安排下一次重试。'
        action = 'wait'

    return PublicationTaskStatus(
        session_id=session_id,
        code=code,
        label=label,
        detail=detail,
        recommended_action=action,
        next_attempt_at=(
            next_attempt
            if code in ('retry_scheduled', 'legacy_chapter_timing') and next_attempt > 0
            else None
        ),
        plan_state=plan_state,
        upload_state=upload_state,
        scan_state=scan_state,
        operator_paused=operator_paused,
    )


def build_publication_plan(
    matches: Sequence[MatchRecord],
    *,
    bvid: Optional[str] = None,
    frame_hashes: Optional[Mapping[int, Optional[str]]] = None,
) -> PublicationPlan:
    known_frame_hashes = frame_hashes or {}
    ordered = tuple(
        sorted(
            matches,
            key=lambda match: (
                match.archive_page or match.part_index,
                match.started_at_ms,
                match.result_at_ms,
                _match_line(0, match, include_timestamp=True),
                known_frame_hashes.get(match.id) or '',
            ),
        )
    )
    if not ordered:
        summary = '共 0 局｜0 胜 0 负'
        snapshot_json = json.dumps(
            {'bvid': bvid, 'matches': [], 'version': 1},
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )
        identity = json.dumps(
            {
                'chapters': [],
                'comments': [],
                'description': '',
                'snapshot': json.loads(snapshot_json),
                'version': 12,
            },
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf8')
        return PublicationPlan(
            hashlib.sha256(identity).hexdigest(), summary, (), 0, snapshot_json, '[]'
        )
    bvids = {match.bvid for match in ordered}
    if len(bvids) != 1 or None in bvids or (bvid is not None and bvid not in bvids):
        raise ValueError('publication matches must belong to one archive')
    bvid = str(ordered[0].bvid)
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
    description_parts = [summary]
    if description_links:
        description_parts.extend(('对局跳转', *description_links))
    description_parts.extend(('逐局对阵', *description_lines))
    description_block = '\n'.join(description_parts)
    snapshot = {
        'bvid': bvid,
        'matches': [
            {
                'page': match.archive_page,
                'part_index': match.part_index,
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
                'result_frame_sha256': known_frame_hashes.get(
                    match.id, 'present' if match.has_result_frame else None
                ),
            }
            for match in ordered
        ],
        'version': 1,
    }
    snapshot_json = json.dumps(
        snapshot, ensure_ascii=False, separators=(',', ':'), sort_keys=True
    )
    comments = _comment_plans(summary, ordered, comment_lines)
    comment_snapshots = [
        {
            'content': comment.content,
            'pictures': [
                known_frame_hashes.get(match_id) for match_id in comment.match_ids
            ],
        }
        for comment in comments
    ]
    comments_json = json.dumps(
        comment_snapshots, ensure_ascii=False, separators=(',', ':'), sort_keys=True
    )
    identity = json.dumps(
        {
            'chapters': [
                {
                    'content': _chapter_content(index, match),
                    'anchor': _match_anchor(match),
                }
                for index, match in enumerate(ordered, 1)
            ],
            'comments': comment_snapshots,
            'description': description_block,
            'snapshot': snapshot,
            'version': 12,
        },
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf8')
    payload_hash = hashlib.sha256(identity).hexdigest()
    return PublicationPlan(
        payload_hash,
        description_block,
        comments,
        len(ordered),
        snapshot_json,
        comments_json,
    )


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
    _MISSING_REPLY_CODES = frozenset((-404, 404, 12002, 12006))
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
        self._worker_lock = asyncio.Lock()
        self._discovery_wake = asyncio.Event()
        self._delivery_wake = asyncio.Event()
        self._discovery_task: Optional[asyncio.Task[None]] = None
        self._delivery_task: Optional[asyncio.Task[None]] = None
        self._last_audit_publication = False

    async def start(self) -> None:
        if self._discovery_task is not None or self._delivery_task is not None:
            return
        await self.recover_interrupted()
        await self._requeue_legacy_chapter_timing()
        await self.ensure_publication_tasks()
        self._discovery_wake.set()
        self._delivery_wake.set()
        self._discovery_task = asyncio.create_task(
            self._run_discovery(), name='vainglory-publication-discovery'
        )
        self._delivery_task = asyncio.create_task(
            self._run_delivery(), name='vainglory-publication-delivery'
        )

    async def close(self) -> None:
        tasks = tuple(
            task
            for task in (self._discovery_task, self._delivery_task)
            if task is not None
        )
        self._discovery_task = None
        self._delivery_task = None
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def publication_statuses(
        self, session_ids: Sequence[int]
    ) -> Dict[int, PublicationTaskStatus]:
        selected = tuple(
            dict.fromkeys(int(value) for value in session_ids if value > 0)
        )
        if not selected:
            return {}
        placeholders = ','.join('?' for _value in selected)
        rows = await self._database.fetchall(
            'SELECT publication.session_id,publication.source_kind,'
            'publication.state AS publication_state,publication.plan_state,'
            'publication.next_attempt_at,publication.error,'
            'publication.remote_verified_at,'
            'upload.state AS upload_state,upload.review_reason,'
            'scan.state AS scan_state,scan.error AS scan_error,'
            'EXISTS(SELECT 1 FROM archive_migration_items paused_item '
            'JOIN archive_migration_jobs paused_job '
            'ON paused_job.id=paused_item.migration_id '
            'WHERE paused_item.session_id=publication.session_id '
            "AND paused_item.state!='task_created' "
            'AND paused_job.operator_paused=1) AS operator_paused '
            'FROM vainglory_publications publication '
            'LEFT JOIN upload_jobs upload ON upload.id=publication.upload_job_id '
            'LEFT JOIN vainglory_scan_jobs scan '
            'ON scan.session_id=publication.session_id '
            'WHERE publication.session_id IN ({}) '
            'ORDER BY publication.session_id,publication.id DESC'.format(placeholders),
            selected,
        )
        statuses: Dict[int, PublicationTaskStatus] = {}
        for row in rows:
            session_id = int(row['session_id'])
            if session_id not in statuses:
                statuses[session_id] = _publication_task_status(dict(row))
        return statuses

    async def publication_audit_status(
        self, *, stale_before: int
    ) -> PublicationAuditStatus:
        row = await self._database.fetchone(
            'SELECT COUNT(*) AS total_count,'
            "SUM(CASE WHEN state='confirmed' AND remote_verified_at IS NOT NULL "
            'THEN 1 ELSE 0 END) AS verified_count,'
            "SUM(CASE WHEN state='confirmed' AND (remote_verified_at IS NULL "
            'OR remote_verified_at<=?) THEN 1 ELSE 0 END) AS stale_count,'
            "SUM(CASE WHEN state IN ('prepared','running','paused') "
            'THEN 1 ELSE 0 END) AS pending_count,'
            "SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed_count,"
            'MIN(CASE WHEN state=\'confirmed\' THEN remote_verified_at END) '
            'AS oldest_verified_at FROM vainglory_publications',
            (max(0, int(stale_before)),),
        )
        assert row is not None
        oldest = row['oldest_verified_at']
        return PublicationAuditStatus(
            total_count=int(row['total_count'] or 0),
            verified_count=int(row['verified_count'] or 0),
            stale_count=int(row['stale_count'] or 0),
            pending_count=int(row['pending_count'] or 0),
            failed_count=int(row['failed_count'] or 0),
            oldest_verified_at=None if oldest is None else int(oldest),
        )

    async def queue_publication_audit(self, *, stale_before: int, limit: int) -> int:
        if not 1 <= limit <= 100:
            raise ValueError('远端复核批次必须在 1 到 100 之间')
        cutoff = max(0, int(stale_before))
        now = self._now()

        def queue(connection: sqlite3.Connection) -> int:
            rows = connection.execute(
                'SELECT id FROM vainglory_publications '
                "WHERE state='confirmed' AND (remote_verified_at IS NULL "
                'OR remote_verified_at<=?) '
                'ORDER BY remote_verified_at IS NOT NULL,remote_verified_at,id LIMIT ?',
                (cutoff, int(limit)),
            ).fetchall()
            ids = tuple(int(row['id']) for row in rows)
            if not ids:
                return 0
            placeholders = ','.join('?' for _publication_id in ids)
            return connection.execute(
                "UPDATE vainglory_publications SET state='prepared',"
                'remote_verified_at=NULL,attempt_count=0,next_attempt_at=0,'
                "error='等待远端重新复核',priority=0,updated_at=? "
                'WHERE id IN ({})'.format(placeholders),
                (now, *ids),
            ).rowcount

        changed = await self._database.write(queue)
        if changed:
            logger.info(
                'Queued confirmed Vainglory publications for remote audit: '
                'count={} stale_before={}',
                changed,
                cutoff,
            )
            self._delivery_wake.set()
        return changed

    async def _requeue_legacy_chapter_timing(self) -> int:
        changed = await self._database.execute(
            "UPDATE vainglory_publications SET state='prepared',"
            'next_attempt_at=0,error=NULL,updated_at=? '
            "WHERE state='paused' AND error=?",
            (self._now(), _LEGACY_CHAPTER_TIMING_ERROR),
        )
        if changed:
            logger.info(
                'Requeued publications affected by legacy chapter timing: count={}',
                changed,
            )
        return changed

    async def retry_failed_step(self, session_id: int, step: str) -> None:
        if session_id <= 0:
            raise ValueError('直播场次编号无效')
        columns = {
            'description': 'description_state',
            'chapter': 'chapter_state',
            'pin': 'pin_state',
        }
        column = columns.get(step)
        if column is None and step != 'comments':
            raise ValueError('不支持重试这个发布步骤')
        await self.ensure_publication_tasks()
        plan: Optional[PublicationPlan] = None
        if step == 'comments':
            current = await self._database.fetchone(
                'SELECT session_id,bvid,plan_state FROM vainglory_publications '
                'WHERE session_id=? ORDER BY id DESC LIMIT 1',
                (session_id,),
            )
            if current is None:
                raise ValueError('这场直播没有可重试的发布任务')
            if str(current['plan_state']) != 'ready':
                raise ValueError('这场直播的对局分析尚未完成')
            matches = await self._session_matches(int(current['session_id']))
            matches = tuple(
                match for match in matches if match.bvid == str(current['bvid'])
            )
            plan = await self._build_plan(str(current['bvid']), matches)
        now = self._now()

        def retry(connection: sqlite3.Connection) -> Tuple[int, str]:
            publication = connection.execute(
                'SELECT id,bvid,state,chapter_state,description_state,pin_state '
                'FROM vainglory_publications WHERE session_id=? '
                'ORDER BY id DESC LIMIT 1',
                (session_id,),
            ).fetchone()
            if publication is None:
                raise ValueError('这场直播没有可重试的发布任务')
            if str(publication['state']) == 'running':
                raise ValueError('发布任务正在执行，请稍后再试')
            publication_id = int(publication['id'])
            if step == 'comments':
                assert plan is not None
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
                connection.execute(
                    "UPDATE vainglory_publications SET state='prepared',"
                    'comment_cleanup_state=\'prepared\',pin_state=?,'
                    'root_rpid=NULL,attempt_count=0,next_attempt_at=0,error=NULL,'
                    'remote_verified_at=NULL,priority=1,updated_at=? WHERE id=?',
                    (
                        'confirmed' if not plan.comments else 'prepared',
                        now,
                        publication_id,
                    ),
                )
            else:
                assert column is not None
                connection.execute(
                    'UPDATE vainglory_publications SET state=\'prepared\','
                    '{}=\'prepared\',attempt_count=0,next_attempt_at=0,error=NULL,'
                    'remote_verified_at=NULL,priority=1,updated_at=? '
                    'WHERE id=?'.format(column),
                    (now, publication_id),
                )
            return publication_id, str(publication['bvid'])

        publication_id, bvid = await self._database.write(retry)
        logger.info(
            'Vainglory publication manually retried: publication_id={} '
            'session_id={} bvid={} step={}',
            publication_id,
            session_id,
            bvid,
            step,
        )
        self._delivery_wake.set()

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
            changed += connection.execute(
                "UPDATE vainglory_publications SET comment_cleanup_state='prepared',"
                "error='进程中断后将重新枚举并清理旧评论',updated_at=? "
                "WHERE comment_cleanup_state='in_flight'",
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
        return await self._run_delivery_once()

    async def ensure_publication_tasks(self) -> int:
        now = self._now()

        def ensure(connection: sqlite3.Connection) -> int:
            before = connection.total_changes
            connection.execute(
                'INSERT OR IGNORE INTO vainglory_publications('
                'account_id,session_id,upload_job_id,aid,bvid,source_kind,'
                'payload_hash,description_block,state,description_state,'
                'comment_cleanup_state,pin_state,needs_refresh,priority,'
                'plan_state,match_count,force_republish,created_at,updated_at) '
                "SELECT job.account_id,job.session_id,job.id,job.aid,job.bvid,'upload',"
                "? ,?,'prepared','prepared','prepared','prepared',1,1,"
                "'waiting_analysis',0,1,?,? "
                'FROM upload_jobs job '
                'JOIN recording_sessions session ON session.id=job.session_id '
                "WHERE job.submit_state='confirmed' AND job.aid>0 "
                "AND job.bvid IS NOT NULL AND job.bvid<>'' "
                "AND instr(COALESCE(session.title,''),'直播剪辑')=0 "
                "AND instr(COALESCE(session.title,''),?)=0 "
                'AND instr(COALESCE(job.policy_snapshot_json,\'\'),?)=0',
                (
                    '0' * 64,
                    _WAITING_DESCRIPTION,
                    now,
                    now,
                    EXCLUDED_TITLE_MARKER,
                    EXCLUDED_TITLE_MARKER,
                ),
            )
            connection.execute(
                'INSERT OR IGNORE INTO vainglory_publications('
                'account_id,session_id,upload_job_id,aid,bvid,source_kind,'
                'payload_hash,description_block,state,description_state,'
                'comment_cleanup_state,pin_state,needs_refresh,priority,'
                'plan_state,match_count,force_republish,created_at,updated_at) '
                'SELECT imported.account_id,imported.session_id,NULL,imported.aid,'
                "imported.bvid,'archive',? ,?,'prepared','prepared','prepared',"
                "'prepared',1,1,'waiting_analysis',0,1,?,? "
                'FROM vainglory_archive_imports imported '
                'JOIN recording_sessions session ON session.id=imported.session_id '
                'WHERE imported.session_id IS NOT NULL '
                "AND instr(COALESCE(session.title,''),'直播剪辑')=0 "
                'AND instr(COALESCE(session.title,\'\'),?)=0 '
                'AND instr(COALESCE(imported.title,\'\'),?)=0 '
                'AND NOT EXISTS(SELECT 1 FROM upload_jobs job '
                'WHERE job.session_id=imported.session_id)',
                (
                    '0' * 64,
                    _WAITING_DESCRIPTION,
                    now,
                    now,
                    EXCLUDED_TITLE_MARKER,
                    EXCLUDED_TITLE_MARKER,
                ),
            )
            return connection.total_changes - before

        created = await self._database.write(ensure)
        if created:
            logger.info(
                'Vainglory publication tasks guaranteed for published archives: '
                'created={}',
                created,
            )
        return created

    async def _run_delivery_once(self) -> bool:
        async with self._worker_lock:
            return await self._run_delivery_once_locked()

    async def _run_delivery_once_locked(self) -> bool:
        has_new_work = bool(
            await self._database.scalar(
                'SELECT EXISTS(SELECT 1 FROM vainglory_publications '
                "WHERE state IN ('prepared','running','paused') "
                'AND needs_refresh=0 AND plan_state=\'ready\' AND priority>0 '
                'AND next_attempt_at<=?)',
                (self._now(),),
            )
        )
        include_audit = not (self._last_audit_publication and has_new_work)
        work_query = (
            'SELECT publication.*,account.state AS account_state,'
            'account.credential_version,account.uid AS account_uid '
            'FROM vainglory_publications publication '
            'JOIN bili_accounts account ON account.id=publication.account_id '
            'JOIN recording_sessions session ON session.id=publication.session_id '
            "WHERE publication.state IN ('prepared','running','paused') "
            'AND publication.needs_refresh=0 '
            "AND publication.plan_state='ready' "
            'AND ' + _PUBLICATION_UPLOAD_APPROVED_PREDICATE + ' '
            'AND instr(COALESCE(session.title,\'\'),?)=0 '
            'AND NOT EXISTS(SELECT 1 FROM archive_migration_items paused_item '
            'JOIN archive_migration_jobs paused_job '
            'ON paused_job.id=paused_item.migration_id '
            'WHERE paused_item.session_id=publication.session_id '
            "AND paused_item.state!='task_created' "
            'AND paused_job.operator_paused=1) '
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
            'AND (publication.remote_verified_at IS NOT NULL '
            'OR publication.priority>0 OR ?) '
            'ORDER BY publication.priority DESC,CASE '
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
                include_audit,
            ),
        )
        if row is not None:
            publication = dict(row)
            self._last_audit_publication = (
                publication['remote_verified_at'] is None
                and int(publication['priority']) == 0
            )
            await self._process(publication)
            return True
        self._last_audit_publication = False
        return False

    async def _run_discovery(self) -> None:
        while True:
            try:
                progressed = await self._discover()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Vainglory publication discovery worker failed')
                progressed = False
            if progressed:
                continue
            try:
                await asyncio.wait_for(
                    self._discovery_wake.wait(), timeout=self._idle_poll_seconds
                )
            except asyncio.TimeoutError:
                pass
            self._discovery_wake.clear()

    async def _run_delivery(self) -> None:
        while True:
            try:
                progressed = await self._run_delivery_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Vainglory publication delivery worker failed')
                progressed = False
            if progressed:
                await self._sleeper(self._action_interval_seconds)
                continue
            try:
                await asyncio.wait_for(
                    self._delivery_wake.wait(), timeout=self._idle_poll_seconds
                )
            except asyncio.TimeoutError:
                pass
            self._delivery_wake.clear()

    async def _discover(self) -> bool:
        async with self._worker_lock:
            return await self._discover_locked()

    async def _discover_locked(self) -> bool:
        created = await self.ensure_publication_tasks()
        candidate = await self._next_candidate()
        if candidate is None:
            return created > 0
        selected = candidate
        matches: Tuple[MatchRecord, ...] = ()
        if selected.analysis_ready:
            session_matches = await self._session_matches(selected.session_id)
            matches = tuple(
                match for match in session_matches if match.bvid == selected.bvid
            )
        plan = await self._build_plan(selected.bvid, matches)
        now = self._now()

        def persist(connection: sqlite3.Connection) -> bool:
            current = connection.execute(
                'SELECT id,payload_hash,state,plan_state,force_republish,'
                'active_revision_id FROM vainglory_publications '
                'WHERE account_id=? AND bvid=?',
                (selected.account_id, selected.bvid),
            ).fetchone()
            if current is None:
                cursor = connection.execute(
                    'INSERT INTO vainglory_publications('
                    'account_id,session_id,upload_job_id,aid,bvid,source_kind,'
                    'payload_hash,description_block,state,description_state,'
                    'comment_cleanup_state,pin_state,needs_refresh,plan_state,'
                    'match_count,force_republish,created_at,updated_at) '
                    "VALUES(?,?,?,?,?,?,?,?,'prepared','prepared','prepared',?,"
                    "0,'ready',?,0,?,?)",
                    (
                        selected.account_id,
                        selected.session_id,
                        selected.upload_job_id,
                        selected.aid,
                        selected.bvid,
                        selected.source_kind,
                        plan.payload_hash,
                        plan.description_block,
                        'confirmed' if not plan.comments else 'prepared',
                        plan.match_count,
                        now,
                        now,
                    ),
                )
                publication_id = int(cursor.lastrowid)
                previous_payload_hash: Optional[str] = None
                reason = 'initial'
                must_apply = True
            else:
                publication_id = int(current['id'])
                previous_payload_hash = (
                    None
                    if str(current['plan_state']) == 'waiting_analysis'
                    else str(current['payload_hash'])
                )
                same_payload = str(current['payload_hash']) == plan.payload_hash
                must_apply = (
                    not same_payload
                    or bool(current['force_republish'])
                    or str(current['state']) != 'confirmed'
                    or str(current['plan_state']) != 'ready'
                )
                if not must_apply:
                    self._insert_publication_revision(
                        connection,
                        publication_id=publication_id,
                        plan=plan,
                        previous_payload_hash=previous_payload_hash,
                        reason='unchanged',
                        state='unchanged',
                        now=now,
                    )
                    connection.execute(
                        'UPDATE vainglory_publications SET needs_refresh=0,'
                        "plan_state='ready',match_count=?,force_republish=0,"
                        'updated_at=? WHERE id=?',
                        (plan.match_count, now, publication_id),
                    )
                    return True
                reason = (
                    'initial'
                    if str(current['plan_state']) == 'waiting_analysis'
                    else (
                        'forced'
                        if bool(current['force_republish']) or same_payload
                        else 'changed'
                    )
                )
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
            revision_id = self._insert_publication_revision(
                connection,
                publication_id=publication_id,
                plan=plan,
                previous_payload_hash=previous_payload_hash,
                reason=reason,
                state='prepared',
                now=now,
            )
            if current is not None:
                connection.execute(
                    'UPDATE vainglory_publications SET session_id=?,'
                    'upload_job_id=?,aid=?,source_kind=?,payload_hash=?,'
                    "description_block=?,state='prepared',"
                    "chapter_state='prepared',description_state='prepared',"
                    'comment_cleanup_state=\'prepared\',pin_state=?,'
                    'root_rpid=NULL,attempt_count=0,next_attempt_at=0,error=NULL,'
                    "needs_refresh=0,plan_state='ready',match_count=?,"
                    'force_republish=0,active_revision_id=?,remote_verified_at=NULL,'
                    'updated_at=? WHERE id=?',
                    (
                        selected.session_id,
                        selected.upload_job_id,
                        selected.aid,
                        selected.source_kind,
                        plan.payload_hash,
                        plan.description_block,
                        'confirmed' if not plan.comments else 'prepared',
                        plan.match_count,
                        revision_id,
                        now,
                        publication_id,
                    ),
                )
            else:
                connection.execute(
                    'UPDATE vainglory_publications SET active_revision_id=? '
                    'WHERE id=?',
                    (revision_id, publication_id),
                )
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

        persisted = await self._database.write(persist)
        if persisted:
            self._delivery_wake.set()
        return persisted

    async def _build_plan(
        self, bvid: str, matches: Sequence[MatchRecord]
    ) -> PublicationPlan:
        frame_hashes: Dict[int, Optional[str]] = {}
        for match in matches:
            if not match.has_result_frame:
                frame_hashes[match.id] = None
                continue
            path = await self._repository.result_frame_path(match.id)
            if path is None:
                frame_hashes[match.id] = None
                continue
            try:
                content = await asyncio.get_running_loop().run_in_executor(
                    None, Path(path).read_bytes
                )
            except OSError:
                logger.warning(
                    'Vainglory publication frame fingerprint unavailable: '
                    'match_id={} path={}',
                    match.id,
                    path,
                )
                frame_hashes[match.id] = None
            else:
                frame_hashes[match.id] = hashlib.sha256(content).hexdigest()
        return build_publication_plan(matches, bvid=bvid, frame_hashes=frame_hashes)

    @staticmethod
    def _insert_publication_revision(
        connection: sqlite3.Connection,
        *,
        publication_id: int,
        plan: PublicationPlan,
        previous_payload_hash: Optional[str],
        reason: str,
        state: str,
        now: int,
    ) -> int:
        revision_no = int(
            connection.execute(
                'SELECT COALESCE(MAX(revision_no),0)+1 '
                'FROM vainglory_publication_revisions WHERE publication_id=?',
                (publication_id,),
            ).fetchone()[0]
        )
        analysis_revision_ids = [
            int(row[0])
            for row in connection.execute(
                'SELECT MAX(id) FROM vainglory_analysis_revisions '
                'WHERE session_id=(SELECT session_id FROM vainglory_publications '
                'WHERE id=?) GROUP BY part_id ORDER BY part_id',
                (publication_id,),
            ).fetchall()
        ]
        cursor = connection.execute(
            'INSERT INTO vainglory_publication_revisions('
            'publication_id,revision_no,previous_payload_hash,payload_hash,'
            'match_count,analysis_revision_ids_json,analysis_snapshot_json,'
            'description_block,comments_json,reason,state,created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                publication_id,
                revision_no,
                previous_payload_hash,
                plan.payload_hash,
                plan.match_count,
                json.dumps(analysis_revision_ids, separators=(',', ':')),
                plan.analysis_snapshot_json,
                plan.description_block,
                plan.comments_json,
                reason,
                state,
                now,
            ),
        )
        return int(cursor.lastrowid)

    async def _next_candidate(self) -> Optional[_Candidate]:
        row = await self._database.fetchone(
            'SELECT publication.account_id,publication.session_id,'
            'publication.upload_job_id,publication.aid,publication.bvid,'
            'publication.source_kind,'
            + _PUBLICATION_ANALYSIS_READY_PREDICATE
            + ' AS analysis_ready FROM vainglory_publications publication '
            'JOIN recording_sessions session ON session.id=publication.session_id '
            'JOIN bili_accounts account ON account.id=publication.account_id '
            "WHERE account.state='active' AND (publication.needs_refresh=1 "
            "OR publication.plan_state='waiting_analysis') AND "
            + _PUBLICATION_READY_PREDICATE
            + " AND instr(COALESCE(session.title,''),'直播剪辑')=0 "
            'AND NOT EXISTS(SELECT 1 FROM archive_migration_items paused_item '
            'JOIN archive_migration_jobs paused_job '
            'ON paused_job.id=paused_item.migration_id '
            'WHERE paused_item.session_id=session.id '
            "AND paused_item.state!='task_created' "
            'AND paused_job.operator_paused=1) '
            'ORDER BY publication.priority DESC,CASE '
            "WHEN publication.source_kind='upload' THEN 0 ELSE 1 END,"
            'session.started_at,publication.id LIMIT 1'
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
            analysis_ready=bool(row['analysis_ready']),
        )

    async def _session_matches(self, session_id: int) -> Tuple[MatchRecord, ...]:
        offset = 0
        matches: List[MatchRecord] = []
        while True:
            page = await self._repository.list_matches(
                session_id=session_id, limit=100, offset=offset
            )
            matches.extend(
                match for match in page.items if match.analysis_state == 'final'
            )
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
                if not await self._confirm_public_visibility(publication, bundle):
                    return
                await self._database.execute(
                    "UPDATE vainglory_publications SET state='running',"
                    'attempt_count=attempt_count+1,error=NULL,updated_at=? WHERE id=?',
                    (self._now(), publication_id),
                )
                if str(publication['comment_cleanup_state']) != 'confirmed':
                    if not await self._prepare_comment_cleanup(publication, bundle):
                        return
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
                if str(publication['chapter_state']) != 'confirmed':
                    await self._publish_chapters(
                        publication,
                        bundle,
                        force=publication['remote_verified_at'] is None,
                    )
                    return
                if str(publication['description_state']) != 'confirmed':
                    await self._publish_description(
                        publication,
                        bundle,
                        force=publication['remote_verified_at'] is None,
                    )
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
                if not await self._verify_remote_publication(publication, bundle):
                    return
                now = self._now()

                def confirm(connection: sqlite3.Connection) -> None:
                    current = connection.execute(
                        'SELECT active_revision_id FROM vainglory_publications '
                        'WHERE id=?',
                        (publication_id,),
                    ).fetchone()
                    revision_id = (
                        None
                        if current is None or current['active_revision_id'] is None
                        else int(current['active_revision_id'])
                    )
                    if revision_id is not None:
                        connection.execute(
                            "UPDATE vainglory_publication_revisions SET "
                            "state='confirmed',confirmed_at=? WHERE id=?",
                            (now, revision_id),
                        )
                    connection.execute(
                        "UPDATE vainglory_publications SET state='confirmed',"
                        'needs_refresh=0,error=NULL,priority=0,'
                        'published_revision_id=active_revision_id,'
                        'remote_verified_at=?,updated_at=? '
                        'WHERE id=?',
                        (now, now, publication_id),
                    )

                await self._database.write(confirm)
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

    async def _verify_remote_publication(
        self, publication: Mapping[str, Any], bundle: CredentialBundle
    ) -> bool:
        publication_id = int(publication['id'])
        try:
            public = await self._protocol.public_archive_view(
                bundle, bvid=str(publication['bvid'])
            )
            data = public.get('data')
            if not isinstance(data, Mapping):
                raise ProtocolContractError('public archive response is incomplete')
            remote_description = str(data.get('desc') or '')
            expected_description = str(publication['description_block'])
            description_ok = (
                remove_generated_description(remote_description, expected_description)
                == remote_description
                if int(publication['match_count']) == 0
                else merge_archive_description(remote_description, expected_description)
                == remote_description
            )
            chapter_ok = await self._remote_chapters_match(
                publication, bundle, _chapter_pages(public)
            )
            comments_ok = await self._remote_comments_match(publication, bundle)
            pin_ok = comments_ok and await self._remote_pin_matches(publication, bundle)
        except (BiliApiError, DefinitelyNotSent, RemoteOutcomeUnknown):
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                '远端复核失败，将自动重试',
                minimum_delay=60,
            )
            return False
        except ProtocolContractError:
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                '远端复核数据异常，将自动重试',
                minimum_delay=3600,
            )
            return False
        if description_ok and chapter_ok and comments_ok and pin_ok:
            return True
        await self._reset_unverified_steps(
            publication_id,
            attempt=int(publication['attempt_count']),
            description_ok=description_ok,
            chapter_ok=chapter_ok,
            comments_ok=comments_ok,
            pin_ok=pin_ok,
        )
        return False

    async def _remote_chapters_match(
        self,
        publication: Mapping[str, Any],
        bundle: CredentialBundle,
        pages: Sequence['_ChapterPage'],
    ) -> bool:
        matches: Tuple[MatchRecord, ...] = ()
        if int(publication['match_count']) > 0:
            session_matches = await self._session_matches(
                int(publication['session_id'])
            )
            matches = tuple(
                match
                for match in session_matches
                if match.bvid == str(publication['bvid'])
            )
        targets = _chapter_targets(matches, pages)
        target_by_page = {page.page: cards for page, cards in targets}
        for page in pages:
            response = await self._protocol.public_player_view(
                bundle, aid=int(publication['aid']), cid=page.cid
            )
            existing = _public_chapter_cards(response)
            expected = target_by_page.get(page.page, ())
            if existing and not _automatic_chapter_cards(existing):
                continue
            if not _same_chapter_cards(existing, expected):
                return False
        return bool(pages)

    async def _remote_comments_match(
        self, publication: Mapping[str, Any], bundle: CredentialBundle
    ) -> bool:
        comments = await self._database.fetchall(
            'SELECT content,rpid,uploaded_pictures_json '
            'FROM vainglory_publication_comments '
            'WHERE publication_id=? ORDER BY ordinal',
            (int(publication['id']),),
        )
        for comment in comments:
            rpid = _positive_int(comment['rpid'])
            if rpid is None:
                return False
            try:
                response = await self._protocol.public_reply_detail(
                    bundle,
                    {
                        'type': 1,
                        'oid': int(publication['aid']),
                        'root': rpid,
                        'pn': 1,
                        'ps': 20,
                    },
                )
            except BiliApiError as error:
                if error.code in self._MISSING_REPLY_CODES:
                    return False
                raise
            root = _reply_root(response)
            if not _reply_matches(
                root,
                content=str(comment['content']),
                account_uid=int(publication['account_uid']),
                aid=int(publication['aid']),
                root_rpid=None,
                is_root=True,
            ):
                return False
            expected_pictures = sum(
                picture is not None
                for picture in _json_pictures(comment['uploaded_pictures_json'])
            )
            if _reply_picture_count(root) < expected_pictures:
                return False
        return True

    async def _remote_pin_matches(
        self, publication: Mapping[str, Any], bundle: CredentialBundle
    ) -> bool:
        comment_count = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM vainglory_publication_comments '
                'WHERE publication_id=?',
                (int(publication['id']),),
            )
            or 0
        )
        if comment_count == 0:
            return True
        root_rpid = _positive_int(publication.get('root_rpid'))
        if root_rpid is None:
            return False
        response = await self._protocol.list_replies(
            bundle,
            {'type': 1, 'oid': int(publication['aid']), 'mode': 2, 'next': 0, 'ps': 20},
        )
        return root_rpid in _top_reply_ids(response)

    async def _reset_unverified_steps(
        self,
        publication_id: int,
        *,
        attempt: int,
        description_ok: bool,
        chapter_ok: bool,
        comments_ok: bool,
        pin_ok: bool,
    ) -> None:
        missing = [
            label
            for ok, label in (
                (description_ok, '简介'),
                (chapter_ok, '视频分段'),
                (comments_ok, '评论'),
            )
            if not ok
        ]
        if comments_ok and not pin_ok:
            missing.append('置顶评论')
        message = '远端缺少{}，将重新发布'.format('、'.join(missing))
        now = self._now()
        retry_delay = max(60, min(6 * 3600, 2 ** min(attempt + 1, 14)))

        def reset(connection: sqlite3.Connection) -> None:
            if not comments_ok:
                connection.execute(
                    "UPDATE vainglory_publication_comments SET state='prepared',"
                    'rpid=NULL,uploaded_pictures_json=\'[]\',next_attempt_at=0,'
                    'error=?,updated_at=? WHERE publication_id=?',
                    (message, now, publication_id),
                )
            connection.execute(
                "UPDATE vainglory_publications SET state='prepared',"
                'chapter_state=?,description_state=?,comment_cleanup_state=?,'
                'pin_state=?,root_rpid=?,remote_verified_at=NULL,'
                'next_attempt_at=?,error=?,priority=0,updated_at=? WHERE id=?',
                (
                    'confirmed' if chapter_ok else 'prepared',
                    'confirmed' if description_ok else 'prepared',
                    'prepared' if not comments_ok else 'confirmed',
                    'confirmed' if comments_ok and pin_ok else 'prepared',
                    (
                        None
                        if not comments_ok
                        else connection.execute(
                            'SELECT root_rpid FROM vainglory_publications WHERE id=?',
                            (publication_id,),
                        ).fetchone()[0]
                    ),
                    now + retry_delay,
                    message,
                    now,
                    publication_id,
                ),
            )

        await self._database.write(reset)

    async def _confirm_public_visibility(
        self, publication: Mapping[str, Any], bundle: CredentialBundle
    ) -> bool:
        if publication['public_visible_at'] is not None:
            return True
        publication_id = int(publication['id'])
        try:
            response = await self._protocol.public_archive_view(
                bundle, bvid=str(publication['bvid'])
            )
        except BiliApiError as error:
            if error.code in (-404, 404):
                await self._retry_publication(
                    publication_id,
                    int(publication['attempt_count']),
                    _WAITING_PUBLICATION_ERROR,
                    minimum_delay=300,
                )
            elif error.code in self._RETRYABLE_CODES:
                await self._handle_api_error(publication_id, publication, error)
            else:
                await self._retry_publication(
                    publication_id,
                    int(publication['attempt_count']),
                    '确认稿件公开状态失败（{}），将自动重试'.format(error.code),
                    minimum_delay=3600,
                )
            return False
        except (DefinitelyNotSent, RemoteOutcomeUnknown):
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                '暂时无法确认稿件是否公开，将自动重试',
                minimum_delay=60,
            )
            return False
        except ProtocolContractError:
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                '公开稿件数据异常，将自动重试',
                minimum_delay=300,
            )
            return False
        data = response.get('data')
        now = self._now()
        if not isinstance(data, Mapping):
            visible = False
        else:
            pages = data.get('pages')
            published_at = _positive_int(data.get('pubdate'))
            visible = (
                _positive_int(data.get('aid')) == int(publication['aid'])
                and data.get('bvid') == str(publication['bvid'])
                and published_at is not None
                and published_at <= now
                and isinstance(pages, list)
                and any(
                    isinstance(page, Mapping)
                    and _positive_int(page.get('cid')) is not None
                    for page in pages
                )
            )
        if not visible:
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                _WAITING_PUBLICATION_ERROR,
                minimum_delay=300,
            )
            return False
        await self._database.execute(
            "UPDATE vainglory_publications SET state='prepared',"
            'public_visible_at=?,next_attempt_at=0,error=NULL,updated_at=? '
            'WHERE id=?',
            (now, now, publication_id),
        )
        logger.info(
            'Vainglory publication confirmed publicly visible: '
            'publication_id={} bvid={}',
            publication_id,
            str(publication['bvid']),
        )
        return True

    async def _prepare_comment_cleanup(
        self, publication: Mapping[str, Any], bundle: CredentialBundle
    ) -> bool:
        publication_id = int(publication['id'])
        await self._database.execute(
            "UPDATE vainglory_publications SET comment_cleanup_state='in_flight',"
            'updated_at=? WHERE id=?',
            (self._now(), publication_id),
        )
        owned: Dict[int, str] = {}
        cursor = 0
        try:
            for _page in range(100):
                response = await self._protocol.list_replies(
                    bundle,
                    {
                        'type': 1,
                        'oid': int(publication['aid']),
                        'mode': 2,
                        'next': cursor,
                        'ps': 20,
                    },
                )
                entries = _reply_entries(response)
                for reply in entries:
                    if not _owned_root_reply(
                        reply,
                        account_uid=int(publication['account_uid']),
                        aid=int(publication['aid']),
                    ):
                        continue
                    rpid = _positive_int(reply.get('rpid'))
                    if rpid is None:
                        continue
                    content = reply.get('content')
                    message = (
                        str(content.get('message') or '')
                        if isinstance(content, Mapping)
                        else ''
                    )
                    owned[rpid] = message[:1000] or '本账号历史顶层评论'
                next_cursor, is_end = _reply_next_cursor(response)
                if is_end or (next_cursor is None and len(entries) < 20):
                    break
                if next_cursor is None or next_cursor == cursor:
                    raise ProtocolContractError('comment list cursor did not advance')
                cursor = next_cursor
            else:
                raise ProtocolContractError('comment list has too many pages')
        except (BiliApiError, DefinitelyNotSent, RemoteOutcomeUnknown):
            await self._database.execute(
                "UPDATE vainglory_publications SET comment_cleanup_state='prepared' "
                'WHERE id=?',
                (publication_id,),
            )
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                '读取历史顶层评论失败，将自动重试',
            )
            return False
        except ProtocolContractError:
            await self._database.execute(
                "UPDATE vainglory_publications SET comment_cleanup_state='prepared' "
                'WHERE id=?',
                (publication_id,),
            )
            await self._retry_publication(
                publication_id,
                int(publication['attempt_count']),
                '历史顶层评论数据异常，将自动重试',
                minimum_delay=3600,
            )
            return False
        now = self._now()

        def persist(connection: sqlite3.Connection) -> None:
            for ordinal, (rpid, content) in enumerate(sorted(owned.items())):
                exists = connection.execute(
                    'SELECT 1 FROM vainglory_publication_stale_comments '
                    'WHERE publication_id=? AND rpid=? LIMIT 1',
                    (publication_id, rpid),
                ).fetchone()
                if exists is not None:
                    continue
                connection.execute(
                    'INSERT INTO vainglory_publication_stale_comments('
                    'publication_id,ordinal,content,rpid,state,attempt_count,'
                    'next_attempt_at,error,created_at,updated_at) '
                    "VALUES(?,?,?,?,'prepared',0,0,NULL,?,?)",
                    (publication_id, ordinal, content, rpid, now, now),
                )
            connection.execute(
                "UPDATE vainglory_publications SET comment_cleanup_state='confirmed',"
                'updated_at=? WHERE id=?',
                (now, publication_id),
            )

        await self._database.write(persist)
        logger.info(
            'Vainglory owned root comments enumerated: publication_id={} '
            'bvid={} comments={}',
            publication_id,
            str(publication['bvid']),
            len(owned),
        )
        return True

    async def _publication_ready(self, publication_id: int) -> bool:
        return bool(
            await self._database.scalar(
                'SELECT EXISTS(SELECT 1 FROM vainglory_publications publication '
                'JOIN recording_sessions session '
                'ON session.id=publication.session_id '
                "WHERE publication.id=? AND publication.needs_refresh=0 "
                "AND publication.plan_state='ready' AND "
                + _PUBLICATION_READY_PREDICATE
                + ' AND '
                + _PUBLICATION_UPLOAD_APPROVED_PREDICATE
                + ')',
                (int(publication_id),),
            )
        )

    async def _publish_chapters(
        self,
        publication: Mapping[str, Any],
        bundle: CredentialBundle,
        *,
        force: bool = False,
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
            if not pages or any(page.duration_seconds is None for page in pages):
                await self._retry_publication(
                    publication_id,
                    int(publication['attempt_count']),
                    '暂时无法取得稿件分 P 及时长，视频分段将自动重试',
                    minimum_delay=3600,
                )
                return
            matches: Tuple[MatchRecord, ...] = ()
            if int(publication['match_count']) > 0:
                session_matches = await self._session_matches(
                    int(publication['session_id'])
                )
                matches = tuple(
                    match
                    for match in session_matches
                    if match.bvid == str(publication['bvid'])
                )
            page_durations = {
                page.page: int(page.duration_seconds or 0) for page in pages
            }
            missing_anchors = tuple(
                match
                for match in matches
                if (
                    (anchor := _match_anchor(match)) is None
                    or anchor[0] not in page_durations
                    or anchor[1] >= page_durations[anchor[0]]
                )
            )
            if missing_anchors:
                await self._fail_analysis_data(
                    publication_id,
                    '{} 局识别结果缺少稿件分 P、结算画面时间或 '
                    'OCR 对局时长，请重新分析这场直播'.format(len(missing_anchors)),
                )
                return
            targets = _chapter_targets(matches, pages)
            target_by_page = {page.page: cards for page, cards in targets}
            for target_index, page in enumerate(pages):
                cards = target_by_page.get(page.page, ())
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
                if _same_chapter_cards(existing, cards) and not force:
                    continue
                await self._protocol.submit_archive_chapters(
                    bundle,
                    aid=int(publication['aid']),
                    cid=page.cid,
                    cards=cards,
                    permanent=True,
                )
                if self._picture_interval_seconds and target_index + 1 < len(pages):
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
                    'Bilibili chapters rejected and will retry: bvid={} code={}',
                    str(publication['bvid']),
                    error.code,
                )
                await self._retry_publication(
                    publication_id,
                    int(publication['attempt_count']),
                    'B 站章节接口拒绝写入（{}）{}，将继续重试'.format(
                        error.code,
                        (
                            ''
                            if not error.public_message
                            else '：{}'.format(error.public_message)
                        ),
                    ),
                    minimum_delay=6 * 3600,
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
        self,
        publication: Mapping[str, Any],
        bundle: CredentialBundle,
        *,
        force: bool = False,
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
        merged: Optional[str]
        if int(publication['match_count']) == 0:
            merged = remove_generated_description(current, block)
            if merged == current and not force:
                await self._set_description_state(publication_id, 'confirmed')
                return
        else:
            if description_contains_block(current, block) and not force:
                await self._set_description_state(publication_id, 'confirmed')
                return
            merged = merge_archive_description(current, block)
            if merged is None:
                await self._retry_publication(
                    publication_id,
                    int(publication['attempt_count']),
                    '稿件简介没有空间，任务不会跳过并将继续重试',
                    minimum_delay=6 * 3600,
                )
                return
        if merged == current and not force:
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
            entries: Tuple[Mapping[str, Any], ...]
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

    async def _fail_analysis_data(self, publication_id: int, message: str) -> None:
        await self._database.execute(
            "UPDATE vainglory_publications SET state='failed',"
            'next_attempt_at=0,error=?,updated_at=? WHERE id=?',
            (message[:500], self._now(), publication_id),
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
        now = self._now()
        await self._database.execute(
            "UPDATE vainglory_publications SET state='paused',next_attempt_at=?,"
            'error=?,updated_at=? WHERE id=?',
            (
                now + 6 * 3600,
                '{}，任务保留并将继续重试'.format(message)[:500],
                now,
                publication_id,
            ),
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
        anchor_page, seconds = anchor
        anchors.setdefault(anchor_page, []).append((seconds, index, match))
    targets: List[Tuple[_ChapterPage, Tuple[Mapping[str, Any], ...]]] = []
    first_page = min((page.page for page in pages), default=None)
    for page in sorted(pages, key=lambda item: item.page):
        if page.duration_seconds is None:
            continue
        cards = _build_chapter_cards(
            page.duration_seconds,
            anchors.get(page.page, ()),
            include_live_start=page.page == first_page,
        )
        if cards:
            targets.append((page, cards))
    return tuple(targets)


def _build_chapter_cards(
    duration_seconds: int,
    anchors: Sequence[Tuple[int, int, MatchRecord]],
    *,
    include_live_start: bool = True,
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
    if include_live_start and unique[0][0] > 0:
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


def _public_chapter_cards(response: Mapping[str, Any]) -> Tuple[Mapping[str, Any], ...]:
    data = response.get('data')
    if not isinstance(data, Mapping):
        raise ProtocolContractError('public chapter response is incomplete')
    cards = data.get('view_points')
    if cards is None:
        return ()
    if not isinstance(cards, list):
        raise ProtocolContractError('public chapter response is incomplete')
    return tuple(card for card in cards if isinstance(card, Mapping))


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
    mode = {'3v3': '3V3', '5v5': '5V5', 'aram': '大乱斗'}.get(
        match.game_mode, '模式待确认'
    )
    label = _circled_match_number(index)
    line = '{}｜{}｜{}｜{} vs {}'.format(label, result, mode, recorded, opponent)
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
    inferred_start_ms = match.result_at_ms - match.duration_seconds * 1_000
    if inferred_start_ms >= 0:
        return match.archive_page, inferred_start_ms // 1_000
    remaining_ms = -inferred_start_ms
    valid_segments = tuple(
        sorted(
            (
                (int(page), int(duration_seconds))
                for page, duration_seconds in segments
                if page > 0 and duration_seconds > 0
            ),
            key=lambda value: value[0],
            reverse=True,
        )
    )
    for page, duration_seconds in valid_segments:
        duration_ms = duration_seconds * 1_000
        if remaining_ms <= duration_ms + 30_000:
            return page, max(0, duration_ms - remaining_ms) // 1_000
        remaining_ms -= duration_ms
    earliest_page = min(
        (match.archive_page, *(page for page, _duration in valid_segments))
    )
    return earliest_page, 0


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


def _top_reply_ids(response: Mapping[str, Any]) -> Tuple[int, ...]:
    data = response.get('data')
    if not isinstance(data, Mapping):
        raise ProtocolContractError('comment list response is incomplete')
    entries: List[Mapping[str, Any]] = []
    top_replies = data.get('top_replies')
    if isinstance(top_replies, list):
        entries.extend(entry for entry in top_replies if isinstance(entry, Mapping))
    upper = data.get('upper')
    if isinstance(upper, Mapping):
        upper_top = upper.get('top')
        if isinstance(upper_top, Mapping):
            entries.append(upper_top)
        elif _positive_int(upper.get('rpid')) is not None:
            entries.append(upper)
    return tuple(
        rpid
        for rpid in (_positive_int(entry.get('rpid')) for entry in entries)
        if rpid is not None
    )


def _owned_root_reply(reply: Mapping[str, Any], *, account_uid: int, aid: int) -> bool:
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
    return (
        _positive_int(reply.get('root')) is None
        and _positive_int(reply.get('parent')) is None
    )


def _reply_next_cursor(response: Mapping[str, Any]) -> Tuple[Optional[int], bool]:
    data = response.get('data')
    if not isinstance(data, Mapping):
        raise ProtocolContractError('comment list response is incomplete')
    cursor = data.get('cursor')
    if not isinstance(cursor, Mapping):
        return None, False
    is_end = cursor.get('is_end') in (True, 1, '1')
    next_cursor = _positive_int(cursor.get('next'))
    if next_cursor is None and cursor.get('next') in (0, '0'):
        next_cursor = 0
    return next_cursor, is_end


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
