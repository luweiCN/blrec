from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Optional, Sequence

from blrec.bili_upload.postgres_database import (
    PostgresBiliUploadDatabase,
    create_bili_upload_database,
)
from blrec.vainglory.repository import VaingloryRepository
from blrec.vainglory.vision_candidate_ingest import VisionCandidateIngestClient


async def _run(batch_size: int) -> int:
    database_target = os.environ.get('BLREC_DATABASE_URL', '').strip()
    if not database_target:
        raise RuntimeError('缺少 BLREC_DATABASE_URL')
    ingest = VisionCandidateIngestClient.from_environment()
    if ingest is None:
        raise RuntimeError('缺少 BLREC_VISION_LAB_INGEST_URL')
    result_root = Path(
        os.environ.get(
            'BLREC_VAINGLORY_RESULT_FRAME_ROOT', '/cfg/vainglory-result-frames'
        )
    )
    candidate_root = Path(
        os.environ.get(
            'BLREC_VAINGLORY_TRAINING_CANDIDATE_ROOT',
            '/cfg/vainglory-training-candidates',
        )
    )
    database = create_bili_upload_database(
        database_target, local_state_path='/cfg/blrec.sqlite3'
    )
    if isinstance(database, PostgresBiliUploadDatabase):
        await database.open_readonly()
    else:
        await database.open()
    try:
        repository = VaingloryRepository(
            database,
            result_frame_root=result_root,
            training_candidate_root=candidate_root,
            candidate_ingest=ingest.ingest,
        )
        after_match_id = 0
        totals = {'scanned': 0, 'written': 0, 'missing': 0, 'failed': 0}
        while True:
            result = await repository.backfill_result_archive_candidates(
                after_match_id=after_match_id, limit=batch_size
            )
            for key in totals:
                totals[key] += int(result[key])
            after_match_id = int(result['last_match_id'])
            print(
                json.dumps(
                    {**totals, 'last_match_id': after_match_id},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
            if int(result['scanned']) < batch_size:
                return 0 if totals['failed'] == 0 else 1
    finally:
        await database.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='补齐 Vision Lab 历史结算候选')
    parser.add_argument('--batch-size', type=int, default=500)
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.batch_size <= 1_000:
        parser.error('--batch-size 必须在 1 到 1000 之间')
    return asyncio.run(_run(arguments.batch_size))


if __name__ == '__main__':
    raise SystemExit(main())
