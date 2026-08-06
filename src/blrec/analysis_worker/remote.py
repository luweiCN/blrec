from __future__ import annotations

import base64
import os
import socket
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, cast
from urllib.parse import urljoin

import requests
from loguru import logger

from blrec.vainglory.analyzer import AnalysisStatus, VaingloryVideoAnalyzer, VideoPart

from .codec import encode_hero, encode_match, encode_recorded_player


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

    def fail(self, *, kind: str, item_id: int, error: str) -> None:
        response = self._session.post(
            self._url('api/v1/vainglory/worker/fail'),
            headers=self._headers,
            json={'kind': kind, 'itemId': item_id, 'error': error[:500]},
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
        while not self._stop.wait(self._interval_seconds):
            with self._lock:
                progress = self._progress
                runtime_status = self._runtime_status
            try:
                self._client.heartbeat(
                    self._kind, self._item_id, progress, runtime_status
                )
            except requests.RequestException as error:
                logger.warning('分析 Worker 心跳发送失败：{!r}', error)


class RemoteAnalysisWorker:
    def __init__(
        self,
        client: AnalysisWorkerClient,
        analyzer: VaingloryVideoAnalyzer,
        *,
        cache_dir: Path,
        poll_seconds: float = 5,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError('轮询间隔必须为正数')
        self._client = client
        self._analyzer = analyzer
        self._cache_dir = cache_dir.expanduser().resolve()
        self._poll_seconds = poll_seconds

    def run(self, *, once: bool = False) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            'Mac 分析 Worker 已启动：worker_id={} cache={}',
            socket.gethostname(),
            self._cache_dir,
        )
        while True:
            try:
                claim = self._client.claim()
            except requests.RequestException as error:
                logger.warning('无法从 NAS 领取分析任务：{!r}', error)
                if once:
                    raise
                time.sleep(self._poll_seconds)
                continue
            if claim is None:
                if once:
                    return
                time.sleep(self._poll_seconds)
                continue
            self._process_claim(claim)
            if once:
                return

    def _process_claim(self, claim: Mapping[str, Any]) -> None:
        kind = str(claim['kind'])
        item_id = int(claim['itemId'])
        started = time.monotonic()
        logger.info('开始处理分析任务：kind={} item_id={}', kind, item_id)
        try:
            with _Heartbeat(self._client, kind=kind, item_id=item_id) as heartbeat:
                payload = self._analyze(claim, heartbeat)
                self._client.complete(payload)
            logger.info(
                '分析任务完成：kind={} item_id={} elapsed_seconds={:.3f}',
                kind,
                item_id,
                time.monotonic() - started,
            )
        except Exception as error:
            logger.exception('分析任务失败：kind={} item_id={}', kind, item_id)
            try:
                self._client.fail(
                    kind=kind,
                    item_id=item_id,
                    error='{}: {}'.format(type(error).__name__, error),
                )
            except requests.RequestException as report_error:
                logger.warning('分析失败状态未能写回 NAS：{!r}', report_error)

    def _analyze(
        self, claim: Mapping[str, Any], heartbeat: _Heartbeat
    ) -> Dict[str, Any]:
        kind = str(claim['kind'])
        item_id = int(claim['itemId'])
        if kind == 'hero_rematch':
            content = base64.b64decode(str(claim['framePng']), validate=True)
            heroes = self._analyzer.recognize_saved_heroes(content)
            return {
                'kind': kind,
                'itemId': item_id,
                'heroes': [encode_hero(hero) for hero in heroes],
            }
        if kind == 'recorded_player_backfill':
            content = base64.b64decode(str(claim['framePng']), validate=True)
            player = self._analyzer.detect_saved_recorded_player(content)
            return {
                'kind': kind,
                'itemId': item_id,
                'recordedPlayer': encode_recorded_player(player),
            }

        part_payload = cast(Mapping[str, Any], claim['part'])
        part_id = int(part_payload['id'])
        video = self._cache_dir / 'part-{}.media'.format(part_id)
        try:
            if not video.is_file():
                heartbeat.update(
                    0.01,
                    AnalysisStatus(
                        stage='probing',
                        detail='Mac Worker 正在从 NAS 读取视频',
                        elapsed_seconds=0,
                    ),
                )
                self._client.download(str(part_payload['mediaPath']), video)
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

            def dense_progress(value: float) -> None:
                heartbeat.update(
                    0.02 + value * 0.68,
                    AnalysisStatus(
                        stage='fine_scan',
                        detail='Mac Worker 正在以 4 FPS 全量扫描结算界面',
                        elapsed_seconds=0,
                    ),
                )

            dense = self._analyzer.scan_part_dense(part, progress=dense_progress)
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
            matches = self._analyzer.recognize_scanned_part(
                part,
                dense.scanned_part,
                progress=lambda value: heartbeat.update(0.70 + value * 0.29),
                status_callback=heartbeat.update_status,
            )
            return {
                'kind': kind,
                'itemId': item_id,
                'candidateCount': len(dense.scanned_part.candidate_times_ms),
                'matches': [encode_match(match) for match in matches],
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
