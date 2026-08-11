"""新模型共用的一图多标签复核与旧人工数据迁移。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Optional

from . import db

_MIGRATION_ID = 'training-review-labels-v1'
_HERO_SELECT_TYPES = {'hero_select_bp', 'hero_select_blind', 'hero_select_aram'}


def _review_status(values: Dict[str, Optional[str]], *, has_result_box: bool) -> str:
    flow = values.get('match_flow_label')
    mode = values.get('match_mode_label')
    select = values.get('hero_select_label')
    result = values.get('result_panel_label')
    if flow is None or result is None:
        return 'partial'
    if flow == 'match_flow' and (mode is None or select != 'not_select'):
        return 'partial'
    if flow == 'not_match_flow' and (select is None or mode is not None):
        return 'partial'
    if flow == 'unreadable' and mode is not None:
        return 'partial'
    if result == 'result_panel' and not has_result_box:
        return 'partial'
    return 'confirmed'


def _upsert_human_labels(
    conn: sqlite3.Connection,
    *,
    frame_id: int,
    labels: Dict[str, Optional[str]],
    source_type: str,
    source_id: str,
    metadata: Dict[str, Any],
) -> None:
    timestamp = db.now()
    current = conn.execute(
        'SELECT * FROM training_review_items WHERE frame_id = ?', (int(frame_id),)
    ).fetchone()
    values: Dict[str, Optional[str]] = {
        'match_flow_label': None,
        'match_mode_label': None,
        'hero_select_label': None,
        'hero_select_variant': None,
        'result_panel_label': None,
    }
    if current is not None:
        for key in values:
            values[key] = current[key]
    for key, value in labels.items():
        values[key] = value
    has_result_box = (
        conn.execute(
            "SELECT 1 FROM boxes WHERE frame_id = ? AND box_type = 'result_panel'",
            (int(frame_id),),
        ).fetchone()
        is not None
    )
    status = _review_status(values, has_result_box=has_result_box)
    conn.execute(
        """
        INSERT INTO training_review_items
            (frame_id, match_flow_label, match_mode_label, hero_select_label,
             hero_select_variant, result_panel_label, review_status,
             created_at, updated_at,
             reviewed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(frame_id) DO UPDATE SET
            match_flow_label=excluded.match_flow_label,
            match_mode_label=excluded.match_mode_label,
            hero_select_label=excluded.hero_select_label,
            hero_select_variant=excluded.hero_select_variant,
            result_panel_label=excluded.result_panel_label,
            review_status=excluded.review_status,
            updated_at=excluded.updated_at,
            reviewed_at=excluded.reviewed_at
        """,
        (
            int(frame_id),
            values['match_flow_label'],
            values['match_mode_label'],
            values['hero_select_label'],
            values['hero_select_variant'],
            values['result_panel_label'],
            status,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    conn.execute(
        """
        INSERT INTO training_review_sources
            (frame_id, source_type, source_id, suggestions_json, metadata_json,
             created_at, updated_at)
        VALUES (?, ?, ?, '{}', ?, ?, ?)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            frame_id=excluded.frame_id,
            metadata_json=excluded.metadata_json,
            updated_at=excluded.updated_at
        """,
        (
            int(frame_id),
            source_type,
            source_id,
            json.dumps(
                metadata, ensure_ascii=False, separators=(',', ':'), sort_keys=True
            ),
            timestamp,
            timestamp,
        ),
    )


def _annotation_labels(row: sqlite3.Row, has_result_box: bool) -> Dict[str, Any]:
    content = str(row['content_family'] or '')
    context = str(row['game_context'] or '')
    screen_type = str(row['screen_type'] or '')
    game_mode = str(row['game_mode'] or '')
    if content == 'not_vainglory' or context in ('out_of_match', 'pre_match'):
        flow: Optional[str] = 'not_match_flow'
    elif context in ('in_match', 'post_match'):
        flow = 'match_flow'
    elif context in ('transition', 'unknown') or content == 'uncertain':
        flow = 'unreadable'
    else:
        return {}

    mode: Optional[str] = None
    if flow == 'match_flow':
        if context == 'in_match' and screen_type == 'gameplay':
            mode = game_mode if game_mode in ('3v3', 'aram', '5v5') else 'unreadable'
        elif context == 'in_match' and screen_type == 'talent_select':
            mode = 'aram'
        else:
            # 商店、赛后页以及三人积分板本身不能可靠区分 3V3/大乱斗。
            mode = 'unreadable'

    if screen_type in _HERO_SELECT_TYPES:
        if screen_type == 'hero_select_aram':
            select = 'select_aram'
            select_variant: Optional[str] = 'random'
        else:
            select = {
                '3v3': 'select_3v3',
                'aram': 'select_aram',
                '5v5': 'select_5v5',
            }.get(game_mode, 'unreadable')
            if select == 'select_aram':
                select_variant = 'random'
            elif select in ('select_3v3', 'select_5v5'):
                select_variant = 'bp' if screen_type == 'hero_select_bp' else 'blind'
            else:
                select_variant = None
    else:
        select = 'not_select'
        select_variant = None

    if screen_type == 'result_page':
        result: Optional[str] = 'result_panel' if has_result_box else None
    else:
        result = 'no_result_panel'
    return {
        'match_flow_label': flow,
        'match_mode_label': mode,
        'hero_select_label': select,
        'hero_select_variant': select_variant,
        'result_panel_label': result,
    }


def migrate_legacy_training_reviews(conn: sqlite3.Connection) -> Dict[str, int]:
    """把已有人工真值映射到新标签；原表和原框只读保留。"""
    prior = conn.execute(
        'SELECT detail_json FROM workspace_migrations WHERE id = ?', (_MIGRATION_ID,)
    ).fetchone()
    if prior is not None:
        value = json.loads(prior['detail_json'] or '{}')
        return {str(key): int(count) for key, count in value.items()}

    counts = {
        'legacy_annotations': 0,
        'bp_reviews': 0,
        'key_screen_reviews': 0,
        'mode_gate_annotations': 0,
    }
    with conn:
        annotations = conn.execute(
            """
            SELECT annotation.*,
                   EXISTS (
                       SELECT 1 FROM boxes box
                       WHERE box.frame_id = annotation.frame_id
                         AND box.box_type = 'result_panel'
                   ) AS has_result_box
            FROM annotations annotation
            WHERE annotation.annotation_status = 'complete'
            ORDER BY annotation.frame_id
            """
        ).fetchall()
        for row in annotations:
            labels = _annotation_labels(row, bool(row['has_result_box']))
            if not labels:
                continue
            _upsert_human_labels(
                conn,
                frame_id=int(row['frame_id']),
                labels=labels,
                source_type='legacy_annotation',
                source_id='frame:{}'.format(int(row['frame_id'])),
                metadata={
                    'content_family': row['content_family'],
                    'game_context': row['game_context'],
                    'screen_type': row['screen_type'],
                    'game_mode': row['game_mode'],
                },
            )
            counts['legacy_annotations'] += 1

        bp_rows = conn.execute(
            """
            SELECT frame_id, confirmed_label, visual_condition
            FROM bp_review_items
            WHERE review_status = 'confirmed' AND confirmed_label IS NOT NULL
            ORDER BY frame_id
            """
        ).fetchall()
        for row in bp_rows:
            old_label = str(row['confirmed_label'])
            select = {
                'bp_3v3': 'select_3v3',
                'bp_aram': 'select_aram',
                'bp_5v5': 'select_5v5',
                'not_bp': 'not_select',
            }.get(old_label)
            if select is None:
                continue
            if row['visual_condition'] == 'unreadable':
                select = 'unreadable'
            labels: Dict[str, Optional[str]] = {'hero_select_label': select}
            if old_label == 'bp_aram' and select == 'select_aram':
                labels['hero_select_variant'] = 'random'
            elif old_label in {'bp_3v3', 'bp_5v5'} and select in {
                'select_3v3',
                'select_5v5',
            }:
                labels['hero_select_variant'] = 'bp'
            if old_label != 'not_bp':
                labels.update(
                    match_flow_label='not_match_flow',
                    match_mode_label=None,
                    result_panel_label='no_result_panel',
                )
            _upsert_human_labels(
                conn,
                frame_id=int(row['frame_id']),
                labels=labels,
                source_type='legacy_bp_review',
                source_id='frame:{}'.format(int(row['frame_id'])),
                metadata={
                    'confirmed_label': old_label,
                    'visual_condition': row['visual_condition'],
                },
            )
            counts['bp_reviews'] += 1

        key_rows = conn.execute(
            """
            SELECT item.frame_id, item.confirmed_label, item.visual_condition,
                   EXISTS (
                       SELECT 1 FROM boxes box
                       WHERE box.frame_id = item.frame_id
                         AND box.box_type = 'result_panel'
                   ) AS has_result_box
            FROM key_screen_review_items item
            WHERE item.review_status = 'confirmed'
              AND item.confirmed_label IS NOT NULL
            ORDER BY item.frame_id
            """
        ).fetchall()
        for row in key_rows:
            label = str(row['confirmed_label'])
            if label == 'result_page':
                labels = {
                    'match_flow_label': 'match_flow',
                    'match_mode_label': 'unreadable',
                    'hero_select_label': 'not_select',
                    'result_panel_label': (
                        'result_panel' if row['has_result_box'] else None
                    ),
                }
            elif label == 'scoreboard':
                labels = {
                    'match_flow_label': 'match_flow',
                    'match_mode_label': 'unreadable',
                    'hero_select_label': 'not_select',
                    'result_panel_label': 'no_result_panel',
                }
            else:
                labels = {'result_panel_label': 'no_result_panel'}
            _upsert_human_labels(
                conn,
                frame_id=int(row['frame_id']),
                labels=labels,
                source_type='legacy_key_screen_review',
                source_id='frame:{}'.format(int(row['frame_id'])),
                metadata={
                    'confirmed_label': label,
                    'visual_condition': row['visual_condition'],
                },
            )
            counts['key_screen_reviews'] += 1

        gate_rows = conn.execute(
            """
            SELECT round_id, frame_id, evidence
            FROM mode_gate_annotations
            WHERE evidence IN ('blocked_gate', 'open_entrance')
            ORDER BY round_id, frame_id
            """
        ).fetchall()
        for row in gate_rows:
            mode = 'aram' if row['evidence'] == 'blocked_gate' else '3v3'
            _upsert_human_labels(
                conn,
                frame_id=int(row['frame_id']),
                labels={
                    'match_flow_label': 'match_flow',
                    'match_mode_label': mode,
                    'hero_select_label': 'not_select',
                    'result_panel_label': 'no_result_panel',
                },
                source_type='legacy_mode_gate',
                source_id='{}:{}'.format(row['round_id'], int(row['frame_id'])),
                metadata={'evidence': row['evidence']},
            )
            counts['mode_gate_annotations'] += 1

        conn.execute(
            'INSERT INTO workspace_migrations (id, applied_at, detail_json) '
            'VALUES (?, ?, ?)',
            (
                _MIGRATION_ID,
                db.now(),
                json.dumps(counts, ensure_ascii=False, sort_keys=True),
            ),
        )
    return counts


def queue_legacy_pending_reviews(conn: sqlite3.Connection) -> Dict[str, int]:
    """把旧入口尚未确认的素材放进统一队列；旧建议不冒充人工真值。"""
    counts = {
        'legacy_annotations': 0,
        'bp_candidates': 0,
        'key_screen_candidates': 0,
        'mode_gate_candidates': 0,
    }
    annotations = conn.execute(
        """
        SELECT annotation.*, frame.frame_path
        FROM annotations annotation
        JOIN frames frame ON frame.id = annotation.frame_id
        WHERE annotation.annotation_status != 'complete'
        ORDER BY annotation.frame_id
        """
    ).fetchall()
    for row in annotations:
        counts['legacy_annotations'] += int(
            db.add_training_review_source(
                conn,
                frame_id=int(row['frame_id']),
                source_type='legacy_annotation_pending',
                source_id='frame:{}'.format(int(row['frame_id'])),
                metadata={
                    'content_family': row['content_family'],
                    'game_context': row['game_context'],
                    'screen_type': row['screen_type'],
                    'game_mode': row['game_mode'],
                    'legacy_status': row['annotation_status'],
                },
                image_path=str(row['frame_path'] or ''),
            )
        )

    bp_rows = conn.execute(
        """
        SELECT item.*, frame.frame_path
        FROM bp_review_items item
        JOIN frames frame ON frame.id = item.frame_id
        WHERE item.review_status = 'pending'
        ORDER BY item.frame_id
        """
    ).fetchall()
    bp_labels = {
        'bp_3v3': 'select_3v3',
        'bp_aram': 'select_aram',
        'bp_5v5': 'select_5v5',
        'not_bp': 'not_select',
    }
    for row in bp_rows:
        select = bp_labels.get(str(row['suggested_label']))
        suggestions: Dict[str, Any] = {}
        if select is not None:
            confidence = float(row['suggestion_confidence'] or 0)
            suggestions['hero_select'] = {
                'label': select,
                'confidence': confidence,
                'origin': 'legacy_candidate',
                'reason': str(row['selection_reason'] or '旧 BP 候选'),
            }
            if select.startswith('select_'):
                suggestions['match_flow'] = {
                    'label': 'not_match_flow',
                    'confidence': float(row['pre_match_confidence'] or confidence),
                    'origin': 'legacy_candidate',
                    'reason': '旧 BP 候选',
                }
                suggestions['result_panel'] = {
                    'label': 'no_result_panel',
                    'confidence': confidence,
                    'origin': 'legacy_candidate',
                    'reason': '旧 BP 候选',
                }
        counts['bp_candidates'] += int(
            db.add_training_review_source(
                conn,
                frame_id=int(row['frame_id']),
                source_type='legacy_bp_candidate',
                source_id='frame:{}'.format(int(row['frame_id'])),
                suggestions=suggestions,
                metadata={
                    'suggested_label': row['suggested_label'],
                    'stage_class': row['stage_class'],
                    'mode_class': row['mode_class'],
                    'selection_reason': row['selection_reason'],
                },
                image_path=str(row['frame_path'] or ''),
            )
        )

    key_rows = conn.execute(
        """
        SELECT item.*, frame.frame_path
        FROM key_screen_review_items item
        JOIN frames frame ON frame.id = item.frame_id
        WHERE item.review_status = 'pending'
        ORDER BY item.frame_id
        """
    ).fetchall()
    for row in key_rows:
        label = str(row['suggested_label'])
        confidence = float(row['suggestion_confidence'] or 0)
        suggestions: Dict[str, Any] = {
            'result_panel': {
                'label': (
                    'result_panel' if label == 'result_page' else 'no_result_panel'
                ),
                'confidence': confidence,
                'origin': 'legacy_candidate',
                'reason': str(row['selection_reason'] or '旧关键界面候选'),
            }
        }
        if label in {'result_page', 'scoreboard'}:
            suggestions['match_flow'] = {
                'label': 'match_flow',
                'confidence': confidence,
                'origin': 'legacy_candidate',
                'reason': '旧关键界面候选',
            }
        counts['key_screen_candidates'] += int(
            db.add_training_review_source(
                conn,
                frame_id=int(row['frame_id']),
                source_type='legacy_key_screen_candidate',
                source_id='frame:{}'.format(int(row['frame_id'])),
                suggestions=suggestions,
                metadata={
                    'screen_type': (
                        label if label in {'result_page', 'scoreboard'} else ''
                    ),
                    'suggested_label': label,
                    'selection_reason': row['selection_reason'],
                },
                image_path=str(row['frame_path'] or ''),
            )
        )

    gate_rows = conn.execute(
        """
        SELECT annotation.round_id, annotation.frame_id, frame.frame_path
        FROM mode_gate_annotations annotation
        JOIN frames frame ON frame.id = annotation.frame_id
        WHERE annotation.evidence = 'no_evidence'
        ORDER BY annotation.round_id, annotation.frame_id
        """
    ).fetchall()
    for row in gate_rows:
        counts['mode_gate_candidates'] += int(
            db.add_training_review_source(
                conn,
                frame_id=int(row['frame_id']),
                source_type='legacy_mode_gate_candidate',
                source_id='{}:{}'.format(row['round_id'], int(row['frame_id'])),
                suggestions={
                    'match_flow': {
                        'label': 'match_flow',
                        'confidence': 1.0,
                        'origin': 'legacy_candidate',
                        'reason': '旧光栅标注：本帧看不出模式证据',
                    },
                    'match_mode': {
                        'label': 'unreadable',
                        'confidence': 1.0,
                        'origin': 'legacy_candidate',
                        'reason': '旧光栅标注：本帧看不出模式证据',
                    },
                },
                metadata={'stage_class': 'gameplay', 'evidence': 'no_evidence'},
                image_path=str(row['frame_path'] or ''),
            )
        )
    return counts


def mirror_confirmed_bp_review(
    conn: sqlite3.Connection, frame_id: int
) -> Optional[Dict[str, Any]]:
    """把迁移完成后仍从旧 BP 入口保存的人工结论同步到统一真值。"""
    row = conn.execute(
        'SELECT confirmed_label, visual_condition FROM bp_review_items '
        "WHERE frame_id = ? AND review_status = 'confirmed' "
        'AND confirmed_label IS NOT NULL',
        (int(frame_id),),
    ).fetchone()
    if row is None:
        return None
    old_label = str(row['confirmed_label'])
    select = {
        'bp_3v3': 'select_3v3',
        'bp_aram': 'select_aram',
        'bp_5v5': 'select_5v5',
        'not_bp': 'not_select',
    }.get(old_label)
    if select is None:
        return None
    if row['visual_condition'] == 'unreadable':
        select = 'unreadable'
    labels: Dict[str, Optional[str]] = {'hero_select_label': select}
    if old_label == 'bp_aram' and select == 'select_aram':
        labels['hero_select_variant'] = 'random'
    elif old_label in {'bp_3v3', 'bp_5v5'} and select in {'select_3v3', 'select_5v5'}:
        labels['hero_select_variant'] = 'bp'
    if old_label != 'not_bp':
        labels.update(
            match_flow_label='not_match_flow',
            match_mode_label=None,
            result_panel_label='no_result_panel',
        )
    with conn:
        _upsert_human_labels(
            conn,
            frame_id=int(frame_id),
            labels=labels,
            source_type='legacy_bp_review',
            source_id=f'frame:{int(frame_id)}',
            metadata={
                'confirmed_label': old_label,
                'visual_condition': row['visual_condition'],
            },
        )
    return db.get_training_review_item(conn, int(frame_id))


def mirror_confirmed_key_screen_review(
    conn: sqlite3.Connection, frame_id: int
) -> Optional[Dict[str, Any]]:
    """把迁移完成后仍从旧关键界面入口保存的结论同步到统一真值。"""
    row = conn.execute(
        'SELECT confirmed_label, visual_condition FROM key_screen_review_items '
        "WHERE frame_id = ? AND review_status = 'confirmed' "
        'AND confirmed_label IS NOT NULL',
        (int(frame_id),),
    ).fetchone()
    if row is None:
        return None
    label = str(row['confirmed_label'])
    has_result_box = (
        conn.execute(
            "SELECT 1 FROM boxes WHERE frame_id = ? AND box_type = 'result_panel'",
            (int(frame_id),),
        ).fetchone()
        is not None
    )
    if label == 'result_page':
        labels: Dict[str, Optional[str]] = {
            'match_flow_label': 'match_flow',
            'match_mode_label': 'unreadable',
            'hero_select_label': 'not_select',
            'result_panel_label': 'result_panel' if has_result_box else None,
        }
    elif label == 'scoreboard':
        labels = {
            'match_flow_label': 'match_flow',
            'match_mode_label': 'unreadable',
            'hero_select_label': 'not_select',
            'result_panel_label': 'no_result_panel',
        }
    else:
        labels = {'result_panel_label': 'no_result_panel'}
    with conn:
        _upsert_human_labels(
            conn,
            frame_id=int(frame_id),
            labels=labels,
            source_type='legacy_key_screen_review',
            source_id=f'frame:{int(frame_id)}',
            metadata={
                'confirmed_label': label,
                'visual_condition': row['visual_condition'],
            },
        )
    return db.get_training_review_item(conn, int(frame_id))
