from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Iterable, Mapping

from .database import DatabaseTarget, connect_database
from .models import AssetBatch


class IdempotencyConflict(Exception):
    pass


def _payload_sha256(batch: AssetBatch) -> str:
    value = batch.json(
        by_alias=True, exclude_none=False, sort_keys=True, separators=(',', ':')
    )
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def apply_asset_batch(
    database_target: DatabaseTarget, *, idempotency_key: str, batch: AssetBatch
) -> Dict[str, Any]:
    payload_sha256 = _payload_sha256(batch)
    now = int(time.time())
    connection = connect_database(database_target)
    try:
        connection.execute('BEGIN IMMEDIATE')
        previous = connection.execute(
            'SELECT payload_sha256,image_count,removed_match_count '
            'FROM asset_batches WHERE idempotency_key=?',
            (idempotency_key,),
        ).fetchone()
        if previous is not None:
            if str(previous['payload_sha256']) != payload_sha256:
                raise IdempotencyConflict(idempotency_key)
            connection.rollback()
            return {
                'batchId': idempotency_key,
                'status': 'duplicate',
                'imageCount': int(previous['image_count']),
                'removedMatchCount': int(previous['removed_match_count']),
            }
        for match_id in batch.removed_match_ids:
            connection.execute(
                'DELETE FROM match_assets WHERE source_match_id=?', (match_id,)
            )
        connection.executemany(
            'INSERT INTO match_assets('
            'source_match_id,image_url,image_width,image_height,image_sha256,updated_at'
            ') VALUES(?,?,?,?,?,?) ON CONFLICT(source_match_id) DO UPDATE SET '
            'image_url=excluded.image_url,image_width=excluded.image_width,'
            'image_height=excluded.image_height,image_sha256=excluded.image_sha256,'
            'updated_at=excluded.updated_at',
            (
                (
                    image.match_id,
                    str(image.url),
                    image.width,
                    image.height,
                    image.sha256,
                    now,
                )
                for image in batch.images
            ),
        )
        connection.execute(
            'INSERT INTO asset_batches('
            'idempotency_key,payload_sha256,image_count,removed_match_count,applied_at'
            ') VALUES(?,?,?,?,?)',
            (
                idempotency_key,
                payload_sha256,
                len(batch.images),
                len(batch.removed_match_ids),
                now,
            ),
        )
        connection.commit()
        return {
            'batchId': idempotency_key,
            'status': 'applied',
            'imageCount': len(batch.images),
            'removedMatchCount': len(batch.removed_match_ids),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_match_assets(
    database_target: DatabaseTarget, match_ids: Iterable[int]
) -> Mapping[int, Mapping[str, Any]]:
    values = tuple(sorted(set(match_ids)))
    if not values:
        return {}
    placeholders = ','.join('?' for _ in values)
    connection = connect_database(database_target)
    try:
        rows = connection.execute(
            'SELECT source_match_id,image_url,image_width,image_height '
            'FROM match_assets WHERE source_match_id IN (' + placeholders + ')',
            values,
        ).fetchall()
        return {
            int(row['source_match_id']): {
                'url': str(row['image_url']),
                'width': int(row['image_width']),
                'height': int(row['image_height']),
            }
            for row in rows
        }
    finally:
        connection.close()
