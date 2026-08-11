import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest
from blrec_analysis_worker.remote import RemoteAnalysisWorker

from blrec.vainglory.analyzer import (
    DenseScanResult,
    ResultScanWindow,
    ScannedPart,
    TimelinePoint,
    TimelineSegment,
    TrainingCandidate,
    TrainingCandidateBox,
    VideoPart,
)
from blrec.vainglory.sampling import UnusableVideoError


class Analyzer:
    def __init__(self, *, block: float = 0.0) -> None:
        self.block = block
        self.completed = 0
        self.max_active = 0
        self._active = 0
        self._lock = threading.Lock()

    def scan_part_cascade(
        self,
        part: VideoPart,
        *,
        progress: Optional[Any] = None,
        status_callback: Optional[Any] = None,
        cancelled: Optional[Any] = None,
        debug_dir: Optional[Path] = None,
    ) -> DenseScanResult:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(self.block)
            return DenseScanResult(
                scanned_part=ScannedPart(
                    video_duration_ms=60_000, candidate_times_ms=(30_000,)
                ),
                decoded_frames=1,
                result_frames=1,
                probe_seconds=0.0,
                decode_seconds=0.0,
                detection_seconds=0.0,
                total_seconds=0.0,
            )
        finally:
            with self._lock:
                self._active -= 1
                self.completed += 1

    def recognize_scanned_part(
        self,
        part: VideoPart,
        scanned: ScannedPart,
        *,
        progress: Optional[Any] = None,
        status_callback: Optional[Any] = None,
        cancelled: Optional[Any] = None,
        debug_dir: Optional[Path] = None,
    ) -> tuple:
        return ()


class Client:
    def __init__(self, queue: List[Mapping[str, Any]], lock: threading.Lock) -> None:
        self._queue = queue
        self._lock = lock
        self.closed = False
        self.completed_payloads: List[Mapping[str, Any]] = []
        self.failures: List[Mapping[str, Any]] = []

    def claim(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._queue:
                return None
            return dict(self._queue.pop(0))

    def download(self, media_path: str, destination: Path) -> None:
        destination.write_bytes(b'x')

    def heartbeat(
        self, kind: str, item_id: int, progress: float, runtime_status: Optional[Any]
    ) -> None:
        pass

    def complete(self, payload: Mapping[str, Any]) -> None:
        self.completed_payloads.append(dict(payload))

    def fail(
        self, *, kind: str, item_id: int, error: str, failure_kind: str = 'task_error'
    ) -> None:
        self.failures.append(
            {
                'kind': kind,
                'item_id': item_id,
                'error': error,
                'failure_kind': failure_kind,
            }
        )

    def close(self) -> None:
        self.closed = True


def _claim(item_id: int) -> Dict[str, Any]:
    return {
        'kind': 'part',
        'itemId': item_id,
        'part': {
            'id': item_id,
            'index': 1,
            'title': 'test',
            'mediaPath': '/media/{}'.format(item_id),
            'manualCandidateTimesMs': [],
        },
    }


def _wait_until(predicate: Any, *, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_parallel_workers_process_claims_concurrently(tmp_path: Path) -> None:
    queue, lock = _shared_queue([_claim(1), _claim(2)])
    analyzer = Analyzer(block=0.2)
    clients: List[Client] = []
    worker = RemoteAnalysisWorker(
        lambda: _tracked_client(clients, queue, lock),
        analyzer,
        cache_dir=tmp_path,
        poll_seconds=0.01,
        concurrency=2,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    assert _wait_until(lambda: analyzer.completed >= 2)
    assert analyzer.max_active >= 2, '两个任务没有真正并行处理'
    assert len(clients) == 2, '每个并发线程应创建独立的 client'
    worker.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert all(client.closed for client in clients)


def test_once_forces_single_worker(tmp_path: Path) -> None:
    queue, lock = _shared_queue([_claim(1)])
    analyzer = Analyzer(block=0.0)
    clients: List[Client] = []
    worker = RemoteAnalysisWorker(
        lambda: _tracked_client(clients, queue, lock),
        analyzer,
        cache_dir=tmp_path,
        poll_seconds=0.01,
        concurrency=4,
    )
    worker.run(once=True)
    assert analyzer.completed == 1
    assert len(clients) == 1, 'once 模式应强制单线程'
    assert clients[0].closed


def test_single_worker_processes_claim_then_polls(tmp_path: Path) -> None:
    queue, lock = _shared_queue([_claim(1)])
    analyzer = Analyzer(block=0.0)
    clients: List[Client] = []
    worker = RemoteAnalysisWorker(
        lambda: _tracked_client(clients, queue, lock),
        analyzer,
        cache_dir=tmp_path,
        poll_seconds=0.01,
        concurrency=1,
    )
    worker.run(once=True)
    assert analyzer.completed == 1
    assert clients[0].closed


def test_concurrency_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RemoteAnalysisWorker(
            lambda: Client([], threading.Lock()),
            Analyzer(),
            cache_dir=tmp_path,
            concurrency=0,
        )


def test_worker_uploads_candidates_already_seen_during_scan(tmp_path: Path) -> None:
    class CandidateAnalyzer(Analyzer):
        def scan_part_cascade(self, part: VideoPart, **kwargs: Any) -> DenseScanResult:
            scanned = super().scan_part_cascade(part, **kwargs)
            return replace(
                scanned,
                model_package_id='vision-package-v1',
                timeline_points=(
                    TimelinePoint(
                        target_ms=0,
                        at_ms=400,
                        sample_source='keyframe',
                        stage=3,
                        stage_confidence=0.9,
                        match_flow_label='match_flow',
                        match_flow_confidence=0.9,
                        hero_select_label='not_select',
                        hero_select_confidence=0.8,
                        match_mode_label='3v3',
                        match_mode_confidence=0.85,
                    ),
                ),
                timeline_segments=(TimelineSegment(0, 20_000, '3v3'),),
                result_windows=(ResultScanWindow(15_000, 30_000, '3v3', 20_000),),
                keyframe_frames=1,
                training_candidates=(
                    TrainingCandidate(
                        at_ms=12_000,
                        segment_start_ms=10_000,
                        image_jpeg=b'\xff\xd8candidate\xff\xd9',
                        model_version='result-detector-v1',
                        suggested_label='result_panel',
                        suggestion_confidence=0.8,
                        stage_class='pre_match',
                        stage_confidence=0.9,
                        mode_class='aram',
                        mode_confidence=0.8,
                        selection_reason='worker 测试候选',
                        task='result_detector',
                        suggested_boxes=(
                            TrainingCandidateBox(
                                box_type='result_panel', x=0.1, y=0.2, w=0.8, h=0.5
                            ),
                        ),
                    ),
                ),
            )

    queue, lock = _shared_queue([_claim(1)])
    clients: List[Client] = []
    worker = RemoteAnalysisWorker(
        lambda: _tracked_client(clients, queue, lock),
        CandidateAnalyzer(),
        cache_dir=tmp_path,
    )

    worker.run(once=True)

    candidate = clients[0].completed_payloads[0]['trainingCandidates'][0]
    assert candidate['at_ms'] == 12_000
    assert candidate['task'] == 'result_detector'
    assert candidate['suggested_label'] == 'result_panel'
    assert candidate['suggested_boxes'][0]['type'] == 'result_panel'
    assert candidate['image_jpeg']
    summary = clients[0].completed_payloads[0]['analysisSummary']
    assert summary['modelPackageId'] == 'vision-package-v1'
    assert summary['keyframeFrames'] == 1
    assert summary['timelineSegments'] == [
        {'startMs': 0, 'endMs': 20_000, 'mode': '3v3'}
    ]
    assert summary['trainingCandidateCounts'] == {'result_detector': 1}


def test_worker_reports_unusable_video_as_structured_failure(tmp_path: Path) -> None:
    class UnusableAnalyzer(Analyzer):
        def scan_part_cascade(self, part: VideoPart, **kwargs: Any) -> DenseScanResult:
            raise UnusableVideoError('FFprobe 无法解析视频')

    queue, lock = _shared_queue([_claim(1)])
    clients: List[Client] = []
    worker = RemoteAnalysisWorker(
        lambda: _tracked_client(clients, queue, lock),
        UnusableAnalyzer(),
        cache_dir=tmp_path,
    )

    worker.run(once=True)

    assert clients[0].failures == [
        {
            'kind': 'part',
            'item_id': 1,
            'error': 'UnusableVideoError: FFprobe 无法解析视频',
            'failure_kind': 'unusable_media',
        }
    ]


def _shared_queue(claims: List[Mapping[str, Any]]) -> tuple:
    return list(claims), threading.Lock()


def _tracked_client(
    clients: List[Client], queue: List[Mapping[str, Any]], lock: threading.Lock
) -> Client:
    client = Client(queue, lock)
    clients.append(client)
    return client
