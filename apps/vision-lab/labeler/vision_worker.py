"""可暂停的 Vision Worker：远程取图、物化数据集并执行训练任务。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from PIL import Image

from . import __version__, inference, model_prefill, model_testing
from .remote_dataset import materialize_dataset
from .training import PROGRESS_PREFIX, RESULT_PREFIX
from .worker_ui import create_worker_control_plane_app


def validate_local_control_plane_url(server_url: str, ui_port: int) -> None:
    parsed = urllib.parse.urlsplit(server_url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'}:
        raise RuntimeError('启用 Worker 本地控制面时，任务 Server 必须使用本地地址')
    if parsed.port != ui_port:
        raise RuntimeError('Worker 本地控制面端口与 VISION_LAB_SERVER_URL 不一致')


def wait_for_local_control_plane(
    server_url: str, *, timeout_seconds: float = 30
) -> None:
    deadline = time.monotonic() + timeout_seconds
    request = urllib.request.Request(
        server_url.rstrip('/') + '/api/config', method='GET'
    )
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=1):
                return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.2)
    raise RuntimeError('Worker 本地标注控制面启动超时') from last_error


class VisionLabClient:
    def __init__(self, base_url: str, token: str, *, timeout: int = 60) -> None:
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout

    def json(self, method: str, path: str, payload: Any = None) -> Dict[str, Any]:
        data = None
        headers = {'Authorization': f'Bearer {self.token}'}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                value = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            detail = error.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'Vision Lab HTTP {error.code}: {detail}') from error
        if not isinstance(value, dict):
            raise RuntimeError('Vision Lab 返回值不是 JSON 对象')
        return value

    def download(self, path: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            self.base_url + path,
            headers={'Authorization': f'Bearer {self.token}'},
            method='GET',
        )
        try:
            with urllib.request.urlopen(
                request, timeout=max(300, self.timeout)
            ) as response:
                with destination.open('wb') as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
        except urllib.error.HTTPError as error:
            detail = error.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'Vision Lab 下载失败 {error.code}: {detail}') from error

    def upload(
        self, path: str, source: Path, *, query: Dict[str, str]
    ) -> Dict[str, Any]:
        encoded_query = urllib.parse.urlencode(query)
        request = urllib.request.Request(
            f'{self.base_url}{path}?{encoded_query}',
            data=source.read_bytes(),
            headers={
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/octet-stream',
            },
            method='PUT',
        )
        try:
            with urllib.request.urlopen(
                request, timeout=max(600, self.timeout)
            ) as response:
                value = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            detail = error.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'Vision Lab 上传失败 {error.code}: {detail}') from error
        if not isinstance(value, dict):
            raise RuntimeError('Vision Lab 上传返回值不是 JSON 对象')
        return value


class VisionWorker:
    def __init__(
        self,
        *,
        client: VisionLabClient,
        worker_id: str,
        display_name: str,
        work_dir: Path,
        base_models_dir: Path,
        poll_seconds: float = 5.0,
        capabilities: Optional[List[str]] = None,
    ) -> None:
        self.client = client
        self.worker_id = worker_id
        self.display_name = display_name
        self.work_dir = work_dir
        self.base_models_dir = base_models_dir
        self.poll_seconds = max(1.0, poll_seconds)
        self.capabilities = capabilities or [
            'model_prefill',
            'train_model',
            'validate_model',
            'package_models',
        ]
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def register(self) -> Dict[str, Any]:
        return self.client.json(
            'POST',
            '/api/vision-workers/register',
            {
                'worker_id': self.worker_id,
                'display_name': self.display_name,
                'capabilities': self.capabilities,
                'version': __version__,
                'platform': platform.platform(),
                'detail': {
                    'hostname': socket.gethostname(),
                    'python': platform.python_version(),
                },
            },
        )

    def run(self, *, once: bool = False) -> None:
        registered = False
        while True:
            try:
                if not registered:
                    self.register()
                    registered = True
                response = self.client.json(
                    'POST',
                    '/api/vision-workers/claim',
                    {'worker_id': self.worker_id, 'capabilities': self.capabilities},
                )
            except Exception as error:  # noqa: BLE001 - 常驻 Worker 必须自动恢复
                if once:
                    raise
                registered = False
                print(
                    f'Vision Worker 连接控制面失败，稍后重试: {error}',
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(self.poll_seconds)
                continue
            job = response.get('job')
            if not isinstance(job, dict):
                if once:
                    return
                time.sleep(self.poll_seconds)
                continue
            self._execute(job)
            if once:
                return

    def _execute(self, job: Dict[str, Any]) -> None:
        try:
            kind = str(job.get('kind') or '')
            if kind == 'train_model':
                result = self._train(job)
            elif kind == 'model_prefill':
                result = self._prefill(job)
            elif kind == 'validate_model':
                result = self._validate_model(job)
            elif kind == 'package_models':
                result = self._package_models(job)
            else:
                raise ValueError(f"不支持的 Vision Worker 任务: {job.get('kind')}")
            self.client.json(
                'POST',
                f"/api/vision-workers/jobs/{job['id']}/complete",
                {
                    'worker_id': self.worker_id,
                    'lease_token': job['lease_token'],
                    'result': result,
                },
            )
        except Exception as error:  # noqa: BLE001
            try:
                self.client.json(
                    'POST',
                    f"/api/vision-workers/jobs/{job['id']}/fail",
                    {
                        'worker_id': self.worker_id,
                        'lease_token': job.get('lease_token', ''),
                        'error': str(error),
                    },
                )
            except Exception:  # noqa: BLE001
                pass

    def _model_contexts(
        self, models: Dict[str, Any], *, required: Iterable[str] = ()
    ) -> Dict[str, Dict[str, Any]]:
        contexts: Dict[str, Dict[str, Any]] = {}
        for task_id, raw in models.items():
            if not isinstance(raw, dict):
                continue
            run_id = str(raw.get('run_id') or '').strip()
            metadata = raw.get('metadata')
            if not run_id or not isinstance(metadata, dict):
                continue
            destination = self.work_dir / 'model-cache' / run_id / 'model.onnx'
            expected_size = int(raw.get('artifact_size') or 0)
            if not destination.is_file() or (
                expected_size > 0 and destination.stat().st_size != expected_size
            ):
                self.client.download(
                    '/api/vision-workers/model-runs/{}/artifact'.format(
                        urllib.parse.quote(run_id)
                    ),
                    destination,
                )
            contexts[str(task_id)] = {
                'run_id': run_id,
                'artifact': destination,
                'metadata': metadata,
            }
        missing = [task_id for task_id in required if task_id not in contexts]
        if missing:
            raise RuntimeError('缺少可用模型：' + '、'.join(missing))
        return contexts

    def _prefill(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get('payload') or {}
        frame_id = int(payload['frame_id'])
        operation = str(payload.get('operation') or 'core')
        frame_path = self.work_dir / 'prefill-cache' / f'{frame_id}.jpg'
        try:
            self.client.download(
                f'/api/vision-workers/frames/{frame_id}/image', frame_path
            )
            with Image.open(frame_path) as image:
                image_width, image_height = image.size
            contexts = self._model_contexts(payload.get('models') or {})
            if operation == 'core':
                result = model_prefill.run_core_prefill(frame_path, contexts)
                errors = result.get('errors') or {}
                if errors:
                    raise RuntimeError(
                        '核心模型预打标失败：'
                        + '；'.join(
                            f'{task}: {error}' for task, error in errors.items()
                        )
                    )
                return {
                    'operation': operation,
                    'frame_id': frame_id,
                    'image_width': image_width,
                    'image_height': image_height,
                    **result,
                }
            screen_type = str(payload.get('screen_type') or '')
            team_size = int(payload.get('team_size') or 0)
            if operation == 'hero_lineup':
                result = model_prefill.run_hero_lineup_prefill(
                    frame_path, contexts, screen_type=screen_type, team_size=team_size
                )
            elif operation == 'hero_slots':
                result = model_prefill.run_hero_slots_prefill(
                    frame_path,
                    list(payload.get('slots') or []),
                    contexts,
                    screen_type=screen_type,
                    team_size=team_size,
                )
            else:
                raise ValueError(f'未知预填操作: {operation}')
            return {
                'operation': operation,
                'frame_id': frame_id,
                'screen_type': screen_type,
                'team_size': team_size,
                'image_width': image_width,
                'image_height': image_height,
                **result,
            }
        finally:
            frame_path.unlink(missing_ok=True)

    def _heartbeat_once(
        self, job: Dict[str, Any], *, progress: float, stage: str, detail: str
    ) -> None:
        response = self.client.json(
            'POST',
            f"/api/vision-workers/jobs/{job['id']}/heartbeat",
            {
                'worker_id': self.worker_id,
                'lease_token': job['lease_token'],
                'progress': progress,
                'stage': stage,
                'detail': detail,
            },
        )
        if response.get('cancel_requested'):
            raise RuntimeError('用户取消')

    def _validate_model(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get('payload') or {}
        run_id = str(payload['run_id'])
        split = str(payload['split'])
        sample_id = str(payload.get('sample_id') or '')
        query = {'split': split}
        if sample_id:
            query['sample_id'] = sample_id
        plan = self.client.json(
            'GET',
            '/api/vision-workers/model-tests/{}/plan?{}'.format(
                urllib.parse.quote(run_id), urllib.parse.urlencode(query)
            ),
        )
        task_id = str(plan['task_id'])
        contexts = self._model_contexts({task_id: plan['model']}, required=(task_id,))
        context = contexts[task_id]
        samples = list(plan.get('samples') or [])
        predictions: Dict[str, Dict[str, Any]] = {}
        job_dir = self.work_dir / 'jobs' / str(job['id']) / 'validation'
        originals = job_dir / 'frames'
        crops = job_dir / 'crops'
        started = time.perf_counter()
        last_heartbeat = 0.0
        try:
            for index, sample in enumerate(samples, start=1):
                current_id = str(sample.get('sample_id') or '')
                try:
                    frame_id = int(sample.get('frame_id') or 0)
                    if frame_id <= 0:
                        raise ValueError('样本缺少可追溯 frame_id')
                    source = originals / f'{frame_id}.jpg'
                    if not source.is_file():
                        self.client.download(
                            f'/api/vision-workers/frames/{frame_id}/image', source
                        )
                    inference_path = source
                    crop = sample.get('crop')
                    if isinstance(crop, dict):
                        inference_path = crops / f'{current_id}.jpg'
                        inference_path.parent.mkdir(parents=True, exist_ok=True)
                        with Image.open(source) as opened:
                            model_prefill._crop_to_path(  # noqa: SLF001
                                opened.convert('RGB'), crop, inference_path
                            )
                    predictions[current_id] = {
                        'result': inference.run_artifact(
                            context['artifact'],
                            context['metadata'],
                            inference_path,
                            conf_thr=float(payload.get('conf_thr', 0.25)),
                        )
                    }
                except Exception as error:  # noqa: BLE001
                    predictions[current_id] = {'error': str(error)}
                now = time.monotonic()
                if now - last_heartbeat >= 10 or index == len(samples):
                    self._heartbeat_once(
                        job,
                        progress=index / max(1, len(samples)),
                        stage='批量验收模型',
                        detail=f'{index}/{len(samples)} 张',
                    )
                    last_heartbeat = now
            report = model_testing.evaluation_report_from_predictions(
                run_id=run_id,
                task_id=task_id,
                kind=str(plan['kind']),
                split=split,
                samples=samples,
                predictions=predictions,
                total=int(plan.get('total') or len(samples)),
                conf_thr=float(payload.get('conf_thr', 0.25)),
                iou_threshold=float(payload.get('iou_threshold', 0.5)),
                elapsed_seconds=time.perf_counter() - started,
            )
            result: Dict[str, Any] = {'report': report}
            if sample_id:
                outcome = predictions.get(sample_id) or {}
                if isinstance(outcome.get('result'), dict):
                    result['prediction'] = outcome['result']
            return result
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def _package_models(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get('payload') or {}
        package_id = str(payload['package_id'])
        job_dir = self.work_dir / 'jobs' / str(job['id']) / package_id
        models_dir = job_dir / 'models'
        models_dir.mkdir(parents=True, exist_ok=True)
        manifest = dict(payload.get('manifest') or {})
        manifest_models: Dict[str, Any] = {}
        models = payload.get('models') or {}
        try:
            for index, (role, spec) in enumerate(sorted(models.items()), start=1):
                run_id = str(spec['run_id'])
                destination = models_dir / f'{role}.onnx'
                self.client.download(
                    '/api/vision-workers/model-runs/{}/artifact'.format(
                        urllib.parse.quote(run_id)
                    ),
                    destination,
                )
                expected_size = int(spec.get('artifact_size') or 0)
                if expected_size and destination.stat().st_size != expected_size:
                    raise RuntimeError(f'{role} 模型大小校验失败')
                manifest_models[role] = {
                    **dict(spec.get('manifest') or {}),
                    'sha256': self._sha256(destination),
                }
                self._heartbeat_once(
                    job,
                    progress=0.75 * index / max(1, len(models)),
                    stage='组装模型包',
                    detail=f'{index}/{len(models)} 个模型',
                )
            manifest['models'] = manifest_models
            for name, value in (
                ('manifest.json', manifest),
                ('dataset-lock.json', payload.get('dataset_lock') or {}),
                ('metrics.json', payload.get('metrics') or {}),
            ):
                (job_dir / name).write_text(
                    json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8'
                )
            archive_base = job_dir.parent / package_id
            archive_path = Path(
                shutil.make_archive(
                    str(archive_base),
                    'zip',
                    root_dir=job_dir.parent,
                    base_dir=job_dir.name,
                )
            )
            self._heartbeat_once(
                job,
                progress=0.9,
                stage='上传模型包',
                detail=f'{archive_path.stat().st_size / 1024 / 1024:.1f} MB',
            )
            self.client.upload(
                f"/api/vision-workers/jobs/{job['id']}/artifacts/package.zip",
                archive_path,
                query={'worker_id': self.worker_id, 'lease_token': job['lease_token']},
            )
            return {
                'package_id': package_id,
                'status': str(payload.get('status') or 'incomplete'),
                'missing_tasks': payload.get('missing_tasks') or [],
                'evaluation_gaps': payload.get('evaluation_gaps') or {},
                'manifest': manifest,
            }
        finally:
            shutil.rmtree(job_dir.parent, ignore_errors=True)

    def _train(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get('payload') or {}
        run_id = str(payload['run_id'])
        task_id = str(payload['task_id'])
        version_id = str(payload['dataset_version_id'])
        job_dir = self.work_dir / 'jobs' / run_id
        dataset_dir = self.work_dir / 'datasets' / version_id
        manifest = job_dir / 'samples.jsonl'
        run_dir = job_dir / 'training'
        log_path = job_dir / 'train.log'
        job_dir.mkdir(parents=True, exist_ok=True)

        state: Dict[str, Any] = {
            'progress': 0.0,
            'stage': '下载数据清单',
            'detail': '',
            'current_epoch': 0,
            'metrics': {},
        }
        state_lock = threading.Lock()
        stop = threading.Event()
        cancelled = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job, state, state_lock, stop, cancelled),
            name=f"vision-heartbeat-{job['id']}",
            daemon=True,
        )
        heartbeat.start()
        try:
            self.client.download(
                f'/api/vision-workers/datasets/{urllib.parse.quote(version_id)}/manifest',
                manifest,
            )

            def fetch_image(frame_id: int, destination: Path) -> None:
                if cancelled.is_set():
                    raise RuntimeError('用户取消')
                self.client.download(
                    f'/api/vision-workers/frames/{frame_id}/image', destination
                )

            def materialize_progress(done: int, total: int) -> None:
                with state_lock:
                    state.update(
                        {
                            'progress': 0.15 * done / max(1, total),
                            'stage': '远程取图并生成数据集',
                            'detail': f'{done}/{total} 张原图',
                        }
                    )

            dataset = materialize_dataset(
                task_id=task_id,
                manifest_path=manifest,
                output_dir=dataset_dir,
                fetch_image=fetch_image,
                progress=materialize_progress,
                frame_cache_dir=self.work_dir / 'frame-cache',
                download_workers=8,
            )
            if cancelled.is_set():
                raise RuntimeError('用户取消')
            artifact = job_dir / 'model.onnx'
            base_model = self.base_models_dir / str(payload['base_model'])
            command = [
                sys.executable,
                '-m',
                'labeler.training_runner',
                '--task-id',
                task_id,
                '--kind',
                str(payload['kind']),
                '--dataset-dir',
                str(dataset_dir),
                '--run-dir',
                str(run_dir),
                '--artifact',
                str(artifact),
                '--base-model',
                str(base_model),
                '--epochs',
                str(int(payload['epochs'])),
                '--imgsz',
                str(int(payload['imgsz'])),
            ]
            if payload['kind'] == 'classify':
                command.extend(
                    [
                        '--input-width',
                        str(int(payload['input_width'])),
                        '--input-height',
                        str(int(payload['input_height'])),
                    ]
                )
            if payload.get('resume'):
                checkpoint = run_dir / 'ultralytics' / 'weights' / 'last.pt'
                if not checkpoint.is_file():
                    raise RuntimeError('该 Worker 没有这个任务的可用训练断点')
                command.extend(['--resume-checkpoint', str(checkpoint)])
            with state_lock:
                state.update({'stage': '训练模型', 'progress': 0.15, 'detail': ''})
            metrics = self._run_training(
                command=command,
                log_path=log_path,
                epochs=int(payload['epochs']),
                job_state=state,
                state_lock=state_lock,
                cancelled=cancelled,
            )
            if not artifact.is_file() or artifact.stat().st_size <= 0:
                raise RuntimeError('训练完成但没有生成 ONNX 模型')
            query = {'worker_id': self.worker_id, 'lease_token': job['lease_token']}
            with state_lock:
                state.update({'stage': '上传模型', 'progress': 0.98})
            self.client.upload(
                f"/api/vision-workers/jobs/{job['id']}/artifacts/model.onnx",
                artifact,
                query=query,
            )
            metadata = artifact.with_suffix('.json')
            if metadata.is_file():
                self.client.upload(
                    f"/api/vision-workers/jobs/{job['id']}/artifacts/model.json",
                    metadata,
                    query=query,
                )
            if log_path.is_file() and log_path.stat().st_size > 0:
                self.client.upload(
                    f"/api/vision-workers/jobs/{job['id']}/artifacts/train.log",
                    log_path,
                    query=query,
                )
            return {
                'run_id': run_id,
                'task_id': task_id,
                'dataset_version_id': version_id,
                'epochs': int(payload['epochs']),
                'metrics': metrics,
                'dataset': dataset,
            }
        finally:
            stop.set()
            heartbeat.join(timeout=10)

    def _run_training(
        self,
        *,
        command: list[str],
        log_path: Path,
        epochs: int,
        job_state: Dict[str, Any],
        state_lock: threading.Lock,
        cancelled: threading.Event,
    ) -> Dict[str, Any]:
        environment = dict(os.environ)
        environment['PYTHONUNBUFFERED'] = '1'
        metrics: Dict[str, Any] = {}
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('a', encoding='utf-8') as log_handle:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=environment,
            )
            assert process.stdout is not None
            for line in process.stdout:
                log_handle.write(line)
                log_handle.flush()
                if cancelled.is_set():
                    process.terminate()
                    process.wait(timeout=30)
                    raise RuntimeError('用户取消')
                stripped = line.strip()
                with state_lock:
                    job_state['detail'] = stripped[-2_000:]
                if stripped.startswith(PROGRESS_PREFIX):
                    value = json.loads(stripped[len(PROGRESS_PREFIX) :])
                    metrics = value.get('metrics') or metrics
                    epoch = int(value.get('epoch', 0))
                    with state_lock:
                        job_state.update(
                            {
                                'current_epoch': epoch,
                                'progress': 0.15
                                + 0.82 * min(1.0, epoch / max(1, epochs)),
                                'metrics': metrics,
                            }
                        )
                elif stripped.startswith(RESULT_PREFIX):
                    value = json.loads(stripped[len(RESULT_PREFIX) :])
                    metrics = value.get('metrics') or metrics
            return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(f'训练进程退出码 {return_code}，请查看日志')
        return metrics

    def _heartbeat_loop(
        self,
        job: Dict[str, Any],
        state: Dict[str, Any],
        state_lock: threading.Lock,
        stop: threading.Event,
        cancelled: threading.Event,
    ) -> None:
        while not stop.is_set():
            with state_lock:
                payload = dict(state)
            payload.update(
                {'worker_id': self.worker_id, 'lease_token': job['lease_token']}
            )
            try:
                response = self.client.json(
                    'POST', f"/api/vision-workers/jobs/{job['id']}/heartbeat", payload
                )
                if response.get('cancel_requested'):
                    cancelled.set()
            except Exception:  # noqa: BLE001
                pass
            stop.wait(20)


def main() -> None:
    parser = argparse.ArgumentParser(description='BLREC Vision Worker')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    server_url = os.environ.get('VISION_LAB_SERVER_URL', '').strip()
    token = os.environ.get('VISION_LAB_WORKER_TOKEN', '').strip()
    token_file = os.environ.get('VISION_LAB_WORKER_TOKEN_FILE', '').strip()
    if not token and token_file:
        token = Path(token_file).expanduser().read_text(encoding='utf-8').strip()
    if not server_url or not token:
        raise RuntimeError('必须配置 VISION_LAB_SERVER_URL 和 Vision Worker token')
    worker_id = os.environ.get('VISION_WORKER_ID', socket.gethostname()).strip()
    display_name = os.environ.get('VISION_WORKER_NAME', worker_id).strip()
    work_dir = Path(
        os.environ.get('VISION_WORKER_DATA_DIR', '~/.local/share/blrec-vision-worker')
    ).expanduser()
    base_models_dir = Path(
        os.environ.get(
            'VISION_WORKER_BASE_MODELS_DIR',
            str(Path(__file__).resolve().parent.parent / 'data' / 'models' / 'base'),
        )
    ).expanduser()
    supported = {'model_prefill', 'train_model', 'validate_model', 'package_models'}
    configured = os.environ.get('VISION_WORKER_CAPABILITIES', '').strip()
    capabilities = (
        [value.strip() for value in configured.split(',') if value.strip() in supported]
        if configured
        else sorted(supported)
    )
    if not capabilities:
        raise RuntimeError('VISION_WORKER_CAPABILITIES 没有有效任务类型')
    ui_port = int(os.environ.get('VISION_WORKER_UI_PORT', '0'))
    if not 0 <= ui_port <= 65_535:
        raise RuntimeError('VISION_WORKER_UI_PORT 必须在 0 到 65535 之间')
    worker = VisionWorker(
        client=VisionLabClient(server_url, token),
        worker_id=worker_id,
        display_name=display_name,
        work_dir=work_dir,
        base_models_dir=base_models_dir,
        poll_seconds=float(os.environ.get('VISION_WORKER_POLL_SECONDS', '5')),
        capabilities=capabilities,
    )
    if not ui_port:
        worker.run(once=args.once)
        return

    import uvicorn

    validate_local_control_plane_url(server_url, ui_port)
    app = create_worker_control_plane_app()
    ui_host = os.environ.get('VISION_WORKER_UI_HOST', '0.0.0.0').strip()
    if args.once:
        threading.Thread(
            target=uvicorn.run,
            args=(app,),
            kwargs={'host': ui_host, 'port': ui_port, 'log_level': 'info'},
            daemon=True,
            name='vision-worker-ui',
        ).start()
        wait_for_local_control_plane(server_url)
        worker.run(once=True)
        return

    def run_worker() -> None:
        while True:
            try:
                wait_for_local_control_plane(server_url)
                worker.run()
            except Exception as error:  # noqa: BLE001 - Server 不能被任务轮询带停
                print(
                    f'Vision Worker 后台线程异常，稍后重试: {error}',
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(worker.poll_seconds)

    threading.Thread(target=run_worker, daemon=True, name='vision-worker-jobs').start()
    uvicorn.run(app, host=ui_host, port=ui_port, log_level='info')


if __name__ == '__main__':
    main()
