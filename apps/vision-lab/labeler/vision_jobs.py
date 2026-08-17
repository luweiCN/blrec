"""Vision Worker 注册、租约和重任务队列。"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional
from uuid import uuid4

from . import db

JOB_STATUSES = {'queued', 'running', 'succeeded', 'failed', 'cancelled'}
JOB_KINDS = {
    'candidate_metadata',
    'model_prefill',
    'train_model',
    'validate_model',
    'package_models',
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _decode(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ''))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _future(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=max(60, int(seconds)))).isoformat(
        timespec='seconds'
    )


def _job_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item['payload'] = _decode(item.pop('payload_json'), {})
    item['result'] = _decode(item.pop('result_json'), {})
    item['cancel_requested'] = bool(item['cancel_requested'])
    return item


def _worker_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item['capabilities'] = _decode(item.pop('capabilities_json'), [])
    item['detail'] = _decode(item.pop('detail_json'), {})
    item['enabled'] = bool(item['enabled'])
    return item


def register_worker(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    display_name: str,
    capabilities: Iterable[str],
    version: str = '',
    platform: str = '',
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized_id = worker_id.strip()[:120]
    normalized_name = display_name.strip()[:160]
    if not normalized_id or not normalized_name:
        raise ValueError('Worker id 和名称不能为空')
    normalized_capabilities = sorted(
        {
            str(value).strip()
            for value in capabilities
            if str(value).strip() in JOB_KINDS
        }
    )
    if not normalized_capabilities:
        raise ValueError('Worker 至少需要声明一项有效能力')
    timestamp = db.now()
    conn.execute(
        """
        INSERT INTO vision_workers
            (id, display_name, capabilities_json, version, platform, state,
             detail_json, last_seen_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'idle', ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            display_name=excluded.display_name,
            capabilities_json=excluded.capabilities_json,
            version=excluded.version,
            platform=excluded.platform,
            detail_json=excluded.detail_json,
            last_seen_at=excluded.last_seen_at,
            updated_at=excluded.updated_at
        """,
        (
            normalized_id,
            normalized_name,
            _json(normalized_capabilities),
            version.strip()[:80],
            platform.strip()[:160],
            _json(detail or {}),
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    conn.commit()
    row = conn.execute(
        'SELECT * FROM vision_workers WHERE id = ?', (normalized_id,)
    ).fetchone()
    assert row is not None
    return _worker_dict(row)


def set_worker_enabled(
    conn: sqlite3.Connection, *, worker_id: str, enabled: bool
) -> Dict[str, Any]:
    cursor = conn.execute(
        'UPDATE vision_workers SET enabled = ?, updated_at = ? WHERE id = ?',
        (int(bool(enabled)), db.now(), worker_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise KeyError(worker_id)
    conn.commit()
    row = conn.execute(
        'SELECT * FROM vision_workers WHERE id = ?', (worker_id,)
    ).fetchone()
    assert row is not None
    return _worker_dict(row)


def create_job(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: Dict[str, Any],
    related_id: str = '',
    priority: int = 0,
) -> Dict[str, Any]:
    if kind not in JOB_KINDS:
        raise ValueError(f'未知 Vision Worker 任务: {kind}')
    job_id = '{}-{}'.format(kind.replace('_', '-'), uuid4().hex)
    timestamp = db.now()
    try:
        conn.execute(
            """
            INSERT INTO vision_jobs
                (id, kind, related_id, status, priority, payload_json,
                 created_at, updated_at)
            VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (
                job_id,
                kind,
                related_id.strip()[:160],
                int(priority),
                _json(payload),
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as error:
        conn.rollback()
        if related_id:
            existing = conn.execute(
                "SELECT * FROM vision_jobs WHERE kind = ? AND related_id = ? "
                "AND status IN ('queued', 'running')",
                (kind, related_id),
            ).fetchone()
            if existing is not None:
                return _job_dict(existing)
        raise error
    row = conn.execute('SELECT * FROM vision_jobs WHERE id = ?', (job_id,)).fetchone()
    assert row is not None
    return _job_dict(row)


def list_jobs(conn: sqlite3.Connection, *, limit: int = 100) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT * FROM vision_jobs ORDER BY created_at DESC, id DESC LIMIT ?',
        (max(1, min(1_000, int(limit))),),
    ).fetchall()
    return [_job_dict(row) for row in rows]


def list_workers(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT * FROM vision_workers ORDER BY enabled DESC, last_seen_at DESC, id'
    ).fetchall()
    return [_worker_dict(row) for row in rows]


def get_job(conn: sqlite3.Connection, job_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute('SELECT * FROM vision_jobs WHERE id = ?', (job_id,)).fetchone()
    return _job_dict(row) if row is not None else None


def validate_lease(
    conn: sqlite3.Connection, *, job_id: str, worker_id: str, lease_token: str
) -> Dict[str, Any]:
    return _job_dict(_leased_job(conn, job_id, worker_id, lease_token))


def claim_job(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    capabilities: Iterable[str],
    lease_seconds: int,
) -> Optional[Dict[str, Any]]:
    accepted = {str(value) for value in capabilities if str(value) in JOB_KINDS}
    if not accepted:
        return None
    timestamp = db.now()
    conn.execute('BEGIN IMMEDIATE')
    try:
        worker = conn.execute(
            'SELECT enabled FROM vision_workers WHERE id = ?', (worker_id,)
        ).fetchone()
        if worker is None:
            raise KeyError(worker_id)
        if not bool(worker['enabled']):
            conn.rollback()
            return None
        rows = conn.execute(
            """
            SELECT * FROM vision_jobs
            WHERE status = 'queued'
               OR (status = 'running' AND cancel_requested = 0
                   AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
            ORDER BY priority DESC, created_at, id
            LIMIT 100
            """,
            (timestamp,),
        ).fetchall()
        selected = next((row for row in rows if str(row['kind']) in accepted), None)
        if selected is None:
            conn.execute(
                "UPDATE vision_workers SET state='idle', active_job_id=NULL, "
                'last_seen_at=?, updated_at=? WHERE id=?',
                (timestamp, timestamp, worker_id),
            )
            conn.commit()
            return None
        lease_token = secrets.token_urlsafe(32)
        started_at = selected['started_at'] or timestamp
        conn.execute(
            """
            UPDATE vision_jobs
            SET status='running', worker_id=?, lease_token=?, lease_expires_at=?,
                started_at=?, updated_at=?, error=''
            WHERE id=?
            """,
            (
                worker_id,
                lease_token,
                _future(lease_seconds),
                started_at,
                timestamp,
                selected['id'],
            ),
        )
        conn.execute(
            "UPDATE vision_workers SET state='busy', active_job_id=?, "
            'last_seen_at=?, updated_at=? WHERE id=?',
            (selected['id'], timestamp, timestamp, worker_id),
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    claimed = conn.execute(
        'SELECT * FROM vision_jobs WHERE id = ?', (selected['id'],)
    ).fetchone()
    assert claimed is not None
    result = _job_dict(claimed)
    result['lease_token'] = lease_token
    return result


def update_job_lease(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    worker_id: str,
    lease_token: str,
    lease_seconds: int,
    progress: Optional[float] = None,
    stage: Optional[str] = None,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    row = _leased_job(conn, job_id, worker_id, lease_token)
    values: Dict[str, Any] = {
        'lease_expires_at': _future(lease_seconds),
        'updated_at': db.now(),
    }
    if progress is not None:
        values['progress'] = max(0.0, min(1.0, float(progress)))
    if stage is not None:
        values['stage'] = stage.strip()[:120]
    if detail is not None:
        values['detail'] = detail.strip()[-2_000:]
    assignments = ', '.join(f'{key} = ?' for key in values)
    conn.execute(
        f'UPDATE vision_jobs SET {assignments} WHERE id = ?', [*values.values(), job_id]
    )
    timestamp = db.now()
    conn.execute(
        'UPDATE vision_workers SET last_seen_at=?, updated_at=? WHERE id=?',
        (timestamp, timestamp, worker_id),
    )
    conn.commit()
    current = get_job(conn, job_id)
    assert current is not None
    current['cancel_requested'] = bool(row['cancel_requested'])
    return current


def finish_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    worker_id: str,
    lease_token: str,
    succeeded: bool,
    result: Optional[Dict[str, Any]] = None,
    error: str = '',
) -> Dict[str, Any]:
    row = _leased_job(conn, job_id, worker_id, lease_token)
    cancelled = bool(row['cancel_requested'])
    status = 'cancelled' if cancelled else ('succeeded' if succeeded else 'failed')
    timestamp = db.now()
    conn.execute(
        """
        UPDATE vision_jobs
        SET status=?, progress=?, result_json=?, error=?, finished_at=?,
            lease_token='', lease_expires_at=NULL, updated_at=?
        WHERE id=?
        """,
        (
            status,
            1.0 if succeeded and not cancelled else float(row['progress']),
            _json(result or {}),
            error.strip()[:2_000],
            timestamp,
            timestamp,
            job_id,
        ),
    )
    conn.execute(
        "UPDATE vision_workers SET state='idle', active_job_id=NULL, "
        'last_seen_at=?, updated_at=? WHERE id=?',
        (timestamp, timestamp, worker_id),
    )
    conn.commit()
    current = get_job(conn, job_id)
    assert current is not None
    return current


def request_cancel(conn: sqlite3.Connection, job_id: str) -> Dict[str, Any]:
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    timestamp = db.now()
    if job['status'] == 'queued':
        conn.execute(
            "UPDATE vision_jobs SET status='cancelled', cancel_requested=1, "
            'finished_at=?, updated_at=? WHERE id=?',
            (timestamp, timestamp, job_id),
        )
    elif job['status'] == 'running':
        conn.execute(
            'UPDATE vision_jobs SET cancel_requested=1, updated_at=? WHERE id=?',
            (timestamp, job_id),
        )
    conn.commit()
    current = get_job(conn, job_id)
    assert current is not None
    return current


def _leased_job(
    conn: sqlite3.Connection, job_id: str, worker_id: str, lease_token: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM vision_jobs WHERE id=? AND status='running' "
        'AND worker_id=? AND lease_token=?',
        (job_id, worker_id, lease_token),
    ).fetchone()
    if row is None:
        raise PermissionError('Vision Worker 任务租约无效或已过期')
    return row
