"""PostgreSQL adapter keeps Vision Lab's SQLite query contract."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from labeler import db, postgres


class PostgresCompatibilityTests(unittest.TestCase):
    def test_qmark_translation_does_not_touch_quoted_text(self) -> None:
        translated = postgres.postgres_sql(
            "SELECT '?' AS literal WHERE name LIKE 'hero%' AND id = ?"
        )

        self.assertEqual(
            translated, "SELECT '?' AS literal WHERE name LIKE 'hero%%' AND id = %s"
        )

    def test_insert_or_ignore_becomes_postgres_conflict_clause(self) -> None:
        translated = postgres.postgres_sql(
            'INSERT OR IGNORE INTO annotation_tasks (id) VALUES (?)'
        )

        self.assertEqual(
            translated,
            'INSERT INTO annotation_tasks (id) VALUES (%s) ON CONFLICT DO NOTHING',
        )

    def test_named_placeholders_are_translated_without_touching_casts(self) -> None:
        translated = postgres.postgres_sql(
            "INSERT INTO samples (payload, label) "
            "VALUES (:payload::jsonb, :label) -- :comment\n"
            "RETURNING ':literal'"
        )

        self.assertEqual(
            translated,
            "INSERT INTO samples (payload, label) "
            "VALUES (%(payload)s::jsonb, %(label)s) -- :comment\n"
            "RETURNING ':literal'",
        )

    def test_connection_keeps_named_parameters_and_returns_insert_id(self) -> None:
        class Cursor:
            rowcount = 1

            def __init__(self) -> None:
                self.statement = ''
                self.parameters: Any = None

            def execute(self, statement: str, parameters: Any) -> None:
                self.statement = statement
                self.parameters = parameters

            def fetchone(self) -> Tuple[int]:
                return (37,)

        class Connection:
            def __init__(self) -> None:
                self.cursor_instance = Cursor()

            def cursor(self) -> Cursor:
                return self.cursor_instance

        connection = Connection()
        adapter = postgres.PostgresConnection(
            connection, pool=None, identity_tables={'frames'}
        )
        values: Dict[str, Any] = {'video_id': 12}

        cursor = adapter.execute(
            'INSERT OR IGNORE INTO frames (video_id) VALUES (:video_id)', values
        )

        self.assertEqual(
            connection.cursor_instance.statement,
            'INSERT INTO frames (video_id) VALUES (%(video_id)s) '
            'ON CONFLICT DO NOTHING RETURNING id',
        )
        self.assertEqual(connection.cursor_instance.parameters, values)
        self.assertEqual(cursor.lastrowid, 37)

    def test_add_frames_uses_adapter_insert_id(self) -> None:
        class Cursor:
            rowcount = 1
            lastrowid = 91

        class Connection:
            def __init__(self) -> None:
                self.committed = False

            def execute(self, statement: str, _parameters: Any = ()) -> Cursor:
                if 'last_insert_rowid' in statement:
                    raise AssertionError('不应在 PostgreSQL 适配器上查询 SQLite ID')
                return Cursor()

            def commit(self) -> None:
                self.committed = True

        connection = Connection()

        frame_ids = db.add_frames(
            connection,
            7,
            [
                {
                    'timestamp_ms': 1000,
                    'width': 1920,
                    'height': 1080,
                    'sha256': 'a' * 64,
                    'phash': 'b' * 16,
                    'frame_path': '/candidates/frame.jpg',
                    'thumb_path': '',
                    'strategy': 'worker_candidate',
                    'model_source': 'worker-unified-v3',
                    'model_confidence': 0.9,
                }
            ],
        )

        self.assertEqual(frame_ids, [91])
        self.assertTrue(connection.committed)

    def test_json_extract_compatibility_function_is_parallel_safe_sql(self) -> None:
        self.assertIn(
            'RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT',
            postgres.POSTGRES_COMPATIBILITY_SQL,
        )
        self.assertNotIn('EXCEPTION WHEN', postgres.POSTGRES_COMPATIBILITY_SQL)

    def test_schema_conversion_orders_referenced_tables_first(self) -> None:
        statements, identities = postgres.ordered_schema_statements(db._SCHEMA)
        table_names = [
            match.group(1)
            for statement in statements
            if (match := postgres._CREATE_TABLE.match(statement)) is not None
        ]

        self.assertLess(table_names.index('events'), table_names.index('frames'))
        self.assertLess(table_names.index('frames'), table_names.index('annotations'))
        self.assertLess(
            table_names.index('training_review_match_contexts'),
            table_names.index('training_review_material_index'),
        )
        self.assertIn('videos', identities)
        self.assertNotIn('training_review_items', identities)
        self.assertNotIn('annotations', identities)
        self.assertTrue(
            all('AUTOINCREMENT' not in statement for statement in statements)
        )

    def test_schema_name_rejects_dynamic_sql_identifiers(self) -> None:
        for schema in ('', 'Vision-Lab', 'vision lab', 'x;drop'):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                postgres.validate_schema_name(schema)

    def test_migration_digest_does_not_depend_on_database_sort_order(self) -> None:
        rows = [
            ('-卢伟-', 'hud', 0.25),
            ('IICelery', 'result', 0.5),
            ('忧郁的乌贼娘', 'scoreboard', 0.75),
        ]

        forward = postgres._rows_digest(iter(rows))
        reverse = postgres._rows_digest(iter(reversed(rows)))

        self.assertEqual(forward, reverse)

    def test_sqlite_remains_the_default_for_local_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            connection = db.connect_sqlite(Path(temporary) / 'lab.db')
            try:
                row = connection.execute(
                    'SELECT COUNT(*) FROM annotation_tasks'
                ).fetchone()
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(int(row[0]), len(db.DEFAULT_TASKS))
            finally:
                connection.close()

    def test_existing_sqlite_material_index_gets_match_columns_before_indexes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'lab.db'
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE training_review_material_index (
                    frame_id INTEGER PRIMARY KEY,
                    video_id INTEGER NOT NULL,
                    review_status TEXT NOT NULL,
                    scene TEXT NOT NULL DEFAULT 'other',
                    match_mode TEXT NOT NULL DEFAULT '',
                    is_new INTEGER NOT NULL DEFAULT 0,
                    is_legacy INTEGER NOT NULL DEFAULT 0,
                    has_worker INTEGER NOT NULL DEFAULT 0,
                    has_result_archive INTEGER NOT NULL DEFAULT 0,
                    has_manual_correction INTEGER NOT NULL DEFAULT 0,
                    has_model_prefill INTEGER NOT NULL DEFAULT 0,
                    has_hero_model_prefill INTEGER NOT NULL DEFAULT 0,
                    has_low_confidence INTEGER NOT NULL DEFAULT 0,
                    has_boundary_confidence INTEGER NOT NULL DEFAULT 0,
                    has_high_confidence INTEGER NOT NULL DEFAULT 0,
                    selects_aram INTEGER NOT NULL DEFAULT 0,
                    suggests_aram INTEGER NOT NULL DEFAULT 0,
                    source_created_at INTEGER NOT NULL DEFAULT 0,
                    source_offset INTEGER NOT NULL DEFAULT 0,
                    result_group_representative_frame_id INTEGER NOT NULL,
                    result_group_size INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
            connection.close()

            migrated = db.connect_sqlite(path)
            try:
                columns = {
                    str(row['name'])
                    for row in migrated.execute(
                        'PRAGMA table_info(training_review_material_index)'
                    )
                }
                self.assertTrue(
                    {
                        'session_id',
                        'part_id',
                        'at_ms',
                        'linked_match_id',
                        'match_link_source',
                        'prefill_status',
                        'prefill_stage',
                        'prefill_attempts',
                        'prefill_error',
                        'prefill_screen_type',
                        'prefill_team_size',
                        'prefill_updated_at',
                        'prefilled_at',
                    }.issubset(columns)
                )
            finally:
                migrated.close()

    def test_existing_sqlite_review_table_keeps_truth_when_contexts_are_added(
        self,
    ) -> None:
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE frames (id INTEGER PRIMARY KEY)')
        connection.execute('INSERT INTO frames (id) VALUES (1)')
        connection.execute(
            """
            CREATE TABLE training_review_items (
                frame_id INTEGER PRIMARY KEY REFERENCES frames(id),
                match_flow_label TEXT,
                match_mode_label TEXT CHECK (
                    match_mode_label IS NULL OR match_mode_label IN (
                        '3v3','aram','5v5','unreadable')),
                hero_select_label TEXT CHECK (
                    hero_select_label IS NULL OR hero_select_label IN (
                        'not_select','select_3v3','select_aram','select_5v5',
                        'unreadable')),
                hero_select_variant TEXT,
                hero_select_visibility TEXT,
                result_panel_label TEXT,
                hero_layout_label TEXT,
                panel_render_state TEXT NOT NULL DEFAULT 'clear',
                ocr_usable TEXT NOT NULL DEFAULT 'yes',
                result_occlusion TEXT NOT NULL DEFAULT 'none',
                occluder_types TEXT NOT NULL DEFAULT '[]',
                review_status TEXT NOT NULL DEFAULT 'pending',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reviewed_at TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO training_review_items ("
            "frame_id,match_flow_label,match_mode_label,hero_select_label,"
            "result_panel_label,review_status,created_at,updated_at) VALUES ("
            "1,'match_flow','3v3','not_select','no_result_panel','confirmed',"
            "'old-created','old-updated')"
        )

        db._migrate_training_review_context_labels(connection)

        row = connection.execute(
            'SELECT * FROM training_review_items WHERE frame_id=1'
        ).fetchone()
        self.assertEqual(row['match_mode_label'], '3v3')
        self.assertEqual(row['created_at'], 'old-created')
        self.assertIsNone(row['match_kind_label'])
        connection.execute(
            "UPDATE training_review_items SET match_mode_label='blitz',"
            "match_kind_label='bot',view_context_label='spectated' WHERE frame_id=1"
        )
        migrated = connection.execute(
            'SELECT match_mode_label,match_kind_label,view_context_label '
            'FROM training_review_items WHERE frame_id=1'
        ).fetchone()
        self.assertEqual(tuple(migrated), ('blitz', 'bot', 'spectated'))
        connection.close()

    def test_existing_schema_does_not_require_database_create_privilege(self) -> None:
        class Cursor:
            def __init__(self) -> None:
                self.calls: List[Tuple[Any, Sequence[Any]]] = []

            def execute(
                self, statement: Any, parameters: Sequence[Any] = ()
            ) -> 'Cursor':
                self.calls.append((statement, parameters))
                return self

            def fetchone(self) -> Tuple[bool]:
                return (True,)

        cursor = Cursor()
        self.assertTrue(postgres._schema_exists(cursor, 'vision_lab'))

        self.assertEqual(len(cursor.calls), 1)

    def test_version_one_schema_migrates_runtime_and_material_index(self) -> None:
        class Cursor:
            def __init__(self) -> None:
                self.calls: List[Tuple[Any, Sequence[Any]]] = []

            def execute(
                self, statement: Any, parameters: Sequence[Any] = ()
            ) -> 'Cursor':
                self.calls.append((statement, parameters))
                return self

        cursor = Cursor()

        postgres._apply_incremental_schema_migrations(cursor, 1)

        statements = [str(statement) for statement, _ in cursor.calls]
        self.assertTrue(
            any(
                'CREATE TABLE IF NOT EXISTS service_runtime_states' in value
                for value in statements
            )
        )
        self.assertTrue(
            any(
                'CREATE TABLE IF NOT EXISTS training_review_material_index' in value
                for value in statements
            )
        )
        self.assertTrue(
            any(
                'CREATE TABLE IF NOT EXISTS training_review_match_contexts' in value
                for value in statements
            )
        )
        self.assertTrue(
            any(
                'ADD COLUMN IF NOT EXISTS prefill_status' in value
                for value in statements
            )
        )
        self.assertTrue(
            any(
                'CREATE TABLE IF NOT EXISTS training_review_candidate_inbox' in value
                for value in statements
            )
        )
        self.assertTrue(
            any(
                'ADD COLUMN IF NOT EXISTS match_kind_label' in value
                for value in statements
            )
        )
        self.assertTrue(
            any(
                'ADD COLUMN IF NOT EXISTS view_context_label' in value
                for value in statements
            )
        )
        self.assertTrue(
            any(
                'training_review_items_match_mode_label_check' in value
                and "'blitz'" in value
                for value in statements
            )
        )
        self.assertTrue(
            any('ADD COLUMN IF NOT EXISTS is_afk' in value for value in statements)
        )
        self.assertTrue(
            any(
                'CREATE TABLE IF NOT EXISTS training_review_model_outcomes' in value
                for value in statements
            )
        )
        self.assertTrue(
            any(
                'ADD COLUMN IF NOT EXISTS afk_prediction_status' in value
                for value in statements
            )
        )
        self.assertEqual(cursor.calls[-1][1], (11,))


if __name__ == '__main__':
    unittest.main()
