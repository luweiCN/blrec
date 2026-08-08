"""3V3 / 大乱斗光栅专项的数据隔离与校验。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import config, db, export


class TestModeGateAnnotations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / 'lab.db')
        self.old_export_dir = config.EXPORT_DIR
        config.EXPORT_DIR = Path(self.tmp.name) / 'datasets'
        self.video_id = db.upsert_video(
            self.conn,
            remote_path='/nas/aram.flv',
            streamer='测试主播',
            room_id='1',
            filename='aram.flv',
            duration_seconds=100,
            size_bytes=1024,
        )
        self.frame_ids = db.add_frames(
            self.conn,
            self.video_id,
            [
                {
                    'timestamp_ms': 1000,
                    'width': 1920,
                    'height': 1080,
                    'sha256': 'a' * 64,
                    'phash': '',
                    'frame_path': '/tmp/a.jpg',
                    'thumb_path': '',
                    'strategy': 'test',
                    'model_source': '',
                    'model_confidence': None,
                },
                {
                    'timestamp_ms': 2000,
                    'width': 1920,
                    'height': 1080,
                    'sha256': 'b' * 64,
                    'phash': '',
                    'frame_path': '/tmp/b.jpg',
                    'thumb_path': '',
                    'strategy': 'test',
                    'model_source': '',
                    'model_confidence': None,
                },
            ],
        )
        db.save_mode_gate_round(
            self.conn,
            round_id='pilot',
            name='光栅试标',
        )
        db.add_mode_gate_round_video(
            self.conn,
            round_id='pilot',
            video_id=self.video_id,
            expected_mode='aram',
            start_ms=1000,
        )

    def tearDown(self):
        config.EXPORT_DIR = self.old_export_dir
        self.conn.close()
        self.tmp.cleanup()

    def test_boxed_evidence_requires_normalized_box(self):
        with self.assertRaises(ValueError):
            db.save_mode_gate_annotation(
                self.conn,
                round_id='pilot',
                frame_id=self.frame_ids[0],
                evidence='blocked_gate',
            )
        with self.assertRaises(ValueError):
            db.save_mode_gate_annotation(
                self.conn,
                round_id='pilot',
                frame_id=self.frame_ids[0],
                evidence='open_entrance',
                x=0.9,
                y=0.1,
                w=0.2,
                h=0.2,
            )

    def test_multiple_boxes_are_saved_for_one_frame(self):
        annotation = db.save_mode_gate_annotation(
            self.conn,
            round_id='pilot',
            frame_id=self.frame_ids[0],
            evidence='blocked_gate',
            boxes=[
                {'x': 0.1, 'y': 0.2, 'w': 0.2, 'h': 0.1},
                {'x': 0.6, 'y': 0.5, 'w': 0.3, 'h': 0.2},
            ],
        )

        self.assertEqual(len(annotation['boxes']), 2)
        self.assertEqual(annotation['boxes'][0]['x'], 0.1)
        self.assertEqual(annotation['boxes'][1]['x'], 0.6)
        stored = db.get_mode_gate_annotation(
            self.conn, round_id='pilot', frame_id=self.frame_ids[0])
        self.assertEqual(stored['boxes'], annotation['boxes'])

    def test_saving_box_list_replaces_previous_boxes(self):
        frame_id = self.frame_ids[0]
        db.save_mode_gate_annotation(
            self.conn,
            round_id='pilot',
            frame_id=frame_id,
            evidence='blocked_gate',
            boxes=[
                {'x': 0.1, 'y': 0.2, 'w': 0.2, 'h': 0.1},
                {'x': 0.6, 'y': 0.5, 'w': 0.3, 'h': 0.2},
            ],
        )
        annotation = db.save_mode_gate_annotation(
            self.conn,
            round_id='pilot',
            frame_id=frame_id,
            evidence='blocked_gate',
            boxes=[{'x': 0.15, 'y': 0.25, 'w': 0.2, 'h': 0.1}],
        )

        self.assertEqual(len(annotation['boxes']), 1)
        self.assertEqual(annotation['boxes'][0]['x'], 0.15)

    def test_legacy_single_box_is_migrated_without_data_loss(self):
        frame_id = self.frame_ids[0]
        self.conn.execute(
            """
            INSERT INTO mode_gate_annotations
                (round_id, frame_id, evidence, x, y, w, h, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ('pilot', frame_id, 'blocked_gate', 0.2, 0.3, 0.4, 0.2,
             '2026-08-08T00:00:00'),
        )
        self.conn.commit()

        db._migrate(self.conn)
        annotation = db.get_mode_gate_annotation(
            self.conn, round_id='pilot', frame_id=frame_id)

        self.assertEqual(len(annotation['boxes']), 1)
        self.assertEqual(
            {key: annotation['boxes'][0][key] for key in ('x', 'y', 'w', 'h')},
            {'x': 0.2, 'y': 0.3, 'w': 0.4, 'h': 0.2},
        )

    def test_specialist_label_does_not_change_general_label(self):
        frame_id = self.frame_ids[0]
        annotation = db.save_mode_gate_annotation(
            self.conn,
            round_id='pilot',
            frame_id=frame_id,
            evidence='blocked_gate',
            x=0.1,
            y=0.2,
            w=0.3,
            h=0.4,
        )
        self.assertEqual(annotation['evidence'], 'blocked_gate')
        self.assertEqual(
            annotation['boxes'],
            [{'id': annotation['boxes'][0]['id'],
              'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.4}],
        )
        self.assertIsNone(db.get_annotation(self.conn, frame_id))
        frame = db.get_frame(self.conn, frame_id)
        self.assertEqual(frame['labeled'], 0)
        self.assertEqual(db.get_boxes(self.conn, frame_id), {})

    def test_no_evidence_has_no_box_and_updates_round_progress(self):
        frame_id = self.frame_ids[1]
        annotation = db.save_mode_gate_annotation(
            self.conn,
            round_id='pilot',
            frame_id=frame_id,
            evidence='no_evidence',
        )
        self.assertIsNone(annotation['x'])
        self.assertEqual(annotation['boxes'], [])
        current_round = db.get_active_mode_gate_round(self.conn)
        self.assertEqual(current_round['annotation_count'], 1)
        video = current_round['videos'][0]
        self.assertEqual(video['no_evidence_count'], 1)
        self.assertEqual(video['last_pts_ms'], 2000)

    def test_frame_must_belong_to_round_video(self):
        other_video = db.upsert_video(
            self.conn,
            remote_path='/nas/other.flv',
            streamer='其他主播',
            room_id='2',
            filename='other.flv',
            duration_seconds=100,
            size_bytes=1024,
        )
        other_frame = db.add_frames(
            self.conn,
            other_video,
            [{
                'timestamp_ms': 1000,
                'width': 1280,
                'height': 720,
                'sha256': 'c' * 64,
                'phash': '',
                'frame_path': '/tmp/c.jpg',
                'thumb_path': '',
                'strategy': 'test',
                'model_source': '',
                'model_confidence': None,
            }],
        )[0]
        with self.assertRaises(KeyError):
            db.save_mode_gate_annotation(
                self.conn,
                round_id='pilot',
                frame_id=other_frame,
                evidence='no_evidence',
            )

    def test_export_uses_blocked_gate_as_positive_and_open_as_negative(self):
        first_path = Path(self.tmp.name) / 'a.jpg'
        second_path = Path(self.tmp.name) / 'b.jpg'
        first_path.write_bytes(b'a-image')
        second_path.write_bytes(b'b-image')
        self.conn.execute(
            'UPDATE frames SET frame_path = ? WHERE id = ?',
            (str(first_path), self.frame_ids[0]),
        )
        self.conn.execute(
            'UPDATE frames SET frame_path = ? WHERE id = ?',
            (str(second_path), self.frame_ids[1]),
        )
        self.conn.commit()
        db.save_mode_gate_annotation(
            self.conn,
            round_id='pilot',
            frame_id=self.frame_ids[0],
            evidence='blocked_gate',
            boxes=[
                {'x': 0.1, 'y': 0.2, 'w': 0.2, 'h': 0.1},
                {'x': 0.6, 'y': 0.5, 'w': 0.3, 'h': 0.2},
            ],
        )
        db.save_mode_gate_annotation(
            self.conn,
            round_id='pilot',
            frame_id=self.frame_ids[1],
            evidence='open_entrance',
            boxes=[{'x': 0.2, 'y': 0.3, 'w': 0.4, 'h': 0.2}],
        )

        result = export.export_mode_gate_detector(self.conn)

        self.assertEqual(result['positive'], 1)
        self.assertEqual(result['negative'], 1)
        self.assertEqual(result['boxes'], 2)
        samples = [
            json.loads(line)
            for line in (Path(result['dir']) / 'samples.jsonl').read_text().splitlines()
        ]
        positive = next(sample for sample in samples
                        if sample['label'] == 'blocked_gate')
        negative = next(sample for sample in samples
                        if sample['label'] == 'open_entrance')
        positive_label = (Path(result['dir']) / 'labels' / positive['split'] /
                          f"{positive['sample_id']}.txt")
        negative_label = (Path(result['dir']) / 'labels' / negative['split'] /
                          f"{negative['sample_id']}.txt")
        self.assertEqual(len(positive_label.read_text().splitlines()), 2)
        self.assertEqual(negative_label.read_text(), '')

if __name__ == '__main__':
    unittest.main()
