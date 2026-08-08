import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

from blrec.analysis_worker.remote import RemoteAnalysisWorker
from blrec.vainglory.analyzer import DenseScanResult, ScannedPart, VideoPart


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
    ) -> tuple:
        return ()


class Client:
    def __init__(self, queue: List[Mapping[str, Any]], lock: threading.Lock) -> None:
        self._queue = queue
        self._lock = lock
        self.closed = False

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
        pass

    def fail(self, *, kind: str, item_id: int, error: str) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _claim(item_id: int) -> Dict[str, Any]:
    return {
        'kind': 'full_scan',
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


def _shared_queue(claims: List[Mapping[str, Any]]) -> tuple:
    return list(claims), threading.Lock()


def _tracked_client(
    clients: List[Client], queue: List[Mapping[str, Any]], lock: threading.Lock
) -> Client:
    client = Client(queue, lock)
    clients.append(client)
    return client
