"""新模型只预填统一复核建议，不覆盖人工真值。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import db, model_prefill  # noqa: E402


class TestModelPrefill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / 'lab.db')
        video_id = db.upsert_video(
            self.conn,
            remote_path='/nas/sample.flv',
            streamer='主播',
            room_id='1',
            filename='sample.flv',
            duration_seconds=10,
            size_bytes=1,
        )
        self.image = self.root / 'frame.jpg'
        self.image.write_bytes(b'frame')
        self.frame_id = db.add_frames(
            self.conn,
            video_id,
            [
                {
                    'timestamp_ms': 1_000,
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
        db.add_training_review_source(
            self.conn,
            frame_id=self.frame_id,
            source_type='worker',
            source_id='old-worker',
            suggestions={
                'match_flow': {'label': 'not_match_flow', 'confidence': 0.999}
            },
        )
        for task_id, kind in (
            ('match_flow', 'classify'),
            ('hero_select', 'classify'),
            ('match_mode', 'classify'),
            ('result_detector', 'detect'),
            ('hero_avatar_detector', 'detect'),
            ('hero_identity', 'classify'),
            ('player_position', 'classify'),
        ):
            manifest = self.root / f'{task_id}.jsonl'
            manifest.write_text('', encoding='utf-8')
            dataset_id = f'{task_id}-v1'
            db.create_dataset_version(
                self.conn,
                version_id=dataset_id,
                task_id=task_id,
                filter_json={},
                counts={'total': 0},
                manifest_path=str(manifest),
            )
            artifact = self.root / task_id / 'model.onnx'
            artifact.parent.mkdir()
            artifact.write_bytes(b'onnx')
            artifact.with_suffix('.json').write_text(
                json.dumps(
                    {
                        'task_id': task_id,
                        'kind': kind,
                        'imgsz': 224 if kind == 'classify' else 640,
                        'classes': {},
                    }
                ),
                encoding='utf-8',
            )
            run_id = f'{task_id}-run'
            db.create_training_run(
                self.conn,
                run_id=run_id,
                task_id=task_id,
                dataset_version_id=dataset_id,
                epochs=1,
                config_json={},
                log_path=str(artifact.parent / 'train.log'),
            )
            db.update_training_run(
                self.conn,
                run_id,
                status='succeeded',
                artifact_path=str(artifact),
                finished_at='2026-08-11T00:00:00',
            )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_prefill_uses_latest_runs_and_keeps_human_fields_empty(self):
        def prediction(_artifact, metadata, _image, conf_thr=0.25):
            task_id = metadata['task_id']
            if task_id == 'result_detector':
                return {
                    'task': 'detect',
                    'found': True,
                    'detections': [
                        {
                            'class': 'result_panel',
                            'conf': 0.91,
                            'xywh_norm': [0.1, 0.2, 0.7, 0.6],
                        }
                    ],
                    'raw_top_conf': 0.91,
                }
            values = {
                'match_flow': ('match_flow', 0.98),
                'hero_select': ('not_select', 0.97),
                'match_mode': ('3v3', 0.88),
            }
            label, probability = values[task_id]
            return {
                'task': 'classify',
                'top1': {'class': label, 'prob': probability},
                'top5': [{'class': label, 'prob': probability}],
            }

        with mock.patch.object(
            model_prefill.inference, 'run_artifact', side_effect=prediction
        ) as run_artifact:
            result = model_prefill.prefill_training_review_item(
                self.conn, self.frame_id
            )
            cached = model_prefill.prefill_training_review_item(
                self.conn, self.frame_id
            )

        self.assertTrue(result['applied'])
        self.assertTrue(cached['cached'])
        self.assertEqual(run_artifact.call_count, 4)
        item = db.get_training_review_item(self.conn, self.frame_id)
        self.assertEqual(item['suggestions']['match_flow']['label'], 'match_flow')
        self.assertEqual(item['suggestions']['hero_select']['label'], 'not_select')
        self.assertEqual(item['suggestions']['match_mode']['label'], '3v3')
        self.assertEqual(item['suggestions']['result_panel']['label'], 'result_panel')
        self.assertEqual(item['review_status'], 'pending')
        self.assertIsNone(item['match_flow_label'])
        source = next(
            value
            for value in item['sources']
            if value['source_type'] == 'new_model_prefill'
        )
        self.assertEqual(
            source['metadata']['suggested_boxes'][0]['type'], 'result_panel'
        )
        self.assertEqual(len(source['metadata']['model_runs']), 4)

    def test_hero_prefill_orders_detector_boxes_and_classifies_each_crop(self):
        Image.new('RGB', (1280, 720), (20, 30, 40)).save(self.image)
        boxes = []
        for center_x in (0.45, 0.55):
            for center_y in (0.3, 0.5, 0.7):
                boxes.append(
                    {
                        'class': 'hero_avatar',
                        'conf': 0.9,
                        'xywh_norm': [center_x - 0.025, center_y - 0.045, 0.05, 0.09],
                    }
                )
        heroes = iter(
            ('adagio', 'alpha', 'ardan', 'baron', 'blackfeather', 'catherine')
        )

        def prediction(_artifact, metadata, _image, conf_thr=0.25):
            task_id = metadata['task_id']
            if task_id == 'hero_avatar_detector':
                return {
                    'task': 'detect',
                    'found': True,
                    'detections': list(reversed(boxes)),
                }
            if task_id == 'hero_identity':
                label = next(heroes)
                return {'task': 'classify', 'top1': {'class': label, 'prob': 0.8}}
            if task_id == 'player_position':
                return {'task': 'classify', 'top1': {'class': 'right2', 'prob': 0.92}}
            raise AssertionError(task_id)

        with mock.patch.object(
            model_prefill.inference, 'run_artifact', side_effect=prediction
        ):
            result = model_prefill.prefill_hero_lineup(
                self.conn, self.image, screen_type='scoreboard', team_size=3
            )

        self.assertTrue(result['complete'])
        self.assertEqual(
            [(slot['side'], slot['slot']) for slot in result['slots']],
            [
                ('left', 1),
                ('left', 2),
                ('left', 3),
                ('right', 1),
                ('right', 2),
                ('right', 3),
            ],
        )
        self.assertEqual(
            [slot['suggested_label'] for slot in result['slots']],
            ['adagio', 'alpha', 'ardan', 'baron', 'blackfeather', 'catherine'],
        )
        self.assertEqual(
            result['player_suggestion'],
            {'side': 'right', 'slot': 2, 'confidence': 0.92},
        )


if __name__ == '__main__':
    unittest.main()
