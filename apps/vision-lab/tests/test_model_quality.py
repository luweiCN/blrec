"""按模型版本统计预打标与人工真值的差异。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import config, db, model_quality  # noqa: E402


class TestModelQuality:
    def setup_method(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_frame_dir = config.FRAME_DIR
        self.old_thumb_dir = config.THUMB_DIR
        config.FRAME_DIR = self.root / 'frames'
        config.THUMB_DIR = self.root / 'thumbs'
        self.conn = db.connect(self.root / 'lab.db')
        self.video_id = db.upsert_video(
            self.conn,
            remote_path='/nas/model-quality.flv',
            streamer='模型质量主播',
            room_id='quality',
            filename='model-quality.flv',
            duration_seconds=100,
            size_bytes=1,
        )

    def teardown_method(self):
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

    def test_core_model_versions_remain_comparable_after_new_prefill(self):
        frame_id = self.frame(1)
        db.add_training_review_source(
            self.conn, frame_id=frame_id, source_type='worker', source_id='worker-1'
        )
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='new_model_prefill',
            source_id=f'frame:{frame_id}',
            suggestions={
                'match_flow': {'label': 'match_flow', 'confidence': 0.99},
                'match_mode': {'label': 'aram', 'confidence': 0.96},
                'hero_select': {'label': 'not_select', 'confidence': 0.98},
                'result_panel': {'label': 'no_result_panel', 'confidence': 0.97},
            },
            metadata={
                'model_runs': {
                    'match_flow': 'match-flow-20260811-old',
                    'match_mode': 'match-mode-20260811-old',
                    'hero_select': 'hero-select-20260811-old',
                    'result_detector': 'result-detector-20260811-old',
                }
            },
        )
        db.save_training_review(
            self.conn,
            frame_id=frame_id,
            match_flow_label='match_flow',
            match_mode_label='3v3',
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            status='confirmed',
        )

        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='new_model_prefill',
            source_id=f'frame:{frame_id}',
            suggestions={
                'match_flow': {'label': 'match_flow', 'confidence': 0.99},
                'match_mode': {'label': '3v3', 'confidence': 0.94},
                'hero_select': {'label': 'not_select', 'confidence': 0.98},
                'result_panel': {'label': 'no_result_panel', 'confidence': 0.97},
            },
            metadata={
                'model_runs': {
                    'match_flow': 'match-flow-20260822-new',
                    'match_mode': 'match-mode-20260822-new',
                    'hero_select': 'hero-select-20260822-new',
                    'result_detector': 'result-detector-20260822-new',
                }
            },
        )

        quality = model_quality.summary(self.conn)
        match_mode = next(
            task for task in quality['tasks'] if task['id'] == 'match_mode'
        )
        versions = {version['run_id']: version for version in match_mode['versions']}

        assert versions['match-mode-20260811-old']['compared'] == 1
        assert versions['match-mode-20260811-old']['wrong'] == 1
        assert versions['match-mode-20260811-old']['high_confidence_wrong'] == 1
        assert versions['match-mode-20260822-new']['correct'] == 1
        assert match_mode['latest_run_id'] == 'match-mode-20260822-new'
        assert versions['match-mode-20260811-old']['contexts'][0]['wrong'] == 1
        latest = model_quality.latest_issue_rates(self.conn)
        assert latest[('match_mode', '', '3v3')]['run_id'] == (
            'match-mode-20260822-new'
        )
        assert latest[('match_mode', '', '3v3')]['wrong'] == 0

    def test_hero_and_player_predictions_are_grouped_by_model_run(self):
        frame_id = self.frame(2)
        db.add_training_review_source(
            self.conn, frame_id=frame_id, source_type='worker', source_id='worker-2'
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
                'suggested_label': 'Adagio',
                'suggestion_confidence': 0.95,
            }
            for side in ('left', 'right')
            for slot in range(1, 4)
        ]
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=frame_id,
            screen_type='scoreboard',
            team_size=3,
            method='new-model-cascade-worker-v1',
            slots=slots,
        )
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='new_model_hero_prefill',
            source_id=f'frame:{frame_id}',
            metadata={
                'screen_type': 'scoreboard',
                'team_size': 3,
                'complete': True,
                'detected': 6,
                'player_suggestion': {'side': 'left', 'slot': 1, 'confidence': 0.91},
                'model_runs': {
                    'hero_avatar_detector': 'avatar-20260822-new',
                    'hero_identity': 'identity-20260822-new',
                    'player_position': 'player-20260822-new',
                },
            },
        )
        db.save_training_review_hero_lineup(
            self.conn,
            frame_id=frame_id,
            labels=[
                {
                    'side': slot['side'],
                    'slot': slot['slot'],
                    'hero_label': (
                        'Alpha'
                        if slot['side'] == 'left' and slot['slot'] == 1
                        else 'Adagio'
                    ),
                }
                for slot in slots
            ],
            allowed_labels={'Adagio', 'Alpha'},
            player_side='left',
            player_slot=2,
        )

        quality = model_quality.summary(self.conn)
        tasks = {task['id']: task for task in quality['tasks']}
        identity = tasks['hero_identity']['versions'][0]
        player = tasks['player_position']['versions'][0]
        avatar = tasks['hero_avatar_detector']['versions'][0]

        assert identity['compared'] == 6
        assert identity['correct'] == 5
        assert identity['wrong'] == 1
        assert player['compared'] == 1
        assert player['wrong'] == 1
        assert avatar['metric'] == 'complete_rate'
        assert avatar['correct'] == 1

    def test_afk_predictions_stay_separate_from_truth_and_feed_model_quality(self):
        frame_id = self.frame(3)
        db.add_training_review_source(
            self.conn, frame_id=frame_id, source_type='worker', source_id='worker-3'
        )
        slots = [
            {
                'side': side,
                'slot': slot,
                'crop': {
                    'x': 0.42 if side == 'left' else 0.54,
                    'y': 0.15 + slot * 0.18,
                    'w': 0.04,
                    'h': 0.07,
                },
                'suggested_label': 'Adagio',
                'suggestion_confidence': 0.9,
            }
            for side in ('left', 'right')
            for slot in range(1, 4)
        ]
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=frame_id,
            screen_type='result_page',
            team_size=3,
            method='test',
            slots=slots,
        )

        candidate = db.next_training_review_afk_candidate(self.conn, 'afk-run-v1')
        assert candidate is not None
        assert all(slot['is_afk'] is None for slot in candidate['slots'])
        predicted = [
            {
                'side': slot['side'],
                'slot': slot['slot'],
                'afk_prediction_label': (
                    'afk' if slot['side'] == 'left' and slot['slot'] == 1 else 'active'
                ),
                'afk_prediction_probability': (
                    0.94 if slot['side'] == 'left' and slot['slot'] == 1 else 0.04
                ),
            }
            for slot in slots
        ]
        db.apply_training_review_afk_predictions(
            self.conn, frame_id=frame_id, model_run_id='afk-run-v1', slots=predicted
        )
        lineup = db.get_training_review_hero_lineup(self.conn, frame_id)
        assert lineup is not None
        assert all(slot['is_afk'] is None for slot in lineup['slots'])
        assert db.training_review_afk_prediction_frame_ids(self.conn, 'afk') == {
            frame_id
        }
        filtered, total = db.training_review_page(
            self.conn, status='all', source_scope='all', afk_prediction='afk', limit=10
        )
        assert total == 1
        assert [item['frame_id'] for item in filtered] == [frame_id]

        db.save_training_review_hero_lineup(
            self.conn,
            frame_id=frame_id,
            labels=[
                {
                    'side': slot['side'],
                    'slot': slot['slot'],
                    'hero_label': 'Adagio',
                    'is_afk': slot['side'] == 'left' and slot['slot'] == 1,
                }
                for slot in slots
            ],
            allowed_labels={'Adagio'},
        )

        quality = model_quality.summary(self.conn)
        afk = next(task for task in quality['tasks'] if task['id'] == 'afk_status')
        version = afk['versions'][0]
        assert version['run_id'] == 'afk-run-v1'
        assert version['compared'] == 6
        assert version['correct'] == 6

    def test_rebuild_restores_existing_model_outcomes(self):
        self.test_core_model_versions_remain_comparable_after_new_prefill()
        self.conn.execute('DELETE FROM training_review_model_outcomes')
        self.conn.commit()

        rebuilt = model_quality.rebuild(self.conn)
        quality = model_quality.summary(self.conn)

        assert rebuilt['frames'] == 1
        assert rebuilt['outcomes'] == 4
        assert any(task['id'] == 'match_mode' for task in quality['tasks'])

    def test_material_targets_are_fixed_instead_of_chasing_largest_mode(self):
        timestamp = db.now()
        frame_ids = db.add_frames(
            self.conn,
            self.video_id,
            [
                {
                    'timestamp_ms': index * 1_000,
                    'width': 1280,
                    'height': 720,
                    'sha256': f'{index + 1000:064x}',
                    'phash': '',
                    'frame_path': str(self.root / f'bulk-{index}.jpg'),
                    'thumb_path': '',
                    'strategy': 'test',
                    'model_source': '',
                    'model_confidence': None,
                }
                for index in range(172)
            ],
        )
        self.conn.executemany(
            'INSERT INTO training_review_items('
            'frame_id,match_flow_label,hero_select_label,result_panel_label,'
            'review_status,created_at,updated_at,reviewed_at) '
            "VALUES(?,'not_match_flow',?,'no_result_panel','confirmed',?,?,?)",
            [
                (
                    frame_id,
                    (
                        'select_3v3'
                        if index == 0
                        else ('select_aram' if index == 1 else 'select_5v5')
                    ),
                    timestamp,
                    timestamp,
                    timestamp,
                )
                for index, frame_id in enumerate(frame_ids)
            ],
        )
        self.conn.commit()

        suggestions = db.training_review_material_suggestions(self.conn)
        rows = {
            item['match_mode']: item
            for item in suggestions
            if item['kind'] == 'scene_mode' and item['scene'] == 'hero_select'
        }

        assert rows['3v3']['target_count'] == 100
        assert rows['aram']['target_count'] == 100
        assert rows['5v5']['target_count'] == 100
        assert rows['5v5']['status'] == 'sufficient'
        assert rows['3v3']['target_count'] < rows['5v5']['confirmed_count']

        self.conn.executemany(
            'INSERT INTO training_review_model_outcomes('
            'frame_id,task_id,model_run_id,subject_key,metric,'
            'predicted_label,confirmed_label,confidence,screen_type,'
            'match_mode,is_correct,source_type,created_at,updated_at) '
            "VALUES(?,'hero_select','hero-select-20260822-current','frame',"
            "'accuracy',?,'select_5v5',0.96,'hero_select','5v5',?,"
            "'new_model_prefill',?,?)",
            [
                (
                    frame_id,
                    'select_aram' if index < 4 else 'select_5v5',
                    0 if index < 4 else 1,
                    timestamp,
                    timestamp,
                )
                for index, frame_id in enumerate(frame_ids[2:22])
            ],
        )
        self.conn.commit()

        suggestions = db.training_review_material_suggestions(self.conn)
        current_5v5 = next(
            item
            for item in suggestions
            if item['kind'] == 'scene_mode'
            and item['scene'] == 'hero_select'
            and item['match_mode'] == '5v5'
        )

        assert current_5v5['confirmed_count'] >= current_5v5['target_count']
        assert current_5v5['status'] == 'model_errors'
        assert current_5v5['model_quality']['compared'] == 20
        assert current_5v5['model_quality']['wrong'] == 4
