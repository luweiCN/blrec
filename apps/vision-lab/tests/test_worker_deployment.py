"""Vision Lab 向 Mac Analysis Worker 发布不可变模型包。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import config, db, server  # noqa: E402
from labeler.worker_deployment import (  # noqa: E402
    _REMOTE_DEPLOY_SCRIPT,
    _REMOTE_STATUS_SCRIPT,
    WorkerDeploymentClient,
    WorkerDeploymentTarget,
)


class FakeProcessRunner:
    def __init__(self, responses: List[subprocess.CompletedProcess[bytes]]) -> None:
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, command: List[str], **kwargs: Any) -> Any:
        self.calls.append({'command': command, **kwargs})
        return self.responses.pop(0)


class TestWorkerDeploymentClient(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'package.zip'
        self.archive.write_bytes(b'zip-content')
        self.target = WorkerDeploymentTarget(
            host='worker.example.test',
            user='luwei',
            model_root=(
                '~/Library/Application Support/BLRECAnalysisWorker/model-packages'
            ),
            launchd_label='com.luwei.blrec-analysis-worker',
            launchd_plist=(
                '~/Library/LaunchAgents/com.luwei.blrec-analysis-worker.plist'
            ),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_remote_scripts_are_valid_python(self) -> None:
        compile(_REMOTE_STATUS_SCRIPT, '<worker-status>', 'exec')
        compile(_REMOTE_DEPLOY_SCRIPT, '<worker-deploy>', 'exec')

    def test_deploy_uploads_archive_and_returns_verified_worker_state(self) -> None:
        payload = {
            'ok': True,
            'package_id': 'vg-vision-v2',
            'previous_package_id': 'vg-vision-v1',
            'worker_state': 'running',
        }
        runner = FakeProcessRunner(
            [
                subprocess.CompletedProcess([], 0, b'', b''),
                subprocess.CompletedProcess(
                    [], 0, json.dumps(payload).encode('utf-8'), b''
                ),
            ]
        )
        client = WorkerDeploymentClient(self.target, run_process=runner)

        result = client.deploy(self.archive, 'vg-vision-v2')

        self.assertEqual(result, payload)
        self.assertEqual(runner.calls[0]['command'][0], 'scp')
        self.assertEqual(runner.calls[1]['command'][0], 'ssh')
        self.assertIn('/usr/bin/python3 -', runner.calls[1]['command'][-1])
        self.assertIn(b'REQUIRED_MODEL_ROLES', runner.calls[1]['input'])

    def test_deploy_does_not_hide_remote_validation_error(self) -> None:
        runner = FakeProcessRunner(
            [
                subprocess.CompletedProcess([], 0, b'', b''),
                subprocess.CompletedProcess(
                    [], 1, b'', '模型 SHA-256 校验失败'.encode()
                ),
            ]
        )
        client = WorkerDeploymentClient(self.target, run_process=runner)

        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            client.deploy(self.archive, 'vg-vision-v2')

    def test_deploy_reads_structured_remote_error_from_stdout(self) -> None:
        runner = FakeProcessRunner(
            [
                subprocess.CompletedProcess([], 0, b'', b''),
                subprocess.CompletedProcess(
                    [],
                    1,
                    json.dumps(
                        {'ok': False, 'error': '新 Worker 未启动，已经回滚'},
                        ensure_ascii=False,
                    ).encode('utf-8'),
                    b'',
                ),
            ]
        )
        client = WorkerDeploymentClient(self.target, run_process=runner)

        with self.assertRaisesRegex(RuntimeError, '已经回滚'):
            client.deploy(self.archive, 'vg-vision-v2')

    def test_package_id_and_ssh_target_are_validated(self) -> None:
        client = WorkerDeploymentClient(self.target, run_process=FakeProcessRunner([]))
        with self.assertRaisesRegex(ValueError, '模型包 ID'):
            client.deploy(self.archive, '../escape')
        with self.assertRaisesRegex(ValueError, 'Worker SSH 主机'):
            WorkerDeploymentClient(
                WorkerDeploymentTarget(
                    host='-oProxyCommand=bad',
                    user='luwei',
                    model_root='/tmp/models',
                    launchd_label='com.luwei.worker',
                    launchd_plist='/tmp/worker.plist',
                ),
                run_process=FakeProcessRunner([]),
            )


class TestWorkerDeploymentApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db_path = config.DB_PATH
        self.old_work_dir = config.WORK_DIR
        config.DB_PATH = self.root / 'lab.db'
        config.WORK_DIR = self.root / 'work'
        self.package_dir = config.WORK_DIR / 'model-packages' / 'vg-ready'
        self.package_dir.mkdir(parents=True)
        conn = db.connect(config.DB_PATH)
        db.create_model_package(
            conn,
            package_id='vg-ready',
            status='ready',
            path=str(self.package_dir),
            manifest={'package_id': 'vg-ready', 'status': 'ready'},
        )
        conn.close()
        self.target = WorkerDeploymentTarget(
            host='worker.example.test',
            user='luwei',
            model_root='/tmp/models',
            launchd_label='com.luwei.blrec-analysis-worker',
            launchd_plist='/tmp/worker.plist',
        )

    def tearDown(self) -> None:
        config.DB_PATH = self.old_db_path
        config.WORK_DIR = self.old_work_dir
        self.tmp.cleanup()

    def test_api_creates_persistent_job_before_starting_background_deploy(self) -> None:
        with mock.patch.object(
            server.worker_deployment, 'configured_target', return_value=self.target
        ), mock.patch.object(server.threading, 'Thread') as thread:
            result = server.api_deploy_model_package_to_worker('vg-ready')

        self.assertEqual(result['deployment']['status'], 'queued')
        self.assertEqual(result['target'], 'luwei@worker.example.test:22')
        thread.return_value.start.assert_called_once_with()
        conn = db.connect(config.DB_PATH)
        try:
            self.assertEqual(db.list_model_deployments(conn)[0]['status'], 'queued')
        finally:
            conn.close()

    def test_background_deploy_records_verified_worker_version(self) -> None:
        conn = db.connect(config.DB_PATH)
        deployment = db.create_model_deployment(
            conn, package_id='vg-ready', target='analysis-worker'
        )
        conn.close()
        archive = config.WORK_DIR / 'model-packages' / 'vg-ready.zip'
        archive.write_bytes(b'archive')
        client = mock.Mock()
        client.deploy.return_value = {
            'ok': True,
            'package_id': 'vg-ready',
            'previous_package_id': 'vg-old',
            'worker_state': 'running',
        }
        with mock.patch.object(
            server.worker_deployment, 'configured_target', return_value=self.target
        ), mock.patch.object(
            server.worker_deployment, 'WorkerDeploymentClient', return_value=client
        ), mock.patch.object(
            server.model_testing, 'model_package_archive', return_value=archive
        ):
            server._deploy_model_package_to_worker(int(deployment['id']), 'vg-ready')

        conn = db.connect(config.DB_PATH)
        try:
            stored = db.get_model_deployment(conn, int(deployment['id']))
        finally:
            conn.close()
        self.assertEqual(stored['status'], 'succeeded')
        self.assertEqual(stored['worker_package_id'], 'vg-ready')
        self.assertEqual(stored['previous_package_id'], 'vg-old')


if __name__ == '__main__':
    unittest.main()
