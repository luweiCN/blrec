from __future__ import annotations

import base64
import os
import socket
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Mapping, Optional, Sequence, cast
from urllib.parse import urljoin

import requests
from loguru import logger

from blrec.vainglory.analysis_protocol import (
    encode_hero,
    encode_match,
    encode_recorded_player,
    encode_training_candidate,
)
from blrec.vainglory.analyzer import AnalysisStatus, VaingloryVideoAnalyzer, VideoPart
from blrec.vainglory.sampling import UnusableVideoError


class AnalysisWorkerClient:
    def __init__(self, server_url: str, token: str) -> None:
        self._server_url = server_url.rstrip('/') + '/'
        self._headers = {'X-BLREC-Analysis-Worker-Token': token}
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def claim(self) -> Optional[Dict[str, Any]]:
        response = self._session.post(
            self._url('api/v1/vainglory/worker/claim'),
            headers=self._headers,
            timeout=(10, 30),
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return cast(Dict[str, Any], response.json())

    def download(self, media_path: str, destination: Path) -> None:
        temporary = destination.with_suffix(destination.suffix + '.download')
        temporary.unlink(missing_ok=True)
        with self._session.get(
            urljoin(self._server_url, media_path.lstrip('/')),
            stream=True,
            timeout=(10, 120),
        ) as response:
            response.raise_for_status()
            with temporary.open('wb') as output:
                for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                    if chunk:
                        output.write(chunk)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            temporary.unlink(missing_ok=True)
            raise RuntimeError('NAS 返回了空视频文件')
        temporary.replace(destination)

    def heartbeat(
        self,
        kind: str,
        item_id: int,
        progress: float,
        runtime_status: Optional[AnalysisStatus],
    ) -> None:
        response = requests.post(
            self._url('api/v1/vainglory/worker/heartbeat'),
            headers=self._headers,
            json={
                'kind': kind,
                'itemId': item_id,
                'progress': progress,
                'runtimeStatus': (
                    None if runtime_status is None else asdict(runtime_status)
                ),
            },
            timeout=(10, 20),
        )
        response.raise_for_status()

    def complete(self, payload: Mapping[str, Any]) -> None:
        response = self._session.post(
            self._url('api/v1/vainglory/worker/complete'),
            headers=self._headers,
            json=dict(payload),
            timeout=(10, 120),
        )
        response.raise_for_status()

    def fail(
        self,
        *,
        kind: str,
        item_id: int,
        error: str,
        failure_kind: Literal['task_error', 'unusable_media'] = 'task_error',
    ) -> None:
        response = self._session.post(
            self._url('api/v1/vainglory/worker/fail'),
            headers=self._headers,
            json={
                'kind': kind,
                'itemId': item_id,
                'error': error[:500],
                'failureKind': failure_kind,
            },
            timeout=(10, 30),
        )
        response.raise_for_status()

    def _url(self, path: str) -> str:
        return urljoin(self._server_url, path)


class _Heartbeat:
    def __init__(
        self,
        client: AnalysisWorkerClient,
        *,
        kind: str,
        item_id: int,
        interval_seconds: float = 30,
    ) -> None:
        self._client = client
        self._kind = kind
        self._item_id = item_id
        self._interval_seconds = interval_seconds
        self._progress = 0.0
        self._runtime_status: Optional[AnalysisStatus] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval_seconds + 5)

    def update(
        self, progress: float, runtime_status: Optional[AnalysisStatus] = None
    ) -> None:
        with self._lock:
            self._progress = max(0.0, min(0.99, float(progress)))
            if runtime_status is not None:
                self._runtime_status = runtime_status

    def update_status(self, runtime_status: AnalysisStatus) -> None:
        with self._lock:
            self._runtime_status = runtime_status

    def _run(self) -> None:
        last_status: Optional[AnalysisStatus] = None
        while not self._stop.wait(self._interval_seconds):
            with self._lock:
                progress = self._progress
                runtime_status = self._runtime_status
            send_status = runtime_status
            if runtime_status is not None and runtime_status == last_status:
                send_status = None
            try:
                self._client.heartbeat(self._kind, self._item_id, progress, send_status)
            except requests.RequestException as error:
                logger.warning('分析 Worker 心跳发送失败：{!r}', error)
            else:
                if send_status is not None:
                    last_status = send_status


class RemoteAnalysisWorker:
    def __init__(
        self,
        client_factory: Callable[[], AnalysisWorkerClient],
        analyzer: VaingloryVideoAnalyzer,
        *,
        cache_dir: Path,
        poll_seconds: float = 5,
        concurrency: int = 1,
        debug_dir: Optional[Path] = None,
        special_anchors: Sequence[str] = (),
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError('轮询间隔必须为正数')
        if concurrency < 1:
            raise ValueError('并发任务数必须大于 0')
        self._client_factory = client_factory
        self._analyzer = analyzer
        self._cache_dir = cache_dir.expanduser().resolve()
        self._poll_seconds = poll_seconds
        self._concurrency = concurrency
        self._debug_dir = (
            None if debug_dir is None else Path(debug_dir).expanduser().resolve()
        )
        self._special_anchors = tuple(
            anchor.strip() for anchor in special_anchors if anchor.strip()
        )
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self, *, once: bool = False) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            'Mac 分析 Worker 已启动：worker_id={} cache={} concurrency={}',
            socket.gethostname(),
            self._cache_dir,
            self._concurrency,
        )
        if once or self._concurrency == 1:
            client = self._client_factory()
            try:
                self._run_loop(client, worker_id=0, once=once)
            finally:
                client.close()
            return
        threads = [
            threading.Thread(
                target=self._worker_thread,
                args=(index,),
                name='analysis-worker-{}'.format(index),
                daemon=True,
            )
            for index in range(self._concurrency)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def _worker_thread(self, worker_id: int) -> None:
        client = self._client_factory()
        try:
            self._run_loop(client, worker_id=worker_id, once=False)
        finally:
            client.close()

    def _run_loop(
        self, client: AnalysisWorkerClient, *, worker_id: int, once: bool
    ) -> None:
        logger.info(
            '分析 Worker 线程已启动：worker_id={} worker={}',
            socket.gethostname(),
            worker_id,
        )
        while not self._stop.is_set():
            try:
                claim = client.claim()
            except requests.RequestException as error:
                logger.warning(
                    'Worker {} 无法从 NAS 领取分析任务：{!r}', worker_id, error
                )
                if once:
                    raise
                time.sleep(self._poll_seconds)
                continue
            if claim is None:
                if once:
                    return
                if self._stop.wait(self._poll_seconds):
                    return
                continue
            self._process_claim(client, claim, worker_id=worker_id)
            if once:
                return

    def _process_claim(
        self, client: AnalysisWorkerClient, claim: Mapping[str, Any], *, worker_id: int
    ) -> None:
        kind = str(claim['kind'])
        item_id = int(claim['itemId'])
        started = time.monotonic()
        logger.info(
            '开始处理分析任务：kind={} item_id={} worker={}', kind, item_id, worker_id
        )
        try:
            with _Heartbeat(client, kind=kind, item_id=item_id) as heartbeat:
                payload = self._analyze(client, claim, heartbeat)
                upload_started = time.monotonic()
                client.complete(payload)
                upload_seconds = time.monotonic() - upload_started
            logger.info(
                '分析任务完成：kind={} item_id={} worker={} '
                'elapsed_seconds={:.3f} upload_seconds={:.3f}',
                kind,
                item_id,
                worker_id,
                time.monotonic() - started,
                upload_seconds,
            )
        except Exception as error:
            logger.exception(
                '分析任务失败：kind={} item_id={} worker={}', kind, item_id, worker_id
            )
            try:
                client.fail(
                    kind=kind,
                    item_id=item_id,
                    error='{}: {}'.format(type(error).__name__, error),
                    failure_kind=(
                        'unusable_media'
                        if isinstance(error, UnusableVideoError)
                        else 'task_error'
                    ),
                )
            except requests.RequestException as report_error:
                logger.warning('分析失败状态未能写回 NAS：{!r}', report_error)

    def _analyze(
        self,
        client: AnalysisWorkerClient,
        claim: Mapping[str, Any],
        heartbeat: _Heartbeat,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        kind = str(claim['kind'])
        item_id = int(claim['itemId'])
        if kind == 'hero_rematch':
            content = base64.b64decode(str(claim['framePng']), validate=True)
            heroes = self._analyzer.recognize_saved_heroes(content)
            logger.info(
                'Vainglory 小任务耗时明细：kind={} item_id={} total={:.3f}s',
                kind,
                item_id,
                time.monotonic() - started,
            )
            return {
                'kind': kind,
                'itemId': item_id,
                'heroes': [encode_hero(hero) for hero in heroes],
            }
        if kind == 'recorded_player_backfill':
            content = base64.b64decode(str(claim['framePng']), validate=True)
            player = self._analyzer.detect_saved_recorded_player(content)
            logger.info(
                'Vainglory 小任务耗时明细：kind={} item_id={} total={:.3f}s',
                kind,
                item_id,
                time.monotonic() - started,
            )
            return {
                'kind': kind,
                'itemId': item_id,
                'recordedPlayer': encode_recorded_player(player),
            }

        part_payload = cast(Mapping[str, Any], claim['part'])
        part_id = int(part_payload['id'])
        video = self._cache_dir / 'part-{}.media'.format(part_id)
        try:
            download_seconds = 0.0
            if not video.is_file():
                heartbeat.update(
                    0.01,
                    AnalysisStatus(
                        stage='probing',
                        detail='Mac Worker 正在从 NAS 读取视频',
                        elapsed_seconds=0,
                    ),
                )
                download_started = time.monotonic()
                client.download(str(part_payload['mediaPath']), video)
                download_seconds = time.monotonic() - download_started
            part = VideoPart(
                id=part_id,
                index=int(part_payload['index']),
                path=str(video),
                title=str(part_payload.get('title', '')),
                manual_candidate_times_ms=tuple(
                    int(value)
                    for value in part_payload.get('manualCandidateTimesMs', ())
                ),
            )
            if kind == 'match_rerun':
                matches = self._analyzer.recognize_candidate(
                    part,
                    at_ms=int(claim['resultAtMs']),
                    view_context=cast(Any, str(claim.get('viewContext', 'unknown'))),
                )
                if len(matches) != 1:
                    raise RuntimeError('原时间点没有识别到唯一结算画面')
                return {
                    'kind': kind,
                    'itemId': item_id,
                    'matches': [encode_match(matches[0])],
                }

            anchor_name = str(claim.get('anchorName', '')).strip()
            dense_mode = bool(anchor_name) and anchor_name in self._special_anchors

            def dense_progress(value: float) -> None:
                heartbeat.update(0.02 + value * 0.68)

            if dense_mode:
                heartbeat.update(
                    0.02,
                    AnalysisStatus(
                        stage='coarse_scan',
                        detail='特殊主播：以 4 FPS 全量扫描结算界面',
                        elapsed_seconds=0,
                    ),
                )
                dense = self._analyzer.scan_part_dense(part, progress=dense_progress)
            else:
                dense = self._analyzer.scan_part_cascade(
                    part,
                    progress=dense_progress,
                    status_callback=heartbeat.update_status,
                    debug_dir=self._debug_dir,
                )
            heartbeat.update(
                0.70,
                AnalysisStatus(
                    stage='ocr_recognition',
                    detail='已找到 {} 个候选，开始 OCR 与英雄识别'.format(
                        len(dense.scanned_part.candidate_times_ms)
                    ),
                    elapsed_seconds=dense.total_seconds,
                    candidate_count=len(dense.scanned_part.candidate_times_ms),
                    total_candidates=len(dense.scanned_part.candidate_times_ms),
                ),
            )
            recognition_started = time.monotonic()
            matches = self._analyzer.recognize_scanned_part(
                part,
                dense.scanned_part,
                progress=lambda value: heartbeat.update(0.70 + value * 0.29),
                status_callback=heartbeat.update_status,
                debug_dir=self._debug_dir,
            )
            recognition_seconds = time.monotonic() - recognition_started
            logger.info(
                'Vainglory 任务耗时明细：kind={} item_id={} download={:.3f}s '
                'dense={:.3f}s recognition={:.3f}s total={:.3f}s '
                'decoded_frames={} candidates={} matches={}',
                kind,
                item_id,
                download_seconds,
                dense.total_seconds,
                recognition_seconds,
                time.monotonic() - started,
                dense.decoded_frames,
                len(dense.scanned_part.candidate_times_ms),
                len(matches),
            )
            return {
                'kind': kind,
                'itemId': item_id,
                'candidateCount': len(dense.scanned_part.candidate_times_ms),
                'matches': [encode_match(match) for match in matches],
                'trainingCandidates': [
                    encode_training_candidate(candidate)
                    for candidate in dense.training_candidates
                ],
            }
        finally:
            video.unlink(missing_ok=True)


def load_worker_token(token_file: Optional[Path]) -> str:
    if token_file is not None:
        token = token_file.expanduser().read_text(encoding='utf8').strip()
    else:
        token = os.environ.get('BLREC_ANALYSIS_WORKER_TOKEN', '').strip()
    if len(token) < 32:
        raise ValueError('分析 Worker Token 缺失或长度不足 32 位')
    return token
