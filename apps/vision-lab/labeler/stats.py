"""数据检查统计:事件数、正负样本、模式/质量分布、冲突检测。"""

from __future__ import annotations

import json
from typing import Any, Dict


def stats(conn: Any) -> Dict[str, Any]:
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    s: Dict[str, Any] = {}

    s['videos'] = q('SELECT COUNT(*) FROM videos')
    s['videos_extracted'] = q("SELECT COUNT(*) FROM videos WHERE status='done'")
    s['streamers'] = q('SELECT COUNT(DISTINCT streamer) FROM videos')
    s['frames'] = q('SELECT COUNT(*) FROM frames')
    s['frames_labeled'] = q('SELECT COUNT(*) FROM frames WHERE labeled=1')
    s['duplicate_groups'] = q(
        'SELECT COUNT(*) FROM (SELECT sha256 FROM frames GROUP BY sha256 HAVING COUNT(*)>1)')
    s['events'] = q('SELECT COUNT(*) FROM events')

    # 结算正样本(权威结论 = screen_type=result_page)
    s['result_positives'] = q(
        "SELECT COUNT(*) FROM annotations WHERE screen_type='result_page'")
    s['result_representatives'] = q(
        "SELECT COUNT(*) FROM frames f JOIN annotations a ON a.frame_id=f.id "
        "WHERE a.screen_type='result_page' AND f.is_representative=1")
    # 积分板 hard negative
    s['scoreboard_negatives'] = q(
        "SELECT COUNT(*) FROM annotations WHERE screen_type IN "
        "('scoreboard','death_scoreboard')")
    # 随机负样本(非虚荣/其他游戏外)
    s['random_negatives'] = q(
        "SELECT COUNT(*) FROM annotations WHERE content_family='not_vainglory' "
        "OR screen_type='gameplay'")
    # 有框的正样本
    s['result_with_bbox'] = q(
        "SELECT COUNT(*) FROM boxes b JOIN annotations a ON a.frame_id=b.frame_id "
        "WHERE a.screen_type='result_page' AND b.box_type='result_panel'")

    # 分布
    s['game_modes'] = dict(conn.execute(
        "SELECT game_mode, COUNT(*) FROM annotations WHERE game_mode IS NOT NULL "
        "GROUP BY game_mode").fetchall())
    s['screen_types'] = dict(conn.execute(
        "SELECT COALESCE(screen_type,'(未标)'), COUNT(*) FROM annotations "
        "GROUP BY screen_type ORDER BY 2 DESC").fetchall())
    s['match_kinds'] = dict(conn.execute(
        "SELECT match_kind, COUNT(*) FROM annotations GROUP BY match_kind").fetchall())
    s['view_contexts'] = dict(conn.execute(
        "SELECT view_context, COUNT(*) FROM annotations GROUP BY view_context").fetchall())

    # 质量覆盖
    flag_counts: Dict[str, int] = {}
    for (flags_json,) in conn.execute(
            'SELECT quality_flags FROM annotations WHERE quality_flags != "[]"'):
        for flag in json.loads(flags_json or '[]'):
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
    s['quality_flags'] = flag_counts
    s['black_bars'] = dict(conn.execute(
        'SELECT black_bars, COUNT(*) FROM annotations GROUP BY black_bars').fetchall())
    s['windowed'] = q(
        "SELECT COUNT(*) FROM annotations a JOIN boxes b ON b.frame_id=a.frame_id "
        "WHERE b.box_type='viewport' AND (b.x > 0.02 OR b.y > 0.02 OR "
        "b.w < 0.96 OR b.h < 0.96)")

    # 冲突/缺失检测
    issues: Dict[str, Any] = {}
    issues['missing_content_family'] = q(
        'SELECT COUNT(*) FROM annotations WHERE content_family IS NULL')
    issues['not_vg_has_game_context'] = q(
        "SELECT COUNT(*) FROM annotations WHERE content_family='not_vainglory' "
        "AND game_context IS NOT NULL")
    issues['result_without_bbox'] = q(
        "SELECT COUNT(*) FROM annotations a WHERE a.screen_type='result_page' "
        "AND NOT EXISTS (SELECT 1 FROM boxes b WHERE b.frame_id=a.frame_id "
        "AND b.box_type='result_panel')")
    issues['result_without_ocr'] = q(
        "SELECT COUNT(*) FROM annotations WHERE screen_type='result_page' "
        "AND ocr_usable IS NULL")
    s['issues'] = issues

    # 切分防泄漏前置:事件跨视频数量(应为 0)
    s['events_cross_video'] = q(
        'SELECT COUNT(*) FROM (SELECT video_id FROM events GROUP BY video_id '
        'HAVING COUNT(DISTINCT video_id) > 1)')
    return s
