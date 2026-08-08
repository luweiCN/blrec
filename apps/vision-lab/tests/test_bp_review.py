"""BP 主动学习候选选择与人工复核。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import bp_review, config, db, export


def _observation(frame_id, video_id, at_ms, *, stage='gameplay',
                 stage_conf=0.9, pre_match=0.01, mode='3v3',
                 mode_conf=0.9, mode_margin=0.8):
    return {
        'frame_id': frame_id,
        'video_id': video_id,
        'timestamp_ms': at_ms,
        'frame_path': f'/tmp/{frame_id}.jpg',
        'stage_class': stage,
        'stage_confidence': stage_conf,
        'pre_match_confidence': pre_match,
        'mode_class': mode,
        'mode_confidence': mode_conf,
        'mode_margin': mode_margin,
        'raw_prediction': {'task': 'multi'},
    }


class TestBpCandidateSelection(unittest.TestCase):
    def test_episode_is_reduced_to_representative_frames(self):
        observations = [
            _observation(
                index, 1, index * 5_000, stage='pre_match',
                pre_match=0.7 + index * 0.01, mode_margin=0.7 - index * 0.05,
            )
            for index in range(1, 8)
        ]

        selected = bp_review.select_candidates(observations, maximum=20)

        self.assertLessEqual(len(selected), 3)
        self.assertTrue(all(item['suggested_label'] == 'bp_3v3'
                            for item in selected))
        self.assertTrue(all('选英雄' in item['selection_reason']
                            for item in selected))

    def test_gameplay_entry_collects_previous_transition_frames(self):
        observations = [
            _observation(1, 1, 1_000, stage='out_of_match'),
            _observation(2, 1, 6_000, stage='transition', mode='aram'),
            _observation(3, 1, 11_000, stage='gameplay'),
        ]

        selected = bp_review.select_candidates(observations, maximum=20)

        ids = {item['frame_id'] for item in selected}
        self.assertEqual(ids, {1, 2})
        self.assertTrue(all('进入游戏前' in item['selection_reason']
                            for item in selected))

    def test_balanced_rows_cover_each_video(self):
        rows = [
            {'frame_id': video * 100 + index, 'video_id': video,
             'timestamp_ms': index * 1_000}
            for video in (1, 2, 3)
            for index in range(20)
        ]

        selected = bp_review.balanced_frame_rows(rows, maximum=12)

        counts = {video: 0 for video in (1, 2, 3)}
        for item in selected:
            counts[item['video_id']] += 1
        self.assertEqual(counts, {1: 4, 2: 4, 3: 4})


class TestBpReviewStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / 'lab.db')
        video_id = db.upsert_video(
            self.conn,
            remote_path='/nas/bp.flv',
            streamer='测试主播',
            room_id='1',
            filename='bp.flv',
            duration_seconds=100,
            size_bytes=1024,
        )
        self.frame_id = db.add_frames(
            self.conn,
            video_id,
            [{
                'timestamp_ms': 1_000,
                'width': 1920,
                'height': 1080,
                'sha256': 'd' * 64,
                'phash': '',
                'frame_path': '/tmp/d.jpg',
                'thumb_path': '',
                'strategy': 'test',
                'model_source': '',
                'model_confidence': None,
            }],
        )[0]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _upsert(self, suggested='bp_3v3'):
        return db.upsert_bp_review_item(
            self.conn,
            frame_id=self.frame_id,
            model_version='multi-v2',
            suggested_label=suggested,
            suggestion_confidence=0.8,
            stage_class='pre_match',
            stage_confidence=0.9,
            pre_match_confidence=0.9,
            mode_class='3v3',
            mode_confidence=0.8,
            mode_margin=0.7,
            selection_reason='测试候选',
            priority=100,
            raw_prediction={'task': 'multi'},
        )

    def test_review_is_isolated_from_general_annotation(self):
        self.assertTrue(self._upsert())

        reviewed = db.review_bp_item(
            self.conn, frame_id=self.frame_id, label='bp_aram',
            visual_condition='occluded')

        self.assertEqual(reviewed['review_status'], 'confirmed')
        self.assertEqual(reviewed['confirmed_label'], 'bp_aram')
        self.assertEqual(reviewed['visual_condition'], 'occluded')
        self.assertIsNone(db.get_annotation(self.conn, self.frame_id))
        self.assertEqual(db.get_frame(self.conn, self.frame_id)['labeled'], 0)

    def test_rerun_does_not_return_confirmed_item_to_pending(self):
        self._upsert()
        db.review_bp_item(self.conn, frame_id=self.frame_id, label='not_bp')

        self.assertFalse(self._upsert(suggested='bp_5v5'))

        self.assertEqual(
            db.list_bp_review_items(self.conn, status='pending'), [])
        confirmed = db.list_bp_review_items(
            self.conn, status='confirmed')
        self.assertEqual(confirmed[0]['confirmed_label'], 'not_bp')
        self.assertEqual(confirmed[0]['suggested_label'], 'bp_5v5')

    def test_skip_is_not_a_negative_label(self):
        self._upsert()

        reviewed = db.review_bp_item(
            self.conn, frame_id=self.frame_id, label=None)

        self.assertEqual(reviewed['review_status'], 'skipped')
        self.assertIsNone(reviewed['confirmed_label'])
        self.assertEqual(reviewed['visual_condition'], 'clear')

    def test_unknown_visual_condition_is_rejected(self):
        self._upsert()

        with self.assertRaises(ValueError):
            db.review_bp_item(
                self.conn, frame_id=self.frame_id, label='bp_3v3',
                visual_condition='covered_somehow')


class TestBpReviewExport(unittest.TestCase):
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

    def _add_frame(self, name, video_id):
        path = self.root / f'{name}.jpg'
        path.write_bytes(b'test-image')
        return db.add_frames(
            self.conn,
            video_id,
            [{
                'timestamp_ms': ord(name) * 1_000,
                'width': 1920,
                'height': 1080,
                'sha256': name * 64,
                'phash': '',
                'frame_path': str(path),
                'thumb_path': '',
                'strategy': 'test',
                'model_source': '',
                'model_confidence': None,
            }],
        )[0]

    def test_export_uses_existing_and_confirmed_labels_only(self):
        first_video = db.upsert_video(
            self.conn, remote_path='/nas/first.flv', streamer='甲',
            room_id='1', filename='first.flv', duration_seconds=10,
            size_bytes=1,
        )
        second_video = db.upsert_video(
            self.conn, remote_path='/nas/second.flv', streamer='乙',
            room_id='2', filename='second.flv', duration_seconds=10,
            size_bytes=1,
        )
        existing_id = self._add_frame('a', first_video)
        reviewed_id = self._add_frame('b', second_video)
        pending_id = self._add_frame('c', second_video)
        occluded_id = self._add_frame('d', second_video)
        unreadable_id = self._add_frame('e', second_video)
        match_confirm_id = self._add_frame('f', first_video)
        db.save_annotation(
            self.conn, existing_id,
            {
                'content_family': 'vainglory',
                'game_context': 'pre_match',
                'screen_type': 'hero_select_bp',
                'game_mode': '3v3',
            },
            status='complete',
        )
        db.save_annotation(
            self.conn, match_confirm_id,
            {
                'content_family': 'vainglory',
                'game_context': 'pre_match',
                'screen_type': 'match_confirm',
                'game_mode': 'unknown',
            },
            status='complete',
        )
        for frame_id in (reviewed_id, pending_id, occluded_id, unreadable_id):
            db.upsert_bp_review_item(
                self.conn, frame_id=frame_id, model_version='multi-v2',
                suggested_label='bp_5v5', suggestion_confidence=0.99,
                stage_class='pre_match', stage_confidence=1.0,
                pre_match_confidence=1.0, mode_class='5v5',
                mode_confidence=0.99, mode_margin=0.98,
                selection_reason='测试候选', priority=100,
                raw_prediction={'task': 'multi'},
            )
        db.review_bp_item(
            self.conn, frame_id=reviewed_id, label='not_bp')
        db.review_bp_item(
            self.conn, frame_id=occluded_id, label='bp_aram',
            visual_condition='occluded')
        db.review_bp_item(
            self.conn, frame_id=unreadable_id, label='bp_5v5',
            visual_condition='unreadable')

        result = export.export_bp_classifier(self.conn)

        self.assertEqual(result['by_label']['bp_3v3'], 1)
        self.assertEqual(result['by_label']['bp_aram'], 1)
        self.assertEqual(result['by_label']['not_bp'], 2)
        self.assertEqual(result['total'], 4)
        self.assertEqual(result['excluded_unreadable'], 1)
        lines = [json.loads(line) for line in
                 (Path(result['dir']) / 'samples.jsonl').read_text().splitlines()]
        self.assertEqual(
            {line['label_source'] for line in lines},
            {'existing_human_annotation', 'bp_review_confirmed'},
        )
        self.assertNotIn(
            f'f{pending_id:08d}', {line['sample_id'] for line in lines})
        self.assertNotIn(
            f'f{unreadable_id:08d}', {line['sample_id'] for line in lines})
        occluded = next(
            line for line in lines if line['sample_id'] == f'f{occluded_id:08d}')
        self.assertEqual(occluded['visual_condition'], 'occluded')
        video_splits = {}
        for line in lines:
            video_splits.setdefault(line['video_id'], set()).add(line['split'])
        self.assertTrue(all(len(splits) == 1
                            for splits in video_splits.values()))


if __name__ == '__main__':
    unittest.main()
