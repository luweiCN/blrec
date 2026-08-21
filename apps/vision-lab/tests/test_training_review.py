"""新模型共用的一图多标签复核流程。"""

import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    def test_pending_legacy_entries_join_the_unified_queue_idempotently(self):
        training_review.migrate_legacy_training_reviews(self.conn)
        annotation_frame = self.frame(40)
        bp_frame = self.frame(41)
        key_frame = self.frame(42)
        gate_frame = self.frame(43)
        db.save_annotation(
            self.conn,
            annotation_frame,
            {
                'content_family': 'vainglory',
                'game_context': 'in_match',
                'screen_type': 'gameplay',
                'game_mode': '3v3',
            },
            status='draft',
        )
        db.upsert_bp_review_item(
            self.conn,
            frame_id=bp_frame,
            model_version='multi-v2',
            suggested_label='bp_3v3',
            suggestion_confidence=0.8,
            stage_class='pre_match',
            stage_confidence=0.9,
            pre_match_confidence=0.9,
            mode_class='3v3',
            mode_confidence=0.8,
            mode_margin=0.7,
            selection_reason='测试 BP 候选',
            priority=100,
            raw_prediction={'task': 'multi'},
        )
        db.upsert_key_screen_review_item(
            self.conn,
            frame_id=key_frame,
            model_version='multi-v2',
            suggested_label='scoreboard',
            suggestion_confidence=0.85,
            selection_reason='测试积分板候选',
            raw_prediction={'task': 'multi'},
        )
        db.save_mode_gate_round(
            self.conn, round_id='round-pending', name='旧光栅', active=True
        )
        db.add_mode_gate_round_video(
            self.conn,
            round_id='round-pending',
            video_id=self.video_id,
            expected_mode='3v3',
        )
        db.save_mode_gate_annotation(
            self.conn,
            round_id='round-pending',
            frame_id=gate_frame,
            evidence='no_evidence',
            boxes=[],
        )

        counts = training_review.queue_legacy_pending_reviews(self.conn)
        repeated = training_review.queue_legacy_pending_reviews(self.conn)

        self.assertEqual(
            counts,
            {
                'legacy_annotations': 1,
                'bp_candidates': 1,
                'key_screen_candidates': 1,
                'mode_gate_candidates': 1,
            },
        )
        self.assertEqual(sum(repeated.values()), 0)
        items = {
            item['frame_id']: item
            for item in db.list_training_review_items(self.conn, status='pending')
        }
        self.assertEqual(
            set(items), {annotation_frame, bp_frame, key_frame, gate_frame}
        )
        self.assertEqual(
            items[bp_frame]['suggestions']['hero_select']['label'], 'select_3v3'
        )
        self.assertEqual(
            items[key_frame]['suggestions']['match_flow']['label'], 'match_flow'
        )
        self.assertEqual(
            items[gate_frame]['suggestions']['match_mode']['label'], 'unreadable'
        )

    def test_old_review_saved_after_migration_is_mirrored_to_unified_truth(self):
        training_review.migrate_legacy_training_reviews(self.conn)
        bp_frame = self.frame(30)
        key_frame = self.frame(31)
        db.upsert_bp_review_item(
            self.conn,
            frame_id=bp_frame,
            model_version='multi-v2',
            suggested_label='bp_3v3',
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
        db.review_bp_item(self.conn, frame_id=bp_frame, label='bp_3v3')
        training_review.mirror_confirmed_bp_review(self.conn, bp_frame)
        db.upsert_key_screen_review_item(
            self.conn,
            frame_id=key_frame,
            model_version='multi-v2',
            suggested_label='scoreboard',
            suggestion_confidence=0.8,
            selection_reason='测试候选',
            raw_prediction={'task': 'multi'},
        )
        db.review_key_screen_item(self.conn, frame_id=key_frame, label='scoreboard')
        training_review.mirror_confirmed_key_screen_review(self.conn, key_frame)

        bp_item = db.get_training_review_item(self.conn, bp_frame)
        key_item = db.get_training_review_item(self.conn, key_frame)
        self.assertEqual(bp_item['hero_select_label'], 'select_3v3')
        self.assertEqual(bp_item['hero_select_variant'], 'bp')
        self.assertEqual(bp_item['review_status'], 'confirmed')
        self.assertEqual(key_item['hero_layout_label'], None)
        self.assertEqual(key_item['match_flow_label'], 'match_flow')
        self.assertEqual(key_item['result_panel_label'], 'no_result_panel')
        self.assertEqual(key_item['review_status'], 'confirmed')

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
        self.assertEqual(rows[select]['hero_select_variant'], 'bp')
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
            self.conn, round_id='round-1', video_id=self.video_id, expected_mode='aram'
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

    def test_same_legacy_result_event_only_needs_one_review(self):
        frame_ids = [self.frame(index) for index in (1, 2, 3)]
        event_id = db.create_event(
            self.conn, self.video_id, 1_000, 3_000, kind='candidate'
        )
        db.assign_event(self.conn, frame_ids, event_id)
        for frame_id in frame_ids:
            db.save_annotation(
                self.conn,
                frame_id,
                {
                    'content_family': 'vainglory',
                    'game_context': 'post_match',
                    'screen_type': 'result_page',
                    'game_mode': '3v3',
                },
                status='complete',
            )
            db.save_box(self.conn, frame_id, 'result_panel', 0.1, 0.2, 0.8, 0.6)

        training_review.migrate_legacy_training_reviews(self.conn)

        items = db.list_training_review_items(self.conn, status='missing_player')
        stats = db.training_review_stats(self.conn)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['result_group_size'], 3)
        self.assertEqual(stats['missing_player_hero'], 1)

    def test_legacy_hero_queue_groups_by_match_and_screen_type(self):
        first_gameplay = self.frame(1)
        second_gameplay = self.frame(2)
        scoreboard = self.frame(3)
        next_select = self.frame(4)
        next_gameplay = self.frame(5)
        values = [
            (first_gameplay, 'in_match', 'gameplay', '3v3'),
            (second_gameplay, 'in_match', 'gameplay', '3v3'),
            (scoreboard, 'in_match', 'scoreboard', '3v3'),
            (next_select, 'pre_match', 'hero_select_bp', '3v3'),
            (next_gameplay, 'in_match', 'gameplay', '3v3'),
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
        training_review.migrate_legacy_training_reviews(self.conn)

        items = db.list_legacy_hero_review_items(self.conn)
        gameplay_items = db.list_legacy_hero_review_items(
            self.conn, streamer='测试主播', screen_type='gameplay_hud'
        )
        stats = db.legacy_hero_review_stats(self.conn)

        self.assertEqual(len(items), 3)
        self.assertEqual(len(gameplay_items), 2)
        self.assertEqual(
            sorted(item['legacy_hero_group_size'] for item in gameplay_items), [1, 2]
        )
        self.assertTrue(
            all(
                item['legacy_hero_screen_type'] == 'gameplay_hud'
                for item in gameplay_items
            )
        )
        self.assertEqual(stats['remaining_groups'], 3)
        self.assertEqual(stats['remaining_frames'], 4)
        self.assertEqual(stats['by_streamer'][0]['streamer'], '测试主播')
        review_stats = db.training_review_stats(self.conn)
        self.assertEqual(review_stats['source_frames']['legacy'], 5)
        self.assertEqual(
            review_stats['legacy_data'],
            {
                'frames': 5,
                'core_label_confirmed': 5,
                'core_label_needs_review': 0,
                'unified_manual_confirmed': 0,
                'migration_pending_review': 5,
                'hero_eligible': 4,
                'hero_complete': 0,
                'hero_missing': 4,
            },
        )

        representative = next(
            item for item in gameplay_items if item['legacy_hero_group_size'] == 2
        )
        slots = [
            {
                'side': side,
                'slot': slot,
                'crop': {
                    'x': 0.1 + slot * 0.05,
                    'y': 0.1 if side == 'left' else 0.3,
                    'w': 0.04,
                    'h': 0.04,
                },
            }
            for side in ('left', 'right')
            for slot in range(1, 4)
        ]
        db.replace_training_review_hero_layout(
            self.conn,
            frame_id=representative['frame_id'],
            screen_type='gameplay_hud',
            team_size=3,
            method='manual-circle-v1',
            slots=slots,
        )
        db.save_training_review_hero_lineup(
            self.conn,
            frame_id=representative['frame_id'],
            labels=[
                {
                    'side': value['side'],
                    'slot': value['slot'],
                    'hero_label': 'hero-{}'.format(index),
                }
                for index, value in enumerate(slots)
            ],
            allowed_labels={'hero-{}'.format(index) for index in range(len(slots))},
        )

        remaining = db.list_legacy_hero_review_items(
            self.conn, screen_type='gameplay_hud'
        )
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]['legacy_hero_group_size'], 1)
        self.assertEqual(
            db.training_review_stats(self.conn)['legacy_data']['hero_complete'], 1
        )


class TestTrainingReviewStorage(TrainingReviewTestCase):
    def test_review_page_batches_item_sources_and_boxes(self):
        for index in range(1, 21):
            frame_id = self.frame(index)
            db.add_training_review_source(
                self.conn,
                frame_id=frame_id,
                source_type='worker',
                source_id=f'worker-{index}',
            )
        statements = []
        self.conn.set_trace_callback(
            lambda statement: (
                statements.append(statement)
                if statement.lstrip().upper().startswith('SELECT')
                else None
            )
        )
        try:
            items, total = db.training_review_page(
                self.conn, status='pending', limit=20, result_groups={}
            )
        finally:
            self.conn.set_trace_callback(None)

        self.assertEqual(total, 20)
        self.assertEqual(len(items), 20)
        self.assertLessEqual(len(statements), 6)

    def test_default_new_review_queue_uses_one_aggregated_query(self):
        normal = self.frame(31)
        inferred_aram = self.frame(32)
        aram_select = self.frame(33)
        legacy = self.frame(34)
        for frame_id, source_type, suggestions, source_created_at in (
            (normal, 'worker', {}, 400),
            (
                inferred_aram,
                'result_archive',
                {'match_mode': {'label': 'aram', 'confidence': 0.8}},
                300,
            ),
            (
                aram_select,
                'manual_correction',
                {'hero_select': {'label': 'select_aram', 'confidence': 0.8}},
                200,
            ),
            (legacy, 'legacy_annotation', {}, 500),
        ):
            db.add_training_review_source(
                self.conn,
                frame_id=frame_id,
                source_type=source_type,
                source_id=f'source-{frame_id}',
                suggestions=suggestions,
                source_created_at=source_created_at,
            )
        statements = []
        self.conn.set_trace_callback(
            lambda statement: (
                statements.append(statement)
                if statement.lstrip().upper().startswith(('SELECT', 'WITH'))
                else None
            )
        )
        try:
            frame_ids = db.training_review_frame_ids(
                self.conn, status='needs_review', source_scope='new', result_groups={}
            )
        finally:
            self.conn.set_trace_callback(None)

        self.assertEqual(frame_ids, [aram_select, inferred_aram, normal])
        self.assertEqual(len(statements), 1)

    def test_pending_queue_hydration_skips_confirmed_review_subqueries(self):
        frame_id = self.frame(35)
        db.add_training_review_source(
            self.conn, frame_id=frame_id, source_type='worker', source_id='worker-35'
        )
        statements = []
        self.conn.set_trace_callback(
            lambda statement: (
                statements.append(statement)
                if statement.lstrip().upper().startswith('SELECT')
                else None
            )
        )
        try:
            items = db.get_training_review_items(
                self.conn, [frame_id], result_groups={}, pending_review_queue=True
            )
        finally:
            self.conn.set_trace_callback(None)

        self.assertEqual(items[0]['frame_id'], frame_id)
        self.assertFalse(items[0]['needs_player_hero_review'])
        self.assertFalse(items[0]['unified_manual_reviewed'])
        joined = '\n'.join(statements)
        self.assertNotIn('audit_log manual_review', joined)
        self.assertNotIn('training_review_hero_lineups lineup', joined)

    def test_save_can_return_current_item_without_rebuilding_all_result_groups(self):
        frame_id = self.frame(1)
        db.add_training_review_source(
            self.conn, frame_id=frame_id, source_type='worker', source_id='worker-1'
        )
        with mock.patch.object(
            db,
            'training_review_result_groups',
            side_effect=AssertionError('不应执行全量结算图分组'),
        ):
            reviewed = db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label='not_match_flow',
                match_mode_label=None,
                hero_select_label='not_select',
                result_panel_label='no_result_panel',
                hero_layout_label='none',
                status='confirmed',
                result_groups={},
            )

        self.assertEqual(reviewed['frame_id'], frame_id)

    def test_save_can_skip_item_hydration_for_fast_api_ack(self):
        frame_id = self.frame(2)
        db.add_training_review_source(
            self.conn, frame_id=frame_id, source_type='worker', source_id='worker-2'
        )
        with mock.patch.object(
            db,
            'get_training_review_item',
            side_effect=AssertionError('快速保存不应重新读取完整素材'),
        ):
            reviewed = db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label='not_match_flow',
                match_mode_label=None,
                hero_select_label='not_select',
                result_panel_label='no_result_panel',
                hero_layout_label='none',
                status='confirmed',
                result_groups={},
                hydrate=False,
            )

        self.assertEqual(reviewed['frame_id'], frame_id)
        self.assertEqual(reviewed['review_status'], 'confirmed')
        self.assertEqual(reviewed['hero_layout_label'], 'none')

    def test_migrated_legacy_label_needs_unified_manual_confirmation(self):
        frame_id = self.frame(1)
        db.save_annotation(
            self.conn,
            frame_id,
            {
                'content_family': 'vainglory',
                'game_context': 'in_match',
                'screen_type': 'gameplay',
                'game_mode': '3v3',
            },
            status='complete',
        )
        training_review.migrate_legacy_training_reviews(self.conn)

        migrated = db.list_training_review_items(
            self.conn, status='migration_review', source_scope='legacy'
        )
        manual = db.list_training_review_items(
            self.conn, status='human_confirmed', source_scope='legacy'
        )
        self.assertEqual([item['frame_id'] for item in migrated], [frame_id])
        self.assertTrue(migrated[0]['legacy_migration_needs_review'])
        self.assertFalse(migrated[0]['unified_manual_reviewed'])
        self.assertEqual(manual, [])

        db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='match_flow',
            match_mode_label='3v3',
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            hero_layout_label=None,
            status='confirmed',
        )

        migrated = db.list_training_review_items(
            self.conn, status='migration_review', source_scope='legacy'
        )
        manual = db.list_training_review_items(
            self.conn, status='human_confirmed', source_scope='legacy'
        )
        stats = db.training_review_stats(self.conn)['source_scopes']['legacy']
        self.assertEqual(migrated, [])
        self.assertEqual([item['frame_id'] for item in manual], [frame_id])
        self.assertTrue(manual[0]['unified_manual_reviewed'])
        self.assertFalse(manual[0]['legacy_migration_needs_review'])
        self.assertEqual(stats['migration_pending_review'], 0)
        self.assertEqual(stats['human_confirmed'], 1)

    def test_review_queue_separates_new_and_legacy_sources(self):
        legacy_frame = self.frame(1)
        new_frame = self.frame(2)
        shared_frame = self.frame(3)
        db.add_training_review_source(
            self.conn,
            frame_id=legacy_frame,
            source_type='legacy_annotation',
            source_id='legacy-only',
        )
        db.add_training_review_source(
            self.conn,
            frame_id=legacy_frame,
            source_type='new_model_prefill',
            source_id='core-prefill',
        )
        db.add_training_review_source(
            self.conn, frame_id=new_frame, source_type='worker', source_id='worker-only'
        )
        db.add_training_review_source(
            self.conn,
            frame_id=new_frame,
            source_type='new_model_hero_prefill',
            source_id='hero-prefill',
        )
        db.add_training_review_source(
            self.conn,
            frame_id=shared_frame,
            source_type='legacy_bp_review',
            source_id='shared-legacy',
        )
        db.add_training_review_source(
            self.conn,
            frame_id=shared_frame,
            source_type='result_archive',
            source_id='shared-new',
        )

        legacy = db.list_training_review_items(
            self.conn, status='needs_review', source_scope='legacy'
        )
        new = db.list_training_review_items(
            self.conn, status='needs_review', source_scope='new'
        )
        stats = db.training_review_stats(self.conn)['source_scopes']

        self.assertEqual(
            {item['frame_id'] for item in legacy}, {legacy_frame, shared_frame}
        )
        self.assertEqual({item['frame_id'] for item in new}, {new_frame, shared_frame})
        self.assertEqual(stats['legacy']['total'], 2)
        self.assertEqual(stats['legacy']['needs_review'], 2)
        self.assertEqual(stats['legacy']['core_model_prefilled'], 1)
        self.assertEqual(stats['new']['total'], 2)
        self.assertEqual(stats['new']['needs_review'], 2)
        self.assertEqual(stats['new']['hero_model_prefilled'], 1)

    def test_review_queue_rejects_unknown_source_scope(self):
        with self.assertRaisesRegex(ValueError, '数据来源无效'):
            db.list_training_review_items(
                self.conn, status='needs_review', source_scope='mystery'
            )

    def test_review_queue_filters_worker_metadata_without_loading_images(self):
        correction = self.frame(10)
        ordinary = self.frame(11)
        db.add_training_review_source(
            self.conn,
            frame_id=correction,
            source_type='manual_correction',
            source_id='manual-10',
            suggestions={
                'match_mode': {'label': 'aram', 'confidence': 0.55},
                'hero_select': {'label': 'select_aram', 'confidence': 0.55},
            },
        )
        db.add_training_review_source(
            self.conn,
            frame_id=ordinary,
            source_type='worker',
            source_id='worker-11',
            suggestions={
                'match_mode': {'label': '3v3', 'confidence': 0.95},
                'hero_select': {'label': 'not_select', 'confidence': 0.95},
            },
        )

        items = db.list_training_review_items(
            self.conn,
            status='needs_review',
            source_scope='new',
            streamer='测试主播',
            source_type='manual_correction',
            scene='hero_select',
            match_mode='aram',
            confidence='low',
        )
        count = db.count_training_review_items(
            self.conn,
            status='needs_review',
            source_scope='new',
            streamer='测试主播',
            source_type='manual_correction',
            scene='hero_select',
            match_mode='aram',
            confidence='low',
        )
        page, page_total = db.training_review_page(
            self.conn,
            status='needs_review',
            source_scope='new',
            streamer='测试主播',
            source_type='manual_correction',
            scene='hero_select',
            match_mode='aram',
            confidence='low',
            limit=1,
        )

        self.assertEqual([item['frame_id'] for item in items], [correction])
        self.assertEqual(count, 1)
        self.assertEqual([item['frame_id'] for item in page], [correction])
        self.assertEqual(page_total, 1)

    def test_material_suggestions_count_confirmed_and_actionable_candidates(self):
        confirmed = self.frame(12)
        pending_select = self.frame(13)
        pending_hud = self.frame(14)
        db.add_training_review_source(
            self.conn,
            frame_id=confirmed,
            source_type='worker',
            source_id='confirmed-select',
        )
        db.save_training_review(
            self.conn,
            frame_id=confirmed,
            match_flow_label='not_match_flow',
            match_mode_label=None,
            hero_select_label='select_3v3',
            hero_select_variant='bp',
            result_panel_label='no_result_panel',
            hero_layout_label='none',
            status='confirmed',
            result_groups={},
        )
        db.add_training_review_source(
            self.conn,
            frame_id=pending_select,
            source_type='worker',
            source_id='pending-select',
            suggestions={'hero_select': {'label': 'select_5v5', 'confidence': 0.91}},
        )
        db.add_training_review_source(
            self.conn,
            frame_id=pending_hud,
            source_type='worker',
            source_id='pending-hud',
            suggestions={'match_mode': {'label': 'aram', 'confidence': 0.88}},
            metadata={
                'hero_context_suggestion': {
                    'screen_type': 'gameplay_hud',
                    'confidence': 0.9,
                }
            },
        )
        for frame_id in (pending_select, pending_hud):
            db.update_training_review_prefill_state(
                self.conn, frame_id=frame_id, status='ready', stage='complete'
            )

        suggestions = db.training_review_stats(self.conn)['material_suggestions']
        select_5v5 = next(
            item
            for item in suggestions
            if item['scene'] == 'hero_select' and item['match_mode'] == '5v5'
        )
        hud_aram = next(
            item
            for item in suggestions
            if item['scene'] == 'gameplay_hud' and item['match_mode'] == 'aram'
        )

        self.assertEqual(select_5v5['confirmed_count'], 0)
        self.assertEqual(select_5v5['candidate_count'], 1)
        self.assertEqual(select_5v5['source_scope'], 'new')
        self.assertEqual(
            select_5v5['filters'],
            {'status': 'needs_review', 'scene': 'hero_select', 'match_mode': '5v5'},
        )
        self.assertEqual(hud_aram['candidate_count'], 1)
        self.assertEqual(
            [
                item['frame_id']
                for item in db.list_training_review_items(
                    self.conn,
                    status='needs_review',
                    source_scope='new',
                    scene='gameplay_hud',
                    match_mode='aram',
                )
            ],
            [pending_hud],
        )

    def test_schema_v3_model_outputs_are_filterable_as_5v5_hud(self):
        frame_id = self.frame(16)
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id='schema-v3-hud',
            suggestions={
                'match_flow': {'label': 'match_flow', 'confidence': 0.94},
                'match_mode': {'label': '5v5', 'confidence': 0.91},
            },
            metadata={
                'schema_version': 3,
                'model_outputs': [
                    {
                        'task': 'match_flow',
                        'stage_class': 'gameplay',
                        'mode_class': '5v5',
                    }
                ],
            },
        )
        db.update_training_review_prefill_state(
            self.conn, frame_id=frame_id, status='ready', stage='complete'
        )

        items = db.list_training_review_items(
            self.conn,
            status='needs_review',
            source_scope='new',
            scene='gameplay_hud',
            match_mode='5v5',
        )
        suggestion = next(
            item
            for item in db.training_review_stats(self.conn)['material_suggestions']
            if item['scene'] == 'gameplay_hud' and item['match_mode'] == '5v5'
        )

        self.assertEqual([item['frame_id'] for item in items], [frame_id])
        self.assertEqual(suggestion['candidate_count'], 1)

        db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='not_match_flow',
            match_mode_label=None,
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            hero_layout_label='none',
            status='confirmed',
            result_groups={},
        )
        indexed = self.conn.execute(
            'SELECT scene,match_mode FROM training_review_material_index '
            'WHERE frame_id=?',
            (frame_id,),
        ).fetchone()
        self.assertEqual(indexed['scene'], 'other')
        self.assertEqual(indexed['match_mode'], '')

    def test_direct_hud_detection_is_reviewed_before_gameplay_fallback(self):
        weak = self.frame(17)
        strong = self.frame(18)
        db.add_training_review_source(
            self.conn,
            frame_id=weak,
            source_type='worker',
            source_id='newer-gameplay-fallback',
            suggestions={'match_mode': {'label': '5v5', 'confidence': 0.9}},
            metadata={
                'model_outputs': [{'task': 'match_flow', 'stage_class': 'gameplay'}]
            },
            source_created_at=200,
        )
        db.add_training_review_source(
            self.conn,
            frame_id=strong,
            source_type='worker',
            source_id='older-direct-hud',
            suggestions={'match_mode': {'label': '5v5', 'confidence': 0.9}},
            metadata={
                'hero_context_suggestion': {
                    'screen_type': 'gameplay_hud',
                    'team_size': 5,
                    'confidence': 0.9,
                }
            },
            source_created_at=100,
        )
        db.update_training_review_prefill_state(
            self.conn, frame_id=weak, status='ready', stage='complete'
        )
        db.update_training_review_prefill_state(
            self.conn,
            frame_id=strong,
            status='ready',
            stage='complete',
            screen_type='gameplay_hud',
            team_size=5,
        )

        items = db.list_training_review_items(
            self.conn,
            status='needs_review',
            source_scope='new',
            scene='gameplay_hud',
            match_mode='5v5',
        )

        self.assertEqual([item['frame_id'] for item in items], [strong, weak])

    def test_material_suggestions_include_sufficient_and_hero_scene_rows(self):
        frames = []
        for index in range(100, 150):
            frame_id = self.frame(index)
            frames.append(frame_id)
            db.add_training_review_source(
                self.conn,
                frame_id=frame_id,
                source_type='worker',
                source_id=f'sufficient-select-{index}',
            )
            db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label='not_match_flow',
                match_mode_label=None,
                hero_select_label='select_3v3',
                hero_select_variant='bp',
                result_panel_label='no_result_panel',
                hero_layout_label='none',
                status='confirmed',
                result_groups={},
            )

        hero_frame = self.frame(151)
        db.add_training_review_source(
            self.conn,
            frame_id=hero_frame,
            source_type='worker',
            source_id='hero-scene-confirmed',
        )
        slots = [
            {
                'side': side,
                'slot': slot,
                'crop': {
                    'x': 0.1 + (0.4 if side == 'right' else 0),
                    'y': 0.1 + slot * 0.1,
                    'w': 0.05,
                    'h': 0.08,
                },
            }
            for side in ('left', 'right')
            for slot in range(1, 4)
        ]
        db.replace_training_review_hero_layout(
            self.conn,
            frame_id=hero_frame,
            screen_type='scoreboard',
            team_size=3,
            method='manual-circle-v1',
            slots=slots,
        )
        db.save_training_review_hero_lineup(
            self.conn,
            frame_id=hero_frame,
            labels=[
                {'side': slot['side'], 'slot': slot['slot'], 'hero_label': 'Adagio'}
                for slot in slots
            ],
            allowed_labels={'Adagio', 'Vox'},
            player_side='left',
            player_slot=1,
        )

        suggestions = db.training_review_stats(
            self.conn,
            hero_catalog=(
                {'label': 'Adagio', 'name': '奥达基'},
                {'label': 'Vox', 'name': '沃克斯'},
            ),
        )['material_suggestions']
        sufficient = next(
            item
            for item in suggestions
            if item.get('kind') == 'scene_mode'
            and item['scene'] == 'hero_select'
            and item['match_mode'] == '3v3'
        )
        adagio_scoreboard = next(
            item
            for item in suggestions
            if item.get('kind') == 'hero_scene'
            and item['hero_label'] == 'Adagio'
            and item['scene'] == 'scoreboard'
        )
        vox_hud = next(
            item
            for item in suggestions
            if item.get('kind') == 'hero_scene'
            and item['hero_label'] == 'Vox'
            and item['scene'] == 'gameplay_hud'
        )

        self.assertEqual(sufficient['status'], 'sufficient')
        self.assertEqual(sufficient['shortage_count'], 0)
        self.assertEqual(adagio_scoreboard['confirmed_count'], 6)
        self.assertEqual(vox_hud['confirmed_count'], 0)
        self.assertEqual(vox_hud['target_count'], 20)

    def test_hero_select_mode_filter_uses_select_model_suggestion(self):
        frame_id = self.frame(15)
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id='select-only-mode',
            suggestions={'hero_select': {'label': 'select_5v5', 'confidence': 0.92}},
        )

        items = db.list_training_review_items(
            self.conn,
            status='needs_review',
            source_scope='new',
            scene='hero_select',
            match_mode='5v5',
        )

        self.assertEqual([item['frame_id'] for item in items], [frame_id])

    def test_review_queue_filters_any_selected_hero(self):
        adagio = self.frame(20)
        vox = self.frame(21)
        for frame_id, label in ((adagio, 'Adagio'), (vox, 'Vox')):
            db.add_training_review_source(
                self.conn,
                frame_id=frame_id,
                source_type='worker',
                source_id=f'worker-{frame_id}',
            )
            slots = [
                {
                    'side': side,
                    'slot': slot,
                    'crop': {
                        'x': 0.05 * slot,
                        'y': 0.1 if side == 'left' else 0.3,
                        'w': 0.04,
                        'h': 0.07,
                    },
                    'suggested_label': label,
                    'suggestion_confidence': 0.9,
                }
                for side in ('left', 'right')
                for slot in range(1, 4)
            ]
            db.replace_training_review_hero_suggestions(
                self.conn,
                frame_id=frame_id,
                screen_type='gameplay_hud',
                team_size=3,
                method='test-model',
                slots=slots,
            )

        one = db.list_training_review_items(
            self.conn, status='needs_review', source_scope='new', hero=['Adagio']
        )
        either = db.list_training_review_items(
            self.conn, status='needs_review', source_scope='new', hero=['Adagio', 'Vox']
        )

        self.assertEqual([item['frame_id'] for item in one], [adagio])
        self.assertEqual({item['frame_id'] for item in either}, {adagio, vox})

    def test_review_filter_options_are_scoped_and_counted(self):
        legacy = self.frame(30)
        fresh = self.frame(31)
        db.add_training_review_source(
            self.conn,
            frame_id=legacy,
            source_type='legacy_annotation',
            source_id='legacy-30',
        )
        db.add_training_review_source(
            self.conn, frame_id=fresh, source_type='worker', source_id='worker-31'
        )

        options = db.training_review_filter_options(self.conn, source_scope='new')

        self.assertEqual(options['streamers'], [{'name': '测试主播', 'frame_count': 1}])

    def test_review_queue_prioritizes_aram_evidence(self):
        normal = self.frame(1)
        inferred_aram = self.frame(2)
        aram_select = self.frame(3)
        db.add_training_review_source(
            self.conn,
            frame_id=normal,
            source_type='worker',
            source_id='normal',
            source_created_at=300,
        )
        db.add_training_review_source(
            self.conn,
            frame_id=inferred_aram,
            source_type='worker',
            source_id='inferred-aram',
            suggestions={'match_mode': {'label': 'aram', 'confidence': 0.8}},
            source_created_at=200,
        )
        db.add_training_review_source(
            self.conn,
            frame_id=aram_select,
            source_type='worker',
            source_id='aram-select',
            suggestions={'hero_select': {'label': 'select_aram', 'confidence': 0.8}},
            source_created_at=100,
        )

        pending = db.list_training_review_items(self.conn, status='pending')
        needs_review = db.list_training_review_items(self.conn, status='needs_review')

        self.assertEqual(
            [item['frame_id'] for item in pending], [aram_select, inferred_aram, normal]
        )
        self.assertEqual(
            [item['frame_id'] for item in needs_review],
            [aram_select, inferred_aram, normal],
        )

    def test_review_queue_uses_source_time_then_video_offset(self):
        earlier_offset = self.frame(1)
        older_source = self.frame(2)
        later_offset = self.frame(3)
        for frame_id, source_created_at, at_ms in (
            (earlier_offset, 200, 1_000),
            (older_source, 100, 9_000),
            (later_offset, 200, 2_000),
        ):
            db.add_training_review_source(
                self.conn,
                frame_id=frame_id,
                source_type='worker',
                source_id=f'source-{frame_id}',
                metadata={'at_ms': at_ms},
                source_created_at=source_created_at,
            )

        pending = db.list_training_review_items(self.conn, status='pending')

        self.assertEqual(
            [item['frame_id'] for item in pending],
            [later_offset, earlier_offset, older_source],
        )

    def test_review_queue_prioritizes_frames_from_confirmed_aram_video(self):
        confirmed = self.frame(1)
        same_video = self.frame(2)
        db.add_training_review_source(
            self.conn,
            frame_id=confirmed,
            source_type='worker',
            source_id='confirmed-aram',
        )
        db.save_training_review(
            self.conn,
            frame_id=confirmed,
            match_flow_label='match_flow',
            match_mode_label='aram',
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            hero_layout_label='none',
            status='confirmed',
        )
        db.add_training_review_source(
            self.conn,
            frame_id=same_video,
            source_type='worker',
            source_id='same-video',
            source_created_at=100,
        )

        unrelated_video = db.upsert_video(
            self.conn,
            remote_path='/nas/unrelated.flv',
            streamer='其他主播',
            room_id='2',
            filename='unrelated.flv',
            duration_seconds=100,
            size_bytes=1,
        )
        unrelated_path = self.root / 'unrelated.jpg'
        unrelated_path.write_bytes(b'unrelated-frame')
        unrelated = db.add_frames(
            self.conn,
            unrelated_video,
            [
                {
                    'timestamp_ms': 1_000,
                    'width': 1280,
                    'height': 720,
                    'sha256': 'f' * 64,
                    'phash': '',
                    'frame_path': str(unrelated_path),
                    'thumb_path': '',
                    'strategy': 'test',
                    'model_source': '',
                    'model_confidence': None,
                }
            ],
        )[0]
        db.add_training_review_source(
            self.conn,
            frame_id=unrelated,
            source_type='worker',
            source_id='unrelated',
            source_created_at=200,
        )

        pending = db.list_training_review_items(self.conn, status='pending')

        self.assertEqual(
            [item['frame_id'] for item in pending], [same_video, unrelated]
        )

    def test_hero_select_variant_is_saved_and_validated(self):
        frame_id = self.frame(1)
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id='part-1:1000:hero-select',
        )

        reviewed = db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='not_match_flow',
            match_mode_label=None,
            hero_select_label='select_3v3',
            hero_select_variant='blind',
            result_panel_label='no_result_panel',
            hero_layout_label='none',
            status='confirmed',
        )

        self.assertEqual(reviewed['hero_select_variant'], 'blind')

        with self.assertRaisesRegex(ValueError, '英雄选择类型'):
            db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label='not_match_flow',
                match_mode_label=None,
                hero_select_label='not_select',
                hero_select_variant='bp',
                result_panel_label='no_result_panel',
                hero_layout_label='none',
                status='confirmed',
            )

        with self.assertRaisesRegex(ValueError, '大乱斗'):
            db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label='not_match_flow',
                match_mode_label=None,
                hero_select_label='select_aram',
                hero_select_variant='bp',
                result_panel_label='no_result_panel',
                hero_layout_label='none',
                status='confirmed',
            )

    def test_hero_select_visibility_is_saved_and_only_applies_to_selection(self):
        frame_id = self.frame(1)
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id='part-1:1000:hero-select-visibility',
        )

        reviewed = db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='not_match_flow',
            match_mode_label=None,
            hero_select_label='select_aram',
            hero_select_variant='random',
            hero_select_visibility='occluded',
            result_panel_label='no_result_panel',
            hero_layout_label='none',
            status='confirmed',
        )

        self.assertEqual(reviewed['hero_select_visibility'], 'occluded')

        reviewed = db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='not_match_flow',
            match_mode_label=None,
            hero_select_label='not_select',
            hero_select_visibility='occluded',
            result_panel_label='no_result_panel',
            hero_layout_label='none',
            status='confirmed',
        )
        self.assertIsNone(reviewed['hero_select_visibility'])

        with self.assertRaisesRegex(ValueError, '英雄选择画面状态无效'):
            db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label='not_match_flow',
                match_mode_label=None,
                hero_select_label='select_3v3',
                hero_select_variant='blind',
                hero_select_visibility='covered',
                result_panel_label='no_result_panel',
                hero_layout_label='none',
                status='confirmed',
            )

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

    def test_result_quality_is_saved_and_cleared_for_non_result_frames(self):
        frame_id = self.frame(1)
        db.save_box(self.conn, frame_id, 'result_panel', 0.1, 0.2, 0.8, 0.6)

        reviewed = db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='match_flow',
            match_mode_label='unreadable',
            hero_select_label='not_select',
            result_panel_label='result_panel',
            panel_render_state='translucent',
            ocr_usable='no',
            result_occlusion='occluded',
            occluder_types=[
                'system_device_ui',
                'game_ui',
                'platform_ui',
                'ad_watermark',
            ],
            status='confirmed',
        )

        self.assertEqual(reviewed['panel_render_state'], 'translucent')
        self.assertEqual(reviewed['ocr_usable'], 'no')
        self.assertEqual(reviewed['result_occlusion'], 'occluded')
        self.assertEqual(
            reviewed['occluder_types'],
            ['system_device_ui', 'game_ui', 'platform_ui', 'ad_watermark'],
        )

        reviewed = db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='not_match_flow',
            match_mode_label=None,
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            panel_render_state='translucent',
            ocr_usable='no',
            result_occlusion='occluded',
            occluder_types=['platform_ui'],
            status='confirmed',
        )

        self.assertEqual(reviewed['panel_render_state'], 'clear')
        self.assertEqual(reviewed['ocr_usable'], 'yes')
        self.assertEqual(reviewed['result_occlusion'], 'none')
        self.assertEqual(reviewed['occluder_types'], [])

    def test_scoreboard_can_be_marked_as_translucent(self):
        frame_id = self.frame(1)

        reviewed = db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='match_flow',
            match_mode_label='unreadable',
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            hero_layout_label='scoreboard',
            panel_render_state='translucent',
            status='partial',
        )

        self.assertEqual(reviewed['panel_render_state'], 'translucent')

    def test_hud_can_be_marked_as_translucent(self):
        frame_id = self.frame(1)

        reviewed = db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='match_flow',
            match_mode_label='unreadable',
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            hero_layout_label='gameplay_hud',
            panel_render_state='translucent',
            status='partial',
        )

        self.assertEqual(reviewed['panel_render_state'], 'translucent')

    def test_panel_render_state_rejects_unknown_values(self):
        frame_id = self.frame(1)

        with self.assertRaisesRegex(ValueError, '面板显示状态无效'):
            db.save_training_review(
                self.conn,
                frame_id=frame_id,
                match_flow_label='match_flow',
                match_mode_label='unreadable',
                hero_select_label='not_select',
                result_panel_label='no_result_panel',
                hero_layout_label='scoreboard',
                panel_render_state='blurred',
                status='partial',
            )

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
        db.save_box(self.conn, result_frame, 'result_panel', 0.1, 0.2, 0.8, 0.6)
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

        missing = db.list_training_review_items(self.conn, status='missing_player')
        needs_review = db.list_training_review_items(self.conn, status='needs_review')

        self.assertEqual([item['frame_id'] for item in missing], [result_frame])
        self.assertTrue(missing[0]['needs_player_hero_review'])
        self.assertEqual([item['frame_id'] for item in needs_review], [pending_frame])
        self.assertEqual(db.training_review_stats(self.conn)['missing_player_hero'], 1)

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
                {'side': slot['side'], 'slot': slot['slot'], 'hero_label': 'Adagio'}
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
        self.assertEqual(db.training_review_stats(self.conn)['missing_player_hero'], 0)

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
            {'side': slot['side'], 'slot': slot['slot'], 'hero_label': 'Adagio'}
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
            self.conn, frame_id=frame_id, labels=labels, allowed_labels={'Adagio'}
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

        missing = db.list_training_review_items(self.conn, status='missing_player')
        self.assertEqual([item['frame_id'] for item in missing], [frame_id])

        unreadable = db.save_training_review_hero_lineup(
            self.conn,
            frame_id=frame_id,
            labels=labels,
            allowed_labels={'Adagio'},
            player_status='unreadable',
        )
        self.assertEqual(unreadable['player_status'], 'unreadable')
        self.assertEqual(
            db.list_training_review_items(self.conn, status='missing_player'), []
        )

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
                'result_panel': {'label': 'no_result_panel', 'confidence': 0.8},
            },
            'suggested_boxes': [],
            'model_outputs': [{'model_version': 'multi-v2'}],
        }

    def candidate_image(self):
        buffer = io.BytesIO()
        Image.new('RGB', (32, 18), (20, 40, 60)).save(buffer, format='JPEG')
        return buffer.getvalue()

    def promote_synced(self, source_id='part-7:12000:test'):
        source = self.conn.execute(
            'SELECT frame_id FROM training_review_sources WHERE source_id=?',
            (source_id,),
        ).fetchone()
        self.assertIsNotNone(source)
        db.promote_training_review_candidate(self.conn, int(source['frame_id']))
        return int(source['frame_id'])

    def test_schema_v3_keeps_suggestions_separate_from_human_labels(self):
        image = self.candidate_image()
        nas = FakeNas(image)
        item = self.unified_item(image)

        result = worker_candidates.sync_worker_candidates(self.conn, nas, [item])

        self.assertEqual(result['inserted'], 1)
        self.assertEqual(result['downloaded'], 1)
        self.assertEqual(db.list_training_review_items(self.conn), [])
        self.promote_synced()
        rows = db.list_training_review_items(self.conn, status='pending')
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['match_flow_label'])
        self.assertEqual(rows[0]['suggestions']['match_flow']['label'], 'match_flow')
        self.assertEqual(rows[0]['source_count'], 1)

    def test_worker_candidate_can_wait_outside_formal_review_until_prefilled(self):
        image = self.candidate_image()
        nas = FakeNas(image)
        item = self.unified_item(image)
        item['source_id'] = 'staged-until-prefilled'

        result = worker_candidates.sync_worker_candidates(self.conn, nas, [item])

        self.assertEqual(result['inserted'], 1)
        self.assertEqual(db.list_training_review_items(self.conn), [])
        inbox = db.training_review_candidate_inbox_stats(self.conn)
        self.assertEqual(inbox['total'], 1)
        self.assertEqual(inbox['statuses']['pending'], 1)
        self.assertEqual(
            db.training_review_queue_summary(self.conn),
            {
                'total': 1,
                'prefill_ready': 0,
                'ready_for_review': 0,
                'prefill_waiting': 1,
                'prefill_failed': 0,
            },
        )
        source = self.conn.execute(
            'SELECT frame_id FROM training_review_sources WHERE source_id=?',
            ('staged-until-prefilled',),
        ).fetchone()
        self.assertIsNotNone(source)

        promoted = db.promote_training_review_candidate(
            self.conn, int(source['frame_id'])
        )

        self.assertTrue(promoted)
        self.assertIsNotNone(
            db.get_training_review_item(self.conn, int(source['frame_id']))
        )
        inbox = db.training_review_candidate_inbox_stats(self.conn)
        self.assertEqual(inbox['statuses']['promoted'], 1)

    def test_empty_pending_shells_migrate_to_candidate_inbox_without_data_loss(self):
        image = self.candidate_image()
        nas = FakeNas(image)
        item = self.unified_item(image)
        item['source_id'] = 'legacy-empty-shell'
        worker_candidates.sync_worker_candidates(self.conn, nas, [item])
        self.promote_synced('legacy-empty-shell')
        source = self.conn.execute(
            'SELECT frame_id FROM training_review_sources WHERE source_id=?',
            ('legacy-empty-shell',),
        ).fetchone()
        frame_id = int(source['frame_id'])
        protected_id = self.frame(901)
        db.add_training_review_source(
            self.conn,
            frame_id=protected_id,
            source_type='worker',
            source_id='already-prefilled-source',
        )
        db.add_training_review_source(
            self.conn,
            frame_id=protected_id,
            source_type='new_model_prefill',
            source_id=f'frame:{protected_id}',
        )

        preview = db.migrate_unprefilled_training_review_candidates(
            self.conn, dry_run=True
        )
        self.assertEqual(preview['eligible'], 1)
        self.assertIsNotNone(db.get_training_review_item(self.conn, frame_id))

        migrated = db.migrate_unprefilled_training_review_candidates(
            self.conn, dry_run=False
        )

        self.assertEqual(migrated['migrated'], 1)
        self.assertIsNone(db.get_training_review_item(self.conn, frame_id))
        self.assertIsNotNone(db.get_training_review_item(self.conn, protected_id))
        self.assertEqual(
            self.conn.execute(
                'SELECT COUNT(*) FROM training_review_sources WHERE frame_id=?',
                (frame_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            db.training_review_candidate_inbox_stats(self.conn)['total'], 1
        )

    def test_schema_v3_resync_skips_an_already_indexed_source(self):
        image = self.candidate_image()
        nas = FakeNas(image)
        item = self.unified_item(image)
        worker_candidates.sync_worker_candidates(self.conn, nas, [item])

        result = worker_candidates.sync_worker_candidates(self.conn, nas, [item])

        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(result['inserted'], 0)
        self.assertEqual(result['updated'], 0)
        self.assertEqual(nas.downloads, 1)

    def test_manual_correction_source_imports_authoritative_partial_review(self):
        image = self.candidate_image()
        nas = FakeNas(image)
        item = self.unified_item(image)
        item['source_type'] = 'manual_correction'
        review = {
            'schema_version': 2,
            'source_ids': [item['source_id']],
            'review_status': 'partial',
            'labels': {
                'match_flow_label': 'match_flow',
                'match_mode_label': '3v3',
                'hero_select_label': 'not_select',
                'hero_select_variant': None,
                'hero_select_visibility': None,
                'result_panel_label': 'result_panel',
                'hero_layout_label': 'result_page',
            },
            'result_box': None,
            'result_quality': {},
            'hero_lineup': None,
        }

        worker_candidates.sync_worker_candidates(self.conn, nas, [item])
        pulled = worker_candidates.pull_training_review_reviews(self.conn, [review])

        self.assertEqual(pulled['reviews_pulled'], 1)
        confirmed = db.list_training_review_items(self.conn, status='partial')[0]
        self.assertEqual(confirmed['match_mode_label'], '3v3')
        self.assertIn('manual_correction', confirmed['source_categories'])
        self.assertEqual(confirmed['suggestions']['match_mode']['label'], 'aram')

    def test_confirmed_unified_labels_are_pushed_as_one_sidecar(self):
        image = self.candidate_image()
        nas = ReviewNas(image)
        worker_candidates.sync_worker_candidates(
            self.conn, nas, [self.unified_item(image)]
        )
        frame_id = self.promote_synced()
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
        self.assertEqual(
            review['result_quality'],
            {
                'panel_render_state': 'clear',
                'ocr_usable': 'yes',
                'result_occlusion': 'none',
                'occluder_types': [],
            },
        )
        self.assertEqual(review['source_ids'], ['part-7:12000:test'])
        source = db.get_training_review_item(self.conn, frame_id)['sources'][0]
        self.assertEqual(source['sync_state'], 'clean')

    def test_hero_select_variant_round_trips_through_nas_sidecar(self):
        image = self.candidate_image()
        nas = ReviewNas(image)
        worker_candidates.sync_worker_candidates(
            self.conn, nas, [self.unified_item(image)]
        )
        frame_id = self.promote_synced()
        db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='not_match_flow',
            match_mode_label=None,
            hero_select_label='select_5v5',
            hero_select_variant='bp',
            hero_select_visibility='occluded',
            result_panel_label='no_result_panel',
            hero_layout_label='none',
            status='confirmed',
        )

        pushed = worker_candidates.push_training_review_reviews(self.conn, nas)
        review = nas.reviews[0][1]
        self.assertEqual(pushed['reviews_pushed'], 1)
        self.assertEqual(review['labels']['hero_select_variant'], 'bp')
        self.assertEqual(review['labels']['hero_select_visibility'], 'occluded')

        with self.conn:
            self.conn.execute(
                'UPDATE training_review_items SET hero_select_variant=NULL, '
                'hero_select_visibility=NULL, '
                "review_status='pending' WHERE frame_id = ?",
                (frame_id,),
            )
            self.conn.execute(
                "UPDATE training_review_sources SET sync_state='clean', "
                "remote_review_hash='' WHERE frame_id = ?",
                (frame_id,),
            )

        pulled = worker_candidates.pull_training_review_reviews(self.conn, [review])

        self.assertEqual(pulled['reviews_pulled'], 1)
        restored = db.get_training_review_item(self.conn, frame_id)
        self.assertEqual(restored['hero_select_variant'], 'bp')
        self.assertEqual(restored['hero_select_visibility'], 'occluded')

    def test_confirmed_hero_circles_and_labels_are_pushed_with_sidecar(self):
        image = self.candidate_image()
        nas = ReviewNas(image)
        worker_candidates.sync_worker_candidates(
            self.conn, nas, [self.unified_item(image)]
        )
        frame_id = self.promote_synced()
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
                {'side': slot['side'], 'slot': slot['slot'], 'hero_label': 'Adagio'}
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
        self.assertEqual(review['labels']['hero_layout_label'], 'gameplay_hud')
        self.assertEqual(review['hero_lineup']['team_size'], 3)
        self.assertEqual(len(review['hero_lineup']['slots']), 6)
        self.assertEqual(review['hero_lineup']['slots'][0]['hero_label'], 'Adagio')
        self.assertEqual(review['hero_lineup']['player_side'], 'left')
        self.assertEqual(review['hero_lineup']['player_slot'], 1)
        self.assertEqual(review['hero_lineup']['player_status'], 'identified')

        with self.conn:
            self.conn.execute(
                'DELETE FROM training_review_hero_slots WHERE frame_id = ?', (frame_id,)
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

        pulled = worker_candidates.pull_training_review_reviews(self.conn, [review])

        self.assertEqual(pulled['reviews_pulled'], 1)
        restored = db.get_training_review_item(self.conn, frame_id)
        self.assertEqual(restored['hero_layout_label'], 'gameplay_hud')
        restored_lineup = db.get_training_review_hero_lineup(self.conn, frame_id)
        self.assertEqual(restored_lineup['review_status'], 'confirmed')
        self.assertEqual(restored_lineup['slots'][0]['confirmed_label'], 'Adagio')
        self.assertEqual(restored_lineup['player_side'], 'left')
        self.assertEqual(restored_lineup['player_slot'], 1)
        self.assertEqual(restored_lineup['player_status'], 'identified')

    def test_remote_unified_review_does_not_overwrite_dirty_local_labels(self):
        image = self.candidate_image()
        nas = FakeNas(image)
        worker_candidates.sync_worker_candidates(
            self.conn, nas, [self.unified_item(image)]
        )
        frame_id = self.promote_synced()
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

        result = worker_candidates.pull_training_review_reviews(self.conn, [remote])

        self.assertEqual(result['review_conflicts'], 1)
        item = db.get_training_review_item(self.conn, frame_id)
        self.assertEqual(item['match_flow_label'], 'match_flow')
        self.assertEqual(item['sources'][0]['sync_state'], 'conflict')

    def test_remote_result_positive_restores_its_required_box(self):
        image = self.candidate_image()
        worker_candidates.sync_worker_candidates(
            self.conn, FakeNas(image), [self.unified_item(image)]
        )
        frame_id = self.promote_synced()
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
            'result_quality': {
                'panel_render_state': 'translucent',
                'ocr_usable': 'no',
                'result_occlusion': 'occluded',
                'occluder_types': ['system_device_ui'],
            },
            'notes': '',
            'reviewed_at': '2026-08-09T12:00:00',
        }

        result = worker_candidates.pull_training_review_reviews(self.conn, [remote])

        self.assertEqual(result['reviews_pulled'], 1)
        item = db.get_training_review_item(self.conn, frame_id)
        self.assertEqual(item['result_panel_label'], 'result_panel')
        self.assertIn('result_panel', item['boxes'])
        self.assertEqual(item['panel_render_state'], 'translucent')
        self.assertEqual(item['ocr_usable'], 'no')
        self.assertEqual(item['result_occlusion'], 'occluded')
        self.assertEqual(item['occluder_types'], ['system_device_ui'])


class ResultArchiveNas:
    def __init__(self, content: bytes):
        self.content = content
        self.downloads = 0

    def read_result_frame(self, _relative_path: str) -> bytes:
        self.downloads += 1
        return self.content


class MultipleResultArchiveNas:
    def __init__(self, content_by_path):
        self.content_by_path = content_by_path
        self.downloads = 0

    def read_result_frame(self, relative_path: str) -> bytes:
        self.downloads += 1
        return self.content_by_path[relative_path]


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
                'session_started_at': 1_765_000_000,
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
        self.assertEqual(items[0]['suggestions']['match_flow']['label'], 'match_flow')
        self.assertEqual(items[0]['suggestions']['match_mode']['label'], '3v3')
        self.assertEqual(
            items[0]['sources'][0]['metadata']['suggested_boxes'][0]['w'], 0.8
        )
        self.assertEqual(items[0]['sources'][0]['source_created_at'], 1_765_000_000)

        candidates[0]['hero_slot_count'] = 8
        result_archive.sync_result_archive(self.conn, nas, candidates)
        updated = db.list_training_review_items(self.conn, status='pending')[0]
        self.assertEqual(updated['suggestions']['match_mode']['label'], '5v5')
        self.assertEqual(nas.downloads, 1)

    def test_duplicate_match_records_import_one_result_candidate(self):
        paths = {}
        for name, color in (('first.png', (30, 60, 90)), ('second.png', (31, 61, 91))):
            buffer = io.BytesIO()
            Image.new('RGB', (64, 36), color).save(buffer, format='PNG')
            paths[name] = buffer.getvalue()
        nas = MultipleResultArchiveNas(paths)
        common = {
            'session_id': 9,
            'part_id': 11,
            'part_index': 2,
            'duration_seconds': 900,
            'game_mode': '3v3',
            'hero_slot_count': 6,
            'confidence': 0.87,
            'anchor_name': '测试主播',
            'room_id': 123,
            'title': '测试直播',
        }
        candidates = [
            {
                **common,
                'match_id': 42,
                'result_at_ms': 900_000,
                'result_frame_path': 'first.png',
            },
            {
                **common,
                'match_id': 43,
                'result_at_ms': 900_250,
                'result_frame_path': 'second.png',
            },
        ]

        result = result_archive.sync_result_archive(self.conn, nas, candidates)

        self.assertEqual(result['inserted'], 1)
        self.assertEqual(result['duplicates_skipped'], 1)
        self.assertEqual(nas.downloads, 1)
        self.assertEqual(
            len(db.list_training_review_items(self.conn, status='pending')), 1
        )


if __name__ == '__main__':
    unittest.main()
