"""结算页 / 计分板主动学习复核与数据集快照。"""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import config, db, export


class TestClassificationSplit(unittest.TestCase):
    def test_train_and_validation_keep_every_class_without_video_leakage(self):
        samples = [
            {'video_id': video_id, 'label': label}
            for label, video_ids in {
                'result_page': (1, 2),
                'scoreboard': (3, 4),
                'other': (5, 6),
            }.items()
            for video_id in video_ids
        ]

        split = export.split_classification_by_video(
            samples, ('result_page', 'scoreboard', 'other')
        )

        self.assertFalse(set(split['train']) & set(split['val']))
        self.assertFalse(set(split['train']) & set(split['test']))
        for split_name in ('train', 'val'):
            present = {
                sample['label']
                for sample in samples
                if sample['video_id'] in split[split_name]
            }
            self.assertEqual(present, {'result_page', 'scoreboard', 'other'})


class TestKeyScreenReviewStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / 'lab.db')
        video_id = db.upsert_video(
            self.conn,
            remote_path='/nas/key-screen.flv',
            streamer='测试主播',
            room_id='1',
            filename='key-screen.flv',
            duration_seconds=100,
            size_bytes=1024,
        )
        image = self.root / 'frame.jpg'
        image.write_bytes(b'test-image')
        self.frame_id = db.add_frames(
            self.conn,
            video_id,
            [
                {
                    'timestamp_ms': 1_000,
                    'width': 1920,
                    'height': 1080,
                    'sha256': 'f' * 64,
                    'phash': '',
                    'frame_path': str(image),
                    'thumb_path': '',
                    'strategy': 'test',
                    'model_source': '',
                    'model_confidence': None,
                }
            ],
        )[0]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _upsert(self, suggested_label='scoreboard'):
        return db.upsert_key_screen_review_item(
            self.conn,
            frame_id=self.frame_id,
            model_version='multi-v2',
            suggested_label=suggested_label,
            suggestion_confidence=0.8,
            selection_reason='worker 关键画面候选',
            raw_prediction={'stage_class': suggested_label},
        )

    def test_human_confirmation_is_not_overwritten_by_new_prelabel(self):
        self.assertTrue(self._upsert())
        reviewed = db.review_key_screen_item(
            self.conn,
            frame_id=self.frame_id,
            label='result_page',
            visual_condition='occluded',
        )

        self.assertEqual(reviewed['confirmed_label'], 'result_page')
        self.assertEqual(reviewed['visual_condition'], 'occluded')
        self.assertFalse(self._upsert('other'))

        confirmed = db.list_key_screen_review_items(self.conn, status='confirmed')
        self.assertEqual(confirmed[0]['confirmed_label'], 'result_page')
        self.assertEqual(confirmed[0]['suggested_label'], 'other')
        self.assertIsNone(db.get_annotation(self.conn, self.frame_id))

    def test_unreadable_is_confirmed_but_not_trainable(self):
        self._upsert()

        reviewed = db.review_key_screen_item(
            self.conn,
            frame_id=self.frame_id,
            label='scoreboard',
            visual_condition='unreadable',
        )

        self.assertEqual(reviewed['review_status'], 'confirmed')
        self.assertEqual(reviewed['visual_condition'], 'unreadable')

    def test_unknown_label_is_rejected(self):
        self._upsert()

        with self.assertRaises(ValueError):
            db.review_key_screen_item(
                self.conn, frame_id=self.frame_id, label='victory_animation'
            )


class TestKeyScreenExport(unittest.TestCase):
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

    def _frame(self, video_id, suffix, at_ms):
        image = self.root / f'{suffix}.jpg'
        image.write_bytes(f'image-{suffix}'.encode())
        return db.add_frames(
            self.conn,
            video_id,
            [
                {
                    'timestamp_ms': at_ms,
                    'width': 1280,
                    'height': 720,
                    'sha256': hashlib.sha256(suffix.encode()).hexdigest(),
                    'phash': '',
                    'frame_path': str(image),
                    'thumb_path': '',
                    'strategy': 'test',
                    'model_source': '',
                    'model_confidence': None,
                }
            ],
        )[0]

    def test_export_freezes_confirmed_and_existing_human_labels(self):
        first_video = db.upsert_video(
            self.conn,
            remote_path='/nas/a.flv',
            streamer='甲',
            room_id='1',
            filename='a.flv',
            duration_seconds=10,
            size_bytes=1,
        )
        second_video = db.upsert_video(
            self.conn,
            remote_path='/nas/b.flv',
            streamer='乙',
            room_id='2',
            filename='b.flv',
            duration_seconds=10,
            size_bytes=1,
        )
        result_id = self._frame(first_video, 'a', 1_000)
        scoreboard_id = self._frame(second_video, 'b', 2_000)
        unreadable_id = self._frame(second_video, 'c', 3_000)

        db.save_annotation(
            self.conn,
            result_id,
            {
                'content_family': 'vainglory',
                'game_context': 'post_match',
                'screen_type': 'result_page',
                'game_mode': '3v3',
            },
            status='complete',
        )
        for frame_id in (scoreboard_id, unreadable_id):
            db.upsert_key_screen_review_item(
                self.conn,
                frame_id=frame_id,
                model_version='multi-v2',
                suggested_label='scoreboard',
                suggestion_confidence=0.9,
                selection_reason='测试候选',
                raw_prediction={},
            )
        db.review_key_screen_item(self.conn, frame_id=scoreboard_id, label='scoreboard')
        db.review_key_screen_item(
            self.conn,
            frame_id=unreadable_id,
            label='other',
            visual_condition='unreadable',
        )

        result = export.export_key_screen_classifier(self.conn)

        self.assertEqual(result['by_label']['result_page'], 1)
        self.assertEqual(result['by_label']['scoreboard'], 1)
        self.assertEqual(result['excluded_unreadable'], 1)
        samples = [
            json.loads(line)
            for line in (Path(result['dir']) / 'samples.jsonl').read_text().splitlines()
        ]
        self.assertEqual(
            {sample['label'] for sample in samples}, {'result_page', 'scoreboard'}
        )
        for sample in samples:
            copied = (
                Path(result['dir'])
                / 'images'
                / sample['split']
                / sample['label']
                / f"{sample['sample_id']}.jpg"
            )
            self.assertTrue(copied.is_file())

    def test_export_caps_generic_other_frames_but_keeps_target_classes(self):
        first_video = db.upsert_video(
            self.conn,
            remote_path='/nas/c.flv',
            streamer='丙',
            room_id='3',
            filename='c.flv',
            duration_seconds=1000,
            size_bytes=1,
        )
        second_video = db.upsert_video(
            self.conn,
            remote_path='/nas/d.flv',
            streamer='丁',
            room_id='4',
            filename='d.flv',
            duration_seconds=1000,
            size_bytes=1,
        )
        labels = [('result_page', first_video), ('scoreboard', second_video)]
        labels.extend(
            ('gameplay', first_video if index % 2 else second_video)
            for index in range(305)
        )
        for index, (screen_type, video_id) in enumerate(labels):
            frame_id = self._frame(video_id, f'balanced-{index}', (index + 1) * 1_000)
            db.save_annotation(
                self.conn,
                frame_id,
                {
                    'content_family': 'vainglory',
                    'game_context': (
                        'post_match' if screen_type == 'result_page' else 'in_match'
                    ),
                    'screen_type': screen_type,
                    'game_mode': '3v3',
                },
                status='complete',
            )

        result = export.export_key_screen_classifier(self.conn)

        self.assertEqual(result['by_label']['result_page'], 1)
        self.assertEqual(result['by_label']['scoreboard'], 1)
        self.assertEqual(result['by_label']['other'], 300)
        self.assertEqual(result['available_other'], 305)
        self.assertEqual(result['excluded_other_balance'], 5)


if __name__ == '__main__':
    unittest.main()
