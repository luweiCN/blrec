"""训练任务必须绑定不可变数据集快照并保留历史。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import config, db, export, training, training_runner


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

    def test_detector_artifact_metadata_does_not_require_classification_size(self):
        metadata = training_runner._artifact_input_metadata(
            'detect', input_width=0, input_height=0
        )

        self.assertNotIn('input', metadata)
        self.assertEqual(metadata['preprocessing']['resize'], 'letterbox')

    def test_interrupted_run_can_resume_only_with_nonempty_last_checkpoint(self):
        old_work_dir = config.WORK_DIR
        config.WORK_DIR = Path(self.tmp.name) / 'work'
        try:
            db.create_training_run(
                self.conn,
                run_id='bp-train-resume',
                task_id='bp_review',
                dataset_version_id='bp-classifier-v1',
                epochs=60,
                config_json={'imgsz': 224},
                log_path='/tmp/resume.log',
            )
            run = db.get_training_run(self.conn, 'bp-train-resume')
            with self.assertRaisesRegex(ValueError, '已中断'):
                training.interrupted_run_checkpoint(run)

            db.update_training_run(self.conn, 'bp-train-resume', status='interrupted')
            run = db.get_training_run(self.conn, 'bp-train-resume')
            with self.assertRaisesRegex(FileNotFoundError, 'last.pt'):
                training.interrupted_run_checkpoint(run)

            checkpoint = (
                config.WORK_DIR
                / 'training-runs'
                / 'bp-train-resume'
                / 'ultralytics'
                / 'weights'
                / 'last.pt'
            )
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_bytes(b'checkpoint')

            self.assertEqual(training.interrupted_run_checkpoint(run), checkpoint)
        finally:
            config.WORK_DIR = old_work_dir


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
            for item in training.task_summaries(self.conn, include_legacy=True)
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

    def test_task_summary_reports_exact_delta_since_latest_run(self):
        video_id = db.upsert_video(
            self.conn,
            remote_path='/nas/delta.flv',
            streamer='主播',
            room_id='delta',
            filename='delta.flv',
            duration_seconds=10,
            size_bytes=1,
        )
        frame_ids = [self._frame(video_id, index) for index in range(20, 24)]
        labels = ('match_flow', 'not_match_flow', 'match_flow')
        manifest = self.root / 'match-flow-delta.jsonl'
        manifest.write_text(
            ''.join(
                json.dumps(
                    {
                        'sample_id': f'f{frame_id:08d}',
                        'video_id': video_id,
                        'label': label,
                        'split': 'train',
                    }
                )
                + '\n'
                for frame_id, label in zip(frame_ids[:3], labels)
            ),
            encoding='utf-8',
        )
        db.create_dataset_version(
            self.conn,
            version_id='match-flow-delta-v1',
            task_id='match_flow',
            filter_json={},
            counts={'total': 3},
            manifest_path=str(manifest),
        )
        db.create_training_run(
            self.conn,
            run_id='match-flow-delta-run',
            task_id='match_flow',
            dataset_version_id='match-flow-delta-v1',
            epochs=1,
            config_json={},
            log_path=str(self.root / 'match-flow-delta.log'),
        )
        db.update_training_run(
            self.conn,
            'match-flow-delta-run',
            status='succeeded',
            artifact_path=str(self.root / 'match-flow-delta.onnx'),
            finished_at='2020-01-01T00:00:00',
        )
        current_labels = ('match_flow', 'match_flow', None, 'not_match_flow')
        for frame_id, label in zip(frame_ids, current_labels):
            if label is None:
                continue
            db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label=label,
                match_mode_label='3v3' if label == 'match_flow' else None,
                hero_select_label='not_select',
                result_panel_label='no_result_panel',
                status='confirmed',
            )

        summary = next(
            item
            for item in training.task_summaries(self.conn)
            if item['id'] == 'match_flow'
        )

        self.assertEqual(
            summary['dataset_delta'],
            {
                'run_id': 'match-flow-delta-run',
                'dataset_version_id': 'match-flow-delta-v1',
                'baseline_total': 3,
                'current_total': 3,
                'new': 1,
                'removed': 1,
                'changed': 1,
                'net': 0,
                'new_videos': 0,
                'new_by_label': {'not_match_flow': 1},
            },
        )


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
