from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional, Sequence, Set, Tuple, cast

from loguru import logger

from blrec.bili_upload.database import BiliUploadDatabase

from .analyzer import AnalyzedHero, AnalyzedMatch, ScannedPart, VideoPart
from .catalog import identify_builtin_hero
from .exclusions import EXCLUDED_TITLE_MARKER, is_excluded_title
from .hero_recognition import HeroReference
from .ocr import clean_player_name, normalize_player_name
from .title_time import current_season_started_at
from .vision import RecordedPlayer


class VaingloryNotFound(ValueError):
    pass


class VaingloryConflict(ValueError):
    pass


@dataclass(frozen=True)
class ScanJob:
    session_id: int
    state: str
    progress: float
    algorithm_version: int
    match_count: int
    error: Optional[str]
    requested_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    updated_at: int


@dataclass(frozen=True)
class ScanClaim:
    session_id: int
    part: VideoPart
    realtime: bool

    @property
    def parts(self) -> Tuple[VideoPart, ...]:
        return (self.part,)


@dataclass(frozen=True)
class OcrClaim:
    session_id: int
    part: VideoPart
    scanned: ScannedPart


@dataclass(frozen=True)
class AnalysisQueueItem:
    part_id: int
    session_id: int
    part_index: int
    title: str
    anchor_name: str
    state: str
    stage: str
    category: str
    progress: float
    requested_at: int
    started_at: Optional[int]
    updated_at: int
    part_count: int
    completed_part_count: int


@dataclass(frozen=True)
class AnalysisQueueStatus:
    active: Tuple[AnalysisQueueItem, ...]
    queued: Tuple[AnalysisQueueItem, ...]
    pending_count: int
    manual_pending: int
    realtime_pending: int
    archive_pending: int
    migration_pending: int
    backlog_pending: int


@dataclass(frozen=True)
class IndexSummary:
    match_count: int
    session_count: int
    anchor_count: int
    unassigned_session_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    player_slot_count: int
    recognized_hero_count: int


@dataclass(frozen=True)
class HeroRematchClaim:
    match_id: int


@dataclass(frozen=True)
class RecordedPlayerBackfillClaim:
    match_id: int


@dataclass(frozen=True)
class MatchPlayerRecord:
    side: str
    slot: int
    name: str
    normalized_name: str
    hero_id: Optional[int]
    hero_label: str
    hero_source: Literal['automatic', 'manual']
    kills: Optional[int]
    deaths: Optional[int]
    assists: Optional[int]
    economy: Optional[int]
    confidence: float
    last_hits: Optional[int] = None
    is_recorded_player: bool = False


@dataclass(frozen=True)
class MatchRecord:
    id: int
    session_id: int
    session_title: str
    session_started_at: int
    part_id: int
    part_index: int
    title: str
    source_title: str
    upload_title: str
    game_mode: str
    team_size: Optional[int]
    started_at_ms: int
    result_at_ms: int
    duration_seconds: Optional[int]
    result_text: str
    end_reason: str
    left_color: str
    right_color: str
    winner_side: str
    winner_color: str
    left_kills: Optional[int]
    right_kills: Optional[int]
    left_economy: Optional[int]
    right_economy: Optional[int]
    confidence: float
    account_id: Optional[int]
    bvid: Optional[str]
    archive_page: Optional[int]
    has_result_frame: bool
    recorded_player_confidence: Optional[float]
    recorded_player_source: str
    players: Tuple[MatchPlayerRecord, ...]
    match_kind: Literal['pvp', 'bot', 'practice', 'unknown'] = 'unknown'
    view_context: Literal['played', 'observed', 'unknown'] = 'unknown'
    stats_eligible: bool = True
    stats_exclusion_reason: Optional[str] = None
    recorded_player_state: str = 'pending'
    previous_archive_page: Optional[int] = None
    previous_archive_duration_seconds: Optional[int] = None
    previous_archive_segments: Tuple[Tuple[int, int], ...] = ()


@dataclass(frozen=True)
class MatchPage:
    total: int
    items: Tuple[MatchRecord, ...]


@dataclass(frozen=True)
class MatchSessionRecord:
    session_id: int
    title: str
    started_at: int
    match_count: int
    teal_win_count: int
    orange_win_count: int
    surrender_count: int
    duration_seconds: int
    game_modes: Tuple[str, ...]
    source_title: str = ''
    anchor_name: str = ''
    win_count: int = 0
    loss_count: int = 0
    unknown_count: int = 0
    stats_included: bool = True
    bvid: Optional[str] = None
    publication_state: Optional[str] = None
    description_state: Optional[str] = None
    pin_state: Optional[str] = None
    chapter_state: Optional[str] = None


@dataclass(frozen=True)
class MatchSessionPage:
    total: int
    items: Tuple[MatchSessionRecord, ...]


@dataclass(frozen=True)
class HeroRecord:
    id: int
    label: str
    fingerprint: str


@dataclass(frozen=True)
class AnchorStatsRecord:
    anchor_uid: Optional[int]
    anchor_name: str
    room_id: int
    session_count: int
    match_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    win_rate: float


@dataclass(frozen=True)
class PlayerRoomRecord:
    room_id: int
    anchor_uid: Optional[int]
    anchor_name: str


@dataclass(frozen=True)
class PlayerRecord:
    id: int
    name: str
    origin: Literal['automatic', 'manual']
    rooms: Tuple[PlayerRoomRecord, ...]
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class GameModeStatsRecord:
    game_mode: str
    match_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    win_rate: float


@dataclass(frozen=True)
class HeroStatsRecord:
    hero_id: int
    hero_label: str
    player_count: int
    match_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    win_rate: float


@dataclass(frozen=True)
class PlayerStatsRecord:
    player_id: int
    player_name: str
    rooms: Tuple[PlayerRoomRecord, ...]
    session_count: int
    match_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    win_rate: float
    modes: Tuple[GameModeStatsRecord, ...]
    heroes: Tuple[HeroStatsRecord, ...]


@dataclass
class _AnchorStatsAccumulator:
    anchor_uid: Optional[int]
    anchor_name: str
    room_id: int
    session_ids: Set[int]
    match_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    unknown_count: int = 0


@dataclass
class _OutcomeAccumulator:
    match_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    unknown_count: int = 0

    def add(self, winner_color: str) -> None:
        self.match_count += 1
        if winner_color == 'teal':
            self.win_count += 1
        elif winner_color == 'orange':
            self.loss_count += 1
        else:
            self.unknown_count += 1

    @property
    def win_rate(self) -> float:
        return 0.0 if self.match_count == 0 else self.win_count / self.match_count


@dataclass
class _PlayerStatsAccumulator:
    player: PlayerRecord
    session_ids: Set[int] = field(default_factory=set)
    outcomes: _OutcomeAccumulator = field(default_factory=_OutcomeAccumulator)
    modes: Dict[str, _OutcomeAccumulator] = field(default_factory=dict)
    heroes: Dict[int, Tuple[str, _OutcomeAccumulator]] = field(default_factory=dict)


def refresh_session_scan_job(
    connection: sqlite3.Connection, session_id: int, now: int
) -> None:
    summary = connection.execute(
        'SELECT COUNT(*) AS part_count,'
        "SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending_count,"
        "SUM(CASE WHEN state='analyzing' THEN 1 ELSE 0 END) AS analyzing_count,"
        "SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed_count,"
        'AVG(progress) AS progress,SUM(match_count) AS match_count,'
        'MIN(requested_at) AS requested_at,MIN(started_at) AS started_at,'
        'MAX(algorithm_version) AS algorithm_version '
        'FROM vainglory_part_jobs WHERE session_id=?',
        (session_id,),
    ).fetchone()
    if summary is None or int(summary['part_count']) == 0:
        return
    pending_count = int(summary['pending_count'])
    analyzing_count = int(summary['analyzing_count'])
    failed_count = int(summary['failed_count'])
    archive = connection.execute(
        'SELECT COALESCE(MAX(imported.page_count),0) AS expected_count,'
        'COUNT(part.id) AS materialized_count,'
        "SUM(CASE WHEN part.state='ready' THEN 1 ELSE 0 END) AS ready_count,"
        "SUM(CASE WHEN part.state='failed' THEN 1 ELSE 0 END) AS failed_count,"
        'COALESCE(SUM(part.progress),0) AS progress_sum '
        'FROM vainglory_archive_imports imported '
        'LEFT JOIN vainglory_archive_parts part ON part.import_id=imported.id '
        'WHERE imported.session_id=?',
        (session_id,),
    ).fetchone()
    expected_count = int(archive['expected_count'])
    materialized_count = int(archive['materialized_count'])
    archive_ready_count = int(archive['ready_count'] or 0)
    archive_failed_count = int(archive['failed_count'] or 0)
    archive_terminal_count = archive_ready_count + archive_failed_count
    archive_incomplete = expected_count > 0 and (
        materialized_count < expected_count
        or archive_terminal_count < expected_count
        or int(summary['part_count']) < expected_count
    )
    archive_failed = (
        expected_count > 0 and not archive_incomplete and archive_failed_count > 0
    )
    error: Optional[str] = None
    completed_at: Optional[int] = None
    if archive_incomplete:
        has_progress = (
            archive_terminal_count > 0
            or float(archive['progress_sum'] or 0) > 0
            or analyzing_count > 0
            or summary['started_at'] is not None
        )
        state = 'analyzing' if has_progress else 'pending'
        progress = min(0.99, float(archive['progress_sum'] or 0) / expected_count)
    elif archive_failed:
        state = 'failed'
        progress = 1.0
        error_row = connection.execute(
            'SELECT part.error AS error FROM vainglory_archive_parts part '
            'JOIN vainglory_archive_imports imported ON imported.id=part.import_id '
            "WHERE imported.session_id=? AND part.state='failed' "
            'ORDER BY part.updated_at DESC,part.id DESC LIMIT 1',
            (session_id,),
        ).fetchone()
        error = (
            '部分分 P 分析失败'
            if error_row is None or error_row['error'] is None
            else str(error_row['error'])
        )
        completed_at = now
    elif analyzing_count:
        state = 'analyzing'
        progress = float(summary['progress'] or 0)
    elif pending_count:
        state = 'pending'
        progress = float(summary['progress'] or 0)
    elif failed_count:
        state = 'failed'
        progress = float(summary['progress'] or 0)
        error_row = connection.execute(
            'SELECT error FROM vainglory_part_jobs '
            "WHERE session_id=? AND state='failed' "
            'ORDER BY updated_at DESC,part_id DESC LIMIT 1',
            (session_id,),
        ).fetchone()
        error = (
            '部分分 P 分析失败'
            if error_row is None or error_row['error'] is None
            else str(error_row['error'])
        )
        completed_at = now
    else:
        state = 'ready'
        progress = float(summary['progress'] or 0)
        completed_at = now
    started_at = None if summary['started_at'] is None else int(summary['started_at'])
    if state == 'pending':
        started_at = None
    connection.execute(
        'UPDATE vainglory_scan_jobs SET state=?,progress=?,'
        'algorithm_version=?,match_count=?,error=?,requested_at=?,'
        'started_at=?,completed_at=?,updated_at=? WHERE session_id=?',
        (
            state,
            progress,
            int(summary['algorithm_version']),
            int(summary['match_count'] or 0),
            error,
            int(summary['requested_at']),
            started_at,
            completed_at,
            now,
            session_id,
        ),
    )


class VaingloryRepository:
    ALGORITHM_VERSION = 17
    HERO_RECOGNITION_VERSION = 5
    RECORDED_PLAYER_DETECTION_VERSION = 3
    _REALTIME_WINDOW_SECONDS = 24 * 60 * 60
    _MATCH_SELECT = (
        'SELECT match.id,match.session_id,match.result_part_id,'
        'match.result_at_ms,match.duration_seconds,match.result_text,'
        'match.end_reason,match.left_color,match.right_color,match.winner_side,'
        'match.left_kills,match.right_kills,match.left_economy,'
        'match.right_economy,match.confidence,match.game_mode,match.team_size,'
        'match.match_kind,match.view_context,match.stats_eligible,'
        'match.stats_exclusion_reason,'
        'match.started_at_ms,match.custom_title,'
        'match.result_frame_path,match.recorded_player_confidence,'
        'match.recorded_player_source,match.recorded_player_detection_version,'
        'session.title AS session_title,'
        'session.started_at AS session_started_at,'
        'part.part_index AS part_index,'
        'COALESCE(job.account_id,CASE WHEN NOT EXISTS('
        'SELECT 1 FROM archive_migration_items source_migration '
        'WHERE source_migration.session_id=session.id) '
        'THEN video_source.account_id END,'
        'archive_import.account_id) AS account_id,'
        'COALESCE(job.bvid,CASE WHEN NOT EXISTS('
        'SELECT 1 FROM archive_migration_items source_migration '
        'WHERE source_migration.session_id=session.id) '
        'THEN video_source.bvid END,archive_import.bvid) AS bvid,'
        'job.policy_snapshot_json AS upload_title_source,'
        'CASE WHEN job.bvid IS NOT NULL AND job.bvid<>\'\' THEN ('
        'SELECT COUNT(*) FROM upload_parts remote_part '
        'WHERE remote_part.job_id=job.id AND remote_part.cid IS NOT NULL '
        'AND remote_part.part_index<=part.part_index) '
        'ELSE COALESCE(CASE WHEN NOT EXISTS('
        'SELECT 1 FROM archive_migration_items source_migration '
        'WHERE source_migration.session_id=session.id) '
        'THEN video_source.page END,archive_part.page,part.part_index) '
        'END AS archive_page,'
        'CASE WHEN job.bvid IS NOT NULL AND job.bvid<>\'\' THEN ('
        'SELECT COUNT(*) FROM upload_parts previous_remote '
        'WHERE previous_remote.job_id=job.id '
        'AND previous_remote.cid IS NOT NULL '
        'AND previous_remote.part_index<part.part_index) '
        'ELSE CASE WHEN archive_part.page>1 '
        'THEN archive_part.page-1 END END AS previous_archive_page,'
        'CASE WHEN job.bvid IS NOT NULL AND job.bvid<>\'\' THEN ('
        'SELECT previous_part.record_duration_seconds '
        'FROM recording_parts previous_part '
        'JOIN upload_parts previous_remote '
        'ON previous_remote.job_id=job.id '
        'AND previous_remote.part_index=previous_part.part_index '
        'WHERE previous_part.session_id=match.session_id '
        'AND previous_remote.cid IS NOT NULL '
        'AND previous_part.part_index<part.part_index '
        'ORDER BY previous_part.part_index DESC LIMIT 1) '
        'ELSE (SELECT previous_archive.duration_seconds '
        'FROM vainglory_archive_parts previous_archive '
        'WHERE previous_archive.import_id=archive_import.id '
        'AND previous_archive.page<archive_part.page '
        'ORDER BY previous_archive.page DESC LIMIT 1) '
        'END AS previous_archive_duration_seconds,'
        'CASE WHEN job.bvid IS NOT NULL AND job.bvid<>\'\' THEN ('
        'SELECT GROUP_CONCAT(('
        'SELECT COUNT(*) FROM upload_parts counted_remote '
        'WHERE counted_remote.job_id=job.id '
        'AND counted_remote.cid IS NOT NULL '
        'AND counted_remote.part_index<=previous_remote.part_index'
        ") || ':' || previous_part.record_duration_seconds, ',') "
        'FROM upload_parts previous_remote '
        'JOIN recording_parts previous_part '
        'ON previous_part.session_id=match.session_id '
        'AND previous_part.part_index=previous_remote.part_index '
        'WHERE previous_remote.job_id=job.id '
        'AND previous_remote.cid IS NOT NULL '
        'AND previous_remote.part_index<part.part_index '
        'AND previous_part.record_duration_seconds>0) '
        'ELSE (SELECT GROUP_CONCAT('
        "previous_archive.page || ':' || previous_archive.duration_seconds, ',') "
        'FROM vainglory_archive_parts previous_archive '
        'WHERE previous_archive.import_id=archive_import.id '
        'AND previous_archive.page<archive_part.page '
        'AND previous_archive.duration_seconds>0) '
        'END AS previous_archive_segments '
        'FROM vainglory_matches match '
        'JOIN recording_sessions session ON session.id=match.session_id '
        'JOIN recording_parts part ON part.id=match.result_part_id '
        'LEFT JOIN upload_jobs job ON job.session_id=match.session_id '
        'LEFT JOIN vainglory_video_sources video_source '
        'ON video_source.part_id=part.id '
        'LEFT JOIN vainglory_archive_parts archive_part '
        'ON archive_part.recording_part_id=part.id '
        'LEFT JOIN vainglory_archive_imports archive_import '
        'ON archive_import.id=archive_part.import_id '
    )

    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        result_frame_root: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._result_frame_root = (
            Path(database.path).parent / 'vainglory-result-frames'
            if result_frame_root is None
            else Path(result_frame_root)
        ).resolve()
        self._clock = clock

    async def recover_interrupted(self) -> int:
        now = self._now()

        def recover(connection: sqlite3.Connection) -> int:
            recovered_ocr = connection.execute(
                "UPDATE vainglory_ocr_jobs SET state='pending',started_at=NULL,"
                'updated_at=? WHERE state=\'running\'',
                (now,),
            ).rowcount
            cursor = connection.execute(
                "UPDATE vainglory_part_jobs SET state='pending',progress=0,"
                "error=NULL,started_at=NULL,completed_at=NULL,updated_at=? "
                "WHERE state='analyzing' AND NOT EXISTS("
                'SELECT 1 FROM vainglory_ocr_jobs ocr '
                'WHERE ocr.part_id=vainglory_part_jobs.part_id)',
                (now,),
            )
            connection.execute(
                'UPDATE vainglory_part_jobs SET progress=MAX(progress,0.7),'
                'error=NULL,updated_at=? WHERE state=\'analyzing\' AND EXISTS('
                'SELECT 1 FROM vainglory_ocr_jobs ocr '
                'WHERE ocr.part_id=vainglory_part_jobs.part_id)',
                (now,),
            )
            rows = connection.execute(
                "SELECT session_id FROM vainglory_scan_jobs WHERE state='analyzing' "
                'UNION SELECT session_id FROM vainglory_ocr_jobs'
            ).fetchall()
            for row in rows:
                self._refresh_session_job(connection, int(row['session_id']), now)
            return cursor.rowcount + recovered_ocr

        return await self._database.write(recover)

    async def purge_excluded_content(self) -> int:
        def purge(connection: sqlite3.Connection) -> Dict[str, int]:
            session_ids = set()
            rows = connection.execute(
                'SELECT session.id,session.title,job.policy_snapshot_json,'
                'migration.title AS migration_title,'
                'imported.title AS import_title '
                'FROM recording_sessions session '
                'LEFT JOIN upload_jobs job ON job.session_id=session.id '
                'LEFT JOIN archive_migration_items migration '
                'ON migration.session_id=session.id '
                'LEFT JOIN vainglory_archive_imports imported '
                'ON imported.session_id=session.id'
            ).fetchall()
            for row in rows:
                if is_excluded_title(
                    row['title'],
                    row['migration_title'],
                    row['import_title'],
                    self._upload_title(row['policy_snapshot_json']),
                ):
                    session_ids.add(int(row['id']))
            import_rows = connection.execute(
                'SELECT id,session_id FROM vainglory_archive_imports '
                'WHERE instr(title,?)>0',
                (EXCLUDED_TITLE_MARKER,),
            ).fetchall()
            import_ids = {int(row['id']) for row in import_rows}
            session_ids.update(
                int(row['session_id'])
                for row in import_rows
                if row['session_id'] is not None
            )
            counts = {
                'sessions': len(session_ids),
                'imports': len(import_ids),
                'part_jobs': 0,
                'ocr_jobs': 0,
                'matches': 0,
            }
            if session_ids:
                ordered_ids = tuple(sorted(session_ids))
                placeholders = ','.join('?' for _value in ordered_ids)
                counts['matches'] = int(
                    connection.execute(
                        'SELECT COUNT(*) FROM vainglory_matches '
                        'WHERE session_id IN ({})'.format(placeholders),
                        ordered_ids,
                    ).fetchone()[0]
                )
                counts['ocr_jobs'] = connection.execute(
                    'DELETE FROM vainglory_ocr_jobs '
                    'WHERE session_id IN ({})'.format(placeholders),
                    ordered_ids,
                ).rowcount
                counts['part_jobs'] = connection.execute(
                    'DELETE FROM vainglory_part_jobs '
                    'WHERE session_id IN ({})'.format(placeholders),
                    ordered_ids,
                ).rowcount
                connection.execute(
                    'DELETE FROM vainglory_scan_jobs '
                    'WHERE session_id IN ({})'.format(placeholders),
                    ordered_ids,
                )
                connection.execute(
                    'DELETE FROM vainglory_archive_imports '
                    'WHERE session_id IN ({})'.format(placeholders),
                    ordered_ids,
                )
            connection.execute(
                'DELETE FROM vainglory_archive_imports WHERE instr(title,?)>0',
                (EXCLUDED_TITLE_MARKER,),
            )
            syncs = connection.execute(
                'SELECT account_id FROM vainglory_archive_syncs'
            ).fetchall()
            for sync in syncs:
                account_id = int(sync['account_id'])
                values = connection.execute(
                    'SELECT state,progress,retryable '
                    'FROM vainglory_archive_imports '
                    'WHERE account_id=?',
                    (account_id,),
                ).fetchall()
                total = len(values)
                completed = sum(
                    str(value['state']) in ('ready', 'skipped')
                    or (
                        str(value['state']) == 'failed' and not bool(value['retryable'])
                    )
                    for value in values
                )
                progress = (
                    1.0
                    if total == 0
                    else sum(
                        (
                            1.0
                            if str(value['state']) in ('ready', 'skipped')
                            or (
                                str(value['state']) == 'failed'
                                and not bool(value['retryable'])
                            )
                            else (
                                0.0
                                if str(value['state']) == 'failed'
                                else float(value['progress'])
                            )
                        )
                        for value in values
                    )
                    / total
                )
                connection.execute(
                    'UPDATE vainglory_archive_syncs SET progress=?,'
                    'discovered_count=?,completed_count=? WHERE account_id=?',
                    (progress, total, completed, account_id),
                )
            return counts

        counts = await self._database.write(purge)
        removed = sum(
            counts[name] for name in ('imports', 'part_jobs', 'ocr_jobs', 'matches')
        )
        if removed:
            logger.info(
                'Purged excluded Vainglory content: marker={!r} counts={}',
                EXCLUDED_TITLE_MARKER,
                counts,
            )
        return removed

    async def invalidate_outdated_results(self) -> int:
        now = self._now()
        obsolete_frame_paths: List[str] = []

        def invalidate(connection: sqlite3.Connection) -> int:
            session_rows = connection.execute(
                'SELECT DISTINCT match.session_id '
                'FROM vainglory_matches match '
                'LEFT JOIN vainglory_part_jobs job '
                'ON job.part_id=match.result_part_id '
                'WHERE job.part_id IS NULL OR job.algorithm_version<? '
                'UNION '
                'SELECT DISTINCT session_id FROM vainglory_part_jobs '
                'WHERE algorithm_version<?',
                (self.ALGORITHM_VERSION, self.ALGORITHM_VERSION),
            ).fetchall()
            import_rows = connection.execute(
                'SELECT DISTINCT import_id FROM vainglory_archive_parts '
                'WHERE recording_part_id IN('
                'SELECT part_id FROM vainglory_part_jobs '
                'WHERE algorithm_version<?)',
                (self.ALGORITHM_VERSION,),
            ).fetchall()
            obsolete_frame_paths.extend(
                str(row['result_frame_path'])
                for row in connection.execute(
                    'SELECT result_frame_path FROM vainglory_matches '
                    'WHERE result_frame_path IS NOT NULL AND NOT EXISTS('
                    'SELECT 1 FROM vainglory_part_jobs job '
                    'WHERE job.part_id=vainglory_matches.result_part_id '
                    'AND job.algorithm_version>=?)',
                    (self.ALGORITHM_VERSION,),
                ).fetchall()
            )
            deleted = connection.execute(
                'DELETE FROM vainglory_matches WHERE NOT EXISTS('
                'SELECT 1 FROM vainglory_part_jobs job '
                'WHERE job.part_id=vainglory_matches.result_part_id '
                'AND job.algorithm_version>=?)',
                (self.ALGORITHM_VERSION,),
            ).rowcount
            connection.execute(
                'DELETE FROM vainglory_ocr_jobs WHERE part_id IN('
                'SELECT part_id FROM vainglory_part_jobs WHERE algorithm_version<?)',
                (self.ALGORITHM_VERSION,),
            )
            connection.execute(
                "UPDATE vainglory_archive_parts SET state='queued',progress=0,"
                'error=NULL,updated_at=? WHERE recording_part_id IN('
                'SELECT part_id FROM vainglory_part_jobs '
                'WHERE algorithm_version<?)',
                (now, self.ALGORITHM_VERSION),
            )
            connection.execute(
                "UPDATE vainglory_part_jobs SET state='pending',progress=0,"
                'algorithm_version=?,match_count=0,error=NULL,started_at=NULL,'
                'completed_at=NULL,updated_at=? WHERE algorithm_version<?',
                (self.ALGORITHM_VERSION, now, self.ALGORITHM_VERSION),
            )
            for import_row in import_rows:
                import_id = int(import_row['import_id'])
                completed = int(
                    connection.execute(
                        'SELECT COUNT(*) FROM vainglory_archive_parts '
                        "WHERE import_id=? AND state='ready'",
                        (import_id,),
                    ).fetchone()[0]
                )
                page_count = int(
                    connection.execute(
                        'SELECT COUNT(*) FROM vainglory_archive_parts '
                        'WHERE import_id=?',
                        (import_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    "UPDATE vainglory_archive_imports SET state='analyzing',"
                    'progress=?,completed_page_count=?,error=NULL,'
                    "content_classification='unknown',classification_reason=NULL,"
                    'retryable=0,next_retry_at=NULL,updated_at=? WHERE id=?',
                    (
                        float(completed) / float(max(1, page_count)),
                        completed,
                        now,
                        import_id,
                    ),
                )
            for row in session_rows:
                self._ensure_scan_job(connection, int(row['session_id']), now)
                self._refresh_session_job(connection, int(row['session_id']), now)
                connection.execute(
                    'UPDATE vainglory_publications SET needs_refresh=1 '
                    'WHERE session_id=?',
                    (int(row['session_id']),),
                )
            return deleted

        deleted = await self._database.write(invalidate)
        self._remove_result_frame_files(obsolete_frame_paths)
        if deleted:
            logger.info(
                'Invalidated outdated Vainglory results: matches={} algorithm={}',
                deleted,
                self.ALGORITHM_VERSION,
            )
        return deleted

    async def apply_builtin_hero_labels(self) -> int:
        now = self._now()

        def apply(connection: sqlite3.Connection) -> int:
            updated = 0
            rows = connection.execute(
                "SELECT id,fingerprint FROM vainglory_heroes WHERE label=''"
            ).fetchall()
            for row in rows:
                label = identify_builtin_hero(str(row['fingerprint']))
                if label is None:
                    continue
                cursor = connection.execute(
                    "UPDATE vainglory_heroes SET label=?,updated_at=? "
                    "WHERE id=? AND label=''",
                    (label, now, int(row['id'])),
                )
                updated += cursor.rowcount
            return updated

        return await self._database.write(apply)

    async def consolidate_hero_catalog(self) -> int:
        now = self._now()

        def consolidate(connection: sqlite3.Connection) -> int:
            return self._consolidate_heroes(connection, now)

        return await self._database.write(consolidate)

    async def sync_hero_references(self, references: Sequence[HeroReference]) -> int:
        now = self._now()

        def sync(connection: sqlite3.Connection) -> int:
            changed = 0
            labels = tuple(reference.label for reference in references)
            for reference in references:
                row = connection.execute(
                    'SELECT id,fingerprint,thumbnail_png FROM vainglory_heroes '
                    'WHERE label=? COLLATE NOCASE ORDER BY id LIMIT 1',
                    (reference.label,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        'INSERT INTO vainglory_heroes('
                        'fingerprint,thumbnail_png,label,created_at,updated_at) '
                        'VALUES(?,?,?,?,?)',
                        (
                            reference.fingerprint,
                            reference.image_jpeg,
                            reference.label,
                            now,
                            now,
                        ),
                    )
                    changed += 1
                    continue
                if (
                    str(row['fingerprint']) == reference.fingerprint
                    and bytes(row['thumbnail_png']) == reference.image_jpeg
                ):
                    continue
                connection.execute(
                    'UPDATE vainglory_heroes SET fingerprint=?,thumbnail_png=?,'
                    'updated_at=? WHERE id=?',
                    (reference.fingerprint, reference.image_jpeg, now, int(row['id'])),
                )
                changed += 1
            if labels:
                allowed = ' OR '.join('label=? COLLATE NOCASE' for _label in labels)
                changed += connection.execute(
                    "DELETE FROM vainglory_heroes WHERE label='' OR NOT ({})".format(
                        allowed
                    ),
                    labels,
                ).rowcount
            return changed

        return await self._database.write(sync)

    async def request_scan(self, session_id: int) -> ScanJob:
        now = self._now()

        def request(connection: sqlite3.Connection) -> None:
            session = connection.execute(
                'SELECT session.state,session.deletion_state,session.title,'
                'job.policy_snapshot_json,migration.title AS migration_title,'
                'imported.title AS import_title '
                'FROM recording_sessions session '
                'LEFT JOIN upload_jobs job ON job.session_id=session.id '
                'LEFT JOIN archive_migration_items migration '
                'ON migration.session_id=session.id '
                'LEFT JOIN vainglory_archive_imports imported '
                'ON imported.session_id=session.id WHERE session.id=?',
                (int(session_id),),
            ).fetchone()
            if session is None:
                raise VaingloryNotFound('录播场次不存在')
            if is_excluded_title(
                session['title'],
                session['migration_title'],
                session['import_title'],
                self._upload_title(session['policy_snapshot_json']),
            ):
                raise VaingloryConflict('标题含“直播剪辑”，不进行对局识别')
            if (
                str(session['state']) in ('cancelled', 'skipped')
                or str(session['deletion_state']) != 'none'
            ):
                raise VaingloryConflict('只能分析可用且未删除的录播')
            part_rows = connection.execute(
                'SELECT id FROM recording_parts '
                "WHERE session_id=? AND artifact_state='ready' "
                'AND video_deleted_at IS NULL ORDER BY part_index,id',
                (int(session_id),),
            ).fetchall()
            if not part_rows:
                raise VaingloryConflict('该录播没有可分析的视频文件')
            analyzing = connection.execute(
                'SELECT 1 FROM vainglory_part_jobs '
                "WHERE session_id=? AND state='analyzing' LIMIT 1",
                (int(session_id),),
            ).fetchone()
            if analyzing is not None:
                raise VaingloryConflict('该录播正在分析')
            self._ensure_scan_job(connection, int(session_id), now)
            connection.executemany(
                'INSERT INTO vainglory_part_jobs('
                'part_id,session_id,state,request_kind,progress,algorithm_version,'
                'match_count,error,requested_at,started_at,completed_at,updated_at) '
                "VALUES(?,?,'pending','manual',0,?,0,NULL,?,NULL,NULL,?) "
                'ON CONFLICT(part_id) DO UPDATE SET '
                "state='pending',request_kind='manual',progress=0,"
                'algorithm_version=excluded.algorithm_version,match_count=0,'
                'error=NULL,requested_at=excluded.requested_at,started_at=NULL,'
                'completed_at=NULL,updated_at=excluded.updated_at',
                (
                    (int(part['id']), int(session_id), self.ALGORITHM_VERSION, now, now)
                    for part in part_rows
                ),
            )
            self._refresh_session_job(connection, int(session_id), now)

        await self._database.write(request)
        job = await self.get_job(session_id)
        assert job is not None
        return job

    async def discover_ready_parts(self) -> int:
        now = self._now()

        def discover(connection: sqlite3.Connection) -> int:
            rows = connection.execute(
                'SELECT part.id AS part_id,part.session_id,session.title,'
                'upload.policy_snapshot_json,'
                'migration.title AS migration_title,'
                'imported.title AS import_title '
                'FROM recording_parts part '
                'JOIN recording_sessions session ON session.id=part.session_id '
                'LEFT JOIN vainglory_part_jobs job ON job.part_id=part.id '
                'LEFT JOIN upload_jobs upload ON upload.session_id=session.id '
                'LEFT JOIN archive_migration_items migration '
                'ON migration.session_id=session.id '
                'LEFT JOIN vainglory_archive_imports imported '
                'ON imported.session_id=session.id '
                "WHERE part.artifact_state='ready' "
                'AND part.video_deleted_at IS NULL '
                "AND session.deletion_state='none' "
                "AND session.state NOT IN ('cancelled','skipped') "
                "AND instr(COALESCE(session.title,''),'直播剪辑')=0 "
                'AND (job.part_id IS NULL OR job.algorithm_version<?) '
                'ORDER BY part.created_at,part.id',
                (self.ALGORITHM_VERSION,),
            ).fetchall()
            touched: Dict[int, bool] = {}
            for row in rows:
                if is_excluded_title(
                    row['title'],
                    row['migration_title'],
                    row['import_title'],
                    self._upload_title(row['policy_snapshot_json']),
                ):
                    continue
                session_id = int(row['session_id'])
                self._ensure_scan_job(connection, session_id, now)
                connection.execute(
                    'INSERT INTO vainglory_part_jobs('
                    'part_id,session_id,state,request_kind,progress,'
                    'algorithm_version,match_count,error,requested_at,started_at,'
                    'completed_at,updated_at) '
                    "VALUES(?,?,'pending','automatic',0,?,0,NULL,?,NULL,NULL,?) "
                    'ON CONFLICT(part_id) DO UPDATE SET '
                    "state='pending',request_kind='automatic',progress=0,"
                    'algorithm_version=excluded.algorithm_version,match_count=0,'
                    'error=NULL,requested_at=excluded.requested_at,started_at=NULL,'
                    'completed_at=NULL,updated_at=excluded.updated_at',
                    (int(row['part_id']), session_id, self.ALGORITHM_VERSION, now, now),
                )
                touched[session_id] = True
            for session_id in touched:
                self._refresh_session_job(connection, session_id, now)
            return len(rows)

        return await self._database.write(discover)

    async def claim_next(self) -> Optional[ScanClaim]:
        await self.discover_ready_parts()
        now = self._now()
        recent_cutoff = max(1, now - self._REALTIME_WINDOW_SECONDS)
        season_start = current_season_started_at(now)

        def claim(connection: sqlite3.Connection) -> Optional[ScanClaim]:
            row = connection.execute(
                'SELECT job.part_id,job.session_id,part.part_index,'
                'part.source_path,part.final_path,session.title AS session_title,'
                "CASE WHEN job.request_kind='manual' THEN 0 "
                "WHEN session.state='open' THEN 1 "
                "WHEN (source.origin IS NULL OR source.origin!='archive') "
                'AND migration_item.id IS NULL AND session.started_at>=? THEN 2 '
                "WHEN session.started_at>=? AND (source.origin IS NULL OR ("
                "source.origin!='archive' AND source.cache_path IS NULL)) THEN 3 "
                'WHEN session.started_at>=? THEN 4 '
                'WHEN EXISTS(SELECT 1 FROM vainglory_publications publication '
                'WHERE publication.session_id=job.session_id '
                'AND publication.needs_refresh=1) THEN 5 '
                "WHEN source.origin IS NULL OR (source.origin!='archive' "
                'AND source.cache_path IS NULL) THEN 6 '
                'ELSE 7 END AS priority '
                'FROM vainglory_part_jobs job '
                'JOIN recording_parts part ON part.id=job.part_id '
                'JOIN recording_sessions session ON session.id=job.session_id '
                'LEFT JOIN vainglory_video_sources source '
                'ON source.part_id=part.id '
                'LEFT JOIN vainglory_archive_parts archive_part '
                'ON archive_part.recording_part_id=part.id '
                'LEFT JOIN vainglory_archive_imports archive_import '
                'ON archive_import.id=archive_part.import_id '
                'LEFT JOIN vainglory_archive_syncs archive_sync '
                'ON archive_sync.account_id=archive_import.account_id '
                'LEFT JOIN archive_migration_items migration_item '
                'ON migration_item.session_id=session.id '
                "WHERE job.state='pending' AND part.artifact_state='ready' "
                'AND part.video_deleted_at IS NULL '
                "AND session.deletion_state='none' "
                "AND instr(COALESCE(session.title,''),'直播剪辑')=0 "
                "AND (source.origin IS NULL OR source.origin!='archive' "
                'OR COALESCE(archive_sync.operator_paused,0)=0) '
                'AND (archive_part.import_id IS NULL OR NOT EXISTS('
                'SELECT 1 FROM vainglory_archive_parts sibling_archive '
                'WHERE sibling_archive.import_id=archive_part.import_id '
                "AND sibling_archive.state NOT IN ('analyzing','ready'))) "
                'ORDER BY priority,'
                'CASE WHEN priority>=3 THEN COALESCE('
                'archive_import.recording_started_at,'
                'archive_import.published_at,migration_item.published_at,'
                'session.started_at) END DESC,'
                'job.session_id,part.part_index,part.created_at,job.part_id LIMIT 1',
                (recent_cutoff, season_start, season_start),
            ).fetchone()
            if row is None:
                return None
            session_id = int(row['session_id'])
            part_id = int(row['part_id'])
            cursor = connection.execute(
                "UPDATE vainglory_part_jobs SET state='analyzing',progress=0,"
                'error=NULL,started_at=?,completed_at=NULL,updated_at=? '
                "WHERE part_id=? AND state='pending'",
                (now, now, part_id),
            )
            if cursor.rowcount != 1:
                return None
            self._refresh_session_job(connection, session_id, now)
            part = VideoPart(
                id=part_id,
                index=int(row['part_index']),
                path=str(
                    row['final_path']
                    if row['final_path'] is not None
                    else row['source_path']
                ),
                title=str(row['session_title'] or ''),
            )
            return ScanClaim(
                session_id=session_id, part=part, realtime=int(row['priority']) <= 2
            )

        return await self._database.write(claim)

    async def enqueue_ocr(self, part_id: int, scanned: ScannedPart) -> None:
        if not scanned.candidate_times_ms:
            raise ValueError('OCR queue needs at least one result candidate')
        now = self._now()
        contexts = (
            scanned.candidate_view_contexts
            if len(scanned.candidate_view_contexts) == len(scanned.candidate_times_ms)
            else tuple('unknown' for _ in scanned.candidate_times_ms)
        )
        hero_lineups = (
            scanned.candidate_hero_lineups
            if len(scanned.candidate_hero_lineups) == len(scanned.candidate_times_ms)
            else tuple(() for _ in scanned.candidate_times_ms)
        )
        candidate_times_json = json.dumps(
            tuple(
                {
                    'at_ms': at_ms,
                    'view_context': view_context,
                    'hero_lineup': hero_lineup,
                }
                for at_ms, view_context, hero_lineup in zip(
                    scanned.candidate_times_ms, contexts, hero_lineups
                )
            ),
            separators=(',', ':'),
        )

        def enqueue(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session_id,state FROM vainglory_part_jobs WHERE part_id=?',
                (int(part_id),),
            ).fetchone()
            if row is None:
                raise VaingloryNotFound('分析任务不存在')
            if str(row['state']) != 'analyzing':
                raise VaingloryConflict('分析任务当前不能进入 OCR 队列')
            session_id = int(row['session_id'])
            connection.execute(
                'INSERT INTO vainglory_ocr_jobs('
                'part_id,session_id,state,video_duration_ms,'
                'candidate_times_json,candidate_count,requested_at,started_at,'
                'updated_at) VALUES(?,?,\'pending\',?,?,?,?,NULL,?) '
                'ON CONFLICT(part_id) DO UPDATE SET '
                'session_id=excluded.session_id,state=\'pending\','
                'video_duration_ms=excluded.video_duration_ms,'
                'candidate_times_json=excluded.candidate_times_json,'
                'candidate_count=excluded.candidate_count,'
                'requested_at=excluded.requested_at,started_at=NULL,'
                'updated_at=excluded.updated_at',
                (
                    int(part_id),
                    session_id,
                    int(scanned.video_duration_ms),
                    candidate_times_json,
                    len(scanned.candidate_times_ms),
                    now,
                    now,
                ),
            )
            connection.execute(
                'UPDATE vainglory_part_jobs SET progress=0.7,error=NULL,'
                'updated_at=? WHERE part_id=? AND state=\'analyzing\'',
                (now, int(part_id)),
            )
            self._refresh_session_job(connection, session_id, now)

        await self._database.write(enqueue)

    async def claim_next_ocr(self) -> Optional[OcrClaim]:
        now = self._now()
        recent_cutoff = max(1, now - self._REALTIME_WINDOW_SECONDS)

        def claim(connection: sqlite3.Connection) -> Optional[OcrClaim]:
            row = connection.execute(
                'SELECT ocr.part_id,ocr.session_id,ocr.video_duration_ms,'
                'ocr.candidate_times_json,part.part_index,part.source_path,'
                'part.final_path,session.title AS session_title,'
                "CASE WHEN job.request_kind='manual' THEN 0 "
                "WHEN session.state='open' THEN 1 "
                "WHEN (source.origin IS NULL OR source.origin!='archive') "
                'AND migration_item.id IS NULL AND session.started_at>=? THEN 2 '
                'WHEN EXISTS(SELECT 1 FROM vainglory_publications publication '
                'WHERE publication.session_id=job.session_id '
                'AND publication.needs_refresh=1) THEN 3 '
                "WHEN source.origin='archive' THEN 4 ELSE 5 END AS priority "
                'FROM vainglory_ocr_jobs ocr '
                'JOIN vainglory_part_jobs job ON job.part_id=ocr.part_id '
                'JOIN recording_parts part ON part.id=ocr.part_id '
                'JOIN recording_sessions session ON session.id=ocr.session_id '
                'LEFT JOIN vainglory_video_sources source '
                'ON source.part_id=part.id '
                'LEFT JOIN vainglory_archive_parts archive_part '
                'ON archive_part.recording_part_id=part.id '
                'LEFT JOIN vainglory_archive_imports archive_import '
                'ON archive_import.id=archive_part.import_id '
                'LEFT JOIN vainglory_archive_syncs archive_sync '
                'ON archive_sync.account_id=archive_import.account_id '
                'LEFT JOIN archive_migration_items migration_item '
                'ON migration_item.session_id=session.id '
                "WHERE ocr.state='pending' AND job.state='analyzing' "
                "AND part.artifact_state='ready' "
                'AND part.video_deleted_at IS NULL '
                "AND session.deletion_state='none' "
                "AND instr(COALESCE(session.title,''),'直播剪辑')=0 "
                "AND (source.origin IS NULL OR source.origin!='archive' "
                'OR COALESCE(archive_sync.operator_paused,0)=0) '
                'ORDER BY priority,'
                'CASE WHEN priority>=3 THEN COALESCE('
                'archive_import.published_at,migration_item.published_at,'
                'session.started_at) END DESC,'
                'ocr.session_id,part.part_index,ocr.requested_at,ocr.part_id LIMIT 1',
                (recent_cutoff,),
            ).fetchone()
            if row is None:
                return None
            part_id = int(row['part_id'])
            cursor = connection.execute(
                "UPDATE vainglory_ocr_jobs SET state='running',started_at=?,"
                "updated_at=? WHERE part_id=? AND state='pending'",
                (now, now, part_id),
            )
            if cursor.rowcount != 1:
                return None
            raw_times = json.loads(str(row['candidate_times_json']))
            candidate_times = tuple(
                int(value['at_ms']) if isinstance(value, dict) else int(value)
                for value in raw_times
            )
            candidate_contexts = tuple(
                (
                    str(value.get('view_context', 'unknown'))
                    if isinstance(value, dict)
                    and str(value.get('view_context', 'unknown'))
                    in ('played', 'observed', 'unknown')
                    else 'unknown'
                )
                for value in raw_times
            )
            candidate_hero_lineups = tuple(
                (
                    tuple(str(label) for label in value.get('hero_lineup', ()))
                    if isinstance(value, dict)
                    and isinstance(value.get('hero_lineup', ()), (list, tuple))
                    else ()
                )
                for value in raw_times
            )
            return OcrClaim(
                session_id=int(row['session_id']),
                part=VideoPart(
                    id=part_id,
                    index=int(row['part_index']),
                    path=str(
                        row['final_path']
                        if row['final_path'] is not None
                        else row['source_path']
                    ),
                    title=str(row['session_title'] or ''),
                ),
                scanned=ScannedPart(
                    video_duration_ms=int(row['video_duration_ms']),
                    candidate_times_ms=candidate_times,
                    candidate_view_contexts=cast(
                        Tuple[Literal['played', 'observed', 'unknown'], ...],
                        candidate_contexts,
                    ),
                    candidate_hero_lineups=candidate_hero_lineups,
                ),
            )

        return await self._database.write(claim)

    async def update_ocr_progress(self, part_id: int, progress: float) -> None:
        bounded = max(0.0, min(0.99, float(progress)))
        overall_progress = 0.7 + bounded * 0.29
        now = self._now()

        def update(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session_id FROM vainglory_ocr_jobs WHERE part_id=? '
                "AND state='running'",
                (int(part_id),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                'UPDATE vainglory_ocr_jobs SET updated_at=? WHERE part_id=?',
                (now, int(part_id)),
            )
            connection.execute(
                'UPDATE vainglory_part_jobs SET progress=?,updated_at=? '
                "WHERE part_id=? AND state='analyzing'",
                (overall_progress, now, int(part_id)),
            )
            self._refresh_session_job(connection, int(row['session_id']), now)

        await self._database.write(update)

    async def requeue_ocr(self, part_id: int) -> None:
        now = self._now()

        def requeue(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session_id FROM vainglory_ocr_jobs WHERE part_id=?',
                (int(part_id),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE vainglory_ocr_jobs SET state='pending',started_at=NULL,"
                'updated_at=? WHERE part_id=?',
                (now, int(part_id)),
            )
            connection.execute(
                'UPDATE vainglory_part_jobs SET progress=0.7,error=NULL,'
                "updated_at=? WHERE part_id=? AND state='analyzing'",
                (now, int(part_id)),
            )
            self._refresh_session_job(connection, int(row['session_id']), now)

        await self._database.write(requeue)

    async def historical_part_paused(self, part_id: int) -> bool:
        return bool(
            await self._database.scalar(
                'SELECT COALESCE(sync.operator_paused,0) '
                'FROM vainglory_archive_parts archive '
                'JOIN vainglory_archive_imports imported '
                'ON imported.id=archive.import_id '
                'JOIN vainglory_archive_syncs sync '
                'ON sync.account_id=imported.account_id '
                'WHERE archive.recording_part_id=?',
                (int(part_id),),
            )
            or False
        )

    async def has_realtime_pending(self) -> bool:
        recent_cutoff = max(1, self._now() - self._REALTIME_WINDOW_SECONDS)
        value = await self._database.scalar(
            'SELECT EXISTS('
            'SELECT 1 FROM vainglory_part_jobs job '
            'JOIN recording_parts part ON part.id=job.part_id '
            'JOIN recording_sessions session ON session.id=job.session_id '
            'LEFT JOIN vainglory_video_sources source ON source.part_id=part.id '
            'LEFT JOIN archive_migration_items migration_item '
            'ON migration_item.session_id=session.id '
            "WHERE job.state='pending' AND part.artifact_state='ready' "
            'AND part.video_deleted_at IS NULL '
            "AND session.deletion_state='none' "
            "AND instr(COALESCE(session.title,''),'直播剪辑')=0 AND ("
            "job.request_kind='manual' OR session.state='open' OR ("
            "(source.origin IS NULL OR source.origin!='archive') "
            'AND migration_item.id IS NULL AND session.started_at>=?)))',
            (recent_cutoff,),
        )
        return bool(value)

    async def analysis_queue_status(self, *, limit: int = 8) -> AnalysisQueueStatus:
        if not 1 <= limit <= 20:
            raise ValueError('analysis queue limit must be between 1 and 20')
        now = self._now()
        recent_cutoff = max(1, now - self._REALTIME_WINDOW_SECONDS)
        season_start = current_season_started_at(now)
        category_rank_sql = (
            "CASE WHEN job.request_kind='manual' THEN 0 "
            "WHEN session.state='open' OR ((source.origin IS NULL "
            "OR source.origin!='archive') AND migration_item.id IS NULL "
            'AND session.started_at>=?) THEN 1 '
            "WHEN source.origin='archive' THEN 2 "
            'WHEN migration_item.id IS NOT NULL THEN 3 ELSE 4 END'
        )
        priority_sql = (
            "CASE WHEN job.request_kind='manual' THEN 0 "
            "WHEN session.state='open' THEN 1 "
            "WHEN (source.origin IS NULL OR source.origin!='archive') "
            'AND migration_item.id IS NULL AND session.started_at>=? THEN 2 '
            "WHEN session.started_at>=? AND (source.origin IS NULL OR ("
            "source.origin!='archive' AND source.cache_path IS NULL)) THEN 3 "
            'WHEN session.started_at>=? THEN 4 '
            'WHEN EXISTS(SELECT 1 FROM vainglory_publications publication '
            'WHERE publication.session_id=job.session_id '
            'AND publication.needs_refresh=1) THEN 5 '
            "WHEN source.origin IS NULL OR (source.origin!='archive' "
            'AND source.cache_path IS NULL) THEN 6 '
            'ELSE 7 END'
        )
        joins = (
            ' FROM vainglory_part_jobs job '
            'JOIN recording_parts part ON part.id=job.part_id '
            'JOIN recording_sessions session ON session.id=job.session_id '
            'LEFT JOIN vainglory_scan_jobs session_job '
            'ON session_job.session_id=job.session_id '
            'LEFT JOIN vainglory_ocr_jobs ocr ON ocr.part_id=job.part_id '
            'LEFT JOIN vainglory_video_sources source ON source.part_id=part.id '
            'LEFT JOIN vainglory_archive_parts archive_part '
            'ON archive_part.recording_part_id=part.id '
            'LEFT JOIN vainglory_archive_imports archive_import '
            'ON archive_import.id=archive_part.import_id '
            'LEFT JOIN vainglory_archive_syncs archive_sync '
            'ON archive_sync.account_id=archive_import.account_id '
            'LEFT JOIN archive_migration_items migration_item '
            'ON migration_item.session_id=session.id '
        )
        claimable = (
            " AND part.artifact_state='ready' AND part.video_deleted_at IS NULL "
            "AND session.deletion_state='none' "
            "AND instr(COALESCE(session.title,''),'直播剪辑')=0 "
            "AND (source.origin IS NULL OR source.origin!='archive' "
            'OR COALESCE(archive_sync.operator_paused,0)=0) '
            'AND (archive_part.import_id IS NULL OR NOT EXISTS('
            'SELECT 1 FROM vainglory_archive_parts sibling_archive '
            'WHERE sibling_archive.import_id=archive_part.import_id '
            "AND sibling_archive.state NOT IN ('analyzing','ready'))) "
        )
        active_predicate = (
            "job.state='analyzing' AND (ocr.state='running' "
            'OR ocr.part_id IS NULL) '
            "AND instr(COALESCE(session.title,''),'直播剪辑')=0"
        )
        no_active_sibling = (
            ' AND NOT EXISTS(SELECT 1 FROM vainglory_part_jobs active_job '
            'LEFT JOIN vainglory_ocr_jobs active_ocr '
            'ON active_ocr.part_id=active_job.part_id '
            'WHERE active_job.session_id=job.session_id '
            "AND active_job.state='analyzing' AND (active_ocr.state='running' "
            'OR active_ocr.part_id IS NULL))'
        )
        task_progress = 'COALESCE(MAX(session_job.progress),AVG(job.progress))'
        part_count = (
            '(SELECT COUNT(*) FROM vainglory_part_jobs all_job '
            'WHERE all_job.session_id=job.session_id)'
        )
        completed_part_count = (
            '(SELECT COUNT(*) FROM vainglory_part_jobs completed_job '
            'WHERE completed_job.session_id=job.session_id '
            "AND completed_job.state='ready')"
        )
        active_select = (
            'SELECT COALESCE(MIN(CASE WHEN ocr.state=\'running\' '
            'THEN job.part_id END),MIN(job.part_id)) AS part_id,'
            'job.session_id,COALESCE(MIN(CASE WHEN ocr.state=\'running\' '
            'THEN part.part_index END),MIN(part.part_index)) AS part_index,'
            'MAX(session.title) AS title,MAX(session.anchor_name) AS anchor_name,'
            "'analyzing' AS state,CASE WHEN SUM(CASE WHEN ocr.state='running' "
            "THEN 1 ELSE 0 END)>0 THEN 'ocr_recognition' "
            "ELSE 'video_scan' END AS stage,MIN("
            + category_rank_sql
            + ') AS category_rank,'
            + task_progress
            + ' AS progress,MIN(job.requested_at) AS requested_at,'
            'MIN(job.started_at) AS started_at,MAX(job.updated_at) AS updated_at,'
            + part_count
            + ' AS part_count,'
            + completed_part_count
            + ' AS completed_part_count'
            + joins
            + ' WHERE '
            + active_predicate
            + ' GROUP BY job.session_id ORDER BY MIN(job.started_at),job.session_id'
        )
        queued_select = (
            'SELECT COALESCE(MIN(CASE WHEN ocr.state=\'pending\' '
            'THEN job.part_id END),MIN(job.part_id)) AS part_id,'
            'job.session_id,COALESCE(MIN(CASE WHEN ocr.state=\'pending\' '
            'THEN part.part_index END),MIN(part.part_index)) AS part_index,'
            'MAX(session.title) AS title,MAX(session.anchor_name) AS anchor_name,'
            "CASE WHEN SUM(CASE WHEN job.state='analyzing' THEN 1 ELSE 0 END)>0 "
            "THEN 'analyzing' ELSE 'pending' END AS state,"
            "CASE WHEN SUM(CASE WHEN ocr.state='pending' THEN 1 ELSE 0 END)>0 "
            "THEN 'ocr_waiting' ELSE 'video_scan' END AS stage,MIN("
            + category_rank_sql
            + ') AS category_rank,MIN('
            + priority_sql
            + ') AS priority,'
            + task_progress
            + ' AS progress,MIN(job.requested_at) AS requested_at,'
            'MIN(job.started_at) AS started_at,MAX(job.updated_at) AS updated_at,'
            + part_count
            + ' AS part_count,'
            + completed_part_count
            + ' AS completed_part_count,'
            'MAX(COALESCE(archive_import.recording_started_at,'
            'archive_import.published_at,'
            'migration_item.published_at,session.started_at)) AS sort_time'
            + joins
            + " WHERE (job.state='pending' OR (job.state='analyzing' "
            "AND ocr.state='pending'))"
            + claimable
            + no_active_sibling
            + ' GROUP BY job.session_id'
        )
        category_names = {
            0: 'manual',
            1: 'realtime',
            2: 'archive',
            3: 'migration',
            4: 'backlog',
        }

        def read(connection: sqlite3.Connection) -> AnalysisQueueStatus:
            active_rows = connection.execute(active_select, (recent_cutoff,)).fetchall()
            count_rows = connection.execute(
                'SELECT category_rank,COUNT(*) AS count FROM ('
                + queued_select
                + ') GROUP BY category_rank',
                (recent_cutoff, recent_cutoff, season_start, season_start),
            ).fetchall()
            queued_rows = connection.execute(
                queued_select + ' ORDER BY priority,sort_time DESC,2 LIMIT ?',
                (recent_cutoff, recent_cutoff, season_start, season_start, limit),
            ).fetchall()
            counts = {
                category_names[int(row['category_rank'])]: int(row['count'])
                for row in count_rows
            }

            def item(row: sqlite3.Row) -> AnalysisQueueItem:
                return AnalysisQueueItem(
                    part_id=int(row['part_id']),
                    session_id=int(row['session_id']),
                    part_index=int(row['part_index']),
                    title=str(row['title'] or ''),
                    anchor_name=str(row['anchor_name'] or ''),
                    state=str(row['state']),
                    stage=str(row['stage']),
                    category=category_names[int(row['category_rank'])],
                    progress=float(row['progress']),
                    requested_at=int(row['requested_at']),
                    started_at=(
                        None if row['started_at'] is None else int(row['started_at'])
                    ),
                    updated_at=int(row['updated_at']),
                    part_count=int(row['part_count']),
                    completed_part_count=int(row['completed_part_count']),
                )

            return AnalysisQueueStatus(
                active=tuple(item(row) for row in active_rows),
                queued=tuple(item(row) for row in queued_rows),
                pending_count=sum(counts.values()),
                manual_pending=counts.get('manual', 0),
                realtime_pending=counts.get('realtime', 0),
                archive_pending=counts.get('archive', 0),
                migration_pending=counts.get('migration', 0),
                backlog_pending=counts.get('backlog', 0),
            )

        return await self._database.read(read)

    async def index_summary(self) -> IndexSummary:
        def read(connection: sqlite3.Connection) -> IndexSummary:
            totals = connection.execute(
                'SELECT COUNT(*) AS match_count,'
                'COUNT(DISTINCT match.session_id) AS session_count,'
                'COUNT(DISTINCT CASE WHEN TRIM(session.anchor_name)<>\'\' '
                'THEN LOWER(TRIM(session.anchor_name)) END) AS anchor_count,'
                'COUNT(DISTINCT CASE WHEN TRIM(session.anchor_name)=\'\' '
                'THEN session.id END) AS unassigned_session_count '
                'FROM vainglory_matches match '
                'JOIN recording_sessions session ON session.id=match.session_id'
            ).fetchone()
            outcomes = connection.execute(
                'SELECT '
                "SUM(CASE WHEN winner_color='teal' THEN 1 ELSE 0 END) AS wins,"
                "SUM(CASE WHEN winner_color='orange' THEN 1 ELSE 0 END) AS losses,"
                "SUM(CASE WHEN winner_color='unknown' THEN 1 ELSE 0 END) AS unknowns "
                'FROM (SELECT CASE match.winner_side '
                "WHEN 'left' THEN match.left_color "
                "WHEN 'right' THEN match.right_color ELSE 'unknown' END "
                'AS winner_color FROM vainglory_matches match '
                'JOIN vainglory_scan_jobs scan ON scan.session_id=match.session_id '
                'WHERE scan.stats_included=1 AND match.stats_eligible=1)'
            ).fetchone()
            heroes = connection.execute(
                'SELECT COUNT(*) AS player_slots,'
                'SUM(CASE WHEN hero_id IS NOT NULL THEN 1 ELSE 0 END) '
                'AS recognized_heroes FROM vainglory_match_players'
            ).fetchone()
            assert totals is not None and outcomes is not None and heroes is not None
            return IndexSummary(
                match_count=int(totals['match_count']),
                session_count=int(totals['session_count']),
                anchor_count=int(totals['anchor_count']),
                unassigned_session_count=int(totals['unassigned_session_count']),
                win_count=int(outcomes['wins'] or 0),
                loss_count=int(outcomes['losses'] or 0),
                unknown_count=int(outcomes['unknowns'] or 0),
                player_slot_count=int(heroes['player_slots']),
                recognized_hero_count=int(heroes['recognized_heroes'] or 0),
            )

        return await self._database.read(read)

    async def requeue(self, part_id: int) -> None:
        now = self._now()

        def requeue(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session_id FROM vainglory_part_jobs WHERE part_id=?',
                (int(part_id),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                'DELETE FROM vainglory_ocr_jobs WHERE part_id=?', (int(part_id),)
            )
            connection.execute(
                "UPDATE vainglory_part_jobs SET state='pending',progress=0,"
                'error=NULL,started_at=NULL,completed_at=NULL,updated_at=? '
                "WHERE part_id=? AND state='analyzing'",
                (now, int(part_id)),
            )
            self._refresh_session_job(connection, int(row['session_id']), now)

        await self._database.write(requeue)

    async def update_progress(self, part_id: int, progress: float) -> None:
        bounded = max(0.0, min(0.99, float(progress)))
        now = self._now()

        def update(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session_id FROM vainglory_part_jobs WHERE part_id=?',
                (int(part_id),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                'UPDATE vainglory_part_jobs SET progress=?,updated_at=? '
                "WHERE part_id=? AND state='analyzing'",
                (bounded, now, int(part_id)),
            )
            self._refresh_session_job(connection, int(row['session_id']), now)

        await self._database.write(update)

    async def fail(self, part_id: int, error: str) -> None:
        message = error.strip()[:500] or '分析失败'
        now = self._now()

        def fail(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session_id FROM vainglory_part_jobs WHERE part_id=?',
                (int(part_id),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                'DELETE FROM vainglory_ocr_jobs WHERE part_id=?', (int(part_id),)
            )
            connection.execute(
                "UPDATE vainglory_part_jobs SET state='failed',progress=0,error=?,"
                'completed_at=?,updated_at=? '
                "WHERE part_id=? AND state='analyzing'",
                (message, now, now, int(part_id)),
            )
            self._refresh_session_job(connection, int(row['session_id']), now)

        await self._database.write(fail)

    async def complete_part(
        self, part_id: int, matches: Sequence[AnalyzedMatch]
    ) -> None:
        now = self._now()
        written_paths: List[Path] = []
        obsolete_frame_paths: List[str] = []

        def complete(connection: sqlite3.Connection) -> None:
            job = connection.execute(
                'SELECT state,session_id FROM vainglory_part_jobs WHERE part_id=?',
                (int(part_id),),
            ).fetchone()
            if job is None:
                raise VaingloryNotFound('分析任务不存在')
            if str(job['state']) != 'analyzing':
                raise VaingloryConflict('分析任务当前不能写入结果')
            session_id = int(job['session_id'])
            manual_hero_overrides = {
                (int(row['result_at_ms']), str(row['side']), int(row['slot'])): int(
                    row['hero_id']
                )
                for row in connection.execute(
                    'SELECT match.result_at_ms,player.side,player.slot,'
                    'player.hero_id FROM vainglory_matches match '
                    'JOIN vainglory_match_players player '
                    'ON player.match_id=match.id '
                    'WHERE match.result_part_id=? '
                    "AND player.hero_source='manual' "
                    'AND player.hero_id IS NOT NULL',
                    (int(part_id),),
                ).fetchall()
            }
            used_manual_overrides: Set[Tuple[int, str, int]] = set()
            obsolete_frame_paths.extend(
                str(row['result_frame_path'])
                for row in connection.execute(
                    'SELECT result_frame_path FROM vainglory_matches '
                    'WHERE result_part_id=? AND result_frame_path IS NOT NULL',
                    (int(part_id),),
                ).fetchall()
            )
            connection.execute(
                'DELETE FROM vainglory_matches WHERE result_part_id=?', (int(part_id),)
            )
            heroes = self._existing_heroes(connection)
            for match in matches:
                if int(match.part_id) != int(part_id):
                    raise VaingloryConflict('结算页不属于当前分 P')
                hero_ids: Dict[Tuple[str, int], Optional[int]] = {}
                for hero in match.heroes:
                    hero_ids[(hero.side, hero.slot)] = self._resolve_hero(
                        connection, hero, heroes, now
                    )
                header = match.ocr.header
                team_size = max(
                    (player.slot for player in match.ocr.players), default=0
                )
                normalized_team_size = team_size if 1 <= team_size <= 5 else None
                recorded_player = (
                    match.recorded_player if normalized_team_size in (3, 5) else None
                )
                game_mode = (
                    match.game_mode
                    if match.game_mode in ('aram', 'other')
                    else (
                        '3v3'
                        if normalized_team_size == 3
                        else '5v5' if normalized_team_size == 5 else 'unknown'
                    )
                )
                match_kind = (
                    match.match_kind
                    if match.match_kind in ('pvp', 'bot', 'practice')
                    else 'unknown'
                )
                view_context = (
                    match.view_context
                    if match.view_context in ('played', 'observed')
                    else 'unknown'
                )
                stats_eligible = bool(match.stats_eligible)
                stats_exclusion_reason = (
                    None
                    if stats_eligible
                    else match.stats_exclusion_reason.strip()[:64] or 'classification'
                )
                started_at_ms = max(
                    0, match.result_at_ms - (header.duration_seconds or 0) * 1_000
                )
                result_frame_path: Optional[str] = None
                if match.result_frame_png:
                    result_frame_path = self._result_frame_relative_path(
                        session_id=session_id,
                        part_id=part_id,
                        result_at_ms=match.result_at_ms,
                        content=match.result_frame_png,
                    )
                    destination = self._resolve_result_frame_path(result_frame_path)
                    self._write_result_frame(destination, match.result_frame_png)
                    written_paths.append(destination)
                cursor = connection.execute(
                    'INSERT INTO vainglory_matches('
                    'session_id,result_part_id,result_at_ms,duration_seconds,'
                    'result_text,end_reason,left_color,right_color,winner_side,'
                    'left_kills,right_kills,left_economy,right_economy,confidence,'
                    'created_at,game_mode,team_size,started_at_ms,'
                    'result_frame_path,hero_recognition_version,'
                    'recorded_player_side,recorded_player_slot,'
                    'recorded_player_confidence,'
                    'recorded_player_detection_version,match_kind,view_context,'
                    'stats_eligible,stats_exclusion_reason) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        session_id,
                        match.part_id,
                        match.result_at_ms,
                        header.duration_seconds,
                        header.result_text,
                        header.end_reason,
                        match.layout.left_color,
                        match.layout.right_color,
                        match.layout.winner_side,
                        header.left_kills,
                        header.right_kills,
                        header.left_economy,
                        header.right_economy,
                        match.confidence,
                        now,
                        game_mode,
                        normalized_team_size,
                        started_at_ms,
                        result_frame_path,
                        self.HERO_RECOGNITION_VERSION,
                        (None if recorded_player is None else recorded_player.side),
                        (None if recorded_player is None else recorded_player.slot),
                        (
                            None
                            if recorded_player is None
                            else recorded_player.confidence
                        ),
                        self.RECORDED_PLAYER_DETECTION_VERSION,
                        match_kind,
                        view_context,
                        1 if stats_eligible else 0,
                        stats_exclusion_reason,
                    ),
                )
                match_id = int(cursor.lastrowid)
                for player in match.ocr.players:
                    stats = player.stats
                    override_key = (
                        int(match.result_at_ms),
                        player.side,
                        int(player.slot),
                    )
                    manual_hero_id = manual_hero_overrides.get(override_key)
                    if manual_hero_id is None:
                        nearby_overrides = (
                            (abs(result_at_ms - int(match.result_at_ms)), key, hero_id)
                            for key, hero_id in manual_hero_overrides.items()
                            for result_at_ms, side, slot in (key,)
                            if key not in used_manual_overrides
                            and side == player.side
                            and slot == int(player.slot)
                            and abs(result_at_ms - int(match.result_at_ms)) <= 30_000
                        )
                        nearest = min(nearby_overrides, default=None)
                        if nearest is not None:
                            _, override_key, manual_hero_id = nearest
                    if manual_hero_id is not None:
                        used_manual_overrides.add(override_key)
                    connection.execute(
                        'INSERT INTO vainglory_match_players('
                        'match_id,side,slot,player_name,normalized_name,hero_id,'
                        'hero_source,kills,deaths,assists,economy,last_hits,'
                        'confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (
                            match_id,
                            player.side,
                            player.slot,
                            player.name,
                            player.normalized_name,
                            (
                                manual_hero_id
                                if manual_hero_id is not None
                                else hero_ids.get((player.side, player.slot))
                            ),
                            ('manual' if manual_hero_id is not None else 'automatic'),
                            stats.kills,
                            stats.deaths,
                            stats.assists,
                            stats.economy,
                            stats.last_hits,
                            player.confidence,
                        ),
                    )
            if matches:
                self._ensure_session_player(connection, session_id, now)
            connection.execute(
                "UPDATE vainglory_part_jobs SET state='ready',progress=1,"
                'match_count=?,error=NULL,completed_at=?,updated_at=? '
                'WHERE part_id=?',
                (len(matches), now, now, int(part_id)),
            )
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (session_id,),
            )
            connection.execute(
                'DELETE FROM vainglory_ocr_jobs WHERE part_id=?', (int(part_id),)
            )
            self._consolidate_heroes(connection, now)
            self._refresh_session_job(connection, session_id, now)

        await self._database.write(complete)
        self._remove_result_frame_files(obsolete_frame_paths, keep=written_paths)
        if written_paths:
            logger.info(
                'Vainglory result frames stored: part_id={} frames={} directory={}',
                part_id,
                len(written_paths),
                self._result_frame_root,
            )

    async def get_job(self, session_id: int) -> Optional[ScanJob]:
        row = await self._database.fetchone(
            'SELECT * FROM vainglory_scan_jobs WHERE session_id=?', (int(session_id),)
        )
        return None if row is None else self._scan_job(row)

    async def list_matches(
        self,
        *,
        player_name: str = '',
        hero_ids: Sequence[int] = (),
        winner_color: Optional[str] = None,
        end_reason: Optional[str] = None,
        game_mode: Optional[str] = None,
        session_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MatchPage:
        if limit < 1 or limit > 100:
            raise ValueError('limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('offset must not be negative')
        where, parameters = self._match_filters(
            player_name=player_name,
            hero_ids=hero_ids,
            winner_color=winner_color,
            end_reason=end_reason,
            game_mode=game_mode,
            session_id=session_id,
        )
        where_sql = ' AND '.join(where)
        total = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM vainglory_matches match WHERE ' + where_sql,
                tuple(parameters),
            )
        )
        rows = await self._database.fetchall(
            self._MATCH_SELECT
            + 'WHERE '
            + where_sql
            + ' ORDER BY session.started_at DESC,part.part_index DESC,'
            'match.result_at_ms DESC,match.id DESC LIMIT ? OFFSET ?',
            tuple(parameters) + (limit, offset),
        )
        return MatchPage(total=total, items=await self._hydrate_matches(rows))

    async def list_match_sessions(
        self,
        *,
        player_name: str = '',
        hero_ids: Sequence[int] = (),
        winner_color: Optional[str] = None,
        end_reason: Optional[str] = None,
        game_mode: Optional[str] = None,
        session_id: Optional[int] = None,
        source_title: str = '',
        anchor_name: Optional[str] = None,
        stats_included: Optional[bool] = None,
        sort_by: str = 'analyzed',
        limit: int = 20,
        offset: int = 0,
    ) -> MatchSessionPage:
        if limit < 1 or limit > 100:
            raise ValueError('limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('offset must not be negative')
        if sort_by not in ('analyzed', 'started'):
            raise ValueError('sort_by must be analyzed or started')
        where, parameters = self._match_filters(
            player_name=player_name,
            hero_ids=hero_ids,
            winner_color=winner_color,
            end_reason=end_reason,
            game_mode=game_mode,
            session_id=session_id,
        )
        conditions = [
            'EXISTS(SELECT 1 FROM vainglory_matches match '
            'WHERE match.session_id=session.id AND ' + ' AND '.join(where) + ')'
        ]
        session_parameters: List[object] = list(parameters)
        normalized_title = source_title.strip()
        if normalized_title:
            escaped_title = (
                normalized_title.replace('\\', '\\\\')
                .replace('%', '\\%')
                .replace('_', '\\_')
            )
            conditions.append("session.title LIKE ? ESCAPE '\\'")
            session_parameters.append('%{}%'.format(escaped_title))
        if anchor_name is not None:
            normalized_anchor = anchor_name.strip()
            if normalized_anchor:
                conditions.append('session.anchor_name=?')
                session_parameters.append(normalized_anchor)
            else:
                conditions.append("trim(session.anchor_name)='' ")
        if stats_included is not None:
            conditions.append(
                'COALESCE((SELECT scan.stats_included FROM vainglory_scan_jobs '
                'scan WHERE scan.session_id=session.id),1)=?'
            )
            session_parameters.append(1 if stats_included else 0)
        matching = ' AND '.join(conditions)
        total = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM recording_sessions session WHERE ' + matching,
                tuple(session_parameters),
            )
        )
        order_by = (
            'ordering_scan.completed_at DESC,session.started_at DESC,session.id DESC'
            if sort_by == 'analyzed'
            else 'session.started_at DESC,session.id DESC'
        )
        id_rows = await self._database.fetchall(
            'SELECT session.id FROM recording_sessions session '
            'LEFT JOIN vainglory_scan_jobs ordering_scan '
            'ON ordering_scan.session_id=session.id WHERE '
            + matching
            + ' ORDER BY '
            + order_by
            + ' LIMIT ? OFFSET ?',
            tuple(session_parameters) + (limit, offset),
        )
        session_ids = [int(row['id']) for row in id_rows]
        if not session_ids:
            return MatchSessionPage(total=total, items=())
        placeholders = ','.join('?' for _ in session_ids)
        winner_color_sql = (
            "(CASE match.winner_side WHEN 'left' THEN match.left_color "
            "WHEN 'right' THEN match.right_color ELSE 'unknown' END)"
        )
        rows = await self._database.fetchall(
            'SELECT session.id AS session_id,'
            'COALESCE(scan.custom_title,session.title) AS title,'
            'session.title AS source_title,session.anchor_name,session.started_at,'
            'COALESCE(scan.stats_included,1) AS stats_included,'
            'COALESCE('
            '(SELECT upload.bvid FROM upload_jobs upload '
            'WHERE upload.session_id=session.id AND upload.bvid IS NOT NULL '
            "AND upload.bvid<>'' ORDER BY upload.id DESC LIMIT 1),"
            '(SELECT source.bvid FROM vainglory_video_sources source '
            'JOIN recording_parts source_part ON source_part.id=source.part_id '
            'WHERE source_part.session_id=session.id AND NOT EXISTS('
            'SELECT 1 FROM archive_migration_items source_migration '
            'WHERE source_migration.session_id=session.id) '
            'ORDER BY source.page LIMIT 1),'
            '(SELECT imported.bvid FROM vainglory_archive_imports imported '
            'JOIN vainglory_archive_parts archive '
            'ON archive.import_id=imported.id '
            'JOIN recording_parts archive_part '
            'ON archive_part.id=archive.recording_part_id '
            'WHERE archive_part.session_id=session.id '
            'ORDER BY archive.page LIMIT 1)) AS bvid,'
            '(SELECT publication.state FROM vainglory_publications publication '
            'WHERE publication.session_id=session.id '
            'ORDER BY publication.id DESC LIMIT 1) AS publication_state,'
            '(SELECT publication.description_state '
            'FROM vainglory_publications publication '
            'WHERE publication.session_id=session.id '
            'ORDER BY publication.id DESC LIMIT 1) AS description_state,'
            '(SELECT publication.pin_state FROM vainglory_publications publication '
            'WHERE publication.session_id=session.id '
            'ORDER BY publication.id DESC LIMIT 1) AS pin_state,'
            '(SELECT publication.chapter_state '
            'FROM vainglory_publications publication '
            'WHERE publication.session_id=session.id '
            'ORDER BY publication.id DESC LIMIT 1) AS chapter_state,'
            'COUNT(match.id) AS match_count,'
            "SUM(CASE WHEN {}='teal' THEN 1 ELSE 0 END) AS teal_win_count,"
            "SUM(CASE WHEN {}='orange' THEN 1 ELSE 0 END) AS orange_win_count,"
            "SUM(CASE WHEN match.end_reason='surrender' THEN 1 ELSE 0 END) "
            'AS surrender_count,'
            'SUM(COALESCE(match.duration_seconds,0)) AS duration_seconds,'
            'GROUP_CONCAT(DISTINCT match.game_mode) AS game_modes '
            'FROM recording_sessions session '
            'JOIN vainglory_matches match ON match.session_id=session.id '
            'LEFT JOIN vainglory_scan_jobs scan ON scan.session_id=session.id '
            'WHERE session.id IN ({}) GROUP BY session.id'.format(
                winner_color_sql, winner_color_sql, placeholders
            ),
            tuple(session_ids),
        )
        by_id = {
            int(row['session_id']): self._match_session_record(row) for row in rows
        }
        return MatchSessionPage(
            total=total, items=tuple(by_id[value] for value in session_ids)
        )

    async def list_recorded_player_reviews(
        self, *, limit: int = 50, offset: int = 0
    ) -> MatchPage:
        if limit < 1 or limit > 100:
            raise ValueError('limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('offset must not be negative')
        condition = (
            'match.team_size IN (3,5) AND match.result_frame_path IS NOT NULL '
            'AND match.recorded_player_detection_version>=? '
            'AND match.recorded_player_side IS NULL'
        )
        parameters = (self.RECORDED_PLAYER_DETECTION_VERSION,)
        total = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM vainglory_matches match WHERE ' + condition,
                parameters,
            )
        )
        rows = await self._database.fetchall(
            self._MATCH_SELECT
            + 'WHERE '
            + condition
            + ' ORDER BY session.started_at DESC,part.part_index DESC,'
            'match.result_at_ms DESC,match.id DESC LIMIT ? OFFSET ?',
            parameters + (limit, offset),
        )
        return MatchPage(total=total, items=await self._hydrate_matches(rows))

    async def list_hero_reviews(self, *, limit: int = 50, offset: int = 0) -> MatchPage:
        if limit < 1 or limit > 100:
            raise ValueError('limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('offset must not be negative')
        condition = (
            'match.result_frame_path IS NOT NULL AND EXISTS('
            'SELECT 1 FROM vainglory_match_players player '
            'WHERE player.match_id=match.id AND player.hero_id IS NULL)'
        )
        total = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM vainglory_matches match WHERE ' + condition
            )
        )
        rows = await self._database.fetchall(
            self._MATCH_SELECT
            + 'WHERE '
            + condition
            + ' ORDER BY session.started_at DESC,part.part_index DESC,'
            'match.result_at_ms DESC,match.id DESC LIMIT ? OFFSET ?',
            (limit, offset),
        )
        return MatchPage(total=total, items=await self._hydrate_matches(rows))

    async def get_match(self, match_id: int) -> MatchRecord:
        rows = await self._database.fetchall(
            self._MATCH_SELECT + 'WHERE match.id=?', (int(match_id),)
        )
        if not rows:
            raise VaingloryNotFound('对局不存在')
        return (await self._hydrate_matches(rows))[0]

    async def result_frame_path(self, match_id: int) -> Optional[Path]:
        row = await self._database.fetchone(
            'SELECT result_frame_path FROM vainglory_matches WHERE id=?',
            (int(match_id),),
        )
        if row is None or row['result_frame_path'] is None:
            return None
        relative_path = str(row['result_frame_path'])
        try:
            path = self._resolve_result_frame_path(relative_path)
        except ValueError:
            logger.warning(
                'Ignored unsafe Vainglory result frame path: match_id={} path={!r}',
                match_id,
                relative_path,
            )
            return None
        return path if path.is_file() else None

    async def next_hero_rematch(self) -> Optional[HeroRematchClaim]:
        row = await self._database.fetchone(
            'SELECT match.id FROM vainglory_matches match '
            'WHERE match.hero_recognition_version<? '
            'AND match.result_frame_path IS NOT NULL AND EXISTS('
            'SELECT 1 FROM vainglory_match_players player '
            'WHERE player.match_id=match.id AND player.hero_id IS NULL) '
            'ORDER BY match.id LIMIT 1',
            (self.HERO_RECOGNITION_VERSION,),
        )
        return None if row is None else HeroRematchClaim(match_id=int(row['id']))

    async def complete_hero_rematch(
        self, match_id: int, heroes: Sequence[AnalyzedHero]
    ) -> int:
        now = self._now()

        def complete(connection: sqlite3.Connection) -> int:
            match = connection.execute(
                'SELECT session_id FROM vainglory_matches WHERE id=?', (int(match_id),)
            ).fetchone()
            if match is None:
                raise VaingloryNotFound('对局不存在')
            existing = self._existing_heroes(connection)
            updated = 0
            for hero in heroes:
                hero_id = self._resolve_hero(connection, hero, existing, now)
                if hero_id is None:
                    continue
                updated += connection.execute(
                    'UPDATE vainglory_match_players SET hero_id=? '
                    'WHERE match_id=? AND side=? AND slot=? AND hero_id IS NULL '
                    "AND hero_source<>'manual'",
                    (hero_id, int(match_id), hero.side, hero.slot),
                ).rowcount
            connection.execute(
                'UPDATE vainglory_matches SET hero_recognition_version=? WHERE id=?',
                (self.HERO_RECOGNITION_VERSION, int(match_id)),
            )
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (int(match['session_id']),),
            )
            return updated

        return await self._database.write(complete)

    async def next_recorded_player_backfill(
        self,
    ) -> Optional[RecordedPlayerBackfillClaim]:
        row = await self._database.fetchone(
            'SELECT id FROM vainglory_matches '
            'WHERE recorded_player_detection_version<? '
            "AND recorded_player_source<>'manual' "
            'AND result_frame_path IS NOT NULL ORDER BY id LIMIT 1',
            (self.RECORDED_PLAYER_DETECTION_VERSION,),
        )
        return (
            None
            if row is None
            else RecordedPlayerBackfillClaim(match_id=int(row['id']))
        )

    async def complete_recorded_player_backfill(
        self, match_id: int, player: Optional[RecordedPlayer]
    ) -> bool:
        def complete(connection: sqlite3.Connection) -> bool:
            match = connection.execute(
                'SELECT session_id,team_size,recorded_player_source,'
                'recorded_player_side FROM vainglory_matches WHERE id=?',
                (int(match_id),),
            ).fetchone()
            if match is None:
                raise VaingloryNotFound('对局不存在')
            if str(match['recorded_player_source']) == 'manual':
                return match['recorded_player_side'] is not None
            selected = player
            if match['team_size'] is not None and int(match['team_size']) not in (3, 5):
                selected = None
            if selected is not None:
                exists = connection.execute(
                    'SELECT 1 FROM vainglory_match_players '
                    'WHERE match_id=? AND side=? AND slot=?',
                    (int(match_id), selected.side, selected.slot),
                ).fetchone()
                if exists is None:
                    selected = None
            connection.execute(
                'UPDATE vainglory_matches SET recorded_player_side=?,'
                'recorded_player_slot=?,recorded_player_confidence=?,'
                'recorded_player_detection_version=?,'
                "recorded_player_source='automatic' "
                'WHERE id=?',
                (
                    None if selected is None else selected.side,
                    None if selected is None else selected.slot,
                    None if selected is None else selected.confidence,
                    self.RECORDED_PLAYER_DETECTION_VERSION,
                    int(match_id),
                ),
            )
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (int(match['session_id']),),
            )
            return selected is not None

        return await self._database.write(complete)

    async def set_recorded_player(
        self, match_id: int, *, side: str, slot: int
    ) -> MatchRecord:
        if side not in ('left', 'right'):
            raise ValueError('player side is invalid')
        if slot < 1 or slot > 5:
            raise ValueError('player slot is invalid')

        def update(connection: sqlite3.Connection) -> None:
            match = connection.execute(
                'SELECT session_id,team_size,left_color,right_color '
                'FROM vainglory_matches WHERE id=?',
                (int(match_id),),
            ).fetchone()
            if match is None:
                raise VaingloryNotFound('对局不存在')
            if match['team_size'] is None or int(match['team_size']) not in (3, 5):
                raise VaingloryConflict('当前对局不支持确认主播英雄')
            teal_side = (
                'left'
                if str(match['left_color']) == 'teal'
                else 'right' if str(match['right_color']) == 'teal' else ''
            )
            if side != teal_side:
                raise VaingloryConflict('只能从主播所在的蓝绿色一方选择')
            player = connection.execute(
                'SELECT 1 FROM vainglory_match_players '
                'WHERE match_id=? AND side=? AND slot=?',
                (int(match_id), side, int(slot)),
            ).fetchone()
            if player is None:
                raise VaingloryNotFound('对局中的玩家位置不存在')
            connection.execute(
                'UPDATE vainglory_matches SET recorded_player_side=?,'
                'recorded_player_slot=?,recorded_player_confidence=1,'
                "recorded_player_source='manual',"
                'recorded_player_detection_version=? WHERE id=?',
                (
                    side,
                    int(slot),
                    self.RECORDED_PLAYER_DETECTION_VERSION,
                    int(match_id),
                ),
            )
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (int(match['session_id']),),
            )

        await self._database.write(update)
        return await self.get_match(match_id)

    async def set_player_hero(
        self, match_id: int, *, side: str, slot: int, hero_id: int
    ) -> MatchRecord:
        if side not in ('left', 'right'):
            raise ValueError('player side is invalid')
        if slot < 1 or slot > 5:
            raise ValueError('player slot is invalid')
        if hero_id < 1:
            raise ValueError('hero id is invalid')

        def update(connection: sqlite3.Connection) -> None:
            match = connection.execute(
                'SELECT session_id FROM vainglory_matches WHERE id=?', (int(match_id),)
            ).fetchone()
            if match is None:
                raise VaingloryNotFound('对局不存在')
            hero = connection.execute(
                "SELECT 1 FROM vainglory_heroes WHERE id=? AND label<>''",
                (int(hero_id),),
            ).fetchone()
            if hero is None:
                raise VaingloryNotFound('英雄不存在')
            changed = connection.execute(
                'UPDATE vainglory_match_players SET hero_id=?,hero_source='
                "'manual' WHERE match_id=? AND side=? AND slot=?",
                (int(hero_id), int(match_id), side, int(slot)),
            ).rowcount
            if changed != 1:
                raise VaingloryNotFound('对局中的玩家位置不存在')
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (int(match['session_id']),),
            )

        await self._database.write(update)
        return await self.get_match(match_id)

    async def update_match_title(self, match_id: int, title: str) -> MatchRecord:
        normalized = title.strip()
        if len(normalized) > 200:
            raise ValueError('match title is too long')
        count = await self._database.execute(
            'UPDATE vainglory_matches SET custom_title=? WHERE id=?',
            (normalized or None, int(match_id)),
        )
        if count != 1:
            raise VaingloryNotFound('对局不存在')
        return await self.get_match(match_id)

    async def update_session_title(
        self, session_id: int, title: str
    ) -> MatchSessionRecord:
        normalized = title.strip()
        if len(normalized) > 200:
            raise ValueError('session title is too long')
        count = await self._database.execute(
            'UPDATE vainglory_scan_jobs SET custom_title=? WHERE session_id=?',
            (normalized or None, int(session_id)),
        )
        if count != 1:
            raise VaingloryNotFound('直播场次不存在')
        page = await self.list_match_sessions(
            session_id=int(session_id), limit=1, offset=0
        )
        if not page.items:
            raise VaingloryNotFound('直播场次暂无对局')
        return page.items[0]

    async def update_session_anchor(
        self, session_id: int, anchor_name: str
    ) -> MatchSessionRecord:
        normalized = anchor_name.strip()
        if len(normalized) > 200:
            raise ValueError('anchor name is too long')

        def update(connection: sqlite3.Connection) -> None:
            session = connection.execute(
                'SELECT id FROM recording_sessions WHERE id=?', (int(session_id),)
            ).fetchone()
            if session is None:
                raise VaingloryNotFound('直播场次不存在')
            self._set_session_anchor(connection, int(session_id), normalized)

        await self._database.write(update)
        page = await self.list_match_sessions(
            session_id=int(session_id), limit=1, offset=0
        )
        if not page.items:
            raise VaingloryNotFound('直播场次暂无对局')
        return page.items[0]

    async def bulk_update_sessions(
        self,
        session_ids: Sequence[int],
        *,
        anchor_name: Optional[str] = None,
        stats_included: Optional[bool] = None,
    ) -> int:
        unique_ids = tuple(dict.fromkeys(int(value) for value in session_ids))
        if not unique_ids or len(unique_ids) > 100:
            raise ValueError('session count must be between 1 and 100')
        if any(value < 1 for value in unique_ids):
            raise ValueError('session ID must be positive')
        if anchor_name is None and stats_included is None:
            raise ValueError('no session update was requested')
        normalized_anchor = None if anchor_name is None else anchor_name.strip()
        if normalized_anchor is not None and len(normalized_anchor) > 200:
            raise ValueError('anchor name is too long')

        def update(connection: sqlite3.Connection) -> int:
            placeholders = ','.join('?' for _ in unique_ids)
            rows = connection.execute(
                'SELECT id FROM recording_sessions WHERE id IN ({})'.format(
                    placeholders
                ),
                unique_ids,
            ).fetchall()
            found_ids = {int(row['id']) for row in rows}
            if found_ids != set(unique_ids):
                raise VaingloryNotFound('部分直播场次不存在')
            if normalized_anchor is not None:
                for selected_id in unique_ids:
                    self._set_session_anchor(connection, selected_id, normalized_anchor)
            if stats_included is not None:
                changed = connection.execute(
                    'UPDATE vainglory_scan_jobs SET stats_included=? '
                    'WHERE session_id IN ({})'.format(placeholders),
                    (1 if stats_included else 0,) + unique_ids,
                ).rowcount
                if changed != len(unique_ids):
                    raise VaingloryNotFound('部分直播场次暂无对局索引')
            return len(unique_ids)

        return await self._database.write(update)

    async def _hydrate_matches(
        self, rows: Sequence[sqlite3.Row]
    ) -> Tuple[MatchRecord, ...]:
        match_ids = [int(row['id']) for row in rows]
        players_by_match: Dict[int, List[MatchPlayerRecord]] = {
            match_id: [] for match_id in match_ids
        }
        if match_ids:
            placeholders = ','.join('?' for _ in match_ids)
            player_rows = await self._database.fetchall(
                'SELECT player.*,COALESCE(hero.label,\'\') AS hero_label,'
                'CASE WHEN player.side=source_match.recorded_player_side '
                'AND player.slot=source_match.recorded_player_slot '
                'THEN 1 ELSE 0 END AS is_recorded_player '
                'FROM vainglory_match_players player '
                'JOIN vainglory_matches source_match '
                'ON source_match.id=player.match_id '
                'LEFT JOIN vainglory_heroes hero ON hero.id=player.hero_id '
                'WHERE player.match_id IN ({}) '
                'ORDER BY player.match_id,'
                "CASE player.side WHEN 'left' THEN 0 ELSE 1 END,player.slot".format(
                    placeholders
                ),
                tuple(match_ids),
            )
            for player in player_rows:
                players_by_match[int(player['match_id'])].append(
                    self._match_player(player)
                )
        return tuple(
            self._match_record(row, tuple(players_by_match[int(row['id'])]))
            for row in rows
        )

    async def list_heroes(self) -> Tuple[HeroRecord, ...]:
        rows = await self._database.fetchall(
            'SELECT id,label,fingerprint FROM vainglory_heroes '
            "WHERE label!='' ORDER BY label COLLATE NOCASE,id"
        )
        return tuple(
            HeroRecord(
                id=int(row['id']),
                label=str(row['label']),
                fingerprint=str(row['fingerprint']),
            )
            for row in rows
        )

    async def list_players(self) -> Tuple[PlayerRecord, ...]:
        player_rows = await self._database.fetchall(
            'SELECT id,name,origin,created_at,updated_at '
            'FROM vainglory_players ORDER BY name COLLATE NOCASE,id'
        )
        room_rows = await self._database.fetchall(
            'SELECT room.player_id,room.room_id,'
            '(SELECT known.anchor_uid FROM recording_sessions known '
            'WHERE known.room_id=room.room_id AND known.anchor_uid IS NOT NULL '
            'AND known.anchor_uid>0 ORDER BY known.started_at DESC,known.id DESC '
            'LIMIT 1) AS anchor_uid,'
            'COALESCE((SELECT known.anchor_name FROM recording_sessions known '
            "WHERE known.room_id=room.room_id AND trim(known.anchor_name)<>'' "
            'ORDER BY known.started_at DESC,known.id DESC LIMIT 1),\'\') '
            'AS anchor_name '
            'FROM vainglory_player_rooms room '
            'ORDER BY room.player_id,room.room_id'
        )
        rooms_by_player: Dict[int, List[PlayerRoomRecord]] = {
            int(row['id']): [] for row in player_rows
        }
        for row in room_rows:
            rooms_by_player.setdefault(int(row['player_id']), []).append(
                PlayerRoomRecord(
                    room_id=int(row['room_id']),
                    anchor_uid=(
                        None if row['anchor_uid'] is None else int(row['anchor_uid'])
                    ),
                    anchor_name=str(row['anchor_name'] or ''),
                )
            )
        return tuple(
            PlayerRecord(
                id=int(row['id']),
                name=str(row['name']),
                origin=cast(Literal['automatic', 'manual'], str(row['origin'])),
                rooms=tuple(rooms_by_player.get(int(row['id']), ())),
                created_at=int(row['created_at']),
                updated_at=int(row['updated_at']),
            )
            for row in player_rows
        )

    async def get_player(self, player_id: int) -> PlayerRecord:
        selected = next(
            (player for player in await self.list_players() if player.id == player_id),
            None,
        )
        if selected is None:
            raise VaingloryNotFound('玩家不存在')
        return selected

    async def create_player(self, name: str) -> PlayerRecord:
        normalized = self._normalize_player_display_name(name)
        now = self._now()

        def create(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                'INSERT INTO vainglory_players('
                'name,origin,created_at,updated_at) VALUES(?,\'manual\',?,?)',
                (normalized, now, now),
            )
            return int(cursor.lastrowid)

        return await self.get_player(await self._database.write(create))

    async def ensure_players_for_rooms(
        self, rooms: Sequence[Tuple[int, str]]
    ) -> Tuple[PlayerRecord, ...]:
        normalized_rooms = tuple(
            (int(room_id), self._normalize_player_display_name(name))
            for room_id, name in rooms
            if int(room_id) > 0
        )
        now = self._now()

        def ensure(connection: sqlite3.Connection) -> None:
            seen: Set[int] = set()
            for room_id, name in normalized_rooms:
                if room_id in seen:
                    continue
                seen.add(room_id)
                if (
                    connection.execute(
                        'SELECT 1 FROM vainglory_player_rooms WHERE room_id=?',
                        (room_id,),
                    ).fetchone()
                    is not None
                ):
                    continue
                if (
                    connection.execute(
                        'SELECT 1 FROM vainglory_player_room_suppressions '
                        'WHERE room_id=?',
                        (room_id,),
                    ).fetchone()
                    is not None
                ):
                    continue
                cursor = connection.execute(
                    'INSERT INTO vainglory_players('
                    'name,origin,created_at,updated_at) '
                    "VALUES(?,'automatic',?,?)",
                    (name, now, now),
                )
                connection.execute(
                    'INSERT INTO vainglory_player_rooms('
                    'room_id,player_id,created_at,updated_at) VALUES(?,?,?,?)',
                    (room_id, int(cursor.lastrowid), now, now),
                )

        await self._database.write(ensure)
        return await self.list_players()

    async def rename_player(self, player_id: int, name: str) -> PlayerRecord:
        normalized = self._normalize_player_display_name(name)
        count = await self._database.execute(
            'UPDATE vainglory_players SET name=?,updated_at=? WHERE id=?',
            (normalized, self._now(), int(player_id)),
        )
        if count != 1:
            raise VaingloryNotFound('玩家不存在')
        return await self.get_player(int(player_id))

    async def bind_player_room(self, player_id: int, room_id: int) -> PlayerRecord:
        selected_player_id = int(player_id)
        selected_room_id = int(room_id)
        if selected_room_id <= 0:
            raise ValueError('room ID must be positive')
        now = self._now()

        def bind(connection: sqlite3.Connection) -> None:
            player = connection.execute(
                'SELECT id FROM vainglory_players WHERE id=?', (selected_player_id,)
            ).fetchone()
            if player is None:
                raise VaingloryNotFound('玩家不存在')
            previous = connection.execute(
                'SELECT player_id FROM vainglory_player_rooms WHERE room_id=?',
                (selected_room_id,),
            ).fetchone()
            previous_player_id = (
                None if previous is None else int(previous['player_id'])
            )
            connection.execute(
                'INSERT INTO vainglory_player_rooms('
                'room_id,player_id,created_at,updated_at) VALUES(?,?,?,?) '
                'ON CONFLICT(room_id) DO UPDATE SET '
                'player_id=excluded.player_id,updated_at=excluded.updated_at',
                (selected_room_id, selected_player_id, now, now),
            )
            connection.execute(
                'DELETE FROM vainglory_player_room_suppressions WHERE room_id=?',
                (selected_room_id,),
            )
            connection.execute(
                'UPDATE vainglory_players SET updated_at=? WHERE id=?',
                (now, selected_player_id),
            )
            if previous_player_id not in (None, selected_player_id):
                connection.execute(
                    'UPDATE vainglory_players SET updated_at=? WHERE id=?',
                    (now, previous_player_id),
                )
                connection.execute(
                    "DELETE FROM vainglory_players WHERE id=? AND origin='automatic' "
                    'AND NOT EXISTS(SELECT 1 FROM vainglory_player_rooms room '
                    'WHERE room.player_id=vainglory_players.id) '
                    'AND NOT EXISTS(SELECT 1 FROM vainglory_player_sessions '
                    'session_player WHERE session_player.player_id='
                    'vainglory_players.id)',
                    (previous_player_id,),
                )

        await self._database.write(bind)
        return await self.get_player(selected_player_id)

    async def unbind_player_room(self, player_id: int, room_id: int) -> PlayerRecord:
        selected_player_id = int(player_id)
        selected_room_id = int(room_id)
        if selected_room_id <= 0:
            raise ValueError('room ID must be positive')
        now = self._now()

        def unbind(connection: sqlite3.Connection) -> None:
            player = connection.execute(
                'SELECT id FROM vainglory_players WHERE id=?', (selected_player_id,)
            ).fetchone()
            if player is None:
                raise VaingloryNotFound('玩家不存在')
            changed = connection.execute(
                'DELETE FROM vainglory_player_rooms ' 'WHERE room_id=? AND player_id=?',
                (selected_room_id, selected_player_id),
            ).rowcount
            if changed != 1:
                raise VaingloryNotFound('直播间未绑定到该玩家')
            connection.execute(
                'INSERT INTO vainglory_player_room_suppressions(room_id,created_at) '
                'VALUES(?,?) ON CONFLICT(room_id) DO NOTHING',
                (selected_room_id, now),
            )
            connection.execute(
                'UPDATE vainglory_players SET updated_at=? WHERE id=?',
                (now, selected_player_id),
            )

        await self._database.write(unbind)
        return await self.get_player(selected_player_id)

    async def delete_player(self, player_id: int) -> None:
        selected_player_id = int(player_id)
        now = self._now()

        def delete(connection: sqlite3.Connection) -> None:
            player = connection.execute(
                'SELECT id FROM vainglory_players WHERE id=?', (selected_player_id,)
            ).fetchone()
            if player is None:
                raise VaingloryNotFound('玩家不存在')
            room_rows = connection.execute(
                'SELECT room_id FROM vainglory_player_rooms WHERE player_id=?',
                (selected_player_id,),
            ).fetchall()
            connection.executemany(
                'INSERT INTO vainglory_player_room_suppressions(room_id,created_at) '
                'VALUES(?,?) ON CONFLICT(room_id) DO NOTHING',
                ((int(row['room_id']), now) for row in room_rows),
            )
            connection.execute(
                'DELETE FROM vainglory_players WHERE id=?', (selected_player_id,)
            )

        await self._database.write(delete)

    async def list_player_stats(self) -> Tuple[PlayerStatsRecord, ...]:
        players = await self.list_players()
        grouped = {
            player.id: _PlayerStatsAccumulator(player=player) for player in players
        }
        for row in await self._player_match_stats_rows():
            player_id = int(row['player_id'])
            value = grouped.get(player_id)
            if value is None:
                continue
            winner_color = str(row['winner_color'])
            value.session_ids.add(int(row['session_id']))
            value.outcomes.add(winner_color)
            game_mode = str(row['game_mode'])
            value.modes.setdefault(game_mode, _OutcomeAccumulator()).add(winner_color)
            if row['hero_id'] is None:
                continue
            hero_id = int(row['hero_id'])
            hero_label = str(row['hero_label'] or '')
            hero_value = value.heroes.get(hero_id)
            if hero_value is None:
                hero_value = (hero_label, _OutcomeAccumulator())
                value.heroes[hero_id] = hero_value
            hero_value[1].add(winner_color)

        mode_order = {'3v3': 0, 'aram': 1, '5v5': 2, 'other': 3, 'unknown': 4}
        result: List[PlayerStatsRecord] = []
        for value in grouped.values():
            modes = tuple(
                GameModeStatsRecord(
                    game_mode=game_mode,
                    match_count=outcomes.match_count,
                    win_count=outcomes.win_count,
                    loss_count=outcomes.loss_count,
                    unknown_count=outcomes.unknown_count,
                    win_rate=outcomes.win_rate,
                )
                for game_mode, outcomes in sorted(
                    value.modes.items(),
                    key=lambda item: (
                        mode_order.get(item[0], len(mode_order)),
                        item[0],
                    ),
                )
            )
            heroes = tuple(
                HeroStatsRecord(
                    hero_id=hero_id,
                    hero_label=hero_label,
                    player_count=1,
                    match_count=outcomes.match_count,
                    win_count=outcomes.win_count,
                    loss_count=outcomes.loss_count,
                    unknown_count=outcomes.unknown_count,
                    win_rate=outcomes.win_rate,
                )
                for hero_id, (hero_label, outcomes) in sorted(
                    value.heroes.items(),
                    key=lambda item: (
                        -item[1][1].match_count,
                        -item[1][1].win_rate,
                        item[1][0],
                        item[0],
                    ),
                )
            )
            result.append(
                PlayerStatsRecord(
                    player_id=value.player.id,
                    player_name=value.player.name,
                    rooms=value.player.rooms,
                    session_count=len(value.session_ids),
                    match_count=value.outcomes.match_count,
                    win_count=value.outcomes.win_count,
                    loss_count=value.outcomes.loss_count,
                    unknown_count=value.outcomes.unknown_count,
                    win_rate=value.outcomes.win_rate,
                    modes=modes,
                    heroes=heroes,
                )
            )
        return tuple(
            sorted(result, key=lambda item: (-item.match_count, item.player_name))
        )

    async def list_hero_stats(
        self, *, game_mode: str = ''
    ) -> Tuple[HeroStatsRecord, ...]:
        if game_mode not in ('', '3v3', '5v5', 'aram', 'other', 'unknown'):
            raise ValueError('game mode is invalid')
        outcomes_by_hero: Dict[int, _OutcomeAccumulator] = {}
        labels_by_hero: Dict[int, str] = {}
        players_by_hero: Dict[int, Set[int]] = {}
        for row in await self._player_match_stats_rows(game_mode=game_mode):
            if row['hero_id'] is None:
                continue
            hero_id = int(row['hero_id'])
            labels_by_hero[hero_id] = str(row['hero_label'] or '')
            players_by_hero.setdefault(hero_id, set()).add(int(row['player_id']))
            outcomes_by_hero.setdefault(hero_id, _OutcomeAccumulator()).add(
                str(row['winner_color'])
            )
        result = tuple(
            HeroStatsRecord(
                hero_id=hero_id,
                hero_label=labels_by_hero[hero_id],
                player_count=len(players_by_hero[hero_id]),
                match_count=outcomes.match_count,
                win_count=outcomes.win_count,
                loss_count=outcomes.loss_count,
                unknown_count=outcomes.unknown_count,
                win_rate=outcomes.win_rate,
            )
            for hero_id, outcomes in outcomes_by_hero.items()
        )
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    -item.win_rate,
                    -item.match_count,
                    item.hero_label,
                    item.hero_id,
                ),
            )
        )

    async def list_anchor_stats(self) -> Tuple[AnchorStatsRecord, ...]:
        rows = await self._database.fetchall(
            'SELECT session.id AS session_id,session.room_id,'
            'session.anchor_uid,session.anchor_name,'
            "CASE match.winner_side WHEN 'left' THEN match.left_color "
            "WHEN 'right' THEN match.right_color ELSE 'unknown' END "
            'AS winner_color '
            'FROM vainglory_matches match '
            'JOIN recording_sessions session ON session.id=match.session_id '
            'JOIN vainglory_scan_jobs scan ON scan.session_id=session.id '
            'WHERE scan.stats_included=1 AND match.stats_eligible=1 '
            'ORDER BY session.started_at,session.id,match.id'
        )
        grouped: Dict[str, _AnchorStatsAccumulator] = {}
        for row in rows:
            anchor_uid = (
                None
                if row['anchor_uid'] is None or int(row['anchor_uid']) <= 0
                else int(row['anchor_uid'])
            )
            anchor_name = str(row['anchor_name']).strip()
            key = (
                'uid:{}'.format(anchor_uid)
                if anchor_uid is not None
                else 'name:{}'.format(anchor_name.casefold() or 'unknown')
            )
            value = grouped.get(key)
            if value is None:
                value = _AnchorStatsAccumulator(
                    anchor_uid=anchor_uid,
                    anchor_name=anchor_name or '未知主播',
                    room_id=int(row['room_id']),
                    session_ids=set(),
                )
                grouped[key] = value
            elif anchor_name:
                value.anchor_name = anchor_name
                value.room_id = int(row['room_id'])
            value.session_ids.add(int(row['session_id']))
            value.match_count += 1
            winner_color = str(row['winner_color'])
            if winner_color == 'teal':
                value.win_count += 1
            elif winner_color == 'orange':
                value.loss_count += 1
            else:
                value.unknown_count += 1
        result = tuple(
            AnchorStatsRecord(
                anchor_uid=value.anchor_uid,
                anchor_name=value.anchor_name,
                room_id=value.room_id,
                session_count=len(value.session_ids),
                match_count=value.match_count,
                win_count=value.win_count,
                loss_count=value.loss_count,
                unknown_count=value.unknown_count,
                win_rate=(
                    0.0
                    if value.match_count == 0
                    else value.win_count / value.match_count
                ),
            )
            for value in grouped.values()
        )
        return tuple(
            sorted(result, key=lambda item: (-item.match_count, item.anchor_name))
        )

    async def label_hero(self, hero_id: int, label: str) -> HeroRecord:
        normalized = label.strip()
        if len(normalized) > 80:
            raise ValueError('hero label is too long')
        count = await self._database.execute(
            'UPDATE vainglory_heroes SET label=?,updated_at=? WHERE id=?',
            (normalized, self._now(), int(hero_id)),
        )
        if count != 1:
            raise VaingloryNotFound('英雄不存在')
        row = await self._database.fetchone(
            'SELECT id,label,fingerprint FROM vainglory_heroes WHERE id=?',
            (int(hero_id),),
        )
        assert row is not None
        return HeroRecord(
            id=int(row['id']),
            label=str(row['label']),
            fingerprint=str(row['fingerprint']),
        )

    async def hero_thumbnail(self, hero_id: int) -> Optional[bytes]:
        row = await self._database.fetchone(
            'SELECT thumbnail_png FROM vainglory_heroes WHERE id=?', (int(hero_id),)
        )
        return None if row is None else bytes(row['thumbnail_png'])

    @staticmethod
    def _result_frame_relative_path(
        *, session_id: int, part_id: int, result_at_ms: int, content: bytes
    ) -> str:
        digest = hashlib.sha256(content).hexdigest()[:16]
        return 'session-{}/part-{}-{}-{}.png'.format(
            session_id, part_id, result_at_ms, digest
        )

    def _resolve_result_frame_path(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute() or '..' in candidate.parts:
            raise ValueError('result frame path must stay inside its storage directory')
        resolved = (self._result_frame_root / candidate).resolve()
        try:
            resolved.relative_to(self._result_frame_root)
        except ValueError as error:
            raise ValueError(
                'result frame path must stay inside its storage directory'
            ) from error
        return resolved

    def _write_result_frame(self, destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self._result_frame_root, 0o700)
        os.chmod(destination.parent, 0o700)
        if destination.is_file():
            return
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='wb',
                prefix='.result-frame-',
                suffix='.tmp',
                dir=str(destination.parent),
                delete=False,
            ) as output:
                temporary_path = Path(output.name)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(str(temporary_path), str(destination))
            os.chmod(destination, 0o600)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _remove_result_frame_files(
        self, relative_paths: Sequence[str], *, keep: Sequence[Path] = ()
    ) -> None:
        preserved = {path.resolve() for path in keep}
        for relative_path in relative_paths:
            try:
                path = self._resolve_result_frame_path(relative_path)
            except ValueError:
                logger.warning(
                    'Skipped invalid Vainglory result frame path: path={}',
                    relative_path,
                )
                continue
            if path in preserved:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                logger.warning(
                    'Failed to remove obsolete Vainglory result frame: '
                    'path={} error={}',
                    path,
                    error,
                )

    def _resolve_hero(
        self,
        connection: sqlite3.Connection,
        hero: AnalyzedHero,
        existing: List[Tuple[int, str, str]],
        now: int,
    ) -> Optional[int]:
        del connection, now
        label = hero.label or identify_builtin_hero(hero.fingerprint)
        if not label:
            return None
        return next(
            (
                hero_id
                for hero_id, _, existing_label in existing
                if existing_label.casefold() == label.casefold()
            ),
            None,
        )

    @staticmethod
    def _consolidate_heroes(connection: sqlite3.Connection, now: int) -> int:
        rows = connection.execute(
            'SELECT id,label,fingerprint,thumbnail_png,updated_at '
            'FROM vainglory_heroes ORDER BY id'
        ).fetchall()
        canonical_by_label: Dict[str, Tuple[int, int]] = {}
        removed = 0
        for row in rows:
            hero_id = int(row['id'])
            label = str(row['label'])
            if not label:
                continue
            normalized = label.casefold()
            canonical = canonical_by_label.setdefault(
                normalized, (hero_id, int(row['updated_at']))
            )
            if canonical[0] == hero_id:
                continue
            connection.execute(
                'UPDATE vainglory_match_players SET hero_id=? WHERE hero_id=?',
                (canonical[0], hero_id),
            )
            removed += connection.execute(
                'DELETE FROM vainglory_heroes WHERE id=?', (hero_id,)
            ).rowcount
            if int(row['updated_at']) >= canonical[1]:
                connection.execute(
                    'UPDATE vainglory_heroes SET fingerprint=?,thumbnail_png=?,'
                    'updated_at=? WHERE id=?',
                    (
                        str(row['fingerprint']),
                        bytes(row['thumbnail_png']),
                        now,
                        canonical[0],
                    ),
                )
                canonical_by_label[normalized] = (canonical[0], now)
            connection.execute(
                'UPDATE vainglory_heroes SET updated_at=? WHERE id=?',
                (now, canonical[0]),
            )
        removed += connection.execute(
            "DELETE FROM vainglory_heroes WHERE label='' AND NOT EXISTS("
            'SELECT 1 FROM vainglory_match_players player '
            'WHERE player.hero_id=vainglory_heroes.id)'
        ).rowcount
        return removed

    def _ensure_scan_job(
        self, connection: sqlite3.Connection, session_id: int, now: int
    ) -> None:
        connection.execute(
            'INSERT OR IGNORE INTO vainglory_scan_jobs('
            'session_id,state,progress,algorithm_version,match_count,error,'
            'requested_at,started_at,completed_at,updated_at) '
            "VALUES(?,'pending',0,?,0,NULL,?,NULL,NULL,?)",
            (session_id, self.ALGORITHM_VERSION, now, now),
        )

    def _refresh_session_job(
        self, connection: sqlite3.Connection, session_id: int, now: int
    ) -> None:
        refresh_session_scan_job(connection, session_id, now)

    @staticmethod
    def _existing_heroes(connection: sqlite3.Connection) -> List[Tuple[int, str, str]]:
        return [
            (int(row['id']), str(row['fingerprint']), str(row['label']))
            for row in connection.execute(
                'SELECT id,fingerprint,label FROM vainglory_heroes ORDER BY id'
            ).fetchall()
        ]

    @staticmethod
    def _scan_job(row: sqlite3.Row) -> ScanJob:
        return ScanJob(
            session_id=int(row['session_id']),
            state=str(row['state']),
            progress=float(row['progress']),
            algorithm_version=int(row['algorithm_version']),
            match_count=int(row['match_count']),
            error=None if row['error'] is None else str(row['error']),
            requested_at=int(row['requested_at']),
            started_at=(None if row['started_at'] is None else int(row['started_at'])),
            completed_at=(
                None if row['completed_at'] is None else int(row['completed_at'])
            ),
            updated_at=int(row['updated_at']),
        )

    async def _player_match_stats_rows(
        self, *, game_mode: str = ''
    ) -> List[sqlite3.Row]:
        conditions = ['scan.stats_included=1', 'match.stats_eligible=1']
        parameters: List[object] = []
        if game_mode:
            conditions.append('match.game_mode=?')
            parameters.append(game_mode)
        return await self._database.fetchall(
            'SELECT COALESCE(room.player_id,direct.player_id) AS player_id,'
            'session.id AS session_id,match.game_mode,'
            "CASE match.winner_side WHEN 'left' THEN match.left_color "
            "WHEN 'right' THEN match.right_color ELSE 'unknown' END "
            'AS winner_color,hero.id AS hero_id,'
            "COALESCE(hero.label,'') AS hero_label "
            'FROM vainglory_matches match '
            'JOIN recording_sessions session ON session.id=match.session_id '
            'JOIN vainglory_scan_jobs scan ON scan.session_id=session.id '
            'LEFT JOIN vainglory_player_rooms room '
            'ON room.room_id=session.room_id AND session.room_id>0 '
            'LEFT JOIN vainglory_player_sessions direct '
            'ON direct.session_id=session.id '
            'LEFT JOIN vainglory_match_players recorded '
            'ON recorded.match_id=match.id '
            'AND recorded.side=match.recorded_player_side '
            'AND recorded.slot=match.recorded_player_slot '
            'LEFT JOIN vainglory_heroes hero ON hero.id=recorded.hero_id '
            'WHERE COALESCE(room.player_id,direct.player_id) IS NOT NULL AND '
            + ' AND '.join(conditions)
            + ' ORDER BY session.started_at,session.id,match.id',
            tuple(parameters),
        )

    @staticmethod
    def _ensure_session_player(
        connection: sqlite3.Connection, session_id: int, now: int
    ) -> None:
        session = connection.execute(
            'SELECT id,room_id,anchor_uid,anchor_name '
            'FROM recording_sessions WHERE id=?',
            (int(session_id),),
        ).fetchone()
        if session is None:
            return
        room_id = int(session['room_id'])
        anchor_uid = (
            None
            if session['anchor_uid'] is None or int(session['anchor_uid']) <= 0
            else int(session['anchor_uid'])
        )
        anchor_name = str(session['anchor_name'] or '').strip()
        if room_id > 0:
            existing = connection.execute(
                'SELECT player_id FROM vainglory_player_rooms WHERE room_id=?',
                (room_id,),
            ).fetchone()
            if existing is not None:
                return
        else:
            existing = connection.execute(
                'SELECT player_id FROM vainglory_player_sessions WHERE session_id=?',
                (int(session_id),),
            ).fetchone()
            if existing is not None:
                return

        player_id: Optional[int] = None
        if anchor_uid is not None:
            known = connection.execute(
                'SELECT candidate.player_id FROM ('
                'SELECT room.player_id,known.started_at,known.id '
                'FROM vainglory_player_rooms room '
                'JOIN recording_sessions known ON known.room_id=room.room_id '
                'WHERE known.anchor_uid=? '
                'UNION ALL '
                'SELECT direct.player_id,known.started_at,known.id '
                'FROM vainglory_player_sessions direct '
                'JOIN recording_sessions known ON known.id=direct.session_id '
                'WHERE known.anchor_uid=?'
                ') candidate ORDER BY candidate.started_at DESC,candidate.id DESC '
                'LIMIT 1',
                (anchor_uid, anchor_uid),
            ).fetchone()
            if known is not None:
                player_id = int(known['player_id'])
        if player_id is None and room_id <= 0 and anchor_name:
            known = connection.execute(
                "SELECT id FROM vainglory_players WHERE origin='automatic' "
                'AND name=? COLLATE NOCASE ORDER BY id LIMIT 1',
                (anchor_name[:80],),
            ).fetchone()
            if known is not None:
                player_id = int(known['id'])
        if player_id is None:
            fallback = '玩家 {}'.format(room_id or session_id)
            display_name = (anchor_name or fallback)[:80]
            cursor = connection.execute(
                'INSERT INTO vainglory_players('
                'name,origin,created_at,updated_at) '
                "VALUES(?,'automatic',?,?)",
                (display_name, now, now),
            )
            player_id = int(cursor.lastrowid)
        if room_id > 0:
            connection.execute(
                'INSERT OR IGNORE INTO vainglory_player_rooms('
                'room_id,player_id,created_at,updated_at) VALUES(?,?,?,?)',
                (room_id, player_id, now, now),
            )
        else:
            connection.execute(
                'INSERT OR IGNORE INTO vainglory_player_sessions('
                'session_id,player_id,created_at,updated_at) VALUES(?,?,?,?)',
                (int(session_id), player_id, now, now),
            )

    @staticmethod
    def _normalize_player_display_name(name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ValueError('player name must not be empty')
        if len(normalized) > 80:
            raise ValueError('player name is too long')
        return normalized

    @staticmethod
    def _match_player(row: sqlite3.Row) -> MatchPlayerRecord:
        return MatchPlayerRecord(
            side=str(row['side']),
            slot=int(row['slot']),
            name=clean_player_name(str(row['player_name'])),
            normalized_name=str(row['normalized_name']),
            hero_id=None if row['hero_id'] is None else int(row['hero_id']),
            hero_label=str(row['hero_label']),
            hero_source=cast(Literal['automatic', 'manual'], str(row['hero_source'])),
            kills=None if row['kills'] is None else int(row['kills']),
            deaths=None if row['deaths'] is None else int(row['deaths']),
            assists=None if row['assists'] is None else int(row['assists']),
            economy=None if row['economy'] is None else int(row['economy']),
            last_hits=(None if row['last_hits'] is None else int(row['last_hits'])),
            confidence=float(row['confidence']),
            is_recorded_player=bool(int(row['is_recorded_player'])),
        )

    @staticmethod
    def _set_session_anchor(
        connection: sqlite3.Connection, session_id: int, anchor_name: str
    ) -> None:
        room_id = 0
        anchor_uid: Optional[int] = None
        if anchor_name:
            known = connection.execute(
                'SELECT room_id,anchor_uid FROM recording_sessions '
                'WHERE id!=? AND anchor_name=? AND anchor_uid IS NOT NULL '
                'AND anchor_uid>0 ORDER BY '
                "CASE WHEN broadcast_session_key LIKE 'bili-migration:%' "
                "OR broadcast_session_key LIKE 'bili-archive:%' "
                'THEN 1 ELSE 0 END,started_at DESC,id DESC LIMIT 1',
                (int(session_id), anchor_name),
            ).fetchone()
            if known is not None:
                room_id = int(known['room_id'])
                anchor_uid = int(known['anchor_uid'])
        connection.execute(
            'UPDATE recording_sessions SET room_id=?,anchor_uid=?,anchor_name=? '
            'WHERE id=?',
            (room_id, anchor_uid, anchor_name, int(session_id)),
        )

    @staticmethod
    def _match_session_record(row: sqlite3.Row) -> MatchSessionRecord:
        mode_order = ('3v3', '5v5', 'aram', 'other', 'unknown')
        present_modes = set(str(row['game_modes'] or '').split(','))
        return MatchSessionRecord(
            session_id=int(row['session_id']),
            title=str(row['title'] or ''),
            source_title=str(row['source_title'] or ''),
            anchor_name=str(row['anchor_name'] or ''),
            started_at=int(row['started_at']),
            match_count=int(row['match_count']),
            teal_win_count=int(row['teal_win_count'] or 0),
            orange_win_count=int(row['orange_win_count'] or 0),
            win_count=int(row['teal_win_count'] or 0),
            loss_count=int(row['orange_win_count'] or 0),
            unknown_count=max(
                0,
                int(row['match_count'])
                - int(row['teal_win_count'] or 0)
                - int(row['orange_win_count'] or 0),
            ),
            surrender_count=int(row['surrender_count'] or 0),
            duration_seconds=int(row['duration_seconds'] or 0),
            game_modes=tuple(mode for mode in mode_order if mode in present_modes),
            stats_included=bool(row['stats_included']),
            bvid=None if row['bvid'] is None else str(row['bvid']),
            publication_state=(
                None
                if row['publication_state'] is None
                else str(row['publication_state'])
            ),
            description_state=(
                None
                if row['description_state'] is None
                else str(row['description_state'])
            ),
            pin_state=None if row['pin_state'] is None else str(row['pin_state']),
            chapter_state=(
                None if row['chapter_state'] is None else str(row['chapter_state'])
            ),
        )

    @staticmethod
    def _match_filters(
        *,
        player_name: str,
        hero_ids: Sequence[int],
        winner_color: Optional[str],
        end_reason: Optional[str],
        game_mode: Optional[str],
        session_id: Optional[int],
    ) -> Tuple[List[str], List[object]]:
        if winner_color not in (None, 'teal', 'orange'):
            raise ValueError('winner color is invalid')
        if end_reason not in (None, 'normal', 'surrender', 'unknown'):
            raise ValueError('end reason is invalid')
        if game_mode not in (None, '3v3', '5v5', 'aram', 'other', 'unknown'):
            raise ValueError('game mode is invalid')
        where = ['1=1']
        parameters: List[object] = []
        normalized = normalize_player_name(player_name)
        if normalized:
            where.append(
                'EXISTS(SELECT 1 FROM vainglory_match_players searched '
                'WHERE searched.match_id=match.id '
                'AND searched.normalized_name LIKE ?)'
            )
            parameters.append('%{}%'.format(normalized))
        for hero_id in dict.fromkeys(int(value) for value in hero_ids):
            if hero_id < 1:
                raise ValueError('hero ID must be positive')
            where.append(
                'EXISTS(SELECT 1 FROM vainglory_match_players searched '
                'LEFT JOIN vainglory_heroes searched_hero '
                'ON searched_hero.id=searched.hero_id '
                'WHERE searched.match_id=match.id AND (searched.hero_id=? OR '
                "(searched_hero.label<>'' AND searched_hero.label=("
                'SELECT selected.label FROM vainglory_heroes selected '
                'WHERE selected.id=?) COLLATE NOCASE)))'
            )
            parameters.extend((hero_id, hero_id))
        if winner_color is not None:
            where.append(
                "(CASE match.winner_side WHEN 'left' THEN match.left_color "
                "WHEN 'right' THEN match.right_color ELSE 'unknown' END)=?"
            )
            parameters.append(winner_color)
        if end_reason is not None:
            where.append('match.end_reason=?')
            parameters.append(end_reason)
        if game_mode is not None:
            where.append('match.game_mode=?')
            parameters.append(game_mode)
        if session_id is not None:
            where.append('match.session_id=?')
            parameters.append(int(session_id))
        return where, parameters

    def _match_record(
        self, row: sqlite3.Row, players: Tuple[MatchPlayerRecord, ...]
    ) -> MatchRecord:
        winner_side = str(row['winner_side'])
        winner_color = (
            str(row['left_color'])
            if winner_side == 'left'
            else str(row['right_color']) if winner_side == 'right' else 'unknown'
        )
        source_title = str(row['session_title'] or '')
        upload_title = VaingloryRepository._upload_title(row['upload_title_source'])
        custom_title = '' if row['custom_title'] is None else str(row['custom_title'])
        archive_page = (
            None
            if row['archive_page'] is None or int(row['archive_page']) <= 0
            else int(row['archive_page'])
        )
        previous_archive_page = (
            None
            if row['previous_archive_page'] is None
            or int(row['previous_archive_page']) <= 0
            else int(row['previous_archive_page'])
        )
        previous_archive_duration_seconds = (
            None
            if row['previous_archive_duration_seconds'] is None
            or int(row['previous_archive_duration_seconds']) <= 0
            else int(row['previous_archive_duration_seconds'])
        )
        previous_archive_segments = self._archive_segments(
            row['previous_archive_segments']
        )
        has_result_frame = False
        if row['result_frame_path'] is not None:
            try:
                has_result_frame = self._resolve_result_frame_path(
                    str(row['result_frame_path'])
                ).is_file()
            except ValueError:
                pass
        recorded_player = next(
            (player for player in players if player.is_recorded_player), None
        )
        if row['team_size'] is None or int(row['team_size']) not in (3, 5):
            recorded_player_state = 'unsupported'
        elif recorded_player is not None:
            recorded_player_state = (
                'manual'
                if str(row['recorded_player_source']) == 'manual'
                else 'automatic'
            )
        elif (
            int(row['recorded_player_detection_version'])
            < self.RECORDED_PLAYER_DETECTION_VERSION
        ):
            recorded_player_state = 'pending'
        else:
            recorded_player_state = 'uncertain'
        return MatchRecord(
            id=int(row['id']),
            session_id=int(row['session_id']),
            session_title=source_title,
            session_started_at=int(row['session_started_at']),
            part_id=int(row['result_part_id']),
            part_index=int(row['part_index']),
            title=custom_title or upload_title or source_title,
            source_title=source_title,
            upload_title=upload_title,
            game_mode=str(row['game_mode']),
            team_size=(None if row['team_size'] is None else int(row['team_size'])),
            match_kind=cast(
                Literal['pvp', 'bot', 'practice', 'unknown'], str(row['match_kind'])
            ),
            view_context=cast(
                Literal['played', 'observed', 'unknown'], str(row['view_context'])
            ),
            stats_eligible=bool(int(row['stats_eligible'])),
            stats_exclusion_reason=(
                None
                if row['stats_exclusion_reason'] is None
                else str(row['stats_exclusion_reason'])
            ),
            started_at_ms=int(row['started_at_ms']),
            result_at_ms=int(row['result_at_ms']),
            duration_seconds=(
                None
                if row['duration_seconds'] is None
                else int(row['duration_seconds'])
            ),
            result_text=str(row['result_text']),
            end_reason=str(row['end_reason']),
            left_color=str(row['left_color']),
            right_color=str(row['right_color']),
            winner_side=winner_side,
            winner_color=winner_color,
            left_kills=(None if row['left_kills'] is None else int(row['left_kills'])),
            right_kills=(
                None if row['right_kills'] is None else int(row['right_kills'])
            ),
            left_economy=(
                None if row['left_economy'] is None else int(row['left_economy'])
            ),
            right_economy=(
                None if row['right_economy'] is None else int(row['right_economy'])
            ),
            confidence=float(row['confidence']),
            account_id=(None if row['account_id'] is None else int(row['account_id'])),
            bvid=None if row['bvid'] is None else str(row['bvid']),
            archive_page=archive_page,
            has_result_frame=has_result_frame,
            recorded_player_confidence=(
                None
                if row['recorded_player_confidence'] is None
                else float(row['recorded_player_confidence'])
            ),
            recorded_player_source=str(row['recorded_player_source']),
            recorded_player_state=recorded_player_state,
            players=players,
            previous_archive_page=previous_archive_page,
            previous_archive_duration_seconds=previous_archive_duration_seconds,
            previous_archive_segments=previous_archive_segments,
        )

    @staticmethod
    def _archive_segments(value: object) -> Tuple[Tuple[int, int], ...]:
        if value is None:
            return ()
        segments: Dict[int, int] = {}
        for encoded in str(value).split(','):
            page_text, separator, duration_text = encoded.partition(':')
            if not separator:
                continue
            try:
                page = int(page_text)
                duration = int(duration_text)
            except ValueError:
                continue
            if page > 0 and duration > 0:
                segments[page] = duration
        return tuple(sorted(segments.items(), reverse=True))

    @staticmethod
    def _upload_title(value: object) -> str:
        if value is None:
            return ''
        try:
            payload = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ''
        if not isinstance(payload, dict):
            return ''
        title = payload.get('title')
        return title.strip() if isinstance(title, str) else ''

    def _now(self) -> int:
        return max(1, int(self._clock()))
