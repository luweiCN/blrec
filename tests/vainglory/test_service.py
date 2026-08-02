import asyncio
import time
from pathlib import Path
from threading import Event

import pytest

from blrec.vainglory.analyzer import AnalysisCancelled, VideoPart
from blrec.vainglory.repository import ScanClaim
from blrec.vainglory.service import VaingloryIndexService


class Repository:
    def __init__(self) -> None:
        self.realtime_pending = False
        self.historical_paused = False
        self.requeued = []
        self.failed = []

    async def claim_next(self) -> ScanClaim:
        return ScanClaim(
            session_id=1, part=VideoPart(id=1, index=1, path='/unused'), realtime=False
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


@pytest.mark.asyncio
async def test_background_analysis_yields_when_a_live_part_arrives(
    tmp_path: Path,
) -> None:
    del tmp_path
    repository = Repository()
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
async def test_historical_analysis_yields_when_its_sync_is_paused() -> None:
    repository = Repository()
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
