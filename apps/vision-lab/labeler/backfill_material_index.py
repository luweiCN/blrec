"""把 NAS 历史候选补入 PostgreSQL，并重建训练素材增量索引。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Dict, Optional, Sequence

from . import config, db, worker_candidates
from .nas import NasClient


def backfill_material_index(
    conn: Any,
    nas: NasClient,
    *,
    import_candidates: bool = True,
    batch_size: int = 500,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    imported = {
        'total': 0,
        'processed': 0,
        'inserted': 0,
        'updated': 0,
        'unchanged': 0,
        'downloaded': 0,
        'failed': 0,
        'last_error': '',
    }
    if import_candidates:
        candidates = nas.list_training_candidates()
        imported['total'] = len(candidates)
        for offset in range(0, len(candidates), batch_size):
            batch = candidates[offset : offset + batch_size]
            result = worker_candidates.sync_worker_candidates(
                conn, nas, batch, maximum=len(batch)
            )
            for key in (
                'processed',
                'inserted',
                'updated',
                'unchanged',
                'downloaded',
                'failed',
            ):
                imported[key] += int(result.get(key) or 0)
            if result.get('last_error'):
                imported['last_error'] = str(result['last_error'])
            if progress is not None:
                progress({'phase': 'candidate_import', **imported})

    def rebuild_progress(value: Dict[str, int]) -> None:
        if progress is not None:
            progress({'phase': 'material_index', **value})

    rebuilt = db.rebuild_training_review_material_index(
        conn, batch_size=batch_size, progress=rebuild_progress
    )
    return {'candidates': imported, 'index': rebuilt}


def main(values: Sequence[str] = ()) -> None:
    parser = argparse.ArgumentParser(
        description='回填历史候选并重建 Vision Lab 训练素材索引'
    )
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--skip-candidate-import', action='store_true')
    parser.add_argument('--batch-size', type=int, default=500)
    args = parser.parse_args(None if not values else values)
    if not args.apply:
        parser.error('--apply 是必需的；该命令会写入共享数据库')
    if args.batch_size < 1 or args.batch_size > 5_000:
        parser.error('--batch-size 必须在 1 到 5000 之间')
    conn = db.connect(config.DB_PATH)
    try:
        result = backfill_material_index(
            conn,
            NasClient(candidate_local_root=config.CANDIDATE_LOCAL_DIR),
            import_candidates=not args.skip_candidate_import,
            batch_size=args.batch_size,
            progress=lambda value: print(
                json.dumps(value, ensure_ascii=False, sort_keys=True),
                file=sys.stderr,
                flush=True,
            ),
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
