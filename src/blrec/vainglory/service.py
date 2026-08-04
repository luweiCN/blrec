from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from .analyzer import (
    AnalysisCancelled,
    AnalysisStatus,
    AnalyzedHero,
    AnalyzedMatch,
    VaingloryVideoAnalyzer,
    VideoPart,
)
from .hero_recognition import load_hero_references
from .repository import (
    AnalysisQueueCompletion,
    AnalysisQueueEvent,
    AnalysisQueueItem,
    AnalysisQueueStatus,
    AnchorStatsRecord,
    HeroStatsRecord,
    IndexSummary,
    MatchPage,
    MatchRecord,
    MatchSessionPage,
    MatchSessionRecord,
    PlayerRecord,
    PlayerStatsRecord,
    ScanJob,
    VaingloryRepository,
    ZeroMatchSessionPage,
)


class VaingloryIndexService:
    def __init__(
        self,
        repository: VaingloryRepository,
        *,
        analyzer: Optional[VaingloryVideoAnalyzer] = None,
        idle_poll_seconds: float = 2,
        realtime_poll_seconds: float = 1,
    ) -> None:
        if idle_poll_seconds <= 0 or realtime_poll_seconds <= 0:
            raise ValueError('poll intervals must be positive')
        self._repository = repository
        self._analyzer = analyzer or VaingloryVideoAnalyzer()
        self._idle_poll_seconds = idle_poll_seconds
        self._realtime_poll_seconds = realtime_poll_seconds
        self._scan_wake = asyncio.Event()
        self._ocr_wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._scan_task: Optional[asyncio.Task[None]] = None
        self._ocr_task: Optional[asyncio.Task[None]] = None
        self._analysis_lock = asyncio.Lock()
        self._runtime_lock = threading.Lock()
        self._runtime_status: Dict[int, AnalysisStatus] = {}
        self._runtime_events: Dict[int, List[AnalysisQueueEvent]] = {}
        self._recent_completions: List[AnalysisQueueCompletion] = []

    @property
    def repository(self) -> VaingloryRepository:
        return self._repository

    @property
    def worker_state(self) -> str:
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

    async def start(self) -> None:
        if self._scan_task is not None or self._ocr_task is not None:
            return
        await self._repository.purge_excluded_content()
        await self._repository.recover_interrupted()
        await self._repository.apply_builtin_hero_labels()
        await self._repository.consolidate_hero_catalog()
        references = load_hero_references()
        if len(references) != 57:
            logger.warning(
                'Vainglory hero reference catalog is incomplete: expected=57 actual={}',
                len(references),
            )
        await self._repository.sync_hero_references(references)
        self._stop.clear()
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

    async def request_scan(self, session_id: int) -> ScanJob:
        job = await self._repository.request_scan(session_id)
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
            recent_completions = tuple(self._recent_completions)

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
                events=item_events,
            )

        return replace(
            queue,
            active=tuple(enrich(item) for item in queue.active),
            queued=tuple(enrich(item) for item in queue.queued),
            recent_completions=recent_completions,
        )

    def _record_runtime_status(self, part_id: int, status: AnalysisStatus) -> None:
        event = AnalysisQueueEvent(
            at=int(time.time()),
            stage=status.stage,
            detail=status.detail,
            elapsed_seconds=status.elapsed_seconds,
        )
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
                )
            self._runtime_status[part_id] = status
            events = self._runtime_events.setdefault(part_id, [])
            events.append(event)
            del events[:-12]

    def _clear_runtime_status(self, part_id: int) -> None:
        with self._runtime_lock:
            self._runtime_status.pop(part_id, None)
            self._runtime_events.pop(part_id, None)

    def _record_completion(
        self,
        *,
        session_id: int,
        part: VideoPart,
        part_duration_seconds: Optional[int],
        recording_duration_seconds: int,
        candidate_count: int,
        match_count: int,
        elapsed_seconds: float,
    ) -> None:
        completion = AnalysisQueueCompletion(
            completed_at=int(time.time()),
            session_id=session_id,
            part_id=part.id,
            part_index=part.index,
            title=part.title,
            part_duration_seconds=part_duration_seconds,
            recording_duration_seconds=recording_duration_seconds,
            candidate_count=candidate_count,
            match_count=match_count,
            elapsed_seconds=elapsed_seconds,
        )
        with self._runtime_lock:
            self._recent_completions.insert(0, completion)
            del self._recent_completions[6:]

    async def index_summary(self) -> IndexSummary:
        return await self._repository.index_summary()

    async def list_matches(self, **filters: Any) -> MatchPage:
        return await self._repository.list_matches(**filters)

    async def list_match_sessions(self, **filters: Any) -> MatchSessionPage:
        return await self._repository.list_match_sessions(**filters)

    async def list_zero_match_sessions(
        self, *, limit: int = 20, offset: int = 0
    ) -> ZeroMatchSessionPage:
        return await self._repository.list_zero_match_sessions(
            limit=limit, offset=offset
        )

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
                await self._repository.complete_part(part.id, ())
                elapsed_seconds = time.monotonic() - task_started
                self._record_completion(
                    session_id=session_id,
                    part=part,
                    part_duration_seconds=claim.part_duration_seconds,
                    recording_duration_seconds=claim.recording_duration_seconds,
                    candidate_count=0,
                    match_count=0,
                    elapsed_seconds=elapsed_seconds,
                )
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
        claim = await self._repository.claim_next_ocr()
        if claim is None:
            return False
        part = claim.part
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
            await self._repository.complete_part(part.id, matches)
            total_seconds = (
                time.monotonic() - recognition_started
                if claim.analysis_started_at is None
                else max(0.0, time.time() - claim.analysis_started_at)
            )
            self._record_completion(
                session_id=claim.session_id,
                part=part,
                part_duration_seconds=claim.part_duration_seconds,
                recording_duration_seconds=claim.recording_duration_seconds,
                candidate_count=len(scanned.candidate_times_ms),
                match_count=len(matches),
                elapsed_seconds=total_seconds,
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
