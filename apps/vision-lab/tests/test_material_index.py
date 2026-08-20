"""训练素材增量索引与统计。"""

import tempfile
from pathlib import Path

from labeler import db


class MaterialIndexTestCase:
    def setup_method(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.conn = db.connect(self.root / 'lab.db')
        self.video_id = db.upsert_video(
            self.conn,
            remote_path='/nas/sample.flv',
            streamer='测试主播',
            room_id='100',
            filename='sample.flv',
            duration_seconds=120,
            size_bytes=1,
        )

    def teardown_method(self) -> None:
        self.conn.close()
        self.temporary.cleanup()

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


class TestIncrementalMaterialIndex(MaterialIndexTestCase):
    @staticmethod
    def hero_slots(hero_label: str) -> list[dict]:
        return [
            {
                'side': side,
                'slot': slot,
                'crop': {
                    'x': 0.10 + slot * 0.05,
                    'y': 0.10 if side == 'left' else 0.30,
                    'w': 0.04,
                    'h': 0.04,
                },
                'suggested_label': hero_label if side == 'left' and slot == 1 else '',
                'suggestion_confidence': 0.92 if side == 'left' and slot == 1 else 0,
            }
            for side in ('left', 'right')
            for slot in range(1, 4)
        ]

    def test_source_ingestion_immediately_indexes_effective_facts(self) -> None:
        frame_id = self.frame(1)

        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id='worker-1',
            suggestions={
                'hero_select': {'label': 'select_5v5', 'confidence': 0.91},
                'match_mode': {'label': '5v5', 'confidence': 0.82},
            },
            metadata={'at_ms': 12_000},
            source_created_at=100,
        )

        indexed = self.conn.execute(
            'SELECT * FROM training_review_material_index WHERE frame_id = ?',
            (frame_id,),
        ).fetchone()
        assert indexed is not None
        assert indexed['review_status'] == 'pending'
        assert indexed['scene'] == 'hero_select'
        assert indexed['match_mode'] == '5v5'
        assert indexed['is_new'] == 1
        assert indexed['is_legacy'] == 0
        assert indexed['source_created_at'] == 100
        assert indexed['source_offset'] == 12_000
        assert indexed['has_boundary_confidence'] == 1
        assert indexed['has_high_confidence'] == 1

    def test_human_correction_replaces_old_statistical_contribution(self) -> None:
        frame_id = self.frame(2)
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id='worker-2',
            suggestions={'hero_select': {'label': 'select_5v5', 'confidence': 0.91}},
        )
        db.update_training_review_prefill_state(
            self.conn, frame_id=frame_id, status='ready', stage='complete'
        )

        before = db.training_review_material_suggestions(self.conn)
        before_5v5 = next(
            item
            for item in before
            if item['kind'] == 'scene_mode'
            and item['scene'] == 'hero_select'
            and item['match_mode'] == '5v5'
        )
        assert before_5v5['candidate_count'] == 1

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

        after = db.training_review_material_suggestions(self.conn)
        after_5v5 = next(
            item
            for item in after
            if item['kind'] == 'scene_mode'
            and item['scene'] == 'hero_select'
            and item['match_mode'] == '5v5'
        )
        after_3v3 = next(
            item
            for item in after
            if item['kind'] == 'scene_mode'
            and item['scene'] == 'hero_select'
            and item['match_mode'] == '3v3'
        )
        assert after_5v5['candidate_count'] == 0
        assert after_3v3['confirmed_count'] == 1

    def test_hero_confirmation_updates_hero_scene_totals(self) -> None:
        frame_id = self.frame(3)
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id='worker-3',
            suggestions={'match_mode': {'label': '3v3', 'confidence': 0.9}},
            metadata={
                'hero_context_suggestion': {
                    'screen_type': 'scoreboard',
                    'confidence': 0.9,
                }
            },
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
                'suggestion_confidence': 0.9,
            }
            for side in ('left', 'right')
            for slot in range(1, 4)
        ]
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=frame_id,
            screen_type='scoreboard',
            team_size=3,
            method='model-v1',
            slots=slots,
        )
        db.update_training_review_prefill_state(
            self.conn,
            frame_id=frame_id,
            status='ready',
            stage='complete',
            screen_type='scoreboard',
            team_size=3,
        )

        candidate = next(
            item
            for item in db.training_review_material_suggestions(
                self.conn, hero_catalog=({'label': 'Adagio', 'name': '奥达基'},)
            )
            if item['kind'] == 'hero_scene'
            and item['hero_label'] == 'Adagio'
            and item['scene'] == 'scoreboard'
        )
        assert candidate['candidate_count'] == 1
        assert candidate['candidate_crop_count'] == 6

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
            match_mode_label='3v3',
            hero_select_label='not_select',
            result_panel_label='no_result_panel',
            hero_layout_label='scoreboard',
            status='confirmed',
            result_groups={},
        )

        confirmed = next(
            item
            for item in db.training_review_material_suggestions(
                self.conn, hero_catalog=({'label': 'Adagio', 'name': '奥达基'},)
            )
            if item['kind'] == 'hero_scene'
            and item['hero_label'] == 'Adagio'
            and item['scene'] == 'scoreboard'
        )
        assert confirmed['candidate_count'] == 0
        assert confirmed['confirmed_count'] == 6

    def test_rebuild_is_idempotent_and_repairs_stale_index(self) -> None:
        frame_id = self.frame(4)
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='legacy_annotation',
            source_id='legacy-4',
            suggestions={'match_mode': {'label': 'aram', 'confidence': 0.8}},
            metadata={'screen_type': 'gameplay_hud'},
        )
        self.conn.execute(
            "UPDATE training_review_material_index SET scene='other' "
            'WHERE frame_id=?',
            (frame_id,),
        )
        self.conn.commit()

        first = db.rebuild_training_review_material_index(self.conn)
        first_totals = [
            tuple(row)
            for row in self.conn.execute(
                'SELECT kind,scene,match_mode,hero_label,source_scope,metric,'
                'frame_count,crop_count FROM training_review_material_totals '
                'ORDER BY kind,scene,match_mode,hero_label,source_scope,metric'
            ).fetchall()
        ]
        second = db.rebuild_training_review_material_index(self.conn)
        second_totals = [
            tuple(row)
            for row in self.conn.execute(
                'SELECT kind,scene,match_mode,hero_label,source_scope,metric,'
                'frame_count,crop_count FROM training_review_material_totals '
                'ORDER BY kind,scene,match_mode,hero_label,source_scope,metric'
            ).fetchall()
        ]

        indexed = self.conn.execute(
            'SELECT scene,is_legacy FROM training_review_material_index '
            'WHERE frame_id=?',
            (frame_id,),
        ).fetchone()
        assert indexed is not None
        assert indexed['scene'] == 'gameplay_hud'
        assert indexed['is_legacy'] == 1
        assert first['indexed'] == 1
        assert second['indexed'] == 1
        assert second_totals == first_totals

    def test_rebuild_keeps_worker_result_group_across_separate_events(self) -> None:
        frame_ids = [self.frame(40), self.frame(41)]
        for offset, frame_id in enumerate(frame_ids):
            event_id = db.create_event(
                self.conn,
                self.video_id,
                40_000 + offset * 1_000,
                40_000 + offset * 1_000,
            )
            db.assign_event(self.conn, [frame_id], event_id)
            db.add_training_review_source(
                self.conn,
                frame_id=frame_id,
                source_type='worker',
                source_id=f'worker-result-{offset}',
                suggestions={
                    'result_panel': {'label': 'result_panel', 'confidence': 0.9},
                    'match_mode': {'label': '3v3', 'confidence': 0.9},
                },
                metadata={'session_id': 7, 'part_id': 8, 'segment_start_ms': 39_000},
            )

        db.rebuild_training_review_material_index(self.conn)

        groups = db.training_review_result_groups(self.conn)
        assert set(groups) == set(frame_ids)
        representatives = {
            int(value['result_group_representative_frame_id'])
            for value in groups.values()
        }
        assert len(representatives) == 1
        assert {int(value['result_group_size']) for value in groups.values()} == {2}

    def test_filtered_page_uses_index_without_json_expressions(self) -> None:
        frame_id = self.frame(5)
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id='worker-5',
            suggestions={'match_mode': {'label': '5v5', 'confidence': 0.9}},
            metadata={'screen_type': 'scoreboard'},
        )
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        try:
            page, total = db.training_review_page(
                self.conn,
                status='needs_review',
                source_scope='new',
                scene='scoreboard',
                match_mode='5v5',
                limit=1,
            )
        finally:
            self.conn.set_trace_callback(None)

        assert total == 1
        assert [item['frame_id'] for item in page] == [frame_id]
        filter_statements = [
            statement
            for statement in statements
            if 'training_review_material_index' in statement
        ]
        assert filter_statements
        assert all('json_extract' not in statement for statement in filter_statements)

    def test_ready_only_review_hides_candidate_until_worker_prefill_finishes(
        self,
    ) -> None:
        frame_id = self.frame(50)
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id='worker-awaiting-prefill',
            metadata={'screen_type': 'gameplay_hud'},
        )

        raw_page, raw_total = db.training_review_page(
            self.conn, status='needs_review', source_scope='new', limit=10
        )
        ready_page, ready_total = db.training_review_page(
            self.conn,
            status='needs_review',
            source_scope='new',
            prefill_ready_only=True,
            limit=10,
        )

        assert raw_total == 1
        assert [item['frame_id'] for item in raw_page] == [frame_id]
        assert ready_total == 0
        assert ready_page == []

        db.update_training_review_prefill_state(
            self.conn, frame_id=frame_id, status='ready', stage='complete'
        )
        ready_page, ready_total = db.training_review_page(
            self.conn,
            status='needs_review',
            source_scope='new',
            prefill_ready_only=True,
            limit=10,
        )

        assert ready_total == 1
        assert [item['frame_id'] for item in ready_page] == [frame_id]

    def test_autonomous_prefill_candidate_keeps_stage_and_retry_state(self) -> None:
        first = self.frame(51)
        second = self.frame(52)
        for frame_id in (first, second):
            db.add_training_review_source(
                self.conn,
                frame_id=frame_id,
                source_type='worker',
                source_id=f'worker-prefill-{frame_id}',
            )
        db.update_training_review_prefill_state(
            self.conn, frame_id=second, status='ready', stage='complete'
        )

        candidate = db.next_training_review_prefill_candidate(self.conn)

        assert candidate is not None
        assert candidate['frame_id'] == first
        assert candidate['prefill_status'] == 'pending'
        assert candidate['prefill_stage'] == 'core'

        db.update_training_review_prefill_state(
            self.conn,
            frame_id=first,
            status='pending',
            stage='hero',
            screen_type='scoreboard',
            team_size=3,
        )
        candidate = db.next_training_review_prefill_candidate(self.conn)

        assert candidate is not None
        assert candidate['frame_id'] == first
        assert candidate['prefill_stage'] == 'hero'
        assert candidate['prefill_screen_type'] == 'scoreboard'
        assert candidate['prefill_team_size'] == 3

        for _attempt in range(3):
            db.update_training_review_prefill_state(
                self.conn,
                frame_id=first,
                status='failed',
                stage='hero',
                screen_type='scoreboard',
                team_size=3,
                error='temporary failure',
                increment_attempt=True,
            )
        assert db.next_training_review_prefill_candidate(self.conn) is None

    def test_filtered_page_falls_back_while_historical_index_is_incomplete(
        self,
    ) -> None:
        frame_id = self.frame(6)
        db.add_training_review_source(
            self.conn,
            frame_id=frame_id,
            source_type='worker',
            source_id='worker-6',
            suggestions={'match_mode': {'label': '5v5', 'confidence': 0.9}},
            metadata={'screen_type': 'scoreboard'},
        )
        self.conn.execute(
            'DELETE FROM training_review_material_index WHERE frame_id=?', (frame_id,)
        )
        self.conn.commit()

        page, total = db.training_review_page(
            self.conn,
            status='needs_review',
            source_scope='new',
            scene='scoreboard',
            match_mode='5v5',
            limit=10,
        )

        assert total == 1
        assert [item['frame_id'] for item in page] == [frame_id]

    def test_hero_filter_finds_hud_from_same_match_when_current_frame_missed(self):
        hud = self.frame(100)
        direct_hud = self.frame(110)
        result = self.frame(200)
        after_match = self.frame(260)
        common = {'session_id': 7, 'part_id': 9, 'part_index': 1}
        db.add_training_review_source(
            self.conn,
            frame_id=hud,
            source_type='worker',
            source_id='hud-before-result',
            metadata={**common, 'at_ms': 100_000, 'screen_type': 'gameplay_hud'},
        )
        db.add_training_review_source(
            self.conn,
            frame_id=direct_hud,
            source_type='worker',
            source_id='direct-adagio-hud',
            metadata={**common, 'at_ms': 110_000, 'screen_type': 'gameplay_hud'},
        )
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=direct_hud,
            screen_type='gameplay_hud',
            team_size=3,
            method='hero-model-v1',
            slots=self.hero_slots('Adagio'),
        )
        db.add_training_review_source(
            self.conn,
            frame_id=after_match,
            source_type='worker',
            source_id='hud-after-result',
            metadata={**common, 'at_ms': 260_000, 'screen_type': 'gameplay_hud'},
        )
        db.add_training_review_source(
            self.conn,
            frame_id=result,
            source_type='result_archive',
            source_id='match:42',
            suggestions={
                'match_flow': {'label': 'match_flow', 'confidence': 0.95},
                'match_mode': {'label': '3v3', 'confidence': 0.95},
                'result_panel': {'label': 'result_panel', 'confidence': 0.95},
            },
            metadata={
                **common,
                'match_id': 42,
                'started_at_ms': 90_000,
                'result_at_ms': 200_000,
                'at_ms': 200_000,
                'duration_seconds': 110,
            },
        )
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=result,
            screen_type='result_page',
            team_size=3,
            method='hero-model-v1',
            slots=self.hero_slots('Adagio'),
        )

        items, total = db.training_review_page(
            self.conn,
            status='needs_review',
            source_scope='new',
            scene='gameplay_hud',
            hero=['Adagio'],
            limit=20,
            result_groups={},
        )

        assert total == 2
        assert [item['frame_id'] for item in items] == [hud, direct_hud]
        assert items[0]['hero_filter_matches'] == [
            {
                'hero_label': 'Adagio',
                'reason': 'same_match',
                'evidence_source': 'model',
                'match_id': 42,
            }
        ]
        assert items[1]['hero_filter_matches'][0]['reason'] == 'direct_suggested'

    def test_hero_filter_uses_video_fallback_only_without_match_windows(self):
        evidence = self.frame(300)
        hud = self.frame(310)
        metadata = {'session_id': 8, 'part_id': 10, 'part_index': 1}
        db.add_training_review_source(
            self.conn,
            frame_id=evidence,
            source_type='worker',
            source_id='unlinked-scoreboard',
            metadata={**metadata, 'at_ms': 300_000, 'screen_type': 'scoreboard'},
        )
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=evidence,
            screen_type='scoreboard',
            team_size=3,
            method='hero-model-v1',
            slots=self.hero_slots('Adagio'),
        )
        db.add_training_review_source(
            self.conn,
            frame_id=hud,
            source_type='worker',
            source_id='unlinked-hud',
            metadata={**metadata, 'at_ms': 310_000, 'screen_type': 'gameplay_hud'},
        )

        items, total = db.training_review_page(
            self.conn,
            status='needs_review',
            source_scope='new',
            scene='gameplay_hud',
            hero=['Adagio'],
            limit=20,
            result_groups={},
        )

        assert total == 1
        assert [item['frame_id'] for item in items] == [hud]
        assert items[0]['hero_filter_matches'] == [
            {
                'hero_label': 'Adagio',
                'reason': 'same_video',
                'evidence_source': 'model',
                'match_id': None,
            }
        ]

    def test_direct_hero_match_reason_distinguishes_human_and_model_labels(self):
        suggested = self.frame(400)
        confirmed = self.frame(401)
        for frame_id, source_id in (
            (suggested, 'suggested-hud'),
            (confirmed, 'confirmed-hud'),
        ):
            db.add_training_review_source(
                self.conn,
                frame_id=frame_id,
                source_type='worker',
                source_id=source_id,
                metadata={
                    'session_id': 9,
                    'part_id': 11,
                    'at_ms': frame_id * 1_000,
                    'screen_type': 'gameplay_hud',
                },
            )
            db.replace_training_review_hero_suggestions(
                self.conn,
                frame_id=frame_id,
                screen_type='gameplay_hud',
                team_size=3,
                method='hero-model-v1',
                slots=self.hero_slots('Adagio'),
            )
        db.save_training_review_hero_lineup(
            self.conn,
            frame_id=confirmed,
            labels=[
                {
                    'side': slot['side'],
                    'slot': slot['slot'],
                    'hero_label': (
                        'Adagio'
                        if slot['side'] == 'left' and slot['slot'] == 1
                        else 'unreadable'
                    ),
                }
                for slot in self.hero_slots('Adagio')
            ],
            allowed_labels={'Adagio', 'unreadable'},
        )

        items, total = db.training_review_page(
            self.conn,
            status='needs_review',
            source_scope='new',
            scene='gameplay_hud',
            hero=['Adagio'],
            limit=20,
            result_groups={},
        )
        reasons = {
            item['frame_id']: item['hero_filter_matches'][0]['reason'] for item in items
        }

        assert total == 2
        assert reasons == {suggested: 'direct_suggested', confirmed: 'direct_confirmed'}

    def test_material_suggestion_counts_same_match_misses_separately(self):
        hud = self.frame(500)
        result = self.frame(600)
        common = {'session_id': 10, 'part_id': 12, 'part_index': 1}
        db.add_training_review_source(
            self.conn,
            frame_id=hud,
            source_type='worker',
            source_id='missing-adagio-hud',
            metadata={**common, 'at_ms': 500_000, 'screen_type': 'gameplay_hud'},
        )
        db.add_training_review_source(
            self.conn,
            frame_id=result,
            source_type='result_archive',
            source_id='match:88',
            suggestions={'result_panel': {'label': 'result_panel', 'confidence': 0.9}},
            metadata={
                **common,
                'match_id': 88,
                'started_at_ms': 490_000,
                'result_at_ms': 600_000,
                'at_ms': 600_000,
            },
        )
        db.replace_training_review_hero_suggestions(
            self.conn,
            frame_id=result,
            screen_type='result_page',
            team_size=3,
            method='hero-model-v1',
            slots=self.hero_slots('Adagio'),
        )

        suggestion = next(
            item
            for item in db.training_review_material_suggestions(
                self.conn, hero_catalog=({'label': 'Adagio', 'name': '奥达基'},)
            )
            if item['kind'] == 'hero_scene'
            and item['hero_label'] == 'Adagio'
            and item['scene'] == 'gameplay_hud'
        )

        assert suggestion['confirmed_count'] == 0
        assert suggestion['model_prefill_count'] == 0
        assert suggestion['same_match_candidate_count'] == 1
        assert suggestion['same_video_candidate_count'] == 0
        assert suggestion['candidate_count'] == 1
        assert suggestion['matches_without_scene_candidate'] == 0

    def test_material_suggestions_do_not_query_once_per_hero(self):
        statements: list[str] = []
        self.conn.set_trace_callback(
            lambda statement: (
                statements.append(statement)
                if statement.lstrip().upper().startswith(('SELECT', 'WITH'))
                else None
            )
        )
        try:
            db.training_review_material_suggestions(
                self.conn,
                hero_catalog=tuple(
                    {'label': f'hero-{index}', 'name': f'英雄 {index}'}
                    for index in range(55)
                ),
            )
        finally:
            self.conn.set_trace_callback(None)

        assert len(statements) == 4
