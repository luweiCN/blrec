"""训练 run 直接测试、人工验收与不可变模型包。"""

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import config, db, model_testing  # noqa: E402


class TestModelTesting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / 'lab.db')
        self.old_work_dir = config.WORK_DIR
        config.WORK_DIR = self.root / 'work'
        self.image = self.root / 'frame.jpg'
        self.image.write_bytes(b'frame')
        video_id = db.upsert_video(
            self.conn,
            remote_path='/nas/sample.flv',
            streamer='主播',
            room_id='1',
            filename='sample.flv',
            duration_seconds=10,
            size_bytes=1,
        )
        self.frame_id = db.add_frames(
            self.conn,
            video_id,
            [
                {
                    'timestamp_ms': 1000,
                    'width': 1280,
                    'height': 720,
                    'sha256': '1' * 64,
                    'phash': '',
                    'frame_path': str(self.image),
                    'thumb_path': '',
                    'strategy': 'test',
                    'model_source': '',
                    'model_confidence': None,
                }
            ],
        )[0]
        dataset_dir = self.root / 'screen-state-v1'
        dataset_dir.mkdir()
        self.manifest = dataset_dir / 'samples.jsonl'
        self.manifest.write_text(
            json.dumps(
                {
                    'sample_id': f'f{self.frame_id:08d}',
                    'video_id': video_id,
                    'timestamp_ms': 1000,
                    'sha256': '1' * 64,
                    'label': 'in_match',
                    'split': 'test',
                }
            )
            + '\n',
            encoding='utf-8',
        )
        self.snapshot_image = (
            dataset_dir / 'images' / 'test' / 'in_match' / f'f{self.frame_id:08d}.jpg'
        )
        self.snapshot_image.parent.mkdir(parents=True)
        self.snapshot_image.write_bytes(b'immutable-snapshot-frame')
        db.create_dataset_version(
            self.conn,
            version_id='screen-state-v1',
            task_id='screen_state',
            filter_json={},
            counts={'total': 1, 'by_split': {'test': 1}},
            manifest_path=str(self.manifest),
        )
        run_dir = self.root / 'run'
        run_dir.mkdir()
        self.artifact = run_dir / 'model.onnx'
        self.artifact.write_bytes(b'onnx-model')
        self.artifact.with_suffix('.json').write_text(
            json.dumps(
                {
                    'task_id': 'screen_state',
                    'kind': 'classify',
                    'imgsz': 224,
                    'classes': {'0': 'in_match', '1': 'transition'},
                }
            ),
            encoding='utf-8',
        )
        db.create_training_run(
            self.conn,
            run_id='screen-state-run-1',
            task_id='screen_state',
            dataset_version_id='screen-state-v1',
            epochs=1,
            config_json={'imgsz': 224},
            log_path=str(run_dir / 'train.log'),
        )
        db.update_training_run(
            self.conn,
            'screen-state-run-1',
            status='succeeded',
            progress=1.0,
            artifact_path=str(self.artifact),
            metrics={'accuracy': 0.9},
        )

    def tearDown(self):
        config.WORK_DIR = self.old_work_dir
        self.conn.close()
        self.tmp.cleanup()

    def test_test_page_reads_the_run_bound_snapshot(self):
        result = model_testing.list_run_samples(
            self.conn, 'screen-state-run-1', split='test'
        )

        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['frame_id'], self.frame_id)
        self.assertEqual(result['items'][0]['expected'], 'in_match')
        self.assertTrue(result['items'][0]['has_snapshot_image'])

    def test_prediction_reads_immutable_snapshot_instead_of_original_frame(self):
        self.image.unlink()
        with mock.patch.object(
            model_testing.inference,
            'run_artifact',
            return_value={'task': 'classify', 'top1': 'in_match'},
        ) as run_artifact:
            result = model_testing.predict_run_sample(
                self.conn,
                'screen-state-run-1',
                sample_id=f'f{self.frame_id:08d}',
                split='test',
            )

        self.assertEqual(result['top1'], 'in_match')
        self.assertEqual(run_artifact.call_args.args[2], self.snapshot_image.resolve())

    def test_only_passed_run_can_build_immutable_package(self):
        with self.assertRaises(ValueError):
            model_testing.build_model_package(self.conn, ['screen-state-run-1'])
        model_testing.validate_run(
            self.conn, 'screen-state-run-1', status='passed', notes='固定测试集通过'
        )

        package = model_testing.build_model_package(
            self.conn, ['screen-state-run-1'], package_id='vg-test-package'
        )

        package_path = Path(package['path'])
        manifest = json.loads(
            (package_path / 'manifest.json').read_text(encoding='utf-8')
        )
        self.assertEqual(package['status'], 'incomplete')
        self.assertEqual(
            (package_path / 'models/screen_state.onnx').read_bytes(), b'onnx-model'
        )
        self.assertEqual(
            manifest['models']['screen_state']['training_run_id'], 'screen-state-run-1'
        )
        self.assertIn('match_flow', manifest['missing_roles'])
        self.assertEqual(
            manifest['evaluation_gaps']['screen_state'],
            ['固定测试集缺少类别 transition'],
        )
        archive = model_testing.model_package_archive(self.conn, 'vg-test-package')
        with zipfile.ZipFile(archive) as bundle:
            self.assertIn('vg-test-package/manifest.json', bundle.namelist())
        with self.assertRaises(ValueError):
            model_testing.build_model_package(
                self.conn, ['screen-state-run-1'], package_id='vg-test-package'
            )


if __name__ == '__main__':
    unittest.main()
