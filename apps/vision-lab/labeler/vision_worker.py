"""可暂停的 Vision Worker：远程取图、物化数据集并执行训练任务。"""

from __future__ import annotations

import argparse
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
from typing import Any, Dict, Optional

from . import __version__
from .remote_dataset import materialize_dataset
from .training import PROGRESS_PREFIX, RESULT_PREFIX


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
    capabilities = ['train_model']

    def __init__(
        self,
        *,
        client: VisionLabClient,
        worker_id: str,
        display_name: str,
        work_dir: Path,
        base_models_dir: Path,
        poll_seconds: float = 5.0,
    ) -> None:
        self.client = client
        self.worker_id = worker_id
        self.display_name = display_name
        self.work_dir = work_dir
        self.base_models_dir = base_models_dir
        self.poll_seconds = max(1.0, poll_seconds)
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
        self.register()
        while True:
            response = self.client.json(
                'POST',
                '/api/vision-workers/claim',
                {'worker_id': self.worker_id, 'capabilities': self.capabilities},
            )
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
            if job.get('kind') != 'train_model':
                raise ValueError(f"不支持的 Vision Worker 任务: {job.get('kind')}")
            result = self._train(job)
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
    if not server_url or not token:
        raise RuntimeError('必须配置 VISION_LAB_SERVER_URL 和 VISION_LAB_WORKER_TOKEN')
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
    VisionWorker(
        client=VisionLabClient(server_url, token),
        worker_id=worker_id,
        display_name=display_name,
        work_dir=work_dir,
        base_models_dir=base_models_dir,
        poll_seconds=float(os.environ.get('VISION_WORKER_POLL_SECONDS', '5')),
    ).run(once=args.once)


if __name__ == '__main__':
    main()
