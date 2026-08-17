"""PostgreSQL adapter keeps Vision Lab's SQLite query contract."""

import tempfile
import unittest
from pathlib import Path
from typing import Any, List, Sequence, Tuple

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

    def test_schema_conversion_orders_referenced_tables_first(self) -> None:
        statements, identities = postgres.ordered_schema_statements(db._SCHEMA)
        table_names = [
            match.group(1)
            for statement in statements
            if (match := postgres._CREATE_TABLE.match(statement)) is not None
        ]

        self.assertLess(table_names.index('events'), table_names.index('frames'))
        self.assertLess(table_names.index('frames'), table_names.index('annotations'))
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


if __name__ == '__main__':
    unittest.main()
