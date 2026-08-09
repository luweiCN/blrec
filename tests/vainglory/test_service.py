import asyncio
import time
from pathlib import Path
from threading import Event

import pytest
from loguru import logger

from blrec.vainglory.analyzer import (
    AnalysisCancelled,
    AnalysisStatus,
    ScannedPart,
    VideoPart,
)
from blrec.vainglory.repository import (
    AnalysisQueueCompletion,
    AnalysisQueueStatus,
    ScanClaim,
)
from blrec.vainglory.service import VaingloryIndexService


class Repository:
    def __init__(self, path: str = '/unused') -> None:
        self.path = path
        self.realtime_pending = False
        self.historical_paused = False
        self.requeued = []
        self.failed = []

    async def claim_next(self) -> ScanClaim:
        return ScanClaim(
            session_id=1, part=VideoPart(id=1, index=1, path=self.path), realtime=False
        )

    async def next_hero_rematch(self):
        return None

    async def discover_ready_parts(self) -> int:
        return 0

    async def has_realtime_pending(self) -> bool:
        return self.realtime_pending

    async def historical_part_paused(self, _part_id: int) -> bool:
        return self.historical_paused

    async def requeue(self, part_id: int) -> None:
        self.requeued.append(part_id)

    async def fail(self, part_id: int, error: str) -> None:
        self.failed.append((part_id, error))

    async def update_progress(self, _part_id: int, _progress: float) -> None:
        return None

    async def complete_part(self, _part_id: int, _matches: object) -> None:
        raise AssertionError('preempted analysis must not complete')


class Analyzer:
    def __init__(self) -> None:
        self.started = Event()

    def analyze_part(self, _part: VideoPart, *, progress: object, cancelled: object):
        del progress
        self.started.set()
        while not cancelled():
            time.sleep(0.002)
        raise AnalysisCancelled('preempted')


def test_runtime_log_deduplicates_repeated_stage_messages() -> None:
    service = VaingloryIndexService(
        Repository(),  # type: ignore[arg-type]
        analyzer=Analyzer(),  # type: ignore[arg-type]
    )

    service._record_runtime_status(
        1,
        AnalysisStatus(
            stage='coarse_scan',
            detail='分类粗扫 10% · 已采样 2 帧',
            elapsed_seconds=1,
            coarse_frames=2,
        ),
    )
    service._record_runtime_status(
        1,
        AnalysisStatus(
            stage='coarse_scan',
            detail='分类粗扫 10% · 已采样 2 帧',
            elapsed_seconds=2,
            coarse_frames=3,
        ),
    )

    assert len(service._runtime_events[1]) == 1
    assert service._runtime_status[1].coarse_frames == 3


@pytest.mark.asyncio
async def test_background_analysis_yields_when_a_live_part_arrives(
    tmp_path: Path,
) -> None:
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')
    repository = Repository(str(video))
    analyzer = Analyzer()
    service = VaingloryIndexService(
        repository,  # type: ignore[arg-type]
        analyzer=analyzer,  # type: ignore[arg-type]
        realtime_poll_seconds=0.01,
    )

    running = asyncio.create_task(service.run_once())
    while not analyzer.started.is_set():
        await asyncio.sleep(0)
    repository.realtime_pending = True
    assert await asyncio.wait_for(running, timeout=1) is True

    assert repository.requeued == [1]
    assert repository.failed == []


@pytest.mark.asyncio
async def test_historical_analysis_yields_when_its_sync_is_paused(
    tmp_path: Path,
) -> None:
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')
    repository = Repository(str(video))
    analyzer = Analyzer()
    service = VaingloryIndexService(
        repository,  # type: ignore[arg-type]
        analyzer=analyzer,  # type: ignore[arg-type]
        realtime_poll_seconds=0.01,
    )

    running = asyncio.create_task(service.run_once())
    while not analyzer.started.is_set():
        await asyncio.sleep(0)
    repository.historical_paused = True
    assert await asyncio.wait_for(running, timeout=1) is True

    assert repository.requeued == [1]
    assert repository.failed == []


@pytest.mark.asyncio
async def test_scan_task_log_includes_part_and_recording_durations(
    tmp_path: Path,
) -> None:
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'video')

    class ScanRepository:
        def __init__(self) -> None:
            self.completed = []
            self.candidate_count = 0

        async def has_realtime_pending(self) -> bool:
            return True

        async def claim_next(self) -> ScanClaim:
            return ScanClaim(
                session_id=7,
                part=VideoPart(id=11, index=2, path=str(video)),
                realtime=True,
                part_duration_seconds=7_200,
                recording_duration_seconds=10_800,
            )

        async def update_progress(self, _part_id: int, _progress: float) -> None:
            return None

        async def complete_part(
            self, part_id: int, matches: object, *, candidate_count: int = 0
        ) -> None:
            self.candidate_count = candidate_count
            self.completed.append((part_id, matches))

        async def analysis_queue_status(self) -> AnalysisQueueStatus:
            recent_completions = (
                ()
                if not self.completed
                else (
                    AnalysisQueueCompletion(
                        completed_at=int(time.time()),
                        session_id=7,
                        part_id=11,
                        part_index=2,
                        title='测试直播',
                        part_duration_seconds=7_200,
                        recording_duration_seconds=10_800,
                        part_match_duration_seconds=1_800,
                        session_match_duration_seconds=2_700,
                        candidate_count=self.candidate_count,
                        match_count=0,
                        elapsed_seconds=0,
                    ),
                )
            )
            return AnalysisQueueStatus(
                active=(),
                queued=(),
                pending_count=0,
                manual_pending=0,
                realtime_pending=0,
                archive_pending=0,
                migration_pending=0,
                backlog_pending=0,
                recent_completions=recent_completions,
            )

    class ScanAnalyzer:
        def scan_part(
            self,
            _part: VideoPart,
            *,
            progress: object,
            status_callback: object,
            cancelled: object,
        ):
            del status_callback, cancelled
            progress(1.0)
            return ScannedPart(7_200_000, ())

    repository = ScanRepository()
    service = VaingloryIndexService(
        repository, analyzer=ScanAnalyzer()  # type: ignore[arg-type]
    )
    messages = []
    sink = logger.add(messages.append, format='{message}')
    try:
        assert await service._scan_once() is True
    finally:
        logger.remove(sink)

    message = ''.join(str(item) for item in messages)
    queue = await service.analysis_queue_status()
    assert repository.completed == [(11, ())]
    assert len(queue.recent_completions) == 1
    assert queue.recent_completions[0].part_id == 11
    assert queue.recent_completions[0].part_duration_seconds == 7_200
    assert queue.recent_completions[0].recording_duration_seconds == 10_800
    assert queue.recent_completions[0].part_match_duration_seconds == 1_800
    assert queue.recent_completions[0].session_match_duration_seconds == 2_700
    assert queue.recent_completions[0].candidate_count == 0
    assert queue.recent_completions[0].match_count == 0
    assert queue.recent_completions[0].elapsed_seconds >= 0
    assert 'Vainglory part analysis task started: session_id=7 part_id=11' in message
    assert 'part_duration_seconds=7200' in message
    assert 'recording_duration_seconds=10800' in message
    assert 'Vainglory part analysis task completed: session_id=7 part_id=11' in message
    assert 'matches=0' in message
