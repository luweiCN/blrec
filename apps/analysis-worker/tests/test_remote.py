import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional
from unittest import mock

import pytest
from blrec_analysis_worker.remote import (
    AnalysisWorkerClient,
    RemoteAnalysisWorker,
    WorkerConcurrency,
    _Heartbeat,
)

from blrec.vainglory.analyzer import (
    AnalysisStatus,
    AnalyzedAfkStatus,
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
        self.afk_team_sizes: List[Optional[int]] = []
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

    def classify_saved_afk_statuses(
        self, content: bytes, *, expected_team_size: Optional[int] = None
    ) -> tuple:
        assert content == b'result-frame'
        self.afk_team_sizes.append(expected_team_size)
        return tuple(
            AnalyzedAfkStatus(
                side=side,
                slot=slot,
                status=('unknown' if (side, slot) == ('right', 2) else 'active'),
                probability=(0.527 if (side, slot) == ('right', 2) else 0.01),
                model_version='afk-status-test',
                gate_reason=(
                    'model_low_positive_probability'
                    if (side, slot) == ('right', 2)
                    else ''
                ),
            )
            for side in ('left', 'right')
            for slot in range(1, 4)
        )


class Client:
    def __init__(self, queue: List[Mapping[str, Any]], lock: threading.Lock) -> None:
        self._queue = queue
        self._lock = lock
        self.closed = False
        self.completed_payloads: List[Mapping[str, Any]] = []
        self.failures: List[Mapping[str, Any]] = []

    def claim(
        self, *, queue: Literal['video', 'image'] = 'video'
    ) -> Optional[Dict[str, Any]]:
        del queue
        with self._lock:
            if not self._queue:
                return None
            return dict(self._queue.pop(0))

    def claim_live(self) -> Optional[Dict[str, Any]]:
        return None

    def desired_concurrency(self) -> int:
        raise ValueError('测试未启用动态并发配置')

    def download(self, media_path: str, destination: Path) -> None:
        destination.write_bytes(b'x')

    def heartbeat(
        self, kind: str, item_id: int, progress: float, runtime_status: Optional[Any]
    ) -> None:
        pass

    def complete(self, payload: Mapping[str, Any]) -> None:
        self.completed_payloads.append(dict(payload))

    def complete_live(self, payload: Mapping[str, Any]) -> None:
        self.completed_payloads.append(dict(payload))

    def fail_live(self, claim: Mapping[str, Any], error: str) -> None:
        self.failures.append({'claim': dict(claim), 'error': error})

    def media_url(self, media_path: str) -> str:
        return 'http://nas:2234{}'.format(media_path)

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


def test_client_reports_loaded_model_while_polling_for_work() -> None:
    response = mock.Mock(status_code=204)
    session = mock.Mock()
    session.post.return_value = response
    with mock.patch(
        'blrec_analysis_worker.remote.requests.Session', return_value=session
    ):
        client = AnalysisWorkerClient(
            'http://nas:2234',
            'token',
            worker_id='macbook-pro',
            model_package_id='vg-vision-v2',
            pipeline_version='timeline-v2',
        )

    assert client.claim() is None
    session.post.assert_called_once_with(
        'http://nas:2234/api/v1/vainglory/worker/claim',
        headers={'X-BLREC-Analysis-Worker-Token': 'token'},
        json={
            'workerId': 'macbook-pro',
            'modelPackageId': 'vg-vision-v2',
            'pipelineVersion': 'timeline-v2',
            'concurrency': 0,
            'queue': 'video',
        },
        timeout=(10, 30),
    )


def test_client_reads_desired_concurrency() -> None:
    response = mock.Mock()
    response.json.return_value = {'desiredConcurrency': 4}
    session = mock.Mock()
    session.post.return_value = response
    current = WorkerConcurrency(2)
    with mock.patch(
        'blrec_analysis_worker.remote.requests.Session', return_value=session
    ):
        client = AnalysisWorkerClient(
            'http://nas:2234',
            'token',
            worker_id='mac-studio',
            model_package_id='vg-vision-v2',
            pipeline_version='timeline-v2',
            concurrency_provider=current.get,
        )

    assert client.desired_concurrency() == 4
    session.post.assert_called_once_with(
        'http://nas:2234/api/v1/vainglory/worker/configuration',
        headers={'X-BLREC-Analysis-Worker-Token': 'token'},
        json={
            'workerId': 'mac-studio',
            'modelPackageId': 'vg-vision-v2',
            'pipelineVersion': 'timeline-v2',
            'concurrency': 2,
        },
        timeout=(10, 30),
    )


def test_client_uses_dedicated_live_queue_endpoint() -> None:
    response = mock.Mock(status_code=204)
    session = mock.Mock()
    session.post.return_value = response
    with mock.patch(
        'blrec_analysis_worker.remote.requests.Session', return_value=session
    ):
        client = AnalysisWorkerClient(
            'http://nas:2234',
            'token',
            worker_id='mac-studio',
            model_package_id='vg-vision-v2',
            pipeline_version='timeline-v2',
            concurrency=3,
        )

    assert client.claim_live() is None
    session.post.assert_called_once_with(
        'http://nas:2234/api/v1/vainglory/worker/live/claim',
        headers={'X-BLREC-Analysis-Worker-Token': 'token'},
        json={
            'workerId': 'mac-studio',
            'modelPackageId': 'vg-vision-v2',
            'pipelineVersion': 'timeline-v2',
            'concurrency': 3,
        },
        timeout=(10, 30),
    )


def test_client_reports_worker_identity_with_heartbeat() -> None:
    response = mock.Mock()
    with mock.patch(
        'blrec_analysis_worker.remote.requests.post', return_value=response
    ) as post:
        client = AnalysisWorkerClient(
            'http://nas:2234',
            'token',
            worker_id='mac-studio',
            model_package_id='vg-vision-v2',
            pipeline_version='timeline-v2',
        )
        client.heartbeat('part', 7, 0.25, None)

    post.assert_called_once_with(
        'http://nas:2234/api/v1/vainglory/worker/heartbeat',
        headers={'X-BLREC-Analysis-Worker-Token': 'token'},
        json={
            'workerId': 'mac-studio',
            'modelPackageId': 'vg-vision-v2',
            'pipelineVersion': 'timeline-v2',
            'concurrency': 0,
            'kind': 'part',
            'itemId': 7,
            'progress': 0.25,
            'runtimeStatus': None,
        },
        timeout=(10, 20),
    )


def test_client_reports_worker_identity_when_work_finishes() -> None:
    response = mock.Mock()
    session = mock.Mock()
    session.post.return_value = response
    with mock.patch(
        'blrec_analysis_worker.remote.requests.Session', return_value=session
    ):
        client = AnalysisWorkerClient(
            'http://nas:2234',
            'token',
            worker_id='mac-studio',
            model_package_id='vg-vision-v2',
            pipeline_version='timeline-v2',
            concurrency=3,
        )

    client.complete({'kind': 'part', 'itemId': 7})
    session.post.assert_called_once_with(
        'http://nas:2234/api/v1/vainglory/worker/complete',
        headers={'X-BLREC-Analysis-Worker-Token': 'token'},
        json={
            'kind': 'part',
            'itemId': 7,
            'workerId': 'mac-studio',
            'modelPackageId': 'vg-vision-v2',
            'pipelineVersion': 'timeline-v2',
            'concurrency': 3,
        },
        timeout=(10, 120),
    )


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
    assert len(clients) == 4, '控制、两个录播线程和实时线程应各有独立 client'
    worker.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert all(client.closed for client in clients)


def test_worker_applies_increased_concurrency_without_restart(tmp_path: Path) -> None:
    class ConfigurableClient(Client):
        def __init__(
            self,
            queue: List[Mapping[str, Any]],
            lock: threading.Lock,
            desired: List[int],
        ) -> None:
            super().__init__(queue, lock)
            self._desired = desired

        def desired_concurrency(self) -> int:
            return self._desired[0]

    queue, lock = _shared_queue([_claim(value) for value in range(1, 9)])
    analyzer = Analyzer(block=0.15)
    desired = [1]
    clients: List[Client] = []

    def client_factory() -> Client:
        client = ConfigurableClient(queue, lock, desired)
        clients.append(client)
        return client

    worker = RemoteAnalysisWorker(
        client_factory, analyzer, cache_dir=tmp_path, poll_seconds=0.01, concurrency=1
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    assert _wait_until(lambda: analyzer.max_active == 1)
    desired[0] = 2
    assert _wait_until(lambda: analyzer.max_active >= 2)

    worker.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


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


def test_worker_processes_afk_status_backfill_without_downloading_video(
    tmp_path: Path,
) -> None:
    import base64

    queue, lock = _shared_queue(
        [
            {
                'kind': 'afk_status_backfill',
                'itemId': 7,
                'teamSize': 5,
                'framePng': base64.b64encode(b'result-frame').decode('ascii'),
            }
        ]
    )
    clients: List[Client] = []
    worker = RemoteAnalysisWorker(
        lambda: _tracked_client(clients, queue, lock), Analyzer(), cache_dir=tmp_path
    )

    worker.run(once=True)

    payload = clients[0].completed_payloads[0]
    assert payload['kind'] == 'afk_status_backfill'
    assert payload['itemId'] == 7
    assert worker._analyzer.afk_team_sizes == [5]
    assert payload['afkStatuses'][4] == {
        'side': 'right',
        'slot': 2,
        'status': 'unknown',
        'probability': 0.527,
        'model_version': 'afk-status-test',
        'gate_reason': 'model_low_positive_probability',
    }


def test_video_and_image_workers_use_independent_claim_queues(tmp_path: Path) -> None:
    import base64

    class SplitClient(Client):
        def __init__(self) -> None:
            super().__init__([], threading.Lock())
            self.claimed_queues: List[str] = []
            self.video_claims = [_claim(1)]
            self.image_claims = [
                {
                    'kind': 'afk_status_backfill',
                    'itemId': 7,
                    'teamSize': 3,
                    'framePng': base64.b64encode(b'result-frame').decode('ascii'),
                }
            ]

        def claim(
            self, *, queue: Literal['video', 'image'] = 'video'
        ) -> Optional[Dict[str, Any]]:
            with self._lock:
                self.claimed_queues.append(queue)
                claims = self.video_claims if queue == 'video' else self.image_claims
                return dict(claims.pop(0)) if claims else None

    clients: List[SplitClient] = []

    def client_factory() -> SplitClient:
        client = SplitClient()
        clients.append(client)
        return client

    worker = RemoteAnalysisWorker(
        client_factory,
        Analyzer(),
        cache_dir=tmp_path,
        poll_seconds=0.01,
        concurrency=1,
        image_concurrency=1,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    assert _wait_until(
        lambda: any(
            payload.get('kind') == 'part'
            for client in clients
            for payload in client.completed_payloads
        )
        and any(
            payload.get('kind') == 'afk_status_backfill'
            for client in clients
            for payload in client.completed_payloads
        )
    )
    worker.stop()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert any(
        client.claimed_queues and set(client.claimed_queues) == {'video'}
        for client in clients
    )
    assert any(
        client.claimed_queues and set(client.claimed_queues) == {'image'}
        for client in clients
    )


def test_concurrency_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RemoteAnalysisWorker(
            lambda: Client([], threading.Lock()),
            Analyzer(),
            cache_dir=tmp_path,
            concurrency=0,
        )


def test_heartbeat_keeps_scan_metrics_during_ocr_stage() -> None:
    heartbeat = _Heartbeat(
        Client([], threading.Lock()), kind='part', item_id=1, interval_seconds=60
    )
    heartbeat.update_status(
        AnalysisStatus(
            stage='fine_scan',
            detail='精扫',
            elapsed_seconds=10,
            coarse_frames=98,
            gameplay_runs=2,
            result_windows=2,
            keyframe_frames=67,
            seek_fill_frames=31,
            decoded_result_frames=240,
            mode_conflict_count=1,
            hud_lineup_candidate_count=2,
            training_candidate_count=12,
        )
    )

    heartbeat.update_status(
        AnalysisStatus(
            stage='ocr_recognition',
            detail='识别',
            elapsed_seconds=12,
            candidate_count=2,
            total_candidates=2,
        )
    )

    status = heartbeat._runtime_status
    assert status is not None
    assert status.stage == 'ocr_recognition'
    assert status.coarse_frames == 98
    assert status.gameplay_runs == 2
    assert status.result_windows == 2
    assert status.keyframe_frames == 67
    assert status.seek_fill_frames == 31
    assert status.decoded_result_frames == 240
    assert status.mode_conflict_count == 1
    assert status.hud_lineup_candidate_count == 2
    assert status.training_candidate_count == 12


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
                mode_conflict_count=2,
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
    assert summary['modeConflictCount'] == 2
    assert summary['timelineSegments'] == [
        {'startMs': 0, 'endMs': 20_000, 'mode': '3v3'}
    ]
    assert summary['trainingCandidateCounts'] == {'result_detector': 1}
    assert clients[0].completed_payloads[0]['videoDurationSeconds'] == 60.0
    assert clients[0].completed_payloads[0]['decodeAnalysisSeconds'] >= 0


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
