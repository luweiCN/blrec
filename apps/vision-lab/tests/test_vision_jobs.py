"""Vision Worker 队列必须保证能力匹配、暂停和租约隔离。"""

import tempfile
from pathlib import Path

from labeler import db, vision_jobs


def _database(tmp: str):
    return db.connect(Path(tmp) / 'lab.db')


def test_worker_only_claims_supported_job() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        conn = _database(temporary)
        try:
            vision_jobs.register_worker(
                conn,
                worker_id='mac-studio',
                display_name='Mac Studio',
                capabilities=['train_model'],
            )
            vision_jobs.create_job(
                conn,
                kind='model_prefill',
                related_id='prefill-1',
                payload={'batch': 1},
                priority=100,
            )
            expected = vision_jobs.create_job(
                conn,
                kind='train_model',
                related_id='run-1',
                payload={'run_id': 'run-1'},
                priority=10,
            )

            claimed = vision_jobs.claim_job(
                conn,
                worker_id='mac-studio',
                capabilities=['train_model'],
                lease_seconds=300,
            )

            assert claimed is not None
            assert claimed['id'] == expected['id']
            assert claimed['lease_token']
        finally:
            conn.close()


def test_paused_worker_cannot_claim() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        conn = _database(temporary)
        try:
            vision_jobs.register_worker(
                conn,
                worker_id='mac-studio',
                display_name='Mac Studio',
                capabilities=['train_model'],
            )
            vision_jobs.set_worker_enabled(conn, worker_id='mac-studio', enabled=False)
            vision_jobs.create_job(
                conn,
                kind='train_model',
                related_id='run-1',
                payload={'run_id': 'run-1'},
            )

            assert (
                vision_jobs.claim_job(
                    conn,
                    worker_id='mac-studio',
                    capabilities=['train_model'],
                    lease_seconds=300,
                )
                is None
            )
        finally:
            conn.close()


def test_only_lease_owner_can_update_or_finish() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        conn = _database(temporary)
        try:
            vision_jobs.register_worker(
                conn,
                worker_id='mac-studio',
                display_name='Mac Studio',
                capabilities=['train_model'],
            )
            job = vision_jobs.create_job(
                conn,
                kind='train_model',
                related_id='run-1',
                payload={'run_id': 'run-1'},
            )
            claimed = vision_jobs.claim_job(
                conn,
                worker_id='mac-studio',
                capabilities=['train_model'],
                lease_seconds=300,
            )
            assert claimed is not None

            try:
                vision_jobs.update_job_lease(
                    conn,
                    job_id=job['id'],
                    worker_id='another-worker',
                    lease_token=claimed['lease_token'],
                    lease_seconds=300,
                )
            except PermissionError:
                pass
            else:
                raise AssertionError('非租约所有者不应能更新任务')

            finished = vision_jobs.finish_job(
                conn,
                job_id=job['id'],
                worker_id='mac-studio',
                lease_token=claimed['lease_token'],
                succeeded=True,
                result={'artifact': 'model.onnx'},
            )
            assert finished['status'] == 'succeeded'
            assert finished['result']['artifact'] == 'model.onnx'
        finally:
            conn.close()


def test_duplicate_active_related_job_is_reused() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        conn = _database(temporary)
        try:
            first = vision_jobs.create_job(
                conn, kind='train_model', related_id='run-1', payload={'attempt': 1}
            )
            second = vision_jobs.create_job(
                conn, kind='train_model', related_id='run-1', payload={'attempt': 2}
            )
            assert second['id'] == first['id']
            assert second['payload'] == {'attempt': 1}
        finally:
            conn.close()
