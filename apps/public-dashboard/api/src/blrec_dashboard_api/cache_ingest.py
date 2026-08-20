from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Union

from .models import IngestBatch
from .service import IdempotencyConflict, apply_ingest_batch


def _database_target() -> Union[Path, str]:
    value = os.environ.get('DASHBOARD_CACHE_DATABASE_TARGET', '')
    if not value:
        raise RuntimeError('dashboard cache database target is missing')
    if value.startswith(('postgresql://', 'postgresql+psycopg://')):
        return value
    return Path(value)


def main() -> None:
    idempotency_key = os.environ.get('DASHBOARD_CACHE_IDEMPOTENCY_KEY', '')
    if not idempotency_key:
        raise RuntimeError('dashboard cache idempotency key is missing')
    try:
        payload = json.load(sys.stdin)
        batch = IngestBatch.parse_obj(payload)
        result = apply_ingest_batch(
            _database_target(), idempotency_key=idempotency_key, batch=batch
        )
    except IdempotencyConflict:
        print('{"error":"idempotency_conflict"}')
        raise SystemExit(3)
    except (TypeError, ValueError):
        print('{"error":"invalid_batch_state"}')
        raise SystemExit(4)
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))


if __name__ == '__main__':
    main()
