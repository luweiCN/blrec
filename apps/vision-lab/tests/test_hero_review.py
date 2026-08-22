"""积分板／结算图英雄阵容预填与人工纠错。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import config, db, export, hero_review, training  # noqa: E402


class HeroReviewTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / 'lab.db')
        video_id = db.upsert_video(
            self.conn,
            remote_path='/nas/sample.flv',
            streamer='测试主播',
            room_id='1',
            filename='sample.flv',
            duration_seconds=100,
            size_bytes=1,
        )
        frame_path = self.root / 'frame.jpg'
        Image.new('RGB', (1280, 720), '#222222').save(frame_path)
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
                    'frame_path': str(frame_path),
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
            source_id='part-1:1000:test',
            suggestions={},
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    @staticmethod
    def slots(label: str = 'Adagio'):
        return [
            {
                'side': side,
                'slot': slot,
                'crop': {
                    'x': 0.4 if side == 'left' else 0.5,
                    'y': 0.2 + slot * 0.1,
                    'w': 0.06,
                    'h': 0.1,
                },
                'suggested_label': label,
                'suggestion_confidence': 0.8,
            }
            for side in ('left', 'right')
            for slot in range(1, 4)
        ]


class TestHeroReviewStorage(HeroReviewTestCase):
    def test_legacy_lineup_schema_is_migrated_without_losing_slots(self):
        with self.conn:
            self.conn.execute('DROP TABLE training_review_hero_slots')
            self.conn.execute('DROP TABLE training_review_hero_lineups')
            self.conn.executescript(
                """
                CREATE TABLE training_review_hero_lineups (
                    frame_id INTEGER PRIMARY KEY,
                    screen_type TEXT NOT NULL CHECK (
                        screen_type IN ('scoreboard', 'result_page')),
                    team_size INTEGER NOT NULL CHECK (team_size IN (3, 5)),
                    suggestion_method TEXT NOT NULL DEFAULT '',
                    review_status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_at TEXT
                );
                CREATE TABLE training_review_hero_slots (
                    frame_id INTEGER NOT NULL,
                    side TEXT NOT NULL,
                    slot INTEGER NOT NULL,
                    crop_x REAL NOT NULL,
                    crop_y REAL NOT NULL,
                    crop_w REAL NOT NULL,
                    crop_h REAL NOT NULL,
                    suggested_label TEXT NOT NULL DEFAULT '',
                    suggestion_confidence REAL NOT NULL DEFAULT 0,
                    confirmed_label TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (frame_id, side, slot)
                );
                """
            )
            timestamp = db.now()
            self.conn.execute(
                'INSERT INTO training_review_hero_lineups VALUES '
                "(?, 'result_page', 3, 'sift-v1', 'pending', ?, ?, NULL)",
                (self.frame_id, timestamp, timestamp),
            )
            self.conn.execute(
                'INSERT INTO training_review_hero_slots VALUES '
                "(?, 'left', 1, 0.1, 0.2, 0.05, 0.08, "
                "'Adagio', 0.8, NULL, ?)",
                (self.frame_id, timestamp),
            )
        self.conn.close()

        self.conn = db.connect(self.root / 'lab.db')

        migrated = db.get_training_review_hero_lineup(self.conn, self.frame_id)
        self.assertEqual(migrated['slots'][0]['suggested_label'], 'Adagio')
        self.assertIsNone(migrated['player_side'])
        self.assertIsNone(migrated['player_slot'])
        self.assertEqual(migrated['player_status'], 'pending')
        hud = db.replace_training_review_hero_layout(
            self.conn,
            frame_id=self.frame_id,
            screen_type='gameplay_hud',
            team_size=3,
            method='manual-circle-v1',
            slots=self.slots()[:1],
        )
        self.assertEqual(hud['screen_type'], 'gameplay_hud')

    def test_suggestions_and_human_labels_stay_separate(self):
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=self.frame_id,
            screen_type='result_page',
            team_size=3,
            method='sift-v1',
            slots=self.slots(),
        )

        pending = db.get_training_review_hero_lineup(self.conn, self.frame_id)
        self.assertEqual(pending['review_status'], 'pending')
        self.assertEqual(pending['slots'][0]['suggested_label'], 'Adagio')
        self.assertIsNone(pending['slots'][0]['confirmed_label'])

        labels = [
            {
                'side': slot['side'],
                'slot': slot['slot'],
                'hero_label': 'Alpha' if slot['slot'] == 1 else 'Adagio',
            }
            for slot in pending['slots']
        ]
        confirmed = db.save_training_review_hero_lineup(
            self.conn,
            frame_id=self.frame_id,
            labels=labels,
            allowed_labels={'Adagio', 'Alpha'},
        )

        self.assertEqual(confirmed['review_status'], 'confirmed')
        self.assertEqual(confirmed['slots'][0]['confirmed_label'], 'Alpha')
        self.assertEqual(confirmed['slots'][0]['suggested_label'], 'Adagio')

    def test_saving_lineup_reuses_the_initial_read(self):
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=self.frame_id,
            screen_type='scoreboard',
            team_size=3,
            method='sift-v1',
            slots=self.slots(),
        )
        labels = [
            {'side': slot['side'], 'slot': slot['slot'], 'hero_label': 'Adagio'}
            for slot in self.slots()
        ]

        with mock.patch.object(
            db,
            'get_training_review_hero_lineup',
            wraps=db.get_training_review_hero_lineup,
        ) as load_lineup:
            confirmed = db.save_training_review_hero_lineup(
                self.conn,
                frame_id=self.frame_id,
                labels=labels,
                allowed_labels={'Adagio'},
            )

        self.assertEqual(load_lineup.call_count, 1)
        self.assertEqual(confirmed['review_status'], 'confirmed')

    def test_lineup_requires_every_expected_slot(self):
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=self.frame_id,
            screen_type='scoreboard',
            team_size=3,
            method='sift-v1',
            slots=self.slots(),
        )

        with self.assertRaisesRegex(ValueError, '6 个英雄位置'):
            db.save_training_review_hero_lineup(
                self.conn,
                frame_id=self.frame_id,
                labels=[{'side': 'left', 'slot': 1, 'hero_label': 'Adagio'}],
                allowed_labels={'Adagio'},
            )

    def test_player_hero_position_is_saved_and_validated(self):
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=self.frame_id,
            screen_type='result_page',
            team_size=3,
            method='sift-v1',
            slots=self.slots(),
        )
        labels = [
            {'side': slot['side'], 'slot': slot['slot'], 'hero_label': 'Adagio'}
            for slot in self.slots()
        ]

        confirmed = db.save_training_review_hero_lineup(
            self.conn,
            frame_id=self.frame_id,
            labels=labels,
            allowed_labels={'Adagio'},
            player_side='right',
            player_slot=2,
        )

        self.assertEqual(confirmed['player_side'], 'right')
        self.assertEqual(confirmed['player_slot'], 2)
        self.assertEqual(confirmed['player_status'], 'identified')
        moved = db.replace_training_review_hero_layout(
            self.conn,
            frame_id=self.frame_id,
            screen_type='result_page',
            team_size=3,
            method='manual-circle-v1',
            slots=self.slots(),
        )
        self.assertEqual(moved['player_side'], 'right')
        self.assertEqual(moved['player_slot'], 2)
        removed = db.replace_training_review_hero_layout(
            self.conn,
            frame_id=self.frame_id,
            screen_type='result_page',
            team_size=3,
            method='manual-circle-v1',
            slots=[
                slot
                for slot in self.slots()
                if not (slot['side'] == 'right' and slot['slot'] == 2)
            ],
        )
        self.assertIsNone(removed['player_side'])
        self.assertIsNone(removed['player_slot'])
        self.assertEqual(removed['player_status'], 'pending')
        with self.assertRaisesRegex(ValueError, '主播英雄位置无效'):
            db.save_training_review_hero_lineup(
                self.conn,
                frame_id=self.frame_id,
                labels=labels,
                allowed_labels={'Adagio'},
                player_side='left',
                player_slot=4,
            )

    def test_player_position_can_be_marked_unreadable(self):
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=self.frame_id,
            screen_type='scoreboard',
            team_size=3,
            method='sift-v1',
            slots=self.slots(),
        )
        labels = [
            {'side': slot['side'], 'slot': slot['slot'], 'hero_label': 'unreadable'}
            for slot in self.slots()
        ]

        confirmed = db.save_training_review_hero_lineup(
            self.conn,
            frame_id=self.frame_id,
            labels=labels,
            allowed_labels={'Adagio'},
            player_status='unreadable',
        )

        self.assertEqual(confirmed['player_status'], 'unreadable')
        self.assertIsNone(confirmed['player_side'])
        self.assertIsNone(confirmed['player_slot'])
        with self.assertRaisesRegex(ValueError, '主播英雄位置状态冲突'):
            db.save_training_review_hero_lineup(
                self.conn,
                frame_id=self.frame_id,
                labels=labels,
                allowed_labels={'Adagio'},
                player_status='unreadable',
                player_side='left',
                player_slot=1,
            )

    def test_manual_hud_layout_can_be_saved_one_circle_at_a_time(self):
        first = self.slots()[0]

        pending = db.replace_training_review_hero_layout(
            self.conn,
            frame_id=self.frame_id,
            screen_type='gameplay_hud',
            team_size=3,
            method='manual-circle-v1',
            slots=[first],
        )

        self.assertEqual(pending['screen_type'], 'gameplay_hud')
        self.assertEqual(len(pending['slots']), 1)
        self.assertIsNone(pending['slots'][0]['confirmed_label'])

        cleared = db.replace_training_review_hero_layout(
            self.conn,
            frame_id=self.frame_id,
            screen_type='gameplay_hud',
            team_size=3,
            method='manual-circle-v1',
            slots=[],
        )
        self.assertEqual(cleared['slots'], [])

    def test_complete_layout_template_is_scoped_by_streamer_screen_and_ratio(self):
        layout_key = db.hero_layout_key(1280, 720)

        saved = db.save_training_review_hero_template(
            self.conn,
            streamer='测试主播',
            screen_type='gameplay_hud',
            team_size=3,
            layout_key=layout_key,
            slots=self.slots(),
        )

        self.assertEqual(len(saved['slots']), 6)
        loaded = db.get_training_review_hero_template(
            self.conn,
            streamer='测试主播',
            screen_type='gameplay_hud',
            team_size=3,
            layout_key=layout_key,
        )
        self.assertEqual(loaded['id'], saved['id'])
        self.assertIsNone(
            db.get_training_review_hero_template(
                self.conn,
                streamer='另一个主播',
                screen_type='gameplay_hud',
                team_size=3,
                layout_key=layout_key,
            )
        )
        self.assertIsNone(
            db.get_training_review_hero_template(
                self.conn,
                streamer='测试主播',
                screen_type='scoreboard',
                team_size=3,
                layout_key=layout_key,
            )
        )

        with self.assertRaisesRegex(ValueError, '6 个英雄位置'):
            db.save_training_review_hero_template(
                self.conn,
                streamer='测试主播',
                screen_type='gameplay_hud',
                team_size=3,
                layout_key=layout_key,
                slots=self.slots()[:-1],
            )

    def test_complete_layout_still_saves_when_streamer_name_is_empty(self):
        from labeler import server

        with self.conn:
            self.conn.execute(
                'UPDATE videos SET streamer = ? WHERE id = '
                '(SELECT video_id FROM frames WHERE id = ?)',
                ('', self.frame_id),
            )

        def recognize(_conn, _path, slots, **_kwargs):
            return {
                'complete': True,
                'slots': [
                    {**slot, 'suggested_label': 'Adagio', 'suggestion_confidence': 0.8}
                    for slot in slots
                ],
                'model_runs': {'hero_identity': 'hero-identity-run'},
                'player_suggestion': None,
            }

        with (
            mock.patch.object(
                server, '_conn', side_effect=lambda: db.connect(self.root / 'lab.db')
            ),
            mock.patch.object(
                server.model_prefill, 'prefill_hero_slots', side_effect=recognize
            ),
        ):
            lineup = server.api_save_training_review_hero_layout(
                self.frame_id,
                {
                    'screen_type': 'result_page',
                    'team_size': 3,
                    'slots': self.slots(),
                    'recognize': True,
                    'save_template': True,
                },
            )

        self.assertEqual(len(lineup['slots']), 6)
        self.assertNotIn('template_saved', lineup)
        saved = db.get_training_review_hero_lineup(self.conn, self.frame_id)
        self.assertEqual(len(saved['slots']), 6)

    def test_complete_layout_does_not_save_cross_frame_template(self):
        from labeler import server

        with self.conn:
            self.conn.execute(
                'UPDATE frames SET width=0,height=0 WHERE id=?', (self.frame_id,)
            )

        with (
            mock.patch.object(config, 'CONTROL_PLANE_ONLY', True),
            mock.patch.object(
                server, '_conn', side_effect=lambda: db.connect(self.root / 'lab.db')
            ),
            mock.patch.object(
                server.model_prefill,
                'latest_model_specs',
                return_value={
                    'hero_identity': {
                        'run_id': 'hero-identity-run',
                        'metadata': {},
                        'artifact_size': 1,
                    },
                    'player_position': {
                        'run_id': 'player-position-run',
                        'metadata': {},
                        'artifact_size': 1,
                    },
                },
            ),
        ):
            lineup = server.api_save_training_review_hero_layout(
                self.frame_id,
                {
                    'screen_type': 'gameplay_hud',
                    'team_size': 3,
                    'slots': self.slots(),
                    'recognize': True,
                    'save_template': True,
                    'image_width': 1280,
                    'image_height': 720,
                },
            )

        self.assertFalse(lineup.get('template_saved', False))
        self.assertEqual(lineup['prefill_job']['payload']['operation'], 'hero_slots')
        dimensions = self.conn.execute(
            'SELECT width,height FROM frames WHERE id=?', (self.frame_id,)
        ).fetchone()
        self.assertEqual((dimensions['width'], dimensions['height']), (1280, 720))
        template = db.get_training_review_hero_template(
            self.conn,
            streamer='测试主播',
            screen_type='gameplay_hud',
            team_size=3,
            layout_key=db.hero_layout_key(1280, 720),
        )
        self.assertIsNone(template)

    def test_control_plane_only_queues_ai_after_explicit_recognition(self):
        from labeler import server

        models = {
            task_id: {'run_id': f'{task_id}-run', 'metadata': {}, 'artifact_size': 1}
            for task_id in server.model_prefill.HERO_PREFILL_TASKS
        }
        with (
            mock.patch.object(config, 'CONTROL_PLANE_ONLY', True),
            mock.patch.object(
                server, '_conn', side_effect=lambda: db.connect(self.root / 'lab.db')
            ),
            mock.patch.object(
                server.model_prefill, 'latest_model_specs', return_value=models
            ) as latest_models,
            mock.patch.object(
                server, '_queue_model_prefill', return_value={'id': 'hero-job'}
            ) as queue_prefill,
        ):
            loaded = server.api_training_review_hero_lineup(
                self.frame_id, screen_type='gameplay_hud', team_size=3, recognize=False
            )
            recognized = server.api_training_review_hero_lineup(
                self.frame_id,
                screen_type='gameplay_hud',
                team_size=3,
                recognize=True,
                refresh=True,
            )

        self.assertNotIn('prefill_job', loaded)
        self.assertEqual(loaded['slots'], [])
        self.assertEqual(recognized['prefill_job']['id'], 'hero-job')
        latest_models.assert_called_once()
        queue_prefill.assert_called_once()
        self.assertEqual(queue_prefill.call_args.kwargs['operation'], 'hero_lineup')

    def test_explicit_ai_ignores_saved_template_and_runs_detector(self):
        from labeler import server

        old_slots = self.slots()
        old_slots[-1]['crop']['w'] = 0.03
        old_slots[-1]['crop']['h'] = 0.05
        db.replace_training_review_hero_layout(
            self.conn,
            frame_id=self.frame_id,
            screen_type='result_page',
            team_size=3,
            method='layout-template+sift-v1',
            slots=old_slots,
        )
        new_slots = self.slots()
        db.save_training_review_hero_template(
            self.conn,
            streamer='测试主播',
            screen_type='result_page',
            team_size=3,
            layout_key=db.hero_layout_key(1280, 720),
            slots=new_slots,
        )
        with self.conn:
            self.conn.execute(
                "UPDATE training_review_hero_lineups SET updated_at = "
                "'2026-08-09T12:00:00' WHERE frame_id = ?",
                (self.frame_id,),
            )
            self.conn.execute(
                "UPDATE training_review_hero_templates SET updated_at = "
                "'2026-08-09T12:01:00' WHERE streamer = '测试主播'"
            )

        detected_slots = self.slots()
        detected_slots[-1]['crop']['w'] = 0.07

        def recognize(_conn, _path, **_kwargs):
            return {
                'complete': True,
                'slots': [
                    {**slot, 'suggested_label': 'Adagio', 'suggestion_confidence': 0.8}
                    for slot in detected_slots
                ],
                'model_runs': {'hero_identity': 'hero-identity-run'},
                'player_suggestion': None,
                'detected': 6,
            }

        with (
            mock.patch.object(
                server, '_conn', side_effect=lambda: db.connect(self.root / 'lab.db')
            ),
            mock.patch.object(
                server.model_prefill, 'prefill_hero_lineup', side_effect=recognize
            ) as detect,
            mock.patch.object(server.model_prefill, 'prefill_hero_slots') as classify,
        ):
            lineup = server.api_training_review_hero_lineup(
                self.frame_id,
                screen_type='result_page',
                team_size=3,
                recognize=True,
                refresh=True,
            )

        self.assertEqual(lineup['slots'][-1]['crop']['w'], 0.07)
        detect.assert_called_once()
        classify.assert_not_called()

    def test_newer_template_does_not_replace_pending_manual_layout(self):
        from labeler import server

        manual_slots = self.slots()
        manual_slots[-1]['crop']['w'] = 0.03
        manual_slots[-1]['crop']['h'] = 0.05
        db.replace_training_review_hero_layout(
            self.conn,
            frame_id=self.frame_id,
            screen_type='result_page',
            team_size=3,
            method='manual-circle-v1',
            slots=manual_slots,
        )
        db.save_training_review_hero_template(
            self.conn,
            streamer='测试主播',
            screen_type='result_page',
            team_size=3,
            layout_key=db.hero_layout_key(1280, 720),
            slots=self.slots(),
        )
        with self.conn:
            self.conn.execute(
                "UPDATE training_review_hero_lineups SET updated_at = "
                "'2026-08-09T12:00:00' WHERE frame_id = ?",
                (self.frame_id,),
            )
            self.conn.execute(
                "UPDATE training_review_hero_templates SET updated_at = "
                "'2026-08-09T12:01:00' WHERE streamer = '测试主播'"
            )

        with mock.patch.object(
            server, '_conn', side_effect=lambda: db.connect(self.root / 'lab.db')
        ):
            lineup = server.api_training_review_hero_lineup(
                self.frame_id, screen_type='result_page', team_size=3, refresh=False
            )

        self.assertEqual(lineup['slots'][-1]['crop']['w'], 0.03)


class TestHeroTrainingExport(unittest.TestCase):
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

    @staticmethod
    def _slots():
        return [
            {
                'side': side,
                'slot': slot,
                'crop': {
                    'x': 0.40 if side == 'left' else 0.52,
                    'y': 0.15 + slot * 0.16,
                    'w': 0.06,
                    'h': 0.106667,
                },
                'suggested_label': 'Adagio',
                'suggestion_confidence': 0.8,
            }
            for side in ('left', 'right')
            for slot in range(1, 4)
        ]

    @staticmethod
    def _player_slots(team_size: int):
        return [
            {
                'side': side,
                'slot': slot,
                'crop': {
                    'x': 0.12 + slot * 0.07 + (0.42 if side == 'right' else 0),
                    'y': 0.2,
                    'w': 0.05,
                    'h': 0.09,
                },
                'suggested_label': 'Adagio',
                'suggestion_confidence': 0.8,
            }
            for side in ('left', 'right')
            for slot in range(1, team_size + 1)
        ]

    def _confirmed_lineup(self, video_id: int, index: int, screen_type: str) -> int:
        path = self.root / f'frame-{index}.jpg'
        Image.new('RGB', (1280, 720), (index * 10, 40, 60)).save(path)
        frame_id = db.add_frames(
            self.conn,
            video_id,
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
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id=f'part-{video_id}:{index}',
            suggestions={},
        )
        slots = self._slots()
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=frame_id,
            screen_type=screen_type,
            team_size=3,
            method='test',
            slots=slots,
        )
        labels = [
            {
                'side': slot['side'],
                'slot': slot['slot'],
                'hero_label': (
                    'unreadable'
                    if index == 1 and slot['side'] == 'left' and slot['slot'] == 1
                    else 'Alpha' if slot['slot'] % 2 else 'Adagio'
                ),
            }
            for slot in slots
        ]
        db.save_training_review_hero_lineup(
            self.conn,
            frame_id=frame_id,
            labels=labels,
            allowed_labels={'Adagio', 'Alpha'},
        )
        return frame_id

    def test_confirmed_boxes_and_readable_heroes_export_to_separate_tasks(self):
        videos = [
            db.upsert_video(
                self.conn,
                remote_path=f'/nas/{index}.flv',
                streamer=f'主播{index}',
                room_id=str(index),
                filename=f'{index}.flv',
                duration_seconds=100,
                size_bytes=1,
            )
            for index in (1, 2)
        ]
        index = 1
        for video_id in videos:
            for screen_type in ('gameplay_hud', 'scoreboard', 'result_page'):
                self._confirmed_lineup(video_id, index, screen_type)
                index += 1

        summaries = {item['id']: item for item in training.task_summaries(self.conn)}
        avatar = export.export_hero_avatar_detector(self.conn)
        identity = export.export_hero_identity_classifier(self.conn)

        self.assertTrue(summaries['hero_avatar_detector']['ready'])
        self.assertTrue(summaries['hero_identity']['ready'])
        self.assertEqual(avatar['total'], 6)
        self.assertEqual(avatar['boxes'], 36)
        self.assertEqual(identity['total'], 35)
        self.assertEqual(identity['excluded_unreadable'], 1)
        avatar_samples = [
            json.loads(line)
            for line in (Path(avatar['dir']) / 'samples.jsonl')
            .read_text(encoding='utf-8')
            .splitlines()
        ]
        first_avatar = avatar_samples[0]
        detector_labels = (
            (
                Path(avatar['dir'])
                / 'labels'
                / first_avatar['split']
                / f"{first_avatar['sample_id']}.txt"
            )
            .read_text(encoding='utf-8')
            .splitlines()
        )
        self.assertEqual(len(detector_labels), 6)
        identity_samples = [
            json.loads(line)
            for line in (Path(identity['dir']) / 'samples.jsonl')
            .read_text(encoding='utf-8')
            .splitlines()
        ]
        self.assertNotIn('unreadable', {sample['label'] for sample in identity_samples})
        first_identity = identity_samples[0]
        crop = (
            Path(identity['dir'])
            / 'images'
            / first_identity['split']
            / first_identity['label']
            / f"{first_identity['sample_id']}.jpg"
        )
        self.assertTrue(crop.is_file())
        with Image.open(crop) as image:
            self.assertGreaterEqual(min(image.size), 70)

    def test_player_position_exports_full_panels_as_an_independent_task(self):
        positions = [
            ('left', 1),
            ('left', 2),
            ('left', 3),
            ('left', 4),
            ('left', 5),
            ('right', 1),
            ('right', 2),
            ('right', 3),
        ]
        index = 100
        for video_number in range(1, 4):
            video_id = db.upsert_video(
                self.conn,
                remote_path=f'/nas/player-{video_number}.flv',
                streamer=f'主播{video_number}',
                room_id=str(video_number),
                filename=f'player-{video_number}.flv',
                duration_seconds=100,
                size_bytes=1,
            )
            for side, slot in positions:
                index += 1
                team_size = 5 if slot > 3 else 3
                frame_path = self.root / f'player-frame-{index}.jpg'
                Image.new('RGB', (1280, 720), (index % 255, 80, 120)).save(frame_path)
                frame_id = db.add_frames(
                    self.conn,
                    video_id,
                    [
                        {
                            'timestamp_ms': index * 1_000,
                            'width': 1280,
                            'height': 720,
                            'sha256': f'{index:064x}',
                            'phash': '',
                            'frame_path': str(frame_path),
                            'thumb_path': '',
                            'strategy': 'test',
                            'model_source': '',
                            'model_confidence': None,
                        }
                    ],
                )[0]
                db.add_training_review_source(
                    self.conn,
                    frame_id=frame_id,
                    source_type='worker',
                    source_id=f'player-{video_number}:{index}',
                    suggestions={},
                )
                slots = self._player_slots(team_size)
                db.replace_training_review_hero_suggestions(
                    self.conn,
                    frame_id=frame_id,
                    screen_type=('scoreboard' if index % 2 else 'result_page'),
                    team_size=team_size,
                    method='test',
                    slots=slots,
                )
                db.save_training_review_hero_lineup(
                    self.conn,
                    frame_id=frame_id,
                    labels=[
                        {
                            'side': value['side'],
                            'slot': value['slot'],
                            'hero_label': 'Adagio',
                        }
                        for value in slots
                    ],
                    allowed_labels={'Adagio'},
                    player_side=side,
                    player_slot=slot,
                )

        summary = next(
            item
            for item in training.task_summaries(self.conn)
            if item['id'] == 'player_position'
        )
        snapshot = export.export_player_position_classifier(self.conn)

        self.assertTrue(summary['ready'])
        self.assertEqual(summary['counts']['total'], 24)
        self.assertEqual(snapshot['classes'], 8)
        self.assertEqual(snapshot['by_split'], {'train': 8, 'val': 8, 'test': 8})
        samples = [
            json.loads(line)
            for line in (Path(snapshot['dir']) / 'samples.jsonl')
            .read_text(encoding='utf-8')
            .splitlines()
        ]
        self.assertEqual(
            {sample['label'] for sample in samples}, set(export.PLAYER_POSITION_LABELS)
        )
        first = samples[0]
        exported_image = (
            Path(snapshot['dir'])
            / 'images'
            / first['split']
            / first['label']
            / f"{first['sample_id']}.jpg"
        )
        with Image.open(exported_image) as image:
            self.assertEqual(image.size, (1280, 720))


class TestHeroReviewInference(unittest.TestCase):
    def test_bundled_hero_catalog_does_not_require_blrec_package(self):
        with mock.patch.object(
            hero_review,
            '_shared',
            side_effect=RuntimeError('BLREC package unavailable'),
        ):
            catalog = hero_review.hero_catalog()

        self.assertEqual(len(catalog), 57)
        self.assertIn({'label': 'Kestrel', 'name': '凯思卓'}, catalog)
        self.assertTrue(hero_review.hero_image_bytes('Kestrel'))

    def test_worker_scoreboard_metadata_does_not_decide_team_size(self):
        context = hero_review.infer_lineup_context(
            {
                'result_panel_label': None,
                'match_mode_label': None,
                'suggestions': {},
                'sources': [
                    {
                        'source_type': 'worker',
                        'metadata': {'stage_class': 'scoreboard', 'mode_class': '5v5'},
                    }
                ],
            }
        )

        self.assertEqual(context, ('scoreboard', None))

    def test_human_mode_label_decides_team_size(self):
        context = hero_review.infer_lineup_context(
            {
                'result_panel_label': 'result_panel',
                'match_mode_label': '5v5',
                'suggestions': {},
                'sources': [],
            }
        )

        self.assertEqual(context, ('result_page', 5))

    def test_result_archive_selects_result_layout(self):
        context = hero_review.infer_lineup_context(
            {
                'result_panel_label': None,
                'match_mode_label': None,
                'suggestions': {
                    'result_panel': {'label': 'result_panel', 'confidence': 0.9}
                },
                'sources': [
                    {'source_type': 'result_archive', 'metadata': {'game_mode': '3v3'}}
                ],
            }
        )

        self.assertEqual(context, ('result_page', None))

    def test_known_panel_is_split_into_six_editable_portraits(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame_path = Path(tmp) / 'frame.jpg'
            Image.new('RGB', (1280, 720), '#333333').save(frame_path)

            team_size, result = hero_review.recognize_lineup(
                frame_path,
                screen_type='scoreboard',
                team_size=3,
                panel_box={'x': 0.0, 'y': 0.2, 'w': 1.0, 'h': 0.6},
                recognize_crop=lambda _image: {'label': 'Adagio', 'confidence': 0.9},
            )

        self.assertEqual(team_size, 3)
        self.assertEqual(len(result), 6)
        self.assertEqual(
            {(slot['side'], slot['slot']) for slot in result},
            {
                ('left', 1),
                ('left', 2),
                ('left', 3),
                ('right', 1),
                ('right', 2),
                ('right', 3),
            },
        )
        self.assertTrue(all(slot['suggested_label'] == 'Adagio' for slot in result))
        self.assertTrue(
            all(
                0 <= slot['crop'][axis] <= 1
                for slot in result
                for axis in ('x', 'y', 'w', 'h')
            )
        )

    def test_manual_circle_crops_are_recognized_without_guessing_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame_path = Path(tmp) / 'frame.jpg'
            Image.new('RGB', (1280, 720), '#333333').save(frame_path)
            slots = [
                {
                    'side': 'left',
                    'slot': 1,
                    'crop': {'x': 0.1, 'y': 0.1, 'w': 0.05, 'h': 0.09},
                },
                {
                    'side': 'right',
                    'slot': 1,
                    'crop': {'x': 0.8, 'y': 0.1, 'w': 0.05, 'h': 0.09},
                },
            ]

            result = hero_review.recognize_slots(
                frame_path,
                slots,
                recognize_crop=lambda _image: {'label': 'Adagio', 'confidence': 0.9},
            )

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['crop'], slots[0]['crop'])
        self.assertEqual(result[0]['suggested_label'], 'Adagio')
        self.assertEqual(result[0]['suggestion_confidence'], 0.9)


if __name__ == '__main__':
    unittest.main()
