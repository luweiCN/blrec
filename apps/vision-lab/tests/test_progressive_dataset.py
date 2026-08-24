"""目录重构后的旧数据复用与画面状态累计快照。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import config, db, export, training  # noqa: E402


class TestManagedPathMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_work_dir = config.WORK_DIR
        self.old_frame_dir = config.FRAME_DIR
        self.old_thumb_dir = config.THUMB_DIR
        self.old_export_dir = config.EXPORT_DIR
        self.old_models_dir = config.MODELS_DIR
        config.WORK_DIR = self.root
        config.FRAME_DIR = self.root / 'frames'
        config.THUMB_DIR = self.root / 'thumbs'
        config.EXPORT_DIR = self.root / 'datasets'
        config.MODELS_DIR = self.root / 'models'
        self.conn = db.connect(self.root / 'lab.db')

    def tearDown(self):
        self.conn.close()
        config.WORK_DIR = self.old_work_dir
        config.FRAME_DIR = self.old_frame_dir
        config.THUMB_DIR = self.old_thumb_dir
        config.EXPORT_DIR = self.old_export_dir
        config.MODELS_DIR = self.old_models_dir
        self.tmp.cleanup()

    def test_only_missing_old_paths_are_repaired_by_sha(self):
        video_id = db.upsert_video(
            self.conn,
            remote_path='/nas/sample.flv',
            streamer='主播',
            room_id='1',
            filename='sample.flv',
            duration_seconds=10,
            size_bytes=1,
        )
        sha = 'a' * 64
        frame_id = db.add_frames(
            self.conn,
            video_id,
            [
                {
                    'timestamp_ms': 1000,
                    'width': 10,
                    'height': 10,
                    'sha256': sha,
                    'phash': '',
                    'frame_path': '/old/vision-lab/frames/a.jpg',
                    'thumb_path': '/old/vision-lab/thumbs/a.jpg',
                    'strategy': 'test',
                    'model_source': '',
                    'model_confidence': None,
                }
            ],
        )[0]
        config.FRAME_DIR.mkdir()
        config.THUMB_DIR.mkdir()
        (config.FRAME_DIR / f'{sha}.jpg').write_bytes(b'frame')
        (config.THUMB_DIR / f'{sha}.jpg').write_bytes(b'thumb')
        self.conn.execute(
            "DELETE FROM workspace_migrations "
            "WHERE id LIKE 'managed-workspace-paths-v2-%'"
        )

        repaired = db.repair_managed_paths(self.conn)
        frame = db.get_frame(self.conn, frame_id)

        self.assertEqual(
            repaired,
            {
                'frames': 1,
                'thumbs': 1,
                'datasets': 0,
                'training_runs': 0,
                'model_packages': 0,
            },
        )
        self.assertEqual(frame['frame_path'], str(config.FRAME_DIR / f'{sha}.jpg'))
        self.assertEqual(frame['thumb_path'], str(config.THUMB_DIR / f'{sha}.jpg'))

    def test_dataset_training_and_package_paths_follow_new_workspace(self):
        dataset_dir = config.EXPORT_DIR / 'match-flow-v1'
        dataset_dir.mkdir(parents=True)
        manifest = dataset_dir / 'samples.jsonl'
        manifest.write_text('{}\n', encoding='utf-8')
        db.create_dataset_version(
            self.conn,
            version_id='match-flow-v1',
            task_id='match_flow',
            filter_json={},
            counts={'total': 1},
            manifest_path='/old/data/datasets/match-flow-v1/samples.jsonl',
        )
        run_dir = config.WORK_DIR / 'training-runs' / 'match-flow-run-1'
        run_dir.mkdir(parents=True)
        artifact = run_dir / 'model.onnx'
        log = run_dir / 'train.log'
        artifact.write_bytes(b'model')
        log.write_text('done', encoding='utf-8')
        db.create_training_run(
            self.conn,
            run_id='match-flow-run-1',
            task_id='match_flow',
            dataset_version_id='match-flow-v1',
            epochs=1,
            config_json={},
            log_path='/old/data/training-runs/match-flow-run-1/train.log',
        )
        db.update_training_run(
            self.conn,
            'match-flow-run-1',
            status='succeeded',
            artifact_path='/old/data/training-runs/match-flow-run-1/model.onnx',
        )
        package = config.WORK_DIR / 'model-packages' / 'package-v1.zip'
        package.parent.mkdir(parents=True)
        package.write_bytes(b'zip')
        db.create_model_package(
            self.conn,
            package_id='package-v1',
            status='ready',
            path='/old/data/model-packages/package-v1',
            manifest={'package_id': 'package-v1'},
        )
        self.conn.execute(
            "DELETE FROM workspace_migrations "
            "WHERE id LIKE 'managed-workspace-paths-v2-%'"
        )

        repaired = db.repair_managed_paths(self.conn)
        dataset = self.conn.execute(
            'SELECT manifest_path FROM dataset_versions WHERE id = ?',
            ('match-flow-v1',),
        ).fetchone()
        run = db.get_training_run(self.conn, 'match-flow-run-1')
        model_package = self.conn.execute(
            'SELECT path FROM model_packages WHERE id = ?', ('package-v1',)
        ).fetchone()

        self.assertEqual(repaired['datasets'], 1)
        self.assertEqual(repaired['training_runs'], 1)
        self.assertEqual(repaired['model_packages'], 1)
        self.assertEqual(dataset['manifest_path'], str(manifest))
        self.assertEqual(run['artifact_path'], str(artifact))
        self.assertEqual(run['log_path'], str(log))
        self.assertEqual(model_package['path'], str(package))


class TestScreenStateSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / 'lab.db')
        self.old_export_dir = config.EXPORT_DIR
        config.EXPORT_DIR = self.root / 'datasets'

    def tearDown(self):
        config.EXPORT_DIR = self.old_export_dir
        self.conn.close()
        self.tmp.cleanup()

    def _frame(self, video_id, index):
        path = self.root / f'{index}.jpg'
        path.write_bytes(f'frame-{index}'.encode())
        return db.add_frames(
            self.conn,
            video_id,
            [
                {
                    'timestamp_ms': index * 1000,
                    'width': 1280,
                    'height': 720,
                    'sha256': f'{index:064x}',
                    'phash': '',
                    'frame_path': str(path),
                    'thumb_path': '',
                    'strategy': 'test',
                    'model_source': '',
                    'model_confidence': None,
                }
            ],
        )[0]

    def test_old_general_annotations_become_seven_class_snapshot(self):
        videos = [
            db.upsert_video(
                self.conn,
                remote_path=f'/nas/{number}.flv',
                streamer=str(number),
                room_id=str(number),
                filename=f'{number}.flv',
                duration_seconds=10,
                size_bytes=1,
            )
            for number in (1, 2)
        ]
        labels = [
            ('not_vainglory', None, None),
            ('vainglory', 'out_of_match', 'main_lobby'),
            ('vainglory', 'pre_match', 'matchmaking'),
            ('vainglory', 'in_match', 'gameplay'),
            ('vainglory', 'in_match', 'talent_select'),
            ('vainglory', 'post_match', 'result_page'),
            ('vainglory', 'transition', 'switch_app'),
        ]
        index = 1
        for video_id in videos:
            for content, context, screen_type in labels:
                db.save_annotation(
                    self.conn,
                    self._frame(video_id, index),
                    {
                        'content_family': content,
                        'game_context': context,
                        'screen_type': screen_type,
                        'game_mode': 'unknown',
                    },
                    status='complete',
                )
                index += 1

        summary = next(
            item
            for item in training.task_summaries(self.conn, include_legacy=True)
            if item['id'] == 'screen_state'
        )
        snapshot = training.export_snapshot(self.conn, 'screen_state')
        samples = [
            json.loads(line)
            for line in (Path(snapshot['dir']) / 'samples.jsonl')
            .read_text(encoding='utf-8')
            .splitlines()
        ]

        self.assertTrue(summary['ready'])
        self.assertEqual(snapshot['total'], 14)
        self.assertEqual(
            set(sample['label'] for sample in samples),
            {
                'not_vainglory',
                'out_of_match',
                'pre_match',
                'in_match',
                'talent_select',
                'post_match',
                'transition',
            },
        )
        first = samples[0]
        snapshot_image = (
            Path(snapshot['dir'])
            / 'images'
            / first['split']
            / first['label']
            / f"{first['sample_id']}.jpg"
        )
        source_image = Path(
            self.conn.execute(
                'SELECT frame_path FROM frames WHERE id = ?',
                (int(first['sample_id'][1:]),),
            ).fetchone()[0]
        )

        source_image.unlink()

        self.assertTrue(snapshot_image.is_file())
        self.assertTrue(snapshot_image.read_bytes())


class TestDetectionSplit(unittest.TestCase):
    def test_moves_smallest_sufficient_positive_video_into_training(self):
        samples = []
        samples.extend({'video_id': 1, 'label': 'blocked_gate'} for _ in range(123))
        samples.append({'video_id': 2, 'label': 'blocked_gate'})
        samples.extend({'video_id': 2, 'label': 'open_entrance'} for _ in range(32))
        samples.extend({'video_id': 3, 'label': 'blocked_gate'} for _ in range(44))

        split = export.split_detection_by_video(
            samples, label_field='label', positive_label='blocked_gate'
        )

        self.assertEqual(split['train'], [2, 3])
        self.assertEqual(split['val'], [1])
        self.assertEqual(split['test'], [])

    def test_fixed_test_set_keeps_each_available_scoreboard_mode(self):
        required = {'scoreboard', 'scoreboard:3v3', 'scoreboard:5v5', 'scoreboard:aram'}
        samples = []
        for video_id in range(1, 13):
            samples.append(
                {
                    'video_id': video_id,
                    'label': 'no_result_panel',
                    'evaluation_groups': ['other_negative'],
                }
            )
        for offset, mode in enumerate(('3v3', '5v5', 'aram')):
            for duplicate in range(3):
                samples.append(
                    {
                        'video_id': 20 + offset * 3 + duplicate,
                        'label': 'no_result_panel',
                        'evaluation_groups': ['scoreboard', f'scoreboard:{mode}'],
                    }
                )
        for video_id in range(40, 46):
            samples.append(
                {
                    'video_id': video_id,
                    'label': 'result_panel',
                    'evaluation_groups': ['result_panel'],
                }
            )

        split = export.split_detection_by_video(
            samples,
            label_field='label',
            positive_label='result_panel',
            evaluation_group_field='evaluation_groups',
            required_test_groups=tuple(sorted(required)),
        )

        for split_name in ('train', 'test'):
            present = {
                group
                for sample in samples
                if sample['video_id'] in split[split_name]
                for group in sample['evaluation_groups']
            }
            self.assertTrue(required <= present)
        self.assertFalse(set(split['train']) & set(split['test']))


class TestUnifiedTrainingSnapshots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / 'lab.db')
        self.old_export_dir = config.EXPORT_DIR
        config.EXPORT_DIR = self.root / 'datasets'

    def tearDown(self):
        config.EXPORT_DIR = self.old_export_dir
        self.conn.close()
        self.tmp.cleanup()

    def _frame(self, video_id, index):
        path = self.root / f'unified-{index}.jpg'
        path.write_bytes(f'unified-frame-{index}'.encode())
        return db.add_frames(
            self.conn,
            video_id,
            [
                {
                    'timestamp_ms': index * 1000,
                    'width': 1280,
                    'height': 720,
                    'sha256': f'{index + 1000:064x}',
                    'phash': '',
                    'frame_path': str(path),
                    'thumb_path': '',
                    'strategy': 'test',
                    'model_source': '',
                    'model_confidence': None,
                }
            ],
        )[0]

    def _save(
        self,
        frame_id,
        flow,
        mode,
        select,
        result='no_result_panel',
        panel_render_state='clear',
    ):
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='test',
            source_id=f'frame:{frame_id}',
        )
        if result == 'result_panel':
            db.save_box(self.conn, frame_id, 'result_panel', 0.1, 0.2, 0.8, 0.6)
        db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label=flow,
            match_mode_label=mode,
            hero_select_label=select,
            result_panel_label=result,
            panel_render_state=panel_render_state,
            status='confirmed',
        )

    def test_one_review_record_exports_all_four_new_model_datasets(self):
        index = 1
        for video_number in (1, 2):
            video_id = db.upsert_video(
                self.conn,
                remote_path=f'/nas/unified-{video_number}.flv',
                streamer=str(video_number),
                room_id=str(video_number),
                filename=f'unified-{video_number}.flv',
                duration_seconds=100,
                size_bytes=1,
            )
            for mode in ('3v3', 'aram', '5v5'):
                self._save(
                    self._frame(video_id, index), 'match_flow', mode, 'not_select'
                )
                index += 1
            for mode in ('3v3', 'aram', '5v5', 'blitz'):
                self._save(
                    self._frame(video_id, index),
                    'match_flow',
                    mode,
                    'not_select',
                    'result_panel',
                )
                index += 1
            for select in ('select_3v3', 'select_aram', 'select_5v5'):
                self._save(self._frame(video_id, index), 'not_match_flow', None, select)
                index += 1
            self._save(
                self._frame(video_id, index), 'not_match_flow', None, 'not_select'
            )
            index += 1
            self._save(
                self._frame(video_id, index),
                'match_flow',
                'unreadable',
                'not_select',
                'result_panel',
                'translucent',
            )
            index += 1

        snapshots = {
            task: training.export_snapshot(self.conn, task)
            for task in (
                'match_flow',
                'match_mode',
                'result_mode',
                'hero_select',
                'result_detector',
            )
        }

        self.assertEqual(
            snapshots['match_flow']['by_label'], {'match_flow': 16, 'not_match_flow': 8}
        )
        self.assertEqual(
            set(snapshots['match_mode']['by_label']), {'3v3', 'aram', '5v5'}
        )
        self.assertEqual(
            snapshots['result_mode']['by_label'],
            {'3v3': 2, 'aram': 2, '5v5': 2, 'blitz': 2},
        )
        self.assertEqual(
            set(snapshots['hero_select']['by_label']),
            {'not_select', 'select_3v3', 'select_aram', 'select_5v5'},
        )
        self.assertEqual(snapshots['result_detector']['positive'], 10)
        self.assertEqual(snapshots['result_detector']['negative'], 14)
        detector_samples = [
            json.loads(line)
            for line in (Path(snapshots['result_detector']['dir']) / 'samples.jsonl')
            .read_text(encoding='utf-8')
            .splitlines()
        ]
        self.assertEqual(
            {
                sample['panel_render_state']
                for sample in detector_samples
                if sample['detector_label'] == 'result_panel'
            },
            {'clear', 'translucent'},
        )

    def test_classifier_snapshot_bulk_loads_frame_metadata(self):
        video_id = db.upsert_video(
            self.conn,
            remote_path='/nas/bulk-metadata.flv',
            streamer='bulk',
            room_id='bulk',
            filename='bulk-metadata.flv',
            duration_seconds=100,
            size_bytes=1,
        )
        frame_id = self._frame(video_id, 900)
        self._save(frame_id, 'match_flow', '3v3', 'not_select')

        with mock.patch.object(
            db, 'get_annotation', side_effect=AssertionError('不应逐帧查询 annotation')
        ), mock.patch.object(
            db, 'get_boxes', side_effect=AssertionError('不应逐帧查询 boxes')
        ):
            snapshot = export.export_training_review_classifier(
                self.conn, 'match_flow', materialize=False
            )

        self.assertEqual(snapshot['total'], 1)

    def test_same_result_event_exports_one_representative(self):
        index = 100
        for video_number in (1, 2):
            video_id = db.upsert_video(
                self.conn,
                remote_path=f'/nas/result-{video_number}.flv',
                streamer=str(video_number),
                room_id=str(video_number),
                filename=f'result-{video_number}.flv',
                duration_seconds=100,
                size_bytes=1,
            )
            result_frames = [
                self._frame(video_id, index),
                self._frame(video_id, index + 1),
            ]
            event_id = db.create_event(
                self.conn, video_id, index * 1_000, (index + 1) * 1_000
            )
            db.assign_event(self.conn, result_frames, event_id)
            for frame_id in result_frames:
                self._save(
                    frame_id, 'match_flow', 'unreadable', 'not_select', 'result_panel'
                )
            self._save(
                self._frame(video_id, index + 2), 'not_match_flow', None, 'not_select'
            )
            index += 10

        flow = training.export_snapshot(self.conn, 'match_flow')
        detector = training.export_snapshot(self.conn, 'result_detector')

        self.assertEqual(flow['by_label'], {'match_flow': 2, 'not_match_flow': 2})
        self.assertEqual(detector['positive'], 2)
        self.assertEqual(detector['negative'], 2)

    def test_result_detector_keeps_scoreboards_through_cap_and_split(self):
        index = 1
        for video_number in range(1, 13):
            video_id = db.upsert_video(
                self.conn,
                remote_path=f'/nas/other-{video_number}.flv',
                streamer=str(video_number),
                room_id=str(video_number),
                filename=f'other-{video_number}.flv',
                duration_seconds=100,
                size_bytes=1,
            )
            self._save(
                self._frame(video_id, index), 'not_match_flow', None, 'not_select'
            )
            index += 1

        for mode_index, mode in enumerate(('3v3', '5v5', 'aram')):
            for duplicate in range(3):
                video_number = 20 + mode_index * 3 + duplicate
                video_id = db.upsert_video(
                    self.conn,
                    remote_path=f'/nas/scoreboard-{video_number}.flv',
                    streamer=str(video_number),
                    room_id=str(video_number),
                    filename=f'scoreboard-{video_number}.flv',
                    duration_seconds=100,
                    size_bytes=1,
                )
                frame_id = self._frame(video_id, index)
                db.save_annotation(
                    self.conn,
                    frame_id,
                    {
                        'content_family': 'vainglory',
                        'game_context': 'in_match',
                        'screen_type': 'scoreboard',
                        'game_mode': mode,
                    },
                    status='complete',
                )
                self._save(frame_id, 'match_flow', mode, 'not_select')
                index += 1

        for video_number in range(40, 46):
            video_id = db.upsert_video(
                self.conn,
                remote_path=f'/nas/result-{video_number}.flv',
                streamer=str(video_number),
                room_id=str(video_number),
                filename=f'result-{video_number}.flv',
                duration_seconds=100,
                size_bytes=1,
            )
            self._save(
                self._frame(video_id, index),
                'match_flow',
                'unreadable',
                'not_select',
                'result_panel',
            )
            index += 1

        detector = export.export_result_detector(
            self.conn, max_negatives=9, version='result-detector-scoreboards'
        )
        samples = [
            json.loads(line)
            for line in (Path(detector['dir']) / 'samples.jsonl')
            .read_text(encoding='utf-8')
            .splitlines()
        ]

        self.assertEqual(detector['negative'], 9)
        self.assertEqual(detector['by_evaluation_group']['scoreboard'], 9)
        self.assertEqual(
            set(detector['by_split_evaluation_group']['test']),
            {
                'result_panel',
                'scoreboard',
                'scoreboard:3v3',
                'scoreboard:5v5',
                'scoreboard:aram',
            },
        )
        self.assertFalse(
            {sample['video_id'] for sample in samples if sample['split'] == 'train'}
            & {sample['video_id'] for sample in samples if sample['split'] == 'test'}
        )


if __name__ == '__main__':
    unittest.main()
