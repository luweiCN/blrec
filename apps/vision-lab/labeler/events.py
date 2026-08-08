"""事件分组:把同一画面连续出现(近似帧+时间接近)的帧聚成一个事件片段。"""

from __future__ import annotations

from typing import Any, Dict, List

from . import db
from .extract import phash_distance

# 相邻帧归入同一事件的条件
MAX_GAP_MS = 8000        # 时间间隔上限
MAX_PHASH_DIST = 12      # 感知哈希汉明距离上限


def auto_group(conn: Any, video_id: int, *, max_gap_ms: int = MAX_GAP_MS,
               max_phash_dist: int = MAX_PHASH_DIST) -> List[Dict[str, Any]]:
    """对视频内帧做事件聚类,返回创建的事件列表。"""
    rows = conn.execute(
        'SELECT id, timestamp_ms, phash FROM frames '
        'WHERE video_id = ? ORDER BY timestamp_ms', (video_id,)
    ).fetchall()
    events: List[int] = []
    current_event_id = None
    current_start = 0
    current_end = 0
    for i, row in enumerate(rows):
        if i == 0:
            current_start = current_end = row['timestamp_ms']
            current_event_id = db.create_event(conn, video_id, current_start,
                                               current_end, 'candidate')
            events.append(current_event_id)
            db.assign_event(conn, [row['id']], current_event_id)
            continue
        prev = rows[i - 1]
        same_time = row['timestamp_ms'] - prev['timestamp_ms'] <= max_gap_ms
        same_image = (not row['phash'] or not prev['phash']
                      or phash_distance(row['phash'], prev['phash']) <= max_phash_dist)
        if same_time and same_image and current_event_id is not None:
            current_end = row['timestamp_ms']
            conn.execute('UPDATE events SET end_ms = ? WHERE id = ?',
                         (current_end, current_event_id))
            conn.execute('UPDATE frames SET event_id = ? WHERE id = ?',
                         (current_event_id, row['id']))
        else:
            current_start = current_end = row['timestamp_ms']
            current_event_id = db.create_event(conn, video_id, current_start,
                                               current_end, 'candidate')
            events.append(current_event_id)
            db.assign_event(conn, [row['id']], current_event_id)
    conn.commit()
    return [dict(r) for r in conn.execute(
        'SELECT * FROM events WHERE id IN (%s) ORDER BY id'
        % ','.join('?' * len(events)), events).fetchall()] if events else []


def group_all_unassigned(conn: Any) -> Dict[int, List[Dict[str, Any]]]:
    """为所有尚未分组且已抽帧的视频执行 auto_group。返回 {video_id: [events]}。"""
    video_ids = [r['video_id'] for r in conn.execute(
        'SELECT DISTINCT video_id FROM frames WHERE event_id IS NULL').fetchall()]
    out: Dict[int, List[Dict[str, Any]]] = {}
    for vid in video_ids:
        out[vid] = auto_group(conn, vid)
    return out
