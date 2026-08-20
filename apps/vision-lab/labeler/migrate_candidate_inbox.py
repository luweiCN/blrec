"""把未预打标的复核空壳移回独立候选收件箱。"""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from . import config, db


def main(values: Sequence[str] = ()) -> None:
    parser = argparse.ArgumentParser(
        description='将未预打标、未人工复核的空壳迁移到候选收件箱'
    )
    parser.add_argument(
        '--apply', action='store_true', help='真正执行迁移；不加时只输出可迁移数量'
    )
    args = parser.parse_args(None if not values else values)
    conn = db.connect(config.DB_PATH)
    try:
        result = db.migrate_unprefilled_training_review_candidates(
            conn, dry_run=not args.apply
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
