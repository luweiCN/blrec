"""新模型共用的一图多标签复核流程。"""

import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import (  # noqa: E402
    config,
    db,
    result_archive,
    training_review,
    worker_candidates,
)


class FakeNas:
    def __init__(self, image: bytes):
        self.image = image
        self.downloads = 0

    def read_training_candidate(self, _relative_path: str) -> bytes:
        self.downloads += 1
        return self.image


class ReviewNas(FakeNas):
    def __init__(self, image: bytes):
        super().__init__(image)
        self.reviews = []

    def write_training_candidate_review(self, image_path, review):
        self.reviews.append((image_path, review))


class TrainingReviewTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_frame_dir = config.FRAME_DIR
        self.old_thumb_dir = config.THUMB_DIR
        config.FRAME_DIR = self.root / 'frames'
        config.THUMB_DIR = self.root / 'thumbs'
        self.conn = db.connect(self.root / 'lab.db')
        self.video_id = db.upsert_video(
            self.conn,
            remote_path='/nas/sample.flv',
            streamer='测试主播',
            room_id='1',
            filename='sample.flv',
            duration_seconds=100,
            size_bytes=1,
        )

    def tearDown(self):
        self.conn.close()
        config.FRAME_DIR = self.old_frame_dir
        config.THUMB_DIR = self.old_thumb_dir
        self.tmp.cleanup()

    def frame(self, index: int) -> int:
        path = self.root / f'{index}.jpg'
        path.write_bytes(f'frame-{index}'.encode())
        return db.add_frames(
            self.conn,
            self.video_id,
            [
                {
                    'timestamp_ms': index * 1_000,
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


class TestLegacyMigration(TrainingReviewTestCase):
    def test_old_human_labels_are_reused_without_deleting_old_boxes(self):
        gameplay = self.frame(1)
        shop = self.frame(2)
        select = self.frame(3)
        result = self.frame(4)
        lobby = self.frame(5)
        values = [
            (gameplay, 'in_match', 'gameplay', 'aram'),
            (shop, 'in_match', 'ingame_shop', 'aram'),
            (select, 'pre_match', 'hero_select_bp', '5v5'),
            (result, 'post_match', 'result_page', '3v3'),
            (lobby, 'out_of_match', 'main_lobby', 'unknown'),
        ]
        for frame_id, context, screen_type, mode in values:
            db.save_annotation(
                self.conn,
                frame_id,
                {
                    'content_family': 'vainglory',
                    'game_context': context,
                    'screen_type': screen_type,
                    'game_mode': mode,
                },
                status='complete',
            )
        db.save_box(self.conn, shop, 'shop_panel', 0.1, 0.1, 0.8, 0.8)
        db.save_box(self.conn, result, 'result_panel', 0.1, 0.2, 0.8, 0.6)

        counts = training_review.migrate_legacy_training_reviews(self.conn)

        self.assertEqual(counts['legacy_annotations'], 5)
        rows = {
            item['frame_id']: item
            for item in db.list_training_review_items(self.conn, status='all')
        }
        self.assertEqual(rows[gameplay]['match_flow_label'], 'match_flow')
        self.assertEqual(rows[gameplay]['match_mode_label'], 'aram')
        self.assertEqual(rows[shop]['match_flow_label'], 'match_flow')
        self.assertEqual(rows[shop]['match_mode_label'], 'unreadable')
        self.assertEqual(rows[select]['match_flow_label'], 'not_match_flow')
        self.assertEqual(rows[select]['hero_select_label'], 'select_5v5')
        self.assertEqual(rows[result]['result_panel_label'], 'result_panel')
        self.assertEqual(rows[lobby]['hero_select_label'], 'not_select')
        self.assertEqual(len(db.get_boxes(self.conn, shop)), 1)
        self.assertEqual(len(db.get_boxes(self.conn, result)), 1)

    def test_gate_boxes_become_mode_evidence_but_remain_available(self):
        frame_id = self.frame(1)
        db.save_mode_gate_round(
            self.conn, round_id='round-1', name='旧光栅标注', active=True
        )
        db.add_mode_gate_round_video(
            self.conn,
            round_id='round-1',
            video_id=self.video_id,
            expected_mode='aram',
        )
        db.save_mode_gate_annotation(
            self.conn,
            round_id='round-1',
            frame_id=frame_id,
            evidence='blocked_gate',
            boxes=[{'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.1}],
        )

        training_review.migrate_legacy_training_reviews(self.conn)

        item = db.get_training_review_item(self.conn, frame_id)
        self.assertEqual(item['match_flow_label'], 'match_flow')
        self.assertEqual(item['match_mode_label'], 'aram')
        gate = db.get_mode_gate_annotation(
            self.conn, round_id='round-1', frame_id=frame_id
        )
        self.assertEqual(len(gate['boxes']), 1)


class TestTrainingReviewStorage(TrainingReviewTestCase):
    def test_unreviewed_and_explicit_negative_are_distinct(self):
        frame_id = self.frame(1)
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id='part-1:1000:abc',
            suggestions={
                'result_panel': {'label': 'no_result_panel', 'confidence': 0.8}
            },
        )

        pending = db.get_training_review_item(self.conn, frame_id)
        self.assertIsNone(pending['result_panel_label'])
        self.assertEqual(
            pending['suggestions']['result_panel']['label'], 'no_result_panel'
        )

        reviewed = db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='not_match_flow',
            match_mode_label=None,
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            hero_layout_label='none',
            status='confirmed',
        )

        self.assertEqual(reviewed['result_panel_label'], 'no_result_panel')
        self.assertEqual(reviewed['hero_layout_label'], 'none')
        self.assertEqual(reviewed['review_status'], 'confirmed')

    def test_hero_layout_label_rejects_unknown_screen_type(self):
        frame_id = self.frame(1)

        with self.assertRaisesRegex(ValueError, '英雄头像画面类型无效'):
            db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label='not_match_flow',
                match_mode_label=None,
                hero_select_label='not_select',
                result_panel_label='no_result_panel',
                hero_layout_label='shop',
                status='confirmed',
            )

    def test_result_positive_requires_one_result_box(self):
        frame_id = self.frame(1)

        with self.assertRaises(ValueError):
            db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label='match_flow',
                match_mode_label='unreadable',
                hero_select_label='not_select',
                result_panel_label='result_panel',
                status='confirmed',
            )

        db.save_box(self.conn, frame_id, 'result_panel', 0.1, 0.2, 0.8, 0.6)
        reviewed = db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='match_flow',
            match_mode_label='unreadable',
            hero_select_label='not_select',
            result_panel_label='result_panel',
            status='confirmed',
        )
        self.assertEqual(reviewed['result_panel_label'], 'result_panel')

    def test_hero_select_and_result_panel_cannot_both_be_positive(self):
        frame_id = self.frame(1)
        db.save_box(self.conn, frame_id, 'result_panel', 0.1, 0.2, 0.8, 0.6)

        with self.assertRaisesRegex(ValueError, '英雄选择.*结算面板'):
            db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label='not_match_flow',
                match_mode_label=None,
                hero_select_label='select_3v3',
                result_panel_label='result_panel',
                status='confirmed',
            )

    def test_result_panel_must_belong_to_match_flow(self):
        frame_id = self.frame(1)
        db.save_box(self.conn, frame_id, 'result_panel', 0.1, 0.2, 0.8, 0.6)

        with self.assertRaisesRegex(ValueError, '结算面板必须属于对局流程'):
            db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label='not_match_flow',
                match_mode_label=None,
                hero_select_label='not_select',
                result_panel_label='result_panel',
                status='confirmed',
            )

    def test_confirmed_result_without_player_returns_to_review_queue(self):
        result_frame = self.frame(1)
        pending_frame = self.frame(2)
        db.save_box(
            self.conn, result_frame, 'result_panel', 0.1, 0.2, 0.8, 0.6
        )
        db.save_training_review(
            self.conn,
            frame_id=result_frame,
            match_flow_label='match_flow',
            match_mode_label='unreadable',
            hero_select_label='not_select',
            result_panel_label='result_panel',
            status='confirmed',
        )
        db.add_training_review_source(
            self.conn,
            frame_id=pending_frame,
            source_type='worker',
            source_id='part-1:2000:pending',
        )

        missing = db.list_training_review_items(
            self.conn, status='missing_player'
        )
        needs_review = db.list_training_review_items(
            self.conn, status='needs_review'
        )

        self.assertEqual([item['frame_id'] for item in missing], [result_frame])
        self.assertTrue(missing[0]['needs_player_hero_review'])
        self.assertEqual(
            [item['frame_id'] for item in needs_review],
            [result_frame, pending_frame],
        )
        self.assertEqual(db.training_review_stats(self.conn)[
            'missing_player_hero'
        ], 1)

        slots = [
            {
                'side': side,
                'slot': slot,
                'crop': {
                    'x': 0.1 + (0.1 if side == 'right' else 0) + slot * 0.05,
                    'y': 0.1 + slot * 0.1,
                    'w': 0.04,
                    'h': 0.07,
                },
            }
            for side in ('left', 'right')
            for slot in range(1, 4)
        ]
        db.replace_training_review_hero_layout(
            self.conn,
            frame_id=result_frame,
            screen_type='result_page',
            team_size=3,
            method='manual-circle-v1',
            slots=slots,
        )
        db.save_training_review_hero_lineup(
            self.conn,
            frame_id=result_frame,
            labels=[
                {
                    'side': slot['side'],
                    'slot': slot['slot'],
                    'hero_label': 'Adagio',
                }
                for slot in slots
            ],
            allowed_labels={'Adagio'},
            player_side='right',
            player_slot=2,
        )
        db.save_training_review(
            self.conn,
            frame_id=result_frame,
            match_flow_label='match_flow',
            match_mode_label='unreadable',
            hero_select_label='not_select',
            result_panel_label='result_panel',
            hero_layout_label='result_page',
            status='confirmed',
        )

        self.assertEqual(
            db.list_training_review_items(self.conn, status='missing_player'), []
        )
        reviewed = db.get_training_review_item(self.conn, result_frame)
        self.assertFalse(reviewed['needs_player_hero_review'])
        self.assertEqual(db.training_review_stats(self.conn)[
            'missing_player_hero'
        ], 0)

    def test_confirmed_scoreboard_without_player_returns_to_review_queue(self):
        frame_id = self.frame(3)
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id='part-1:3000:scoreboard',
        )
        slots = [
            {
                'side': side,
                'slot': slot,
                'crop': {
                    'x': 0.1 + (0.1 if side == 'right' else 0) + slot * 0.05,
                    'y': 0.1 + slot * 0.1,
                    'w': 0.04,
                    'h': 0.07,
                },
            }
            for side in ('left', 'right')
            for slot in range(1, 4)
        ]
        labels = [
            {
                'side': slot['side'],
                'slot': slot['slot'],
                'hero_label': 'Adagio',
            }
            for slot in slots
        ]
        db.replace_training_review_hero_layout(
            self.conn,
            frame_id=frame_id,
            screen_type='scoreboard',
            team_size=3,
            method='manual-circle-v1',
            slots=slots,
        )
        db.save_training_review_hero_lineup(
            self.conn,
            frame_id=frame_id,
            labels=labels,
            allowed_labels={'Adagio'},
        )
        db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='match_flow',
            match_mode_label='3v3',
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            hero_layout_label='scoreboard',
            status='confirmed',
        )

        missing = db.list_training_review_items(
            self.conn, status='missing_player'
        )
        self.assertEqual([item['frame_id'] for item in missing], [frame_id])

        db.save_training_review_hero_lineup(
            self.conn,
            frame_id=frame_id,
            labels=labels,
            allowed_labels={'Adagio'},
            player_side='left',
            player_slot=1,
        )
        self.assertEqual(
            db.list_training_review_items(self.conn, status='missing_player'), []
        )


class TestUnifiedWorkerCandidate(TrainingReviewTestCase):
    def unified_item(self, image: bytes):
        return {
            'schema_version': 3,
            'task': 'unified_review',
            'source_id': 'part-7:12000:test',
            'session_id': 3,
            'part_id': 7,
            'part_index': 2,
            'at_ms': 12_000,
            'segment_start_ms': 10_000,
            'streamer': '测试主播',
            'room_id': '123',
            'session_title': '测试直播',
            'filename': 'sample.flv',
            'image_path': 'objects/aa/frame.jpg',
            'image_sha256': hashlib.sha256(image).hexdigest(),
            'created_at': 100,
            'suggestions': {
                'match_flow': {'label': 'match_flow', 'confidence': 0.9},
                'match_mode': {'label': 'aram', 'confidence': 0.7},
                'hero_select': {'label': 'not_select', 'confidence': 0.9},
                'result_panel': {
                    'label': 'no_result_panel',
                    'confidence': 0.8,
                },
            },
            'suggested_boxes': [],
            'model_outputs': [{'model_version': 'multi-v2'}],
        }

    def candidate_image(self):
        buffer = io.BytesIO()
        Image.new('RGB', (32, 18), (20, 40, 60)).save(buffer, format='JPEG')
        return buffer.getvalue()

    def test_schema_v3_keeps_suggestions_separate_from_human_labels(self):
        image = self.candidate_image()
        nas = FakeNas(image)
        item = self.unified_item(image)

        result = worker_candidates.sync_worker_candidates(
            self.conn, nas, [item]
        )

        self.assertEqual(result['inserted'], 1)
        self.assertEqual(result['downloaded'], 1)
        rows = db.list_training_review_items(self.conn, status='pending')
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['match_flow_label'])
        self.assertEqual(
            rows[0]['suggestions']['match_flow']['label'], 'match_flow'
        )
        self.assertEqual(rows[0]['source_count'], 1)

    def test_confirmed_unified_labels_are_pushed_as_one_sidecar(self):
        image = self.candidate_image()
        nas = ReviewNas(image)
        worker_candidates.sync_worker_candidates(
            self.conn, nas, [self.unified_item(image)]
        )
        frame_id = db.list_training_review_items(
            self.conn, status='pending')[0]['frame_id']
        db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='match_flow',
            match_mode_label='aram',
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            status='confirmed',
        )

        result = worker_candidates.push_training_review_reviews(self.conn, nas)

        self.assertEqual(result['reviews_pushed'], 1)
        review = nas.reviews[0][1]
        self.assertEqual(review['schema_version'], 2)
        self.assertEqual(review['labels']['match_mode_label'], 'aram')
        self.assertEqual(review['source_ids'], ['part-7:12000:test'])
        source = db.get_training_review_item(self.conn, frame_id)['sources'][0]
        self.assertEqual(source['sync_state'], 'clean')

    def test_confirmed_hero_circles_and_labels_are_pushed_with_sidecar(self):
        image = self.candidate_image()
        nas = ReviewNas(image)
        worker_candidates.sync_worker_candidates(
            self.conn, nas, [self.unified_item(image)]
        )
        frame_id = db.list_training_review_items(
            self.conn, status='pending')[0]['frame_id']
        slots = [
            {
                'side': side,
                'slot': slot,
                'crop': {
                    'x': 0.1 + slot * 0.05,
                    'y': 0.1 if side == 'left' else 0.3,
                    'w': 0.04,
                    'h': 0.07,
                },
            }
            for side in ('left', 'right')
            for slot in range(1, 4)
        ]
        db.replace_training_review_hero_layout(
            self.conn,
            frame_id=frame_id,
            screen_type='gameplay_hud',
            team_size=3,
            method='manual-circle-v1',
            slots=slots,
        )
        db.save_training_review_hero_lineup(
            self.conn,
            frame_id=frame_id,
            labels=[
                {
                    'side': slot['side'],
                    'slot': slot['slot'],
                    'hero_label': 'Adagio',
                }
                for slot in slots
            ],
            allowed_labels={'Adagio'},
            player_side='left',
            player_slot=1,
        )
        db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='match_flow',
            match_mode_label='aram',
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            hero_layout_label='gameplay_hud',
            status='confirmed',
        )

        result = worker_candidates.push_training_review_reviews(self.conn, nas)

        self.assertEqual(result['reviews_pushed'], 1)
        review = nas.reviews[0][1]
        self.assertEqual(
            review['labels']['hero_layout_label'], 'gameplay_hud'
        )
        self.assertEqual(review['hero_lineup']['team_size'], 3)
        self.assertEqual(len(review['hero_lineup']['slots']), 6)
        self.assertEqual(
            review['hero_lineup']['slots'][0]['hero_label'], 'Adagio'
        )
        self.assertEqual(review['hero_lineup']['player_side'], 'left')
        self.assertEqual(review['hero_lineup']['player_slot'], 1)

        with self.conn:
            self.conn.execute(
                'DELETE FROM training_review_hero_slots WHERE frame_id = ?',
                (frame_id,),
            )
            self.conn.execute(
                'DELETE FROM training_review_hero_lineups WHERE frame_id = ?',
                (frame_id,),
            )
            self.conn.execute(
                'UPDATE training_review_items SET match_flow_label=NULL, '
                'match_mode_label=NULL, hero_select_label=NULL, '
                'result_panel_label=NULL, hero_layout_label=NULL, '
                "review_status='pending' WHERE frame_id = ?",
                (frame_id,),
            )
            self.conn.execute(
                "UPDATE training_review_sources SET sync_state='clean', "
                "remote_review_hash='' WHERE frame_id = ?",
                (frame_id,),
            )

        pulled = worker_candidates.pull_training_review_reviews(
            self.conn, [review]
        )

        self.assertEqual(pulled['reviews_pulled'], 1)
        restored = db.get_training_review_item(self.conn, frame_id)
        self.assertEqual(restored['hero_layout_label'], 'gameplay_hud')
        restored_lineup = db.get_training_review_hero_lineup(
            self.conn, frame_id
        )
        self.assertEqual(restored_lineup['review_status'], 'confirmed')
        self.assertEqual(
            restored_lineup['slots'][0]['confirmed_label'], 'Adagio'
        )
        self.assertEqual(restored_lineup['player_side'], 'left')
        self.assertEqual(restored_lineup['player_slot'], 1)

    def test_remote_unified_review_does_not_overwrite_dirty_local_labels(self):
        image = self.candidate_image()
        nas = FakeNas(image)
        worker_candidates.sync_worker_candidates(
            self.conn, nas, [self.unified_item(image)]
        )
        frame_id = db.list_training_review_items(
            self.conn, status='pending')[0]['frame_id']
        db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='match_flow',
            match_mode_label='aram',
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            status='confirmed',
        )
        remote = {
            'schema_version': 2,
            'source_ids': ['part-7:12000:test'],
            'image_path': 'objects/aa/frame.jpg',
            'review_status': 'confirmed',
            'labels': {
                'match_flow_label': 'not_match_flow',
                'match_mode_label': None,
                'hero_select_label': 'not_select',
                'result_panel_label': 'no_result_panel',
            },
            'result_box': None,
            'notes': '',
            'reviewed_at': '2026-08-09T12:00:00',
        }

        result = worker_candidates.pull_training_review_reviews(
            self.conn, [remote]
        )

        self.assertEqual(result['review_conflicts'], 1)
        item = db.get_training_review_item(self.conn, frame_id)
        self.assertEqual(item['match_flow_label'], 'match_flow')
        self.assertEqual(item['sources'][0]['sync_state'], 'conflict')

    def test_remote_result_positive_restores_its_required_box(self):
        image = self.candidate_image()
        worker_candidates.sync_worker_candidates(
            self.conn, FakeNas(image), [self.unified_item(image)]
        )
        frame_id = db.list_training_review_items(
            self.conn, status='pending')[0]['frame_id']
        remote = {
            'schema_version': 2,
            'source_ids': ['part-7:12000:test'],
            'image_path': 'objects/aa/frame.jpg',
            'review_status': 'confirmed',
            'labels': {
                'match_flow_label': 'match_flow',
                'match_mode_label': 'unreadable',
                'hero_select_label': 'not_select',
                'result_panel_label': 'result_panel',
            },
            'result_box': {'x': 0.1, 'y': 0.2, 'w': 0.8, 'h': 0.6},
            'notes': '',
            'reviewed_at': '2026-08-09T12:00:00',
        }

        result = worker_candidates.pull_training_review_reviews(
            self.conn, [remote]
        )

        self.assertEqual(result['reviews_pulled'], 1)
        item = db.get_training_review_item(self.conn, frame_id)
        self.assertEqual(item['result_panel_label'], 'result_panel')
        self.assertIn('result_panel', item['boxes'])


class ResultArchiveNas:
    def __init__(self, content: bytes):
        self.content = content
        self.downloads = 0

    def read_result_frame(self, _relative_path: str) -> bytes:
        self.downloads += 1
        return self.content


class TestResultArchiveImport(TrainingReviewTestCase):
    def test_recognized_match_is_pending_prelabel_not_human_truth(self):
        buffer = io.BytesIO()
        Image.new('RGB', (64, 36), (30, 60, 90)).save(buffer, format='PNG')
        nas = ResultArchiveNas(buffer.getvalue())
        candidates = [
            {
                'match_id': 42,
                'session_id': 9,
                'part_id': 11,
                'part_index': 2,
                'result_at_ms': 900_000,
                'game_mode': 'aram',
                'hero_slot_count': 6,
                'confidence': 0.87,
                'result_frame_path': 'session-9/part-11-900000.png',
                'anchor_name': '测试主播',
                'room_id': 123,
                'title': '测试直播',
            }
        ]

        first = result_archive.sync_result_archive(
            self.conn,
            nas,
            candidates,
            box_suggester=lambda _path: {
                'type': 'result_panel',
                'x': 0.1,
                'y': 0.2,
                'w': 0.8,
                'h': 0.6,
                'confidence': 0.91,
            },
        )
        second = result_archive.sync_result_archive(
            self.conn,
            nas,
            candidates,
            box_suggester=lambda _path: self.fail('预标框不应重复推理'),
        )

        self.assertEqual(first['inserted'], 1)
        self.assertEqual(second['updated'], 1)
        self.assertEqual(nas.downloads, 1)
        items = db.list_training_review_items(self.conn, status='pending')
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]['result_panel_label'])
        self.assertEqual(
            items[0]['suggestions']['result_panel']['label'], 'result_panel'
        )
        self.assertEqual(
            items[0]['suggestions']['match_flow']['label'], 'match_flow'
        )
        self.assertEqual(
            items[0]['suggestions']['match_mode']['label'], '3v3'
        )
        self.assertEqual(
            items[0]['sources'][0]['metadata']['suggested_boxes'][0]['w'],
            0.8,
        )

        candidates[0]['hero_slot_count'] = 8
        result_archive.sync_result_archive(self.conn, nas, candidates)
        updated = db.list_training_review_items(self.conn, status='pending')[0]
        self.assertEqual(
            updated['suggestions']['match_mode']['label'], '5v5'
        )
        self.assertEqual(nas.downloads, 1)


if __name__ == '__main__':
    unittest.main()
