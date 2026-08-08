"""训练任务必须绑定不可变数据集快照并保留历史。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import config, db, export, training


class TestTrainingRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / 'lab.db')
        for version in ('bp-classifier-v1', 'bp-classifier-v2'):
            db.create_dataset_version(
                self.conn,
                version_id=version,
                task_id='bp_review',
                filter_json={},
                counts={'total': 10},
                manifest_path=f'/tmp/{version}/samples.jsonl',
            )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_each_run_keeps_its_dataset_snapshot(self):
        db.create_training_run(
            self.conn,
            run_id='bp-train-1',
            task_id='bp_review',
            dataset_version_id='bp-classifier-v1',
            epochs=60,
            config_json={'imgsz': 224},
            log_path='/tmp/one.log',
        )
        db.create_training_run(
            self.conn,
            run_id='bp-train-2',
            task_id='bp_review',
            dataset_version_id='bp-classifier-v2',
            epochs=60,
            config_json={'imgsz': 224},
            log_path='/tmp/two.log',
        )
        db.update_training_run(
            self.conn,
            'bp-train-1',
            status='succeeded',
            current_epoch=60,
            progress=1.0,
            metrics={'accuracy': 0.9},
            artifact_path='/tmp/bp-train-1.onnx',
        )

        runs = db.list_training_runs(self.conn)

        self.assertEqual(len(runs), 2)
        by_id = {run['id']: run for run in runs}
        self.assertEqual(by_id['bp-train-1']['dataset_version_id'], 'bp-classifier-v1')
        self.assertEqual(by_id['bp-train-2']['dataset_version_id'], 'bp-classifier-v2')
        self.assertEqual(by_id['bp-train-1']['metrics_json']['accuracy'], 0.9)

    def test_update_rejects_unknown_fields(self):
        db.create_training_run(
            self.conn,
            run_id='bp-train-1',
            task_id='bp_review',
            dataset_version_id='bp-classifier-v1',
            epochs=60,
            config_json={},
            log_path='/tmp/one.log',
        )

        with self.assertRaises(ValueError):
            db.update_training_run(
                self.conn, 'bp-train-1', unsafe_sql_fragment='DROP TABLE'
            )


class TestTrainingReadiness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / 'lab.db')

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _frame(self, video_id, index):
        image = self.root / f'{index}.jpg'
        image.write_bytes(f'image-{index}'.encode())
        return db.add_frames(
            self.conn,
            video_id,
            [
                {
                    'timestamp_ms': index * 1_000,
                    'width': 1280,
                    'height': 720,
                    'sha256': f'{index:064x}',
                    'phash': '',
                    'frame_path': str(image),
                    'thumb_path': '',
                    'strategy': 'test',
                    'model_source': '',
                    'model_confidence': None,
                }
            ],
        )[0]

    def _ready_bp_data(self):
        videos = [
            db.upsert_video(
                self.conn,
                remote_path=f'/nas/{index}.flv',
                streamer=str(index),
                room_id=str(index),
                filename=f'{index}.flv',
                duration_seconds=10,
                size_bytes=1,
            )
            for index in (1, 2)
        ]
        labels = [
            ('hero_select_bp', '3v3'),
            ('hero_select_bp', '3v3'),
            ('hero_select_aram', 'aram'),
            ('hero_select_aram', 'aram'),
            ('hero_select_bp', '5v5'),
            ('hero_select_bp', '5v5'),
            ('match_confirm', 'unknown'),
            ('match_confirm', 'unknown'),
        ]
        for index, (screen_type, mode) in enumerate(labels, 1):
            db.save_annotation(
                self.conn,
                self._frame(videos[index % 2], index),
                {
                    'content_family': 'vainglory',
                    'game_context': 'pre_match',
                    'screen_type': screen_type,
                    'game_mode': mode,
                },
                status='complete',
            )
        return videos

    def test_match_confirmation_counts_only_as_not_bp(self):
        self._ready_bp_data()

        summary = next(
            item
            for item in training.task_summaries(self.conn)
            if item['id'] == 'bp_review'
        )

        self.assertEqual(
            summary['counts']['by_label'],
            {'bp_3v3': 2, 'bp_aram': 2, 'bp_5v5': 2, 'not_bp': 2},
        )
        self.assertTrue(summary['ready'])
        self.assertIn('非 BP 2/200', summary['quality_warnings'])

    def test_retraining_freezes_a_new_snapshot(self):
        videos = self._ready_bp_data()
        old_export_dir = config.EXPORT_DIR
        config.EXPORT_DIR = self.root / 'datasets'
        try:
            first = training.export_snapshot(self.conn, 'bp_review')
            first_manifest = Path(first['dir']) / 'samples.jsonl'
            frozen_contents = first_manifest.read_text(encoding='utf-8')

            frame_id = self._frame(videos[0], 9)
            db.save_annotation(
                self.conn,
                frame_id,
                {
                    'content_family': 'vainglory',
                    'game_context': 'pre_match',
                    'screen_type': 'match_confirm',
                    'game_mode': 'unknown',
                },
                status='complete',
            )
            second = training.export_snapshot(self.conn, 'bp_review')

            self.assertEqual(first['version'], 'bp-classifier-v1')
            self.assertEqual(second['version'], 'bp-classifier-v2')
            self.assertEqual(first['total'], 8)
            self.assertEqual(second['total'], 9)
            self.assertEqual(
                first_manifest.read_text(encoding='utf-8'), frozen_contents
            )
        finally:
            config.EXPORT_DIR = old_export_dir

    def test_partial_export_directory_does_not_block_the_next_version(self):
        old_export_dir = config.EXPORT_DIR
        config.EXPORT_DIR = self.root / 'datasets'
        (config.EXPORT_DIR / 'bp-classifier-v1').mkdir(parents=True)
        try:
            self.assertEqual(
                export.next_version_id(self.conn, 'bp_review'), 'bp-classifier-v2'
            )
        finally:
            config.EXPORT_DIR = old_export_dir


class TestLocalModelPublish(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / 'lab.db')
        db.create_dataset_version(
            self.conn,
            version_id='bp-classifier-v1',
            task_id='bp_review',
            filter_json={},
            counts={'total': 8},
            manifest_path=str(self.root / 'samples.jsonl'),
        )
        self.artifact = self.root / 'run' / 'model.onnx'
        self.artifact.parent.mkdir()
        self.artifact.write_bytes(b'new-model')
        self.artifact.with_suffix('.json').write_text(
            '{"classes": {"0": "bp_3v3"}}', encoding='utf-8'
        )
        db.create_training_run(
            self.conn,
            run_id='bp-train-1',
            task_id='bp_review',
            dataset_version_id='bp-classifier-v1',
            epochs=1,
            config_json={},
            log_path=str(self.root / 'train.log'),
        )
        db.update_training_run(
            self.conn,
            'bp-train-1',
            status='succeeded',
            progress=1.0,
            artifact_path=str(self.artifact),
        )
        self.old_work_dir = config.WORK_DIR
        self.old_models_dir = config.MODELS_DIR
        config.WORK_DIR = self.root / 'work'
        config.MODELS_DIR = self.root / 'models'

    def tearDown(self):
        config.WORK_DIR = self.old_work_dir
        config.MODELS_DIR = self.old_models_dir
        self.conn.close()
        self.tmp.cleanup()

    def test_publish_replaces_only_local_test_model_and_keeps_backup(self):
        config.MODELS_DIR.mkdir(parents=True)
        current = config.MODELS_DIR / 'bp-classifier-current.onnx'
        current.write_bytes(b'old-model')
        current.with_suffix('.json').write_text(
            '{"classes": {"0": "old"}}', encoding='utf-8'
        )

        result = training.publish_local_model(self.conn, 'bp-train-1')

        self.assertEqual(Path(result['path']), current)
        self.assertEqual(current.read_bytes(), b'new-model')
        self.assertTrue(current.with_suffix('.json').is_file())
        backups = list((config.WORK_DIR / 'model-backups').glob('*.onnx'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'old-model')
        metadata_backups = list((config.WORK_DIR / 'model-backups').glob('*.json'))
        self.assertEqual(len(metadata_backups), 1)
        self.assertIn('old', metadata_backups[0].read_text(encoding='utf-8'))
        run = db.get_training_run(self.conn, 'bp-train-1')
        self.assertEqual(run['published_path'], str(current))


if __name__ == '__main__':
    unittest.main()
