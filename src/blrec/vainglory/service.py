from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from loguru import logger

from blrec.bili_upload.remote_media import (
    RemoteMediaCache,
    RemoteMediaNotFound,
    RemoteMediaUnavailable,
)

from .analyzer import (
    AnalysisCancelled,
    AnalysisStatus,
    AnalyzedHero,
    AnalyzedMatch,
    TrainingCandidate,
    VaingloryVideoAnalyzer,
    VideoPart,
)
from .archive_backfill import ArchiveBackfillUnavailable
from .hero_recognition import load_hero_references
from .repository import (
    AnalysisQueueEvent,
    AnalysisQueueItem,
    AnalysisQueueStatus,
    AnalysisWorkerRecord,
    AnchorStatsRecord,
    HeroStatsRecord,
    IndexSummary,
    ManualMatchMarkerRecord,
    MatchPage,
    MatchRecord,
    MatchSessionPage,
    MatchSessionRecord,
    PlayerRecord,
    PlayerStatsRecord,
    ScanJob,
    VaingloryConflict,
    VaingloryNotFound,
    VaingloryRepository,
    ZeroMatchSessionPage,
)
from .sampling import UnusableVideoError
from .vision import RecordedPlayer


@dataclass(frozen=True)
class RemoteAnalysisClaim:
    kind: Literal['part', 'match_rerun', 'hero_rematch', 'recorded_player_backfill']
    item_id: int
    part: Optional[VideoPart] = None
    session_id: Optional[int] = None
    result_at_ms: Optional[int] = None
    view_context: Literal['played', 'observed', 'unknown'] = 'unknown'
    frame_png: bytes = b''
    part_duration_seconds: Optional[int] = None
    recording_duration_seconds: Optional[int] = None
    anchor_name: str = ''


@dataclass(frozen=True)
class AnalysisWorkerNodeStatus:
    state: Literal['running', 'stopped', 'failed']
    worker_id: str
    display_name: str
    enabled: bool
    model_package_id: str
    pipeline_version: str
    last_seen_at: Optional[int]
    active_task_count: int = 0
    active_part_ids: Tuple[int, ...] = ()
    concurrency: int = 0
    completed_task_count: int = 0
    failed_task_count: int = 0
    total_processing_seconds: float = 0
    profiled_task_count: int = 0
    profiled_video_seconds: float = 0
    total_decode_analysis_seconds: float = 0
    total_profiled_task_seconds: float = 0
    last_task_finished_at: Optional[int] = None


@dataclass(frozen=True)
class AnalysisWorkerStatus:
    state: Literal['running', 'stopped', 'failed']
    remote_enabled: bool
    worker_id: str
    model_package_id: str
    pipeline_version: str
    last_seen_at: Optional[int]
    workers: Tuple[AnalysisWorkerNodeStatus, ...] = ()


@dataclass
class _RemoteWorkerRegistration:
    worker_id: str
    model_package_id: str
    pipeline_version: str
    concurrency: int
    last_seen: float
    last_seen_at: int


@dataclass
class _RemoteWorkAssignment:
    worker_id: str
    started_at: float
    last_seen: float


class VaingloryIndexService:
    def __init__(
        self,
        repository: VaingloryRepository,
        *,
        analyzer: Optional[VaingloryVideoAnalyzer] = None,
        remote_media_cache: Optional[RemoteMediaCache] = None,
        archive_page_reconciler: Optional[Callable[[int], Awaitable[int]]] = None,
        remote_worker_enabled: bool = False,
        idle_poll_seconds: float = 2,
        realtime_poll_seconds: float = 1,
    ) -> None:
        if idle_poll_seconds <= 0 or realtime_poll_seconds <= 0:
            raise ValueError('poll intervals must be positive')
        self._repository = repository
        self._analyzer = analyzer or VaingloryVideoAnalyzer()
        self._remote_media_cache = remote_media_cache
        self._archive_page_reconciler = archive_page_reconciler
        self._remote_worker_enabled = bool(remote_worker_enabled)
        self._remote_worker_last_seen = 0.0
        self._remote_worker_last_seen_at: Optional[int] = None
        self._remote_worker_id = ''
        self._remote_worker_model_package_id = ''
        self._remote_worker_pipeline_version = ''
        self._remote_workers: Dict[str, _RemoteWorkerRegistration] = {}
        self._remote_task_workers: Dict[Tuple[str, int], _RemoteWorkAssignment] = {}
        self._idle_poll_seconds = idle_poll_seconds
        self._realtime_poll_seconds = realtime_poll_seconds
        self._scan_wake = asyncio.Event()
        self._ocr_wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._scan_task: Optional[asyncio.Task[None]] = None
        self._ocr_task: Optional[asyncio.Task[None]] = None
        self._analysis_lock = asyncio.Lock()
        self._remote_claim_lock = asyncio.Lock()
        self._runtime_lock = threading.Lock()
        self._runtime_status: Dict[int, AnalysisStatus] = {}
        self._runtime_events: Dict[int, List[AnalysisQueueEvent]] = {}

    @property
    def repository(self) -> VaingloryRepository:
        return self._repository

    @property
    def worker_state(self) -> Literal['running', 'stopped', 'failed']:
        if self._remote_worker_enabled:
            with self._runtime_lock:
                last_seen = self._remote_worker_last_seen
            return (
                'running'
                if last_seen > 0 and time.monotonic() - last_seen <= 90
                else 'stopped'
            )
        tasks = tuple(
            task for task in (self._scan_task, self._ocr_task) if task is not None
        )
        if not tasks or all(task.cancelled() for task in tasks):
            return 'stopped'
        if any(
            task.done() and not task.cancelled() and task.exception() is not None
            for task in tasks
        ):
            return 'failed'
        if all(task.done() for task in tasks):
            return 'stopped'
        return 'running'

    @property
    def analysis_worker_status(self) -> AnalysisWorkerStatus:
        with self._runtime_lock:
            worker_id = self._remote_worker_id
            model_package_id = self._remote_worker_model_package_id
            pipeline_version = self._remote_worker_pipeline_version
            last_seen_at = self._remote_worker_last_seen_at
            now = time.monotonic()
            active_by_worker: Dict[str, List[Tuple[str, int]]] = {}
            for task, assignment in tuple(self._remote_task_workers.items()):
                if now - assignment.last_seen > 90:
                    self._remote_task_workers.pop(task, None)
                    continue
                active_by_worker.setdefault(assignment.worker_id, []).append(task)
            workers = tuple(
                AnalysisWorkerNodeStatus(
                    state=(
                        'running' if now - registration.last_seen <= 90 else 'stopped'
                    ),
                    worker_id=registration.worker_id,
                    display_name='',
                    enabled=True,
                    model_package_id=registration.model_package_id,
                    pipeline_version=registration.pipeline_version,
                    last_seen_at=registration.last_seen_at,
                    active_task_count=(
                        len(active_by_worker.get(registration.worker_id, ()))
                        if now - registration.last_seen <= 90
                        else 0
                    ),
                    active_part_ids=tuple(
                        sorted(
                            item_id
                            for kind, item_id in active_by_worker.get(
                                registration.worker_id, ()
                            )
                            if kind == 'part'
                        )
                    ),
                    concurrency=registration.concurrency,
                )
                for registration in sorted(
                    self._remote_workers.values(),
                    key=lambda value: (-value.last_seen, value.worker_id),
                )
            )
        return AnalysisWorkerStatus(
            state=self.worker_state,
            remote_enabled=self._remote_worker_enabled,
            worker_id=worker_id,
            model_package_id=model_package_id,
            pipeline_version=pipeline_version,
            last_seen_at=last_seen_at,
            workers=workers,
        )

    async def list_analysis_workers(self) -> Tuple[AnalysisWorkerNodeStatus, ...]:
        records = await self._repository.list_analysis_workers()
        with self._runtime_lock:
            registrations = dict(self._remote_workers)
            assignments = dict(self._remote_task_workers)
        now = time.monotonic()
        active_by_worker: Dict[str, List[Tuple[str, int]]] = {}
        for task, assignment in assignments.items():
            if now - assignment.last_seen <= 90:
                active_by_worker.setdefault(assignment.worker_id, []).append(task)
        return tuple(
            self._analysis_worker_node(record, registrations, active_by_worker, now)
            for record in records
        )

    @staticmethod
    def _analysis_worker_node(
        record: AnalysisWorkerRecord,
        registrations: Mapping[str, _RemoteWorkerRegistration],
        active_by_worker: Mapping[str, Sequence[Tuple[str, int]]],
        now: float,
    ) -> AnalysisWorkerNodeStatus:
        registration = registrations.get(record.worker_id)
        online = registration is not None and now - registration.last_seen <= 90
        active = active_by_worker.get(record.worker_id, ()) if online else ()
        return AnalysisWorkerNodeStatus(
            state='running' if online else 'stopped',
            worker_id=record.worker_id,
            display_name=record.display_name,
            enabled=record.enabled,
            model_package_id=(
                record.model_package_id
                if registration is None
                else registration.model_package_id
            ),
            pipeline_version=(
                record.pipeline_version
                if registration is None
                else registration.pipeline_version
            ),
            last_seen_at=(
                record.last_seen_at
                if registration is None
                else registration.last_seen_at
            ),
            active_task_count=len(active),
            active_part_ids=tuple(
                sorted(item_id for kind, item_id in active if kind == 'part')
            ),
            concurrency=(
                record.concurrency if registration is None else registration.concurrency
            ),
            completed_task_count=record.completed_task_count,
            failed_task_count=record.failed_task_count,
            total_processing_seconds=record.total_processing_seconds,
            profiled_task_count=record.profiled_task_count,
            profiled_video_seconds=record.profiled_video_seconds,
            total_decode_analysis_seconds=record.total_decode_analysis_seconds,
            total_profiled_task_seconds=record.total_profiled_task_seconds,
            last_task_finished_at=record.last_task_finished_at,
        )

    async def add_analysis_worker(
        self, worker_id: str, display_name: str
    ) -> AnalysisWorkerNodeStatus:
        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError('Worker ID 不能为空')
        await self._repository.add_analysis_worker(worker_id, display_name)
        workers = await self.list_analysis_workers()
        return next(worker for worker in workers if worker.worker_id == worker_id)

    async def update_analysis_worker(
        self,
        worker_id: str,
        *,
        display_name: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> AnalysisWorkerNodeStatus:
        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError('Worker ID 不能为空')
        if enabled is None:
            await self._repository.update_analysis_worker(
                worker_id, display_name=display_name
            )
        else:
            async with self._remote_claim_lock:
                await self._repository.update_analysis_worker(
                    worker_id, display_name=display_name, enabled=enabled
                )
        workers = await self.list_analysis_workers()
        return next(worker for worker in workers if worker.worker_id == worker_id)

    async def start(self) -> None:
        if self._scan_task is not None or self._ocr_task is not None:
            return
        await self._repository.purge_excluded_content()
        await self._repository.recover_interrupted()
        await self._repository.apply_builtin_hero_labels()
        await self._repository.consolidate_hero_catalog()
        await self._repository.discover_ready_parts()
        references = load_hero_references()
        if len(references) != 57:
            logger.warning(
                'Vainglory hero reference catalog is incomplete: expected=57 actual={}',
                len(references),
            )
        await self._repository.sync_hero_references(references)
        self._stop.clear()
        if self._remote_worker_enabled:
            recovered = await self._repository.prepare_remote_worker()
            logger.info(
                'Vainglory remote analysis worker mode enabled: recovered={}', recovered
            )
            return
        self._scan_wake.set()
        self._ocr_wake.set()
        self._scan_task = asyncio.create_task(
            self._run_scan(), name='vainglory-video-scan'
        )
        self._ocr_task = asyncio.create_task(
            self._run_ocr(), name='vainglory-result-ocr'
        )

    async def close(self) -> None:
        tasks = tuple(
            task for task in (self._scan_task, self._ocr_task) if task is not None
        )
        if not tasks:
            return
        self._stop.set()
        self._scan_wake.set()
        self._ocr_wake.set()
        await asyncio.gather(*tasks)
        self._scan_task = None
        self._ocr_task = None

    @property
    def remote_worker_enabled(self) -> bool:
        return self._remote_worker_enabled

    def _require_remote_worker(
        self,
        *,
        worker_id: str = '',
        model_package_id: str = '',
        pipeline_version: str = '',
        concurrency: int = 0,
    ) -> None:
        if not self._remote_worker_enabled:
            raise VaingloryConflict('远程分析 Worker 未启用')
        with self._runtime_lock:
            now = time.monotonic()
            now_at = int(time.time())
            self._remote_worker_last_seen = now
            self._remote_worker_last_seen_at = now_at
            if worker_id:
                self._remote_worker_id = worker_id
                previous = self._remote_workers.get(worker_id)
                registration = _RemoteWorkerRegistration(
                    worker_id=worker_id,
                    model_package_id=(
                        model_package_id
                        or ('' if previous is None else previous.model_package_id)
                    ),
                    pipeline_version=(
                        pipeline_version
                        or ('' if previous is None else previous.pipeline_version)
                    ),
                    concurrency=(
                        concurrency or (0 if previous is None else previous.concurrency)
                    ),
                    last_seen=now,
                    last_seen_at=now_at,
                )
                self._remote_workers[worker_id] = registration
                self._remote_worker_model_package_id = registration.model_package_id
                self._remote_worker_pipeline_version = registration.pipeline_version
            else:
                if model_package_id:
                    self._remote_worker_model_package_id = model_package_id
                if pipeline_version:
                    self._remote_worker_pipeline_version = pipeline_version

    def register_remote_worker_activity(
        self,
        *,
        worker_id: str = '',
        model_package_id: str = '',
        pipeline_version: str = '',
        concurrency: int = 0,
    ) -> None:
        self._require_remote_worker(
            worker_id=worker_id,
            model_package_id=model_package_id,
            pipeline_version=pipeline_version,
            concurrency=concurrency,
        )

    def _assign_remote_work(
        self, claim: RemoteAnalysisClaim, worker_id: str
    ) -> RemoteAnalysisClaim:
        if not worker_id:
            return claim
        with self._runtime_lock:
            now = time.monotonic()
            self._remote_task_workers[(claim.kind, claim.item_id)] = (
                _RemoteWorkAssignment(
                    worker_id=worker_id, started_at=now, last_seen=now
                )
            )
        return claim

    def _touch_remote_work(self, kind: str, item_id: int, worker_id: str = '') -> None:
        with self._runtime_lock:
            key = (kind, item_id)
            assignment = self._remote_task_workers.get(key)
            assigned_worker_id = worker_id or (
                '' if assignment is None else assignment.worker_id
            )
            registration = self._remote_workers.get(assigned_worker_id)
            if registration is None:
                return
            now = time.monotonic()
            now_at = int(time.time())
            self._remote_task_workers[key] = _RemoteWorkAssignment(
                worker_id=assigned_worker_id,
                started_at=(now if assignment is None else assignment.started_at),
                last_seen=now,
            )
            registration.last_seen = now
            registration.last_seen_at = now_at
            self._remote_worker_last_seen = now
            self._remote_worker_last_seen_at = now_at

    def _clear_remote_work(
        self, kind: str, item_id: int
    ) -> Optional[_RemoteWorkAssignment]:
        with self._runtime_lock:
            return self._remote_task_workers.pop((kind, item_id), None)

    async def _record_remote_work_result(
        self,
        kind: str,
        item_id: int,
        *,
        succeeded: bool,
        video_duration_seconds: Optional[float] = None,
        decode_analysis_seconds: Optional[float] = None,
    ) -> None:
        assignment = self._clear_remote_work(kind, item_id)
        if assignment is None:
            return
        await self._repository.record_analysis_worker_task(
            assignment.worker_id,
            succeeded=succeeded,
            processing_seconds=max(0.0, time.monotonic() - assignment.started_at),
            video_duration_seconds=video_duration_seconds,
            decode_analysis_seconds=decode_analysis_seconds,
        )

    def remote_worker_for(self, kind: str, item_id: int) -> str:
        with self._runtime_lock:
            assignment = self._remote_task_workers.get((kind, item_id))
            if assignment is None or time.monotonic() - assignment.last_seen > 90:
                return ''
            return assignment.worker_id

    async def claim_remote_work(
        self,
        *,
        worker_id: str = '',
        model_package_id: str = '',
        pipeline_version: str = '',
        concurrency: int = 0,
    ) -> Optional[RemoteAnalysisClaim]:
        async with self._remote_claim_lock:
            return await self._claim_remote_work(
                worker_id=worker_id,
                model_package_id=model_package_id,
                pipeline_version=pipeline_version,
                concurrency=concurrency,
            )

    async def _claim_remote_work(
        self,
        *,
        worker_id: str,
        model_package_id: str,
        pipeline_version: str,
        concurrency: int,
    ) -> Optional[RemoteAnalysisClaim]:
        if worker_id:
            worker = await self._repository.register_analysis_worker(
                worker_id,
                model_package_id=model_package_id,
                pipeline_version=pipeline_version,
                concurrency=concurrency,
            )
            if not worker.enabled:
                self._require_remote_worker(
                    worker_id=worker_id,
                    model_package_id=model_package_id,
                    pipeline_version=pipeline_version,
                    concurrency=concurrency,
                )
                return None
        self._require_remote_worker(
            worker_id=worker_id,
            model_package_id=model_package_id,
            pipeline_version=pipeline_version,
            concurrency=concurrency,
        )
        recovered = await self._repository.recover_stale_remote_work(180)
        if recovered:
            logger.warning('Recovered stale remote Vainglory work: count={}', recovered)

        rerun = await self._repository.claim_next_match_rerun()
        if rerun is not None:
            return self._assign_remote_work(
                RemoteAnalysisClaim(
                    kind='match_rerun',
                    item_id=rerun.match_id,
                    part=rerun.part,
                    session_id=rerun.session_id,
                    result_at_ms=rerun.result_at_ms,
                    view_context=rerun.view_context,
                ),
                worker_id,
            )

        if not await self._repository.has_realtime_pending():
            recorded_player = await self._repository.next_recorded_player_backfill()
            if recorded_player is not None:
                path = await self._repository.result_frame_path(
                    recorded_player.match_id
                )
                if path is not None:
                    return self._assign_remote_work(
                        RemoteAnalysisClaim(
                            kind='recorded_player_backfill',
                            item_id=recorded_player.match_id,
                            frame_png=path.read_bytes(),
                        ),
                        worker_id,
                    )
                await self._repository.complete_recorded_player_backfill(
                    recorded_player.match_id, None
                )

            hero = await self._repository.next_hero_rematch()
            if hero is not None:
                path = await self._repository.result_frame_path(hero.match_id)
                if path is not None:
                    return self._assign_remote_work(
                        RemoteAnalysisClaim(
                            kind='hero_rematch',
                            item_id=hero.match_id,
                            frame_png=path.read_bytes(),
                        ),
                        worker_id,
                    )
                await self._repository.complete_hero_rematch(hero.match_id, ())

        claim = await self._repository.claim_next()
        if claim is None:
            return None
        return self._assign_remote_work(
            RemoteAnalysisClaim(
                kind='part',
                item_id=claim.part.id,
                part=claim.part,
                session_id=claim.session_id,
                part_duration_seconds=claim.part_duration_seconds,
                recording_duration_seconds=claim.recording_duration_seconds,
                anchor_name=claim.anchor_name,
            ),
            worker_id,
        )

    async def heartbeat_remote_work(
        self,
        kind: str,
        item_id: int,
        progress: float,
        status: Optional[AnalysisStatus] = None,
        *,
        worker_id: str = '',
        model_package_id: str = '',
        pipeline_version: str = '',
        concurrency: int = 0,
    ) -> None:
        self._require_remote_worker(
            worker_id=worker_id,
            model_package_id=model_package_id,
            pipeline_version=pipeline_version,
            concurrency=concurrency,
        )
        self._touch_remote_work(kind, item_id, worker_id)
        if kind == 'part':
            await self._repository.update_progress(item_id, progress)
            if status is not None:
                self._record_runtime_status(item_id, status)
        elif kind == 'match_rerun':
            await self._repository.touch_match_rerun(item_id)

    async def complete_remote_part(
        self,
        part_id: int,
        matches: Sequence[AnalyzedMatch],
        *,
        candidate_count: int,
        training_candidates: Sequence[TrainingCandidate] = (),
        analysis_summary: Optional[Mapping[str, Any]] = None,
        video_duration_seconds: Optional[float] = None,
        decode_analysis_seconds: Optional[float] = None,
    ) -> None:
        self._require_remote_worker()
        await self._repository.complete_part(
            part_id,
            matches,
            candidate_count=candidate_count,
            training_candidates=training_candidates,
            analysis_summary=analysis_summary,
        )
        self._clear_runtime_status(part_id)
        await self._record_remote_work_result(
            'part',
            part_id,
            succeeded=True,
            video_duration_seconds=video_duration_seconds,
            decode_analysis_seconds=decode_analysis_seconds,
        )

    async def complete_remote_match_rerun(
        self, match_id: int, match: AnalyzedMatch
    ) -> None:
        self._require_remote_worker()
        await self._repository.complete_match_rerun(match_id, match)
        await self._record_remote_work_result('match_rerun', match_id, succeeded=True)

    async def complete_remote_hero_rematch(
        self, match_id: int, heroes: Sequence[AnalyzedHero]
    ) -> None:
        self._require_remote_worker()
        await self._repository.complete_hero_rematch(match_id, heroes)
        await self._record_remote_work_result('hero_rematch', match_id, succeeded=True)

    async def complete_remote_recorded_player_backfill(
        self, match_id: int, player: Optional[RecordedPlayer]
    ) -> None:
        self._require_remote_worker()
        await self._repository.complete_recorded_player_backfill(match_id, player)
        await self._record_remote_work_result(
            'recorded_player_backfill', match_id, succeeded=True
        )

    async def fail_remote_work(
        self,
        kind: str,
        item_id: int,
        error: str,
        failure_kind: Literal['task_error', 'unusable_media'] = 'task_error',
    ) -> None:
        self._require_remote_worker()
        if kind == 'match_rerun':
            await self._repository.fail_match_rerun(item_id, error)
        elif kind == 'part':
            if failure_kind == 'unusable_media':
                await self._repository.ignore_unusable_part(item_id, error)
            else:
                await self._repository.fail(item_id, error)
            self._clear_runtime_status(item_id)
        elif kind == 'hero_rematch':
            await self._repository.complete_hero_rematch(item_id, ())
        elif kind == 'recorded_player_backfill':
            await self._repository.complete_recorded_player_backfill(item_id, None)
        await self._record_remote_work_result(kind, item_id, succeeded=False)

    async def request_scan(self, session_id: int) -> ScanJob:
        if self._archive_page_reconciler is not None:
            try:
                added_pages = await self._archive_page_reconciler(session_id)
            except ArchiveBackfillUnavailable as error:
                raise VaingloryConflict(
                    '重新分析前核对 B 站分 P 失败：{}'.format(error)
                ) from error
            if added_pages:
                logger.info(
                    'Vainglory reanalysis added missing archive pages: '
                    'session_id={} pages={}',
                    session_id,
                    added_pages,
                )
        if self._remote_media_cache is None:
            job = await self._repository.request_scan(session_id)
        else:
            job, remote_part_ids = (
                await self._repository.request_scan_with_remote_media(session_id)
            )
            try:
                for part_id in remote_part_ids:
                    await self._remote_media_cache.request(part_id, force_remote=True)
            except (RemoteMediaNotFound, RemoteMediaUnavailable) as error:
                raise VaingloryConflict(
                    '无法重新下载稿件视频：{}'.format(error)
                ) from error
        self._scan_wake.set()
        return job

    async def get_job(self, session_id: int) -> Optional[ScanJob]:
        return await self._repository.get_job(session_id)

    async def analysis_queue_status(self) -> AnalysisQueueStatus:
        queue = await self._repository.analysis_queue_status()
        with self._runtime_lock:
            statuses = dict(self._runtime_status)
            events = {
                part_id: tuple(items) for part_id, items in self._runtime_events.items()
            }

        def enrich(item: AnalysisQueueItem) -> AnalysisQueueItem:
            status = statuses.get(item.part_id)
            if status is None:
                return item
            item_events = events.get(item.part_id, ())
            return replace(
                item,
                updated_at=max(
                    item.updated_at,
                    item_events[-1].at if item_events else item.updated_at,
                ),
                runtime_stage=status.stage,
                runtime_detail=status.detail,
                runtime_elapsed_seconds=status.elapsed_seconds,
                coarse_frames=status.coarse_frames,
                gameplay_runs=status.gameplay_runs,
                result_windows=status.result_windows,
                current_window=status.current_window,
                total_windows=status.total_windows,
                candidate_count=status.candidate_count,
                current_candidate=status.current_candidate,
                total_candidates=status.total_candidates,
                rejected_candidates=status.rejected_candidates,
                recognized_matches=status.recognized_matches,
                model_package_id=status.model_package_id,
                keyframe_frames=status.keyframe_frames,
                seek_fill_frames=status.seek_fill_frames,
                decoded_result_frames=status.decoded_result_frames,
                mode_conflict_count=status.mode_conflict_count,
                hud_lineup_candidate_count=status.hud_lineup_candidate_count,
                training_candidate_count=status.training_candidate_count,
                events=item_events,
            )

        return replace(
            queue,
            active=tuple(enrich(item) for item in queue.active),
            queued=tuple(enrich(item) for item in queue.queued),
        )

    def _record_runtime_status(self, part_id: int, status: AnalysisStatus) -> None:
        with self._runtime_lock:
            previous = self._runtime_status.get(part_id)
            if previous is not None:
                status = replace(
                    status,
                    coarse_frames=status.coarse_frames or previous.coarse_frames,
                    gameplay_runs=status.gameplay_runs or previous.gameplay_runs,
                    result_windows=status.result_windows or previous.result_windows,
                    current_window=status.current_window or previous.current_window,
                    total_windows=status.total_windows or previous.total_windows,
                    candidate_count=status.candidate_count or previous.candidate_count,
                    model_package_id=(
                        status.model_package_id or previous.model_package_id
                    ),
                    keyframe_frames=(
                        status.keyframe_frames or previous.keyframe_frames
                    ),
                    seek_fill_frames=(
                        status.seek_fill_frames or previous.seek_fill_frames
                    ),
                    decoded_result_frames=(
                        status.decoded_result_frames or previous.decoded_result_frames
                    ),
                    mode_conflict_count=(
                        status.mode_conflict_count or previous.mode_conflict_count
                    ),
                    hud_lineup_candidate_count=(
                        status.hud_lineup_candidate_count
                        or previous.hud_lineup_candidate_count
                    ),
                    training_candidate_count=(
                        status.training_candidate_count
                        or previous.training_candidate_count
                    ),
                )
            self._runtime_status[part_id] = status
            if (
                previous is None
                or previous.stage != status.stage
                or previous.detail != status.detail
            ):
                events = self._runtime_events.setdefault(part_id, [])
                events.append(
                    AnalysisQueueEvent(
                        at=int(time.time()),
                        stage=status.stage,
                        detail=status.detail,
                        elapsed_seconds=status.elapsed_seconds,
                    )
                )
                del events[:-12]

    def _clear_runtime_status(self, part_id: int) -> None:
        with self._runtime_lock:
            self._runtime_status.pop(part_id, None)
            self._runtime_events.pop(part_id, None)

    async def index_summary(self) -> IndexSummary:
        return await self._repository.index_summary()

    async def list_matches(self, **filters: Any) -> MatchPage:
        return await self._repository.list_matches(**filters)

    async def suppress_match_review(self, match_id: int, review_type: str) -> None:
        await self._repository.suppress_match_review(match_id, review_type)

    async def list_match_sessions(self, **filters: Any) -> MatchSessionPage:
        return await self._repository.list_match_sessions(**filters)

    async def list_zero_match_sessions(
        self, *, limit: int = 20, offset: int = 0, suppressed: bool = False
    ) -> ZeroMatchSessionPage:
        return await self._repository.list_zero_match_sessions(
            limit=limit, offset=offset, suppressed=suppressed
        )

    async def suppress_zero_match_session(self, session_id: int) -> None:
        await self._repository.suppress_zero_match_session(session_id)

    async def restore_zero_match_session(self, session_id: int) -> None:
        await self._repository.restore_zero_match_session(session_id)
        self._scan_wake.set()

    async def find_video_part(
        self, bvid: str, page: int
    ) -> Optional[ManualMatchMarkerRecord]:
        return await self._repository.find_video_part(bvid, page)

    async def mark_video_match(
        self, *, bvid: str, page: int, at_ms: int
    ) -> ManualMatchMarkerRecord:
        target = await self._repository.find_video_part(bvid, page)
        if target is None:
            raise VaingloryNotFound('这个稿件分 P 尚未进入对局索引')
        await self._prepare_manual_marker_media(target.part_id)
        marker = await self._repository.create_manual_match_marker(
            target.session_id,
            part_index=target.part_index,
            at_ms=at_ms,
            source='browser_extension',
        )
        self._scan_wake.set()
        return marker

    async def mark_session_match(
        self, session_id: int, *, part_index: int, at_ms: int
    ) -> ManualMatchMarkerRecord:
        target = await self._repository.find_session_part(session_id, part_index)
        if target is None:
            raise VaingloryNotFound('这场直播不存在该分 P')
        await self._prepare_manual_marker_media(target.part_id)
        marker = await self._repository.create_manual_match_marker(
            session_id, part_index=part_index, at_ms=at_ms, source='dashboard'
        )
        self._scan_wake.set()
        return marker

    async def _prepare_manual_marker_media(self, part_id: int) -> None:
        if self._remote_media_cache is None:
            return
        try:
            await self._remote_media_cache.request(part_id)
        except (RemoteMediaNotFound, RemoteMediaUnavailable) as error:
            raise VaingloryConflict(str(error)) from error

    async def list_recorded_player_reviews(
        self, *, limit: int = 50, offset: int = 0
    ) -> MatchPage:
        return await self._repository.list_recorded_player_reviews(
            limit=limit, offset=offset
        )

    async def list_hero_reviews(self, *, limit: int = 50, offset: int = 0) -> MatchPage:
        return await self._repository.list_hero_reviews(limit=limit, offset=offset)

    async def list_anchor_stats(self) -> Tuple[AnchorStatsRecord, ...]:
        return await self._repository.list_anchor_stats()

    async def list_players(self) -> Tuple[PlayerRecord, ...]:
        return await self._repository.list_players()

    async def create_player(self, name: str) -> PlayerRecord:
        return await self._repository.create_player(name)

    async def ensure_players_for_rooms(
        self, rooms: Sequence[Tuple[int, str]]
    ) -> Tuple[PlayerRecord, ...]:
        return await self._repository.ensure_players_for_rooms(rooms)

    async def rename_player(self, player_id: int, name: str) -> PlayerRecord:
        return await self._repository.rename_player(player_id, name)

    async def bind_player_room(self, player_id: int, room_id: int) -> PlayerRecord:
        return await self._repository.bind_player_room(player_id, room_id)

    async def unbind_player_room(self, player_id: int, room_id: int) -> PlayerRecord:
        return await self._repository.unbind_player_room(player_id, room_id)

    async def delete_player(self, player_id: int) -> None:
        await self._repository.delete_player(player_id)

    async def list_player_stats(self) -> Tuple[PlayerStatsRecord, ...]:
        return await self._repository.list_player_stats()

    async def list_hero_stats(
        self, *, game_mode: str = ''
    ) -> Tuple[HeroStatsRecord, ...]:
        return await self._repository.list_hero_stats(game_mode=game_mode)

    async def update_match_title(self, match_id: int, title: str) -> MatchRecord:
        return await self._repository.update_match_title(match_id, title)

    async def update_match_fields(
        self, match_id: int, changes: Dict[str, Any]
    ) -> MatchRecord:
        return await self._repository.update_match_fields(match_id, changes)

    async def request_match_rerun(self, match_id: int) -> None:
        match = await self._repository.get_match(match_id)
        await self._prepare_manual_marker_media(match.part_id)
        await self._repository.request_match_rerun(match_id)
        self._ocr_wake.set()

    async def delete_match(self, match_id: int) -> None:
        await self._repository.delete_match(match_id)

    async def set_recorded_player(
        self, match_id: int, *, side: str, slot: int
    ) -> MatchRecord:
        return await self._repository.set_recorded_player(
            match_id, side=side, slot=slot
        )

    async def set_player_hero(
        self, match_id: int, *, side: str, slot: int, hero_id: int
    ) -> MatchRecord:
        return await self._repository.set_player_hero(
            match_id, side=side, slot=slot, hero_id=hero_id
        )

    async def update_session_title(
        self, session_id: int, title: str
    ) -> MatchSessionRecord:
        return await self._repository.update_session_title(session_id, title)

    async def update_session_anchor(
        self, session_id: int, anchor_name: str
    ) -> MatchSessionRecord:
        return await self._repository.update_session_anchor(session_id, anchor_name)

    async def bulk_update_sessions(
        self,
        session_ids: Sequence[int],
        *,
        anchor_name: Optional[str] = None,
        stats_included: Optional[bool] = None,
    ) -> int:
        return await self._repository.bulk_update_sessions(
            session_ids, anchor_name=anchor_name, stats_included=stats_included
        )

    async def run_once(self) -> bool:
        if await self._run_hero_rematch_once():
            return True
        claim = await self._repository.claim_next()
        if claim is None:
            return False
        session_id = claim.session_id
        part = claim.part
        if not Path(part.path).is_file():
            await self._repository.fail(part.id, '视频文件不存在，未开始扫描')
            return True
        loop = asyncio.get_running_loop()
        preempt = threading.Event()
        monitor: Optional[asyncio.Task[None]] = None
        if not claim.realtime:
            monitor = asyncio.create_task(
                self._watch_for_preemption(part.id, preempt),
                name='vainglory-background-watch',
            )
        try:
            async with self._analysis_lock:
                last_progress = -0.05

                def report(part_progress: float) -> None:
                    nonlocal last_progress
                    progress = max(0.0, min(1.0, part_progress))
                    if progress < last_progress + 0.05 and progress < 0.99:
                        return
                    last_progress = progress
                    update = asyncio.run_coroutine_threadsafe(
                        self._repository.update_progress(part.id, progress), loop
                    )
                    try:
                        update.result(timeout=10)
                    except TimeoutError:
                        logger.warning(
                            'Vainglory progress update delayed by database load: '
                            'part_id={} stage=analysis',
                            part.id,
                        )

                matches: Tuple[AnalyzedMatch, ...] = await loop.run_in_executor(
                    None,
                    lambda: self._analyzer.analyze_part(
                        part,
                        progress=report,
                        cancelled=lambda: self._stop.is_set() or preempt.is_set(),
                    ),
                )
            await self._repository.complete_part(part.id, matches)
        except AnalysisCancelled:
            if self._stop.is_set() or preempt.is_set():
                await self._repository.requeue(part.id)
            else:
                await self._repository.fail(part.id, '对局分析意外停止')
        except UnusableVideoError as error:
            logger.warning(
                'Ignored unusable Vainglory media for session {} part {}: {!r}',
                session_id,
                part.id,
                error,
            )
            await self._repository.ignore_unusable_part(
                part.id, '{}: {}'.format(type(error).__name__, error)
            )
        except Exception as error:
            logger.exception(
                'Vainglory match analysis failed for session {} part {}',
                session_id,
                part.id,
            )
            await self._repository.fail(
                part.id, '{}: {}'.format(type(error).__name__, error)
            )
        finally:
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
        return True

    async def _run_hero_rematch_once(self) -> bool:
        rematch = await self._repository.next_hero_rematch()
        if rematch is not None:
            path = await self._repository.result_frame_path(rematch.match_id)
            heroes: Tuple[AnalyzedHero, ...] = ()
            if path is not None:
                try:
                    loop = asyncio.get_running_loop()
                    content = await loop.run_in_executor(None, path.read_bytes)
                    heroes = await loop.run_in_executor(
                        None, lambda: self._analyzer.recognize_saved_heroes(content)
                    )
                except (OSError, RuntimeError, ValueError):
                    logger.exception(
                        'Vainglory stored hero rematch failed for match {}',
                        rematch.match_id,
                    )
            updated = await self._repository.complete_hero_rematch(
                rematch.match_id, heroes
            )
            logger.info(
                'Vainglory stored hero rematch completed: match_id={} updated={}',
                rematch.match_id,
                updated,
            )
            return True
        return False

    async def _run_recorded_player_backfill_once(self) -> bool:
        backfill = await self._repository.next_recorded_player_backfill()
        if backfill is None:
            return False
        path = await self._repository.result_frame_path(backfill.match_id)
        player = None
        if path is not None:
            try:
                loop = asyncio.get_running_loop()
                content = await loop.run_in_executor(None, path.read_bytes)
                player = await loop.run_in_executor(
                    None, lambda: self._analyzer.detect_saved_recorded_player(content)
                )
            except (OSError, RuntimeError, ValueError):
                logger.exception(
                    'Vainglory recorded player backfill failed for match {}',
                    backfill.match_id,
                )
        detected = await self._repository.complete_recorded_player_backfill(
            backfill.match_id, player
        )
        logger.info(
            'Vainglory recorded player backfill completed: match_id={} detected={}',
            backfill.match_id,
            detected,
        )
        return True

    async def _scan_once(self) -> bool:
        if not await self._repository.has_realtime_pending():
            if await self._run_recorded_player_backfill_once():
                return True
            if await self._run_hero_rematch_once():
                return True
        claim = await self._repository.claim_next()
        if claim is None:
            return False
        session_id = claim.session_id
        part = claim.part
        task_started = time.monotonic()
        if not Path(part.path).is_file():
            message = '视频文件不存在，未开始扫描'
            logger.warning(
                'Vainglory scan skipped unavailable video: session_id={} '
                'part_id={} path={!r}',
                session_id,
                part.id,
                part.path,
            )
            await self._repository.fail(part.id, message)
            return True
        logger.info(
            'Vainglory part analysis task started: session_id={} part_id={} '
            'part_index={} realtime={} part_duration_seconds={} '
            'recording_duration_seconds={}',
            session_id,
            part.id,
            part.index,
            claim.realtime,
            claim.part_duration_seconds,
            claim.recording_duration_seconds,
        )
        loop = asyncio.get_running_loop()
        preempt = threading.Event()
        monitor: Optional[asyncio.Task[None]] = None
        if not claim.realtime:
            monitor = asyncio.create_task(
                self._watch_for_preemption(part.id, preempt),
                name='vainglory-background-watch',
            )
        try:
            last_progress = -0.05

            def report(scan_progress: float) -> None:
                nonlocal last_progress
                progress = max(0.0, min(0.69, scan_progress * 0.69))
                if progress < last_progress + 0.05 and progress < 0.68:
                    return
                last_progress = progress
                update = asyncio.run_coroutine_threadsafe(
                    self._repository.update_progress(part.id, progress), loop
                )
                try:
                    update.result(timeout=10)
                except TimeoutError:
                    logger.warning(
                        'Vainglory progress update delayed by database load: '
                        'part_id={} stage=scan',
                        part.id,
                    )

            scanned = await loop.run_in_executor(
                None,
                lambda: self._analyzer.scan_part(
                    part,
                    progress=report,
                    status_callback=lambda status: self._record_runtime_status(
                        part.id, status
                    ),
                    cancelled=lambda: self._stop.is_set() or preempt.is_set(),
                ),
            )
            if scanned.candidate_times_ms:
                await self._repository.enqueue_ocr(part.id, scanned)
                self._record_runtime_status(
                    part.id,
                    AnalysisStatus(
                        stage='ocr_waiting',
                        detail='已定位 {} 个结算候选，等待 OCR 与英雄识别'.format(
                            len(scanned.candidate_times_ms)
                        ),
                        elapsed_seconds=time.monotonic() - task_started,
                        candidate_count=len(scanned.candidate_times_ms),
                        total_candidates=len(scanned.candidate_times_ms),
                    ),
                )
                self._ocr_wake.set()
                logger.info(
                    'Vainglory part scan stage completed: session_id={} '
                    'part_id={} candidates={} scan_seconds={:.3f}',
                    session_id,
                    part.id,
                    len(scanned.candidate_times_ms),
                    time.monotonic() - task_started,
                )
            else:
                await self._repository.complete_part(part.id, (), candidate_count=0)
                elapsed_seconds = time.monotonic() - task_started
                logger.info(
                    'Vainglory part analysis task completed: session_id={} '
                    'part_id={} matches=0 candidates=0 total_seconds={:.3f}',
                    session_id,
                    part.id,
                    elapsed_seconds,
                )
                self._clear_runtime_status(part.id)
        except AnalysisCancelled:
            if self._stop.is_set() or preempt.is_set():
                await self._repository.requeue(part.id)
            else:
                await self._repository.fail(part.id, '对局分析意外停止')
            logger.info(
                'Vainglory part analysis task stopped: session_id={} part_id={} '
                'elapsed_seconds={:.3f}',
                session_id,
                part.id,
                time.monotonic() - task_started,
            )
            self._clear_runtime_status(part.id)
        except UnusableVideoError as error:
            logger.warning(
                'Ignored unusable Vainglory media for session {} part {}: {!r}',
                session_id,
                part.id,
                error,
            )
            await self._repository.ignore_unusable_part(
                part.id, '{}: {}'.format(type(error).__name__, error)
            )
            self._clear_runtime_status(part.id)
        except Exception as error:
            logger.exception(
                'Vainglory video scan failed for session {} part {}',
                session_id,
                part.id,
            )
            await self._repository.fail(
                part.id, '{}: {}'.format(type(error).__name__, error)
            )
            self._clear_runtime_status(part.id)
        finally:
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
        return True

    async def _ocr_once(self) -> bool:
        rerun = await self._repository.claim_next_match_rerun()
        if rerun is not None:
            claimed_rerun = rerun
            part = claimed_rerun.part
            if not Path(part.path).is_file():
                await self._repository.fail_match_rerun(
                    claimed_rerun.match_id, '视频文件尚未下载完成，请稍后重试'
                )
                return True
            try:
                rerun_matches = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: self._analyzer.recognize_candidate(
                        part,
                        at_ms=claimed_rerun.result_at_ms,
                        view_context=claimed_rerun.view_context,
                        cancelled=self._stop.is_set,
                    ),
                )
                if len(rerun_matches) != 1:
                    await self._repository.fail_match_rerun(
                        claimed_rerun.match_id,
                        '原时间点没有识别到唯一结算画面，' '已保留原结果',
                    )
                    return True
                await self._repository.complete_match_rerun(
                    claimed_rerun.match_id, rerun_matches[0]
                )
                logger.info(
                    'Vainglory single match rerun completed: match_id={} '
                    'part_id={} at_ms={}',
                    claimed_rerun.match_id,
                    part.id,
                    claimed_rerun.result_at_ms,
                )
            except AnalysisCancelled:
                await self._repository.fail_match_rerun(
                    claimed_rerun.match_id, '单局重新识别已停止，请重新提交'
                )
            except Exception as error:
                logger.exception(
                    'Vainglory single match rerun failed for match {}',
                    claimed_rerun.match_id,
                )
                await self._repository.fail_match_rerun(
                    claimed_rerun.match_id, '{}: {}'.format(type(error).__name__, error)
                )
            return True
        claim = await self._repository.claim_next_ocr()
        if claim is None:
            return False
        part = claim.part
        if not Path(part.path).is_file():
            message = '视频文件不存在，未开始 OCR 识别'
            logger.warning(
                'Vainglory OCR skipped unavailable video: session_id={} '
                'part_id={} path={!r}',
                claim.session_id,
                part.id,
                part.path,
            )
            await self._repository.fail(part.id, message)
            return True
        scanned = claim.scanned
        recognition_started = time.monotonic()
        elapsed_before_recognition = (
            0.0
            if claim.analysis_started_at is None
            else max(0.0, time.time() - claim.analysis_started_at)
        )
        logger.info(
            'Vainglory part recognition task started: session_id={} part_id={} '
            'part_index={} candidates={} part_duration_seconds={} '
            'recording_duration_seconds={} elapsed_before_recognition={:.3f}',
            claim.session_id,
            part.id,
            part.index,
            len(scanned.candidate_times_ms),
            claim.part_duration_seconds,
            claim.recording_duration_seconds,
            elapsed_before_recognition,
        )
        loop = asyncio.get_running_loop()
        try:
            last_progress = -0.05

            def report(ocr_progress: float) -> None:
                nonlocal last_progress
                progress = max(0.0, min(1.0, ocr_progress))
                if progress < last_progress + 0.05 and progress < 0.99:
                    return
                last_progress = progress
                update = asyncio.run_coroutine_threadsafe(
                    self._repository.update_ocr_progress(part.id, progress), loop
                )
                try:
                    update.result(timeout=10)
                except TimeoutError:
                    logger.warning(
                        'Vainglory progress update delayed by database load: '
                        'part_id={} stage=ocr',
                        part.id,
                    )

            matches: Tuple[AnalyzedMatch, ...] = await loop.run_in_executor(
                None,
                lambda: self._analyzer.recognize_scanned_part(
                    part,
                    scanned,
                    progress=report,
                    status_callback=lambda status: self._record_runtime_status(
                        part.id, status
                    ),
                    cancelled=self._stop.is_set,
                ),
            )
            await self._repository.complete_part(
                part.id, matches, candidate_count=len(scanned.candidate_times_ms)
            )
            total_seconds = (
                time.monotonic() - recognition_started
                if claim.analysis_started_at is None
                else max(0.0, time.time() - claim.analysis_started_at)
            )
            logger.info(
                'Vainglory part analysis task completed: session_id={} '
                'part_id={} candidates={} matches={} recognition_seconds={:.3f} '
                'total_seconds={:.3f}',
                claim.session_id,
                part.id,
                len(scanned.candidate_times_ms),
                len(matches),
                time.monotonic() - recognition_started,
                total_seconds,
            )
            self._clear_runtime_status(part.id)
        except AnalysisCancelled:
            if self._stop.is_set():
                await self._repository.requeue_ocr(part.id)
            else:
                await self._repository.fail(part.id, 'OCR 识别意外停止')
            self._clear_runtime_status(part.id)
        except Exception as error:
            logger.exception(
                'Vainglory OCR failed for session {} part {}', claim.session_id, part.id
            )
            await self._repository.fail(
                part.id, '{}: {}'.format(type(error).__name__, error)
            )
            self._clear_runtime_status(part.id)
        return True

    async def _watch_for_preemption(
        self, part_id: int, preempt: threading.Event
    ) -> None:
        while not self._stop.is_set() and not preempt.is_set():
            await asyncio.sleep(self._realtime_poll_seconds)
            if await self._repository.historical_part_paused(part_id):
                preempt.set()
                self._scan_wake.set()
                return
            await self._repository.discover_ready_parts()
            if await self._repository.has_realtime_pending():
                preempt.set()
                self._scan_wake.set()

    async def _run_scan(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self._scan_once()
            except Exception:
                logger.exception('Vainglory video scan worker iteration failed')
                processed = False
            if processed:
                continue
            self._scan_wake.clear()
            try:
                await asyncio.wait_for(
                    self._scan_wake.wait(), timeout=self._idle_poll_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _run_ocr(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self._ocr_once()
            except Exception:
                logger.exception('Vainglory OCR worker iteration failed')
                processed = False
            if processed:
                continue
            self._ocr_wake.clear()
            try:
                await asyncio.wait_for(
                    self._ocr_wake.wait(), timeout=self._idle_poll_seconds
                )
            except asyncio.TimeoutError:
                pass
