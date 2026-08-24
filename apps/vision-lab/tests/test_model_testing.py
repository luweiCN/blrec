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

    def test_model_deployment_history_separates_running_and_finished(self):
        package_path = self.root / 'ready-package'
        package_path.mkdir()
        db.create_model_package(
            self.conn,
            package_id='ready-package',
            status='ready',
            path=str(package_path),
            manifest={'package_id': 'ready-package', 'status': 'ready'},
        )

        deployment = db.create_model_deployment(
            self.conn, package_id='ready-package', target='analysis-worker'
        )
        self.assertEqual(deployment['status'], 'queued')
        with self.assertRaisesRegex(ValueError, '正在部署'):
            db.create_model_deployment(
                self.conn, package_id='ready-package', target='analysis-worker'
            )

        running = db.update_model_deployment(
            self.conn,
            deployment_id=deployment['id'],
            status='running',
            previous_package_id='old-package',
        )
        self.assertEqual(running['previous_package_id'], 'old-package')
        finished = db.update_model_deployment(
            self.conn,
            deployment_id=deployment['id'],
            status='succeeded',
            worker_package_id='ready-package',
            detail={'worker_state': 'running'},
        )
        self.assertEqual(finished['status'], 'succeeded')
        self.assertEqual(finished['detail_json']['worker_state'], 'running')
        self.assertIsNotNone(finished['finished_at'])
        self.assertEqual(
            db.list_model_deployments(self.conn, limit=1)[0]['id'], deployment['id']
        )

    def test_only_ready_package_can_be_deployed(self):
        package_path = self.root / 'incomplete-package'
        package_path.mkdir()
        db.create_model_package(
            self.conn,
            package_id='incomplete-package',
            status='incomplete',
            path=str(package_path),
            manifest={'package_id': 'incomplete-package', 'status': 'incomplete'},
        )

        with self.assertRaisesRegex(ValueError, '尚未达到发布条件'):
            db.create_model_deployment(
                self.conn, package_id='incomplete-package', target='analysis-worker'
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

    def test_worker_plan_uses_source_frame_for_manifest_only_snapshot(self):
        self.snapshot_image.unlink()

        plan = model_testing.worker_evaluation_plan(
            self.conn, 'screen-state-run-1', split='test'
        )

        self.assertEqual(plan['task_id'], 'screen_state')
        self.assertEqual(plan['kind'], 'classify')
        self.assertEqual(plan['samples'][0]['frame_id'], self.frame_id)
        self.assertTrue(plan['samples'][0]['has_snapshot_image'])
        self.assertEqual(
            model_testing.run_sample_image_path(
                self.conn,
                'screen-state-run-1',
                sample_id=f'f{self.frame_id:08d}',
                split='test',
            ),
            self.image.resolve(),
        )

    def test_remote_test_image_reference_uses_manifest_frame_id(self):
        reference = model_testing.run_sample_image_reference(
            self.conn,
            'screen-state-run-1',
            sample_id=f'f{self.frame_id:08d}',
            split='test',
        )

        self.assertEqual(reference, {'frame_id': self.frame_id, 'crop': None})

    def test_worker_plan_resolves_managed_manifest_and_model_assets(self):
        remote_manifest = self.root / 'remote-manifest.jsonl'
        remote_manifest.write_bytes(self.manifest.read_bytes())
        remote_artifact = self.root / 'remote-model.onnx'
        remote_artifact.write_bytes(self.artifact.read_bytes())
        remote_metadata = remote_artifact.with_suffix('.json')
        remote_metadata.write_bytes(self.artifact.with_suffix('.json').read_bytes())
        self.manifest.unlink()
        self.artifact.unlink()
        self.artifact.with_suffix('.json').unlink()

        with mock.patch.object(
            model_testing.managed_assets,
            'resolve_dataset_manifest',
            return_value=remote_manifest,
        ) as resolve_manifest, mock.patch.object(
            model_testing.managed_assets,
            'resolve_model_run',
            return_value=(remote_artifact, remote_metadata),
        ) as resolve_model:
            plan = model_testing.worker_evaluation_plan(
                self.conn, 'screen-state-run-1', split='test'
            )

        self.assertEqual(plan['total'], 1)
        resolve_manifest.assert_called_with('screen-state-v1', self.manifest)
        resolve_model.assert_called_with('screen-state-run-1', self.artifact)

    def test_testable_run_exposes_recorded_input_contract(self):
        metadata = json.loads(
            self.artifact.with_suffix('.json').read_text(encoding='utf-8')
        )
        metadata.update(
            {
                'input': {'width': 512, 'height': 288},
                'preprocessing': {
                    'resize': 'aspect_fit_letterbox',
                    'pad_value': 114,
                    'preserve_full_image': True,
                },
            }
        )
        self.artifact.with_suffix('.json').write_text(
            json.dumps(metadata), encoding='utf-8'
        )

        run = model_testing.list_testable_runs(self.conn)[0]

        self.assertEqual(
            run['artifact_metadata']['input'], {'width': 512, 'height': 288}
        )
        self.assertEqual(
            run['artifact_metadata']['preprocessing']['resize'], 'aspect_fit_letterbox'
        )

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

    def test_batch_evaluation_returns_accuracy_confusion_and_errors(self):
        second_id = 'f00000002'
        with self.manifest.open('a', encoding='utf-8') as handle:
            handle.write(
                json.dumps(
                    {
                        'sample_id': second_id,
                        'video_id': 2,
                        'timestamp_ms': 2000,
                        'sha256': '2' * 64,
                        'label': 'transition',
                        'split': 'test',
                    }
                )
                + '\n'
            )
        second_image = (
            self.manifest.parent / 'images' / 'test' / 'transition' / f'{second_id}.jpg'
        )
        second_image.parent.mkdir(parents=True)
        second_image.write_bytes(b'second-snapshot-frame')

        with mock.patch.object(
            model_testing.inference,
            'run_artifact',
            side_effect=[
                {'task': 'classify', 'top1': {'class': 'in_match', 'prob': 0.95}},
                {'task': 'classify', 'top1': {'class': 'in_match', 'prob': 0.8}},
            ],
        ):
            report = model_testing.evaluate_run_samples(
                self.conn, 'screen-state-run-1', split='test'
            )

        self.assertEqual(report['total'], 2)
        self.assertEqual(report['evaluated'], 2)
        self.assertEqual(report['correct'], 1)
        self.assertEqual(report['accuracy'], 0.5)
        self.assertEqual(report['by_label']['in_match']['accuracy'], 1.0)
        self.assertEqual(report['by_label']['transition']['accuracy'], 0.0)
        self.assertEqual(report['confusion']['transition']['in_match'], 1)
        self.assertEqual(report['errors'][0]['sample_id'], second_id)
        self.assertEqual(report['errors'][0]['expected'], 'transition')
        self.assertEqual(report['errors'][0]['predicted'], 'in_match')

    def test_detector_sample_returns_ground_truth_boxes_for_visual_comparison(self):
        dataset_dir = self.root / 'result-detector-v1'
        dataset_dir.mkdir()
        manifest_path = dataset_dir / 'samples.jsonl'
        manifest_path.write_text(
            json.dumps(
                {
                    'sample_id': f'f{self.frame_id:08d}',
                    'video_id': 1,
                    'timestamp_ms': 1000,
                    'sha256': '1' * 64,
                    'label': 'result_panel',
                    'boxes': {'result_panel': {'x': 0.1, 'y': 0.2, 'w': 0.7, 'h': 0.6}},
                    'split': 'test',
                }
            )
            + '\n',
            encoding='utf-8',
        )
        snapshot = dataset_dir / 'images' / 'test' / f'f{self.frame_id:08d}.jpg'
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(b'detector-snapshot')
        db.create_dataset_version(
            self.conn,
            version_id='result-detector-v1',
            task_id='result_detector',
            filter_json={},
            counts={'total': 1},
            manifest_path=str(manifest_path),
        )
        run_dir = self.root / 'result-run'
        run_dir.mkdir()
        artifact = run_dir / 'model.onnx'
        artifact.write_bytes(b'onnx-model')
        artifact.with_suffix('.json').write_text(
            json.dumps(
                {
                    'task_id': 'result_detector',
                    'kind': 'detect',
                    'imgsz': 640,
                    'classes': {'0': 'result_panel'},
                }
            ),
            encoding='utf-8',
        )
        db.create_training_run(
            self.conn,
            run_id='result-detector-run-1',
            task_id='result_detector',
            dataset_version_id='result-detector-v1',
            epochs=1,
            config_json={'imgsz': 640},
            log_path=str(run_dir / 'train.log'),
        )
        db.update_training_run(
            self.conn,
            'result-detector-run-1',
            status='succeeded',
            artifact_path=str(artifact),
        )

        result = model_testing.list_run_samples(
            self.conn, 'result-detector-run-1', split='test'
        )

        self.assertTrue(result['items'][0]['expected']['found'])
        self.assertEqual(
            result['items'][0]['expected']['boxes'][0]['xywh_norm'],
            [0.1, 0.2, 0.7, 0.6],
        )

    def test_detector_batch_report_separates_scoreboard_false_positives(self):
        dataset_dir = self.root / 'result-detector-batch'
        dataset_dir.mkdir()
        manifest_path = dataset_dir / 'samples.jsonl'
        samples = [
            {
                'sample_id': 'positive',
                'video_id': 1,
                'label': 'result_panel',
                'boxes': {'result_panel': {'x': 0.1, 'y': 0.2, 'w': 0.7, 'h': 0.6}},
                'evaluation_groups': ['result_panel'],
                'split': 'test',
            },
            {
                'sample_id': 'scoreboard',
                'video_id': 2,
                'label': 'no_result_panel',
                'evaluation_scenario': 'scoreboard',
                'evaluation_mode': '3v3',
                'evaluation_groups': ['scoreboard', 'scoreboard:3v3'],
                'split': 'test',
            },
        ]
        manifest_path.write_text(
            ''.join(json.dumps(sample) + '\n' for sample in samples), encoding='utf-8'
        )
        image_dir = dataset_dir / 'images' / 'test'
        image_dir.mkdir(parents=True)
        for sample in samples:
            (image_dir / f"{sample['sample_id']}.jpg").write_bytes(b'image')
        db.create_dataset_version(
            self.conn,
            version_id='result-detector-batch',
            task_id='result_detector',
            filter_json={},
            counts={'total': 2},
            manifest_path=str(manifest_path),
        )
        run_dir = self.root / 'result-batch-run'
        run_dir.mkdir()
        artifact = run_dir / 'model.onnx'
        artifact.write_bytes(b'onnx-model')
        artifact.with_suffix('.json').write_text(
            json.dumps(
                {
                    'task_id': 'result_detector',
                    'kind': 'detect',
                    'imgsz': 640,
                    'classes': {'0': 'result_panel'},
                }
            ),
            encoding='utf-8',
        )
        db.create_training_run(
            self.conn,
            run_id='result-detector-batch-run',
            task_id='result_detector',
            dataset_version_id='result-detector-batch',
            epochs=1,
            config_json={'imgsz': 640},
            log_path=str(run_dir / 'train.log'),
        )
        db.update_training_run(
            self.conn,
            'result-detector-batch-run',
            status='succeeded',
            artifact_path=str(artifact),
        )

        detection = {
            'class': 'result_panel',
            'xywh_norm': [0.1, 0.2, 0.7, 0.6],
            'conf': 0.9,
        }
        with mock.patch.object(
            model_testing.inference,
            'run_artifact',
            side_effect=[
                {'task': 'detect', 'found': True, 'detections': [detection]},
                {'task': 'detect', 'found': True, 'detections': [detection]},
            ],
        ):
            report = model_testing.evaluate_run_samples(
                self.conn, 'result-detector-batch-run', split='test'
            )

        self.assertEqual(report['correct'], 1)
        self.assertEqual(report['accuracy'], 0.5)
        self.assertEqual(report['by_scenario']['scoreboard']['accuracy'], 0.0)
        self.assertEqual(report['scoreboard_by_mode']['3v3']['correct'], 0)
        self.assertEqual(report['errors'][0]['reason'], '负样本误报')

    def test_result_detector_reports_missing_scoreboard_test_coverage(self):
        dataset_dir = self.root / 'result-detector-without-scoreboard-test'
        dataset_dir.mkdir()
        manifest_path = dataset_dir / 'samples.jsonl'
        samples = [
            {
                'sample_id': 'positive',
                'video_id': 1,
                'detector_label': 'result_panel',
                'evaluation_groups': ['result_panel'],
                'split': 'test',
            },
            {
                'sample_id': 'negative',
                'video_id': 2,
                'detector_label': 'no_result_panel',
                'evaluation_groups': ['other_negative'],
                'split': 'test',
            },
        ]
        for index, mode in enumerate(('3v3', '5v5', 'aram'), start=3):
            samples.append(
                {
                    'sample_id': f'scoreboard-{mode}',
                    'video_id': index,
                    'detector_label': 'no_result_panel',
                    'evaluation_groups': ['scoreboard', f'scoreboard:{mode}'],
                    'split': 'train',
                }
            )
        manifest_path.write_text(
            ''.join(json.dumps(sample) + '\n' for sample in samples), encoding='utf-8'
        )
        db.create_dataset_version(
            self.conn,
            version_id='result-detector-without-scoreboard-test',
            task_id='result_detector',
            filter_json={},
            counts={'total': len(samples)},
            manifest_path=str(manifest_path),
        )
        run_dir = self.root / 'result-gap-run'
        run_dir.mkdir()
        artifact = run_dir / 'model.onnx'
        artifact.write_bytes(b'onnx-model')
        artifact.with_suffix('.json').write_text(
            json.dumps(
                {
                    'task_id': 'result_detector',
                    'kind': 'detect',
                    'imgsz': 640,
                    'classes': {'0': 'result_panel'},
                }
            ),
            encoding='utf-8',
        )
        db.create_training_run(
            self.conn,
            run_id='result-gap-run',
            task_id='result_detector',
            dataset_version_id='result-detector-without-scoreboard-test',
            epochs=1,
            config_json={'imgsz': 640},
            log_path=str(run_dir / 'train.log'),
        )
        db.update_training_run(
            self.conn, 'result-gap-run', status='succeeded', artifact_path=str(artifact)
        )

        run = next(
            item
            for item in model_testing.list_testable_runs(self.conn)
            if item['id'] == 'result-gap-run'
        )

        self.assertEqual(
            run['evaluation_gaps'],
            [
                '固定测试集没有计分板难例',
                '固定测试集缺少：3V3 计分板',
                '固定测试集缺少：5V5 计分板',
                '固定测试集缺少：大乱斗计分板',
            ],
        )

    def test_result_detector_can_test_current_confirmed_scoreboard_challenge(self):
        db.save_annotation(
            self.conn,
            self.frame_id,
            {
                'content_family': 'vainglory',
                'game_context': 'in_match',
                'screen_type': 'scoreboard',
                'game_mode': '3v3',
            },
            status='complete',
        )
        db.save_training_review(
            self.conn,
            frame_id=self.frame_id,
            match_flow_label='match_flow',
            match_mode_label='3v3',
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            status='confirmed',
        )
        dataset_dir = self.root / 'result-detector-challenge'
        dataset_dir.mkdir()
        manifest_path = dataset_dir / 'samples.jsonl'
        manifest_path.write_text('', encoding='utf-8')
        db.create_dataset_version(
            self.conn,
            version_id='result-detector-challenge',
            task_id='result_detector',
            filter_json={},
            counts={'total': 0},
            manifest_path=str(manifest_path),
        )
        run_dir = self.root / 'result-challenge-run'
        run_dir.mkdir()
        artifact = run_dir / 'model.onnx'
        artifact.write_bytes(b'onnx-model')
        artifact.with_suffix('.json').write_text(
            json.dumps(
                {
                    'task_id': 'result_detector',
                    'kind': 'detect',
                    'imgsz': 640,
                    'classes': {'0': 'result_panel'},
                }
            ),
            encoding='utf-8',
        )
        db.create_training_run(
            self.conn,
            run_id='result-challenge-run',
            task_id='result_detector',
            dataset_version_id='result-detector-challenge',
            epochs=1,
            config_json={'imgsz': 640},
            log_path=str(run_dir / 'train.log'),
        )
        db.update_training_run(
            self.conn,
            'result-challenge-run',
            status='succeeded',
            artifact_path=str(artifact),
        )

        result = model_testing.list_run_samples(
            self.conn, 'result-challenge-run', split='scoreboard_challenge'
        )

        self.assertEqual(result['total'], 1)
        self.assertEqual(result['distribution']['scoreboard'], 1)
        self.assertEqual(result['items'][0]['evaluation_scenario'], 'scoreboard')
        self.assertEqual(result['items'][0]['evaluation_mode'], '3v3')
        self.assertFalse(result['items'][0]['expected']['found'])
        self.assertEqual(
            model_testing.run_sample_image_path(
                self.conn,
                'result-challenge-run',
                sample_id=f'f{self.frame_id:08d}',
                split='scoreboard_challenge',
            ),
            self.image.resolve(),
        )

    def test_post_run_challenge_excludes_seen_and_pre_run_samples(self):
        video_id = int(
            self.conn.execute(
                'SELECT video_id FROM frames WHERE id = ?', (self.frame_id,)
            ).fetchone()['video_id']
        )
        challenge_images = []
        challenge_frame_ids = []
        for index in range(2):
            image = self.root / f'challenge-{index}.jpg'
            image.write_bytes(f'challenge-{index}'.encode())
            challenge_images.append(image)
            challenge_frame_ids.append(
                db.add_frames(
                    self.conn,
                    video_id,
                    [
                        {
                            'timestamp_ms': 2_000 + index * 1_000,
                            'width': 1280,
                            'height': 720,
                            'sha256': str(index + 2) * 64,
                            'phash': '',
                            'frame_path': str(image),
                            'thumb_path': '',
                            'strategy': 'test',
                            'model_source': '',
                            'model_confidence': None,
                        }
                    ],
                )[0]
            )

        dataset_dir = self.root / 'match-flow-challenge'
        dataset_dir.mkdir()
        manifest_path = dataset_dir / 'samples.jsonl'
        manifest_path.write_text(
            json.dumps(
                {
                    'sample_id': f'f{self.frame_id:08d}',
                    'video_id': video_id,
                    'label': 'match_flow',
                    'split': 'train',
                }
            )
            + '\n',
            encoding='utf-8',
        )
        db.create_dataset_version(
            self.conn,
            version_id='match-flow-challenge',
            task_id='match_flow',
            filter_json={},
            counts={'total': 1},
            manifest_path=str(manifest_path),
        )
        run_dir = self.root / 'match-flow-challenge-run'
        run_dir.mkdir()
        artifact = run_dir / 'model.onnx'
        artifact.write_bytes(b'onnx-model')
        artifact.with_suffix('.json').write_text(
            json.dumps(
                {
                    'task_id': 'match_flow',
                    'kind': 'classify',
                    'imgsz': 224,
                    'classes': {'0': 'match_flow', '1': 'not_match_flow'},
                }
            ),
            encoding='utf-8',
        )
        db.create_training_run(
            self.conn,
            run_id='match-flow-challenge-run',
            task_id='match_flow',
            dataset_version_id='match-flow-challenge',
            epochs=1,
            config_json={'imgsz': 224},
            log_path=str(run_dir / 'train.log'),
        )
        db.update_training_run(
            self.conn,
            'match-flow-challenge-run',
            status='succeeded',
            artifact_path=str(artifact),
            finished_at='2020-01-01T00:00:00',
        )
        for frame_id in (self.frame_id, *challenge_frame_ids):
            db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label='match_flow',
                match_mode_label='3v3',
                hero_select_label='not_select',
                result_panel_label='no_result_panel',
                status='confirmed',
            )
        self.conn.execute(
            'UPDATE training_review_items SET reviewed_at = ? WHERE frame_id = ?',
            ('2019-12-31T23:59:59', challenge_frame_ids[0]),
        )
        self.conn.commit()

        result = model_testing.list_run_samples(
            self.conn, 'match-flow-challenge-run', split='post_run_challenge'
        )

        expected_id = f'f{challenge_frame_ids[1]:08d}'
        self.assertEqual(result['total'], 1)
        self.assertFalse(result['is_fixed_snapshot'])
        self.assertEqual(result['items'][0]['sample_id'], expected_id)
        self.assertEqual(result['items'][0]['expected'], 'match_flow')
        self.assertEqual(result['new_video_count'], 0)
        self.assertEqual(
            model_testing.run_sample_image_path(
                self.conn,
                'match-flow-challenge-run',
                sample_id=expected_id,
                split='post_run_challenge',
            ),
            challenge_images[1].resolve(),
        )

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
        self.assertEqual(manifest['schema_version'], 2)
        self.assertEqual(manifest['pipeline_version'], 'timeline-v2')
        self.assertEqual(manifest['runtime']['coarse_interval_ms'], 60_000)
        self.assertEqual(manifest['runtime']['maximum_keyframe_distance_ms'], 5_000)
        self.assertEqual(manifest['runtime']['result_scan_fps'], 4)
        self.assertEqual(manifest['runtime']['thresholds']['result_panel'], 0.55)
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

    def test_result_mode_package_requires_only_runtime_consumed_classes(self):
        manifest = self.root / 'result-mode-samples.jsonl'
        manifest.write_text(
            '\n'.join(
                json.dumps({'split': 'test', 'label': label})
                for label in ('3v3', 'aram')
            ),
            encoding='utf-8',
        )

        gaps = model_testing._evaluation_gaps(
            {
                'run': {'task_id': 'result_mode'},
                'dataset': {'manifest_path': str(manifest)},
                'metadata': {
                    'kind': 'classify',
                    'classes': {'0': '3v3', '1': '5v5', '2': 'aram', '3': 'blitz'},
                },
            }
        )

        self.assertEqual(gaps, [])

    def test_package_uses_the_training_artifacts_preprocessing_contract(self):
        metadata_path = self.artifact.with_suffix('.json')
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        metadata.update(
            {
                'input': {'width': 512, 'height': 288},
                'preprocessing': {
                    'color': 'RGB',
                    'resize': 'aspect_fit_letterbox',
                    'pad_value': 114,
                    'preserve_full_image': True,
                    'scale': '0_to_1',
                    'normalize': 'imagenet',
                    'training_augmentation': {'pad_color': 'random_neutral'},
                },
            }
        )
        metadata_path.write_text(json.dumps(metadata), encoding='utf-8')
        model_testing.validate_run(self.conn, 'screen-state-run-1', status='passed')

        package = model_testing.build_model_package(
            self.conn, ['screen-state-run-1'], package_id='vg-input-contract'
        )
        manifest = package['manifest']['models']['screen_state']

        self.assertEqual(manifest['input']['width'], 512)
        self.assertEqual(manifest['input']['height'], 288)
        self.assertEqual(manifest['input']['resize'], 'aspect_fit_letterbox')
        self.assertTrue(manifest['input']['preserve_full_image'])
        self.assertEqual(
            manifest['training_augmentation']['pad_color'], 'random_neutral'
        )

    def test_avatar_detection_requires_every_box_to_match_once(self):
        expected = [
            {'xywh_norm': [0.10, 0.10, 0.10, 0.10]},
            {'xywh_norm': [0.30, 0.10, 0.10, 0.10]},
        ]
        complete = [
            {'xywh_norm': [0.10, 0.10, 0.10, 0.10]},
            {'xywh_norm': [0.30, 0.10, 0.10, 0.10]},
        ]
        incomplete = [complete[0]]

        matched = model_testing._detection_match_summary(
            expected, complete, iou_threshold=0.5
        )
        missed = model_testing._detection_match_summary(
            expected, incomplete, iou_threshold=0.5
        )

        self.assertEqual(matched['matched_count'], 2)
        self.assertEqual(matched['recall'], 1.0)
        self.assertEqual(missed['matched_count'], 1)
        self.assertEqual(missed['recall'], 0.5)

    def test_hero_avatar_run_lists_all_boxes_and_batch_checks_the_count(self):
        dataset_dir = self.root / 'hero-avatar-detector-v1'
        dataset_dir.mkdir()
        manifest_path = dataset_dir / 'samples.jsonl'
        sample = {
            'sample_id': 'avatar-layout',
            'video_id': 1,
            'label': 'hero_avatar',
            'hero_screen_type': 'gameplay_hud',
            'avatar_boxes': [
                {'x': 0.10, 'y': 0.05, 'w': 0.08, 'h': 0.08},
                {'x': 0.20, 'y': 0.05, 'w': 0.08, 'h': 0.08},
            ],
            'split': 'test',
        }
        manifest_path.write_text(json.dumps(sample) + '\n', encoding='utf-8')
        image_dir = dataset_dir / 'images' / 'test'
        image_dir.mkdir(parents=True)
        (image_dir / 'avatar-layout.jpg').write_bytes(b'avatar-layout')
        db.create_dataset_version(
            self.conn,
            version_id='hero-avatar-detector-v1',
            task_id='hero_avatar_detector',
            filter_json={},
            counts={'total': 1, 'boxes': 2},
            manifest_path=str(manifest_path),
        )
        run_dir = self.root / 'hero-avatar-run'
        run_dir.mkdir()
        artifact = run_dir / 'model.onnx'
        artifact.write_bytes(b'onnx-model')
        artifact.with_suffix('.json').write_text(
            json.dumps(
                {
                    'task_id': 'hero_avatar_detector',
                    'kind': 'detect',
                    'imgsz': 960,
                    'classes': {'0': 'hero_avatar'},
                }
            ),
            encoding='utf-8',
        )
        db.create_training_run(
            self.conn,
            run_id='hero-avatar-run',
            task_id='hero_avatar_detector',
            dataset_version_id='hero-avatar-detector-v1',
            epochs=1,
            config_json={'imgsz': 960},
            log_path=str(run_dir / 'train.log'),
        )
        db.update_training_run(
            self.conn,
            'hero-avatar-run',
            status='succeeded',
            artifact_path=str(artifact),
        )

        listed = model_testing.list_run_samples(
            self.conn, 'hero-avatar-run', split='test'
        )
        self.assertEqual(len(listed['items'][0]['expected']['boxes']), 2)
        self.assertEqual(listed['items'][0]['evaluation_scenario'], 'gameplay_hud')

        with mock.patch.object(
            model_testing.inference,
            'run_artifact',
            return_value={
                'task': 'detect',
                'found': True,
                'detections': [
                    {
                        'class': 'hero_avatar',
                        'xywh_norm': [0.10, 0.05, 0.08, 0.08],
                        'conf': 0.9,
                    }
                ],
            },
        ):
            report = model_testing.evaluate_run_samples(
                self.conn, 'hero-avatar-run', split='test'
            )

        self.assertEqual(report['correct'], 0)
        self.assertEqual(report['errors'][0]['expected_count'], 2)
        self.assertEqual(report['errors'][0]['predicted_count'], 1)
        self.assertEqual(report['errors'][0]['matched_count'], 1)

    def test_hero_identity_run_uses_the_cropped_avatar_snapshot(self):
        dataset_dir = self.root / 'hero-identity-classifier-v1'
        dataset_dir.mkdir()
        manifest_path = dataset_dir / 'samples.jsonl'
        sample = {
            'sample_id': 'hero-adagio-1',
            'video_id': 1,
            'label': 'adagio',
            'split': 'test',
        }
        manifest_path.write_text(json.dumps(sample) + '\n', encoding='utf-8')
        snapshot = dataset_dir / 'images' / 'test' / 'adagio' / 'hero-adagio-1.jpg'
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(b'cropped-avatar')
        db.create_dataset_version(
            self.conn,
            version_id='hero-identity-classifier-v1',
            task_id='hero_identity',
            filter_json={},
            counts={'total': 1, 'classes': 1},
            manifest_path=str(manifest_path),
        )
        run_dir = self.root / 'hero-identity-run'
        run_dir.mkdir()
        artifact = run_dir / 'model.onnx'
        artifact.write_bytes(b'onnx-model')
        artifact.with_suffix('.json').write_text(
            json.dumps(
                {
                    'task_id': 'hero_identity',
                    'kind': 'classify',
                    'imgsz': 160,
                    'input': {'width': 160, 'height': 160},
                    'classes': {'0': 'adagio'},
                }
            ),
            encoding='utf-8',
        )
        db.create_training_run(
            self.conn,
            run_id='hero-identity-run',
            task_id='hero_identity',
            dataset_version_id='hero-identity-classifier-v1',
            epochs=1,
            config_json={'imgsz': 160},
            log_path=str(run_dir / 'train.log'),
        )
        db.update_training_run(
            self.conn,
            'hero-identity-run',
            status='succeeded',
            artifact_path=str(artifact),
        )

        listed = model_testing.list_run_samples(
            self.conn, 'hero-identity-run', split='test'
        )

        self.assertEqual(listed['items'][0]['expected'], 'adagio')
        self.assertTrue(listed['items'][0]['has_snapshot_image'])
        self.assertEqual(
            model_testing.run_sample_image_path(
                self.conn, 'hero-identity-run', sample_id='hero-adagio-1', split='test'
            ),
            snapshot.resolve(),
        )


if __name__ == '__main__':
    unittest.main()
