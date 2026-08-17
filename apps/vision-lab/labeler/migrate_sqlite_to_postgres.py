"""One-time Vision Lab SQLite to PostgreSQL migration command."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional, Sequence

from . import config, db, postgres


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description='Migrate a Vision Lab SQLite workspace to PostgreSQL'
    )
    parser.add_argument('--sqlite', type=Path, default=config.DB_PATH)
    parser.add_argument(
        '--database-url', default=os.environ.get('VISION_LAB_DATABASE_URL', '')
    )
    parser.add_argument(
        '--schema', default=os.environ.get('VISION_LAB_DATABASE_SCHEMA', 'vision_lab')
    )
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    database_url = str(args.database_url or '').strip()
    if not database_url:
        raise RuntimeError('必须配置 VISION_LAB_DATABASE_URL')
    sqlite_path = args.sqlite.expanduser().resolve()
    if not sqlite_path.is_file():
        raise FileNotFoundError(sqlite_path)
    report = postgres.migrate_sqlite_database(
        sqlite_path,
        database_url,
        schema=str(args.schema),
        schema_sql=db._SCHEMA,  # noqa: SLF001 - migration must match runtime schema
        default_tasks=db.DEFAULT_TASKS,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        report_path = args.report.expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(encoded + '\n', encoding='utf-8')
    print(encoded)


if __name__ == '__main__':
    main()
