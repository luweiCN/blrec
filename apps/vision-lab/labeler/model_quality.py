"""按模型版本物化预打标与人工真值的对照结果。"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from . import db

TASKS: Dict[str, Dict[str, str]] = {
    'match_flow': {'name': '是否在对局中', 'metric': 'accuracy'},
    'match_mode': {'name': '对局模式', 'metric': 'accuracy'},
    'hero_select': {'name': '英雄选择', 'metric': 'accuracy'},
    'result_detector': {'name': '结算面板', 'metric': 'accuracy'},
    'hero_avatar_detector': {'name': '头像位置找齐', 'metric': 'complete_rate'},
    'hero_identity': {'name': '英雄身份', 'metric': 'accuracy'},
    'player_position': {'name': '主播本人', 'metric': 'accuracy'},
    'afk_status': {'name': '挂机状态', 'metric': 'accuracy'},
}

_CORE_TASKS = {
    'match_flow': ('match_flow', 'match_flow_label'),
    'match_mode': ('match_mode', 'match_mode_label'),
    'hero_select': ('hero_select', 'hero_select_label'),
    'result_detector': ('result_panel', 'result_panel_label'),
}


def _object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or '{}'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _usable_label(value: Any) -> str:
    label = str(value or '')
    return '' if label in {'', 'unreadable'} else label


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value or 0)))
    except (TypeError, ValueError):
        return 0.0


def _source(conn: Any, frame_id: int, source_type: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        'SELECT suggestions_json,metadata_json FROM training_review_sources '
        'WHERE frame_id=? AND source_type=? '
        'ORDER BY updated_at DESC,id DESC LIMIT 1',
        (int(frame_id), source_type),
    ).fetchone()
    if row is None:
        return None
    return {
        'suggestions': _object(row['suggestions_json']),
        'metadata': _object(row['metadata_json']),
    }


def _sync_truth(
    conn: Any,
    *,
    frame_id: int,
    task_id: str,
    subject_key: str,
    confirmed_label: str,
    screen_type: str,
    match_mode: str,
    timestamp: str,
) -> None:
    if not confirmed_label:
        conn.execute(
            'DELETE FROM training_review_model_outcomes '
            'WHERE frame_id=? AND task_id=? AND subject_key=?',
            (int(frame_id), task_id, subject_key),
        )
        return
    conn.execute(
        'UPDATE training_review_model_outcomes SET confirmed_label=?, '
        'is_correct=CASE WHEN predicted_label=? THEN 1 ELSE 0 END, '
        'screen_type=?,match_mode=?,updated_at=? '
        'WHERE frame_id=? AND task_id=? AND subject_key=?',
        (
            confirmed_label,
            confirmed_label,
            screen_type,
            match_mode,
            timestamp,
            int(frame_id),
            task_id,
            subject_key,
        ),
    )


def _upsert(
    conn: Any,
    *,
    frame_id: int,
    task_id: str,
    model_run_id: str,
    subject_key: str,
    metric: str,
    predicted_label: str,
    confirmed_label: str,
    confidence: float,
    screen_type: str,
    match_mode: str,
    source_type: str,
    timestamp: str,
) -> bool:
    if not model_run_id or not predicted_label or not confirmed_label:
        return False
    conn.execute(
        """
        INSERT INTO training_review_model_outcomes
            (frame_id,task_id,model_run_id,subject_key,metric,
             predicted_label,confirmed_label,confidence,screen_type,
             match_mode,is_correct,source_type,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,CASE WHEN ?=? THEN 1 ELSE 0 END,?,?,?)
        ON CONFLICT(frame_id,task_id,model_run_id,subject_key) DO UPDATE SET
            metric=excluded.metric,
            predicted_label=excluded.predicted_label,
            confirmed_label=excluded.confirmed_label,
            confidence=excluded.confidence,
            screen_type=excluded.screen_type,
            match_mode=excluded.match_mode,
            is_correct=excluded.is_correct,
            source_type=excluded.source_type,
            updated_at=excluded.updated_at
        """,
        (
            int(frame_id),
            task_id,
            model_run_id[:200],
            subject_key[:80],
            metric,
            predicted_label[:100],
            confirmed_label[:100],
            _confidence(confidence),
            screen_type[:40],
            match_mode[:40],
            predicted_label[:100],
            confirmed_label[:100],
            source_type[:80],
            timestamp,
            timestamp,
        ),
    )
    return True


def refresh_frame(
    conn: Any,
    frame_id: int,
    *,
    hero_lineup: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> int:
    """刷新一帧的当前真值，同时保留此前已见过的模型版本。"""
    item = conn.execute(
        'SELECT * FROM training_review_items WHERE frame_id=?', (int(frame_id),)
    ).fetchone()
    if item is None:
        return 0
    timestamp = db.now()
    match_mode = _usable_label(item['match_mode_label'])
    screen_type = _usable_label(item['hero_layout_label'])
    if not screen_type and str(item['hero_select_label'] or '').startswith('select_'):
        screen_type = 'hero_select'
    elif not screen_type and item['result_panel_label'] == 'result_panel':
        screen_type = 'result_page'

    written = 0
    core = _source(conn, frame_id, 'new_model_prefill')
    core_metadata = (core or {}).get('metadata') or {}
    core_runs = _object(core_metadata.get('model_runs'))
    core_suggestions = (core or {}).get('suggestions') or {}
    for task_id, (suggestion_key, truth_column) in _CORE_TASKS.items():
        truth = (
            _usable_label(item[truth_column])
            if item['review_status'] == 'confirmed'
            else ''
        )
        _sync_truth(
            conn,
            frame_id=frame_id,
            task_id=task_id,
            subject_key='frame',
            confirmed_label=truth,
            screen_type=screen_type,
            match_mode=match_mode,
            timestamp=timestamp,
        )
        suggestion = _object(core_suggestions.get(suggestion_key))
        written += int(
            _upsert(
                conn,
                frame_id=frame_id,
                task_id=task_id,
                model_run_id=str(core_runs.get(task_id) or ''),
                subject_key='frame',
                metric='accuracy',
                predicted_label=_usable_label(suggestion.get('label')),
                confirmed_label=truth,
                confidence=_confidence(suggestion.get('confidence')),
                screen_type=screen_type,
                match_mode=match_mode,
                source_type='new_model_prefill',
                timestamp=timestamp,
            )
        )

    lineup = hero_lineup or db.get_training_review_hero_lineup(conn, int(frame_id))
    hero = _source(conn, frame_id, 'new_model_hero_prefill')
    hero_metadata = (hero or {}).get('metadata') or {}
    hero_runs = _object(hero_metadata.get('model_runs'))
    if lineup is not None and lineup['review_status'] == 'confirmed':
        lineup_screen = str(lineup['screen_type'])
        same_context = bool(
            hero
            and str(hero_metadata.get('screen_type') or '') == lineup_screen
            and int(hero_metadata.get('team_size') or 0) == int(lineup['team_size'])
        )
        identity_run = str(hero_runs.get('hero_identity') or '') if same_context else ''
        current_subjects = set()
        for slot in lineup.get('slots') or []:
            subject = '{}:{}'.format(slot['side'], slot['slot'])
            current_subjects.add(subject)
            truth = _usable_label(slot.get('confirmed_label'))
            _sync_truth(
                conn,
                frame_id=frame_id,
                task_id='hero_identity',
                subject_key=subject,
                confirmed_label=truth,
                screen_type=lineup_screen,
                match_mode=match_mode,
                timestamp=timestamp,
            )
            written += int(
                _upsert(
                    conn,
                    frame_id=frame_id,
                    task_id='hero_identity',
                    model_run_id=identity_run,
                    subject_key=subject,
                    metric='accuracy',
                    predicted_label=_usable_label(slot.get('suggested_label')),
                    confirmed_label=truth,
                    confidence=_confidence(slot.get('suggestion_confidence')),
                    screen_type=lineup_screen,
                    match_mode=match_mode,
                    source_type='new_model_hero_prefill',
                    timestamp=timestamp,
                )
            )
        existing_subjects = conn.execute(
            'SELECT DISTINCT subject_key FROM training_review_model_outcomes '
            "WHERE frame_id=? AND task_id='hero_identity'",
            (int(frame_id),),
        ).fetchall()
        for row in existing_subjects:
            if str(row['subject_key']) not in current_subjects:
                conn.execute(
                    'DELETE FROM training_review_model_outcomes '
                    "WHERE frame_id=? AND task_id='hero_identity' "
                    'AND subject_key=?',
                    (int(frame_id), str(row['subject_key'])),
                )

        player_truth = ''
        if lineup.get('player_status') == 'identified':
            player_truth = '{}{}'.format(
                lineup.get('player_side') or '', lineup.get('player_slot') or ''
            )
        _sync_truth(
            conn,
            frame_id=frame_id,
            task_id='player_position',
            subject_key='lineup',
            confirmed_label=player_truth,
            screen_type=lineup_screen,
            match_mode=match_mode,
            timestamp=timestamp,
        )
        player_prediction = _object(hero_metadata.get('player_suggestion'))
        player_label = ''
        if player_prediction.get('side') and player_prediction.get('slot'):
            player_label = '{}{}'.format(
                player_prediction['side'], player_prediction['slot']
            )
        written += int(
            _upsert(
                conn,
                frame_id=frame_id,
                task_id='player_position',
                model_run_id=(
                    str(hero_runs.get('player_position') or '') if same_context else ''
                ),
                subject_key='lineup',
                metric='accuracy',
                predicted_label=player_label,
                confirmed_label=player_truth,
                confidence=_confidence(player_prediction.get('confidence')),
                screen_type=lineup_screen,
                match_mode=match_mode,
                source_type='new_model_hero_prefill',
                timestamp=timestamp,
            )
        )

        expected_slots = int(lineup['team_size']) * 2
        avatar_truth = (
            'complete'
            if len(lineup.get('slots') or []) == expected_slots
            else 'incomplete'
        )
        _sync_truth(
            conn,
            frame_id=frame_id,
            task_id='hero_avatar_detector',
            subject_key='lineup',
            confirmed_label=avatar_truth,
            screen_type=lineup_screen,
            match_mode=match_mode,
            timestamp=timestamp,
        )
        avatar_prediction = (
            'complete' if hero_metadata.get('complete') else 'incomplete'
        )
        detected = int(hero_metadata.get('detected') or 0)
        written += int(
            _upsert(
                conn,
                frame_id=frame_id,
                task_id='hero_avatar_detector',
                model_run_id=(
                    str(hero_runs.get('hero_avatar_detector') or '')
                    if same_context
                    else ''
                ),
                subject_key='lineup',
                metric='complete_rate',
                predicted_label=avatar_prediction,
                confirmed_label=avatar_truth,
                confidence=min(1.0, detected / expected_slots),
                screen_type=lineup_screen,
                match_mode=match_mode,
                source_type='new_model_hero_prefill',
                timestamp=timestamp,
            )
        )

    if commit:
        conn.commit()
    return written


def rebuild(conn: Any, *, batch_size: int = 500) -> Dict[str, int]:
    """从目前仍保留的模型来源重建版本化结果。"""
    conn.execute('DELETE FROM training_review_model_outcomes')
    rows = conn.execute(
        'SELECT DISTINCT source.frame_id FROM training_review_sources source '
        'LEFT JOIN training_review_items item ON item.frame_id=source.frame_id '
        'LEFT JOIN training_review_hero_lineups lineup '
        'ON lineup.frame_id=source.frame_id '
        "WHERE source.source_type IN ('new_model_prefill','new_model_hero_prefill') "
        "AND (item.review_status='confirmed' OR lineup.review_status='confirmed') "
        'ORDER BY source.frame_id'
    ).fetchall()
    frame_ids = [int(row['frame_id']) for row in rows]
    for offset in range(0, len(frame_ids), max(1, int(batch_size))):
        for frame_id in frame_ids[offset : offset + batch_size]:
            refresh_frame(conn, frame_id, commit=False)
        conn.commit()
    outcomes = int(
        conn.execute('SELECT COUNT(*) FROM training_review_model_outcomes').fetchone()[
            0
        ]
    )
    return {'frames': len(frame_ids), 'outcomes': outcomes}


def _version_sort_key(run_id: str) -> tuple[str, str]:
    digits = ''.join(character for character in run_id if character.isdigit())
    return digits, run_id


def summary(conn: Any) -> Dict[str, Any]:
    rows = conn.execute(
        'SELECT task_id,model_run_id,metric,screen_type,match_mode,'
        'predicted_label,confirmed_label,COUNT(*) AS compared,'
        'SUM(is_correct) AS correct,'
        'SUM(CASE WHEN is_correct=0 AND confidence>=0.85 THEN 1 ELSE 0 END) '
        'AS high_confidence_wrong,MAX(updated_at) AS updated_at '
        'FROM training_review_model_outcomes '
        'GROUP BY task_id,model_run_id,metric,screen_type,match_mode,'
        'predicted_label,confirmed_label'
    ).fetchall()
    grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        task_id = str(row['task_id'])
        run_id = str(row['model_run_id'])
        version = grouped.setdefault(task_id, {}).setdefault(
            run_id,
            {
                'run_id': run_id,
                'metric': str(row['metric']),
                'compared': 0,
                'correct': 0,
                'high_confidence_wrong': 0,
                'updated_at': '',
                'contexts': {},
                'confusions': {},
            },
        )
        compared = int(row['compared'] or 0)
        correct = int(row['correct'] or 0)
        version['compared'] += compared
        version['correct'] += correct
        version['high_confidence_wrong'] += int(row['high_confidence_wrong'] or 0)
        version['updated_at'] = max(
            str(version['updated_at']), str(row['updated_at'] or '')
        )
        context = '{}|{}'.format(row['screen_type'] or '', row['match_mode'] or '')
        context_value = version['contexts'].setdefault(
            context,
            {
                'screen_type': str(row['screen_type'] or ''),
                'match_mode': str(row['match_mode'] or ''),
                'compared': 0,
                'correct': 0,
            },
        )
        context_value['compared'] += compared
        context_value['correct'] += correct
        if str(row['predicted_label']) != str(row['confirmed_label']):
            confusion = '{}→{}'.format(row['confirmed_label'], row['predicted_label'])
            version['confusions'][confusion] = (
                int(version['confusions'].get(confusion, 0)) + compared
            )

    tasks = []
    for task_id, versions_by_id in grouped.items():
        versions = []
        for version in versions_by_id.values():
            version['wrong'] = version['compared'] - version['correct']
            version['accuracy'] = (
                version['correct'] / version['compared'] if version['compared'] else 0.0
            )
            version['correction_rate'] = 1.0 - version['accuracy']
            version['contexts'] = sorted(
                (
                    {
                        **value,
                        'accuracy': (
                            value['correct'] / value['compared']
                            if value['compared']
                            else 0.0
                        ),
                    }
                    for value in version['contexts'].values()
                ),
                key=lambda value: (-value['compared'], value['screen_type']),
            )
            version['confusions'] = [
                {'labels': labels, 'count': count}
                for labels, count in sorted(
                    version['confusions'].items(), key=lambda item: (-item[1], item[0])
                )[:8]
            ]
            version['status'] = (
                'insufficient'
                if version['compared'] < 30
                else (
                    'stable'
                    if version['accuracy'] >= 0.95
                    else 'watch' if version['accuracy'] >= 0.85 else 'needs_attention'
                )
            )
            versions.append(version)
        versions.sort(
            key=lambda value: _version_sort_key(value['run_id']), reverse=True
        )
        for index, version in enumerate(versions):
            version['latest'] = index == 0
            previous = versions[index + 1] if index + 1 < len(versions) else None
            version['change_points'] = (
                None
                if previous is None
                else round((version['accuracy'] - previous['accuracy']) * 100, 2)
            )
        tasks.append(
            {
                'id': task_id,
                'name': TASKS.get(task_id, {}).get('name', task_id),
                'latest_run_id': versions[0]['run_id'],
                'versions': versions,
            }
        )
    order = {task_id: index for index, task_id in enumerate(TASKS)}
    tasks.sort(key=lambda value: order.get(value['id'], 999))
    return {'generated_at': db.now(), 'tasks': tasks}


def latest_issue_rates(conn: Any) -> Dict[tuple[str, str, str], Dict[str, Any]]:
    """返回最新版本在场景/真值维度的人工纠正率。"""
    result: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    rows = conn.execute(
        'SELECT task_id,model_run_id,screen_type,confirmed_label,'
        'COUNT(*) AS compared,SUM(CASE WHEN is_correct=0 THEN 1 ELSE 0 END) '
        'AS wrong,SUM(CASE WHEN is_correct=0 AND confidence>=0.85 THEN 1 ELSE 0 END) '
        'AS high_confidence_wrong '
        'FROM training_review_model_outcomes '
        'GROUP BY task_id,model_run_id,screen_type,confirmed_label'
    ).fetchall()
    latest: Dict[str, str] = {}
    for row in rows:
        task_id = str(row['task_id'])
        run_id = str(row['model_run_id'])
        if _version_sort_key(run_id) > _version_sort_key(latest.get(task_id, '')):
            latest[task_id] = run_id
    for row in rows:
        task_id = str(row['task_id'])
        if str(row['model_run_id']) != latest.get(task_id):
            continue
        compared = int(row['compared'] or 0)
        wrong = int(row['wrong'] or 0)
        result[
            (task_id, str(row['screen_type'] or ''), str(row['confirmed_label']))
        ] = {
            'run_id': str(row['model_run_id']),
            'compared': compared,
            'wrong': wrong,
            'high_confidence_wrong': int(row['high_confidence_wrong'] or 0),
            'correction_rate': wrong / compared if compared else 0.0,
        }
    return result
