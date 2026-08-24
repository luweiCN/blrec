"""不可变数据集快照上的本机训练任务与模型发布。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from . import classification_preprocessing, config, db, export, managed_assets

PROGRESS_PREFIX = '@@BLREC_TRAIN_PROGRESS@@'
RESULT_PREFIX = '@@BLREC_TRAIN_RESULT@@'

_CLASSIFICATION_INPUT = {
    'imgsz': classification_preprocessing.CLASSIFICATION_INPUT_WIDTH,
    'input_width': classification_preprocessing.CLASSIFICATION_INPUT_WIDTH,
    'input_height': classification_preprocessing.CLASSIFICATION_INPUT_HEIGHT,
}

TRAINING_TASKS: Dict[str, Dict[str, Any]] = {
    'match_flow': {
        'name': '是否在虚荣对局流程中',
        'kind': 'classify',
        'description': '低频判断当前画面是否属于一局比赛流程；看不清的样本不训练。',
        'epochs': 60,
        **_CLASSIFICATION_INPUT,
        'base_model': 'yolov8n-cls.pt',
        'publish_name': 'match-flow-classifier-current.onnx',
        'recommended': '对局与非对局各至少 300 张，并覆盖多位主播和不同设备。',
        'active': True,
    },
    'hero_select': {
        'name': '英雄选择与模式分类',
        'kind': 'classify',
        'description': '识别非英雄选择、3V3、大乱斗和 5V5 英雄选择界面。',
        'epochs': 60,
        **_CLASSIFICATION_INPUT,
        'base_model': 'yolov8n-cls.pt',
        'publish_name': 'hero-select-classifier-current.onnx',
        'recommended': '三种英雄选择各至少 100 张，非英雄选择至少 300 张。',
        'active': True,
    },
    'hero_avatar_detector': {
        'name': '英雄头像位置检测',
        'kind': 'detect',
        'description': '在 HUD、积分板和结算界面中定位 6 个或 10 个英雄头像。',
        'epochs': 100,
        'imgsz': 960,
        'base_model': 'yolov8n.pt',
        'publish_name': 'hero-avatar-detector-current.onnx',
        'recommended': '三类画面各至少 100 张，重点覆盖高分辨率 HUD 和不同设备。',
        'active': True,
    },
    'hero_identity': {
        'name': '英雄头像身份识别',
        'kind': 'classify',
        'description': '对已定位的单个头像判断是哪位英雄；看不清的头像不训练。',
        'epochs': 80,
        'imgsz': 160,
        'input_width': 160,
        'input_height': 160,
        'base_model': 'yolov8n-cls.pt',
        'publish_name': 'hero-identity-classifier-current.onnx',
        'recommended': '57 位英雄均需覆盖，每位建议至少 50 个可读头像。',
        'active': True,
    },
    'player_position': {
        'name': '主播本人位置识别',
        'kind': 'classify',
        'description': (
            '查看完整积分板或结算界面，判断主播本人位于哪一队、第几个头像；'
            'HUD 不进入训练。'
        ),
        'epochs': 60,
        **_CLASSIFICATION_INPUT,
        'base_model': 'yolov8n-cls.pt',
        'publish_name': 'player-position-classifier-current.onnx',
        'recommended': '8 个有效位置各至少 30 张，并覆盖多位主播和不同设备。',
        'active': True,
    },
    'afk_status': {
        'name': '结算图挂机识别',
        'kind': 'classify',
        'description': (
            '只看真正结算图中单个玩家的头像和名字区域，判断最终是否挂机；'
            '积分板上的临时掉线不训练。'
        ),
        'epochs': 60,
        'imgsz': 224,
        'input_width': 224,
        'input_height': 224,
        'base_model': 'yolov8n-cls.pt',
        'publish_name': 'afk-status-classifier-current.onnx',
        'recommended': '挂机至少 200 个、正常至少 500 个，并覆盖不同模式和设备。',
        'active': True,
    },
    'match_mode': {
        'name': '对局画面模式分类',
        'kind': 'classify',
        'description': '只对能看出地图的对局画面判断 3V3、大乱斗或 5V5。',
        'epochs': 60,
        **_CLASSIFICATION_INPUT,
        'base_model': 'yolov8n-cls.pt',
        'publish_name': 'match-mode-classifier-current.onnx',
        'recommended': '每种模式至少 200 张，并覆盖地图区域、设备和遮挡差异。',
        'active': True,
    },
    'screen_state': {
        'name': '画面状态分类',
        'kind': 'classify',
        'description': '构建非虚荣、游戏外、对局前、对局中、天赋、赛后和转场时间线。',
        'epochs': 60,
        **_CLASSIFICATION_INPUT,
        'base_model': 'yolov8n-cls.pt',
        'publish_name': 'screen-state-classifier-current.onnx',
        'recommended': '每类至少 100 张并覆盖多个视频；对局中样本会自动限量。',
        'active': False,
    },
    'bp_review': {
        'name': 'BP 模式分类',
        'kind': 'classify',
        'description': '识别 3V3、大乱斗、5V5 BP，并排除匹配确认等非 BP 画面。',
        'epochs': 60,
        **_CLASSIFICATION_INPUT,
        'base_model': 'yolov8n-cls.pt',
        'publish_name': 'bp-classifier-current.onnx',
        'recommended': '每种 BP 至少 100 张，非 BP 至少 200 张，并覆盖多个主播。',
        'active': False,
    },
    'key_screen_review': {
        'name': '结算页／计分板分类',
        'kind': 'classify',
        'description': '区分赛后结算页、对局中计分板和其他易混淆画面。',
        'epochs': 60,
        **_CLASSIFICATION_INPUT,
        'base_model': 'yolov8n-cls.pt',
        'publish_name': 'key-screen-classifier-current.onnx',
        'recommended': '结算页和计分板各 100 张以上，其他 hard negative 300 张以上。',
        'active': False,
    },
    'mode_gate': {
        'name': '大乱斗光栅检测',
        'kind': 'detect',
        'description': '检测大乱斗地图入口的黄色光栅；开放入口画面作为负样本。',
        'epochs': 100,
        'imgsz': 640,
        'base_model': 'yolov8n.pt',
        'publish_name': 'mode-gate-detector-current.onnx',
        'recommended': '至少 100 张有光栅、100 张同位置开放入口，覆盖不同设备。',
        'active': False,
    },
    'result_detector': {
        'name': '结算面板检测',
        'kind': 'detect',
        'description': '定位真正结算面板，计分板和其他游戏画面作为 hard negative。',
        'epochs': 100,
        'imgsz': 640,
        'base_model': 'yolov8n.pt',
        'publish_name': 'result-detector-current.onnx',
        'recommended': '120～250 张有框结算页，800～1500 张负样本，重点覆盖计分板。',
        'active': True,
    },
}


def _existing_bp_labels(conn: Any) -> Dict[int, str]:
    labels: Dict[int, str] = {}
    mode_labels = {'3v3': 'bp_3v3', 'aram': 'bp_aram', '5v5': 'bp_5v5'}
    rows = conn.execute(
        'SELECT a.frame_id, a.game_mode, a.screen_type, f.frame_path '
        'FROM annotations a JOIN frames f ON f.id = a.frame_id '
        "WHERE a.annotation_status = 'complete' "
        "AND a.screen_type IN ('hero_select_bp', 'hero_select_blind', "
        "'hero_select_aram', 'match_confirm')"
    ).fetchall()
    for row in rows:
        if not managed_assets.frame_available(row['frame_path']):
            continue
        label = (
            'not_bp'
            if row['screen_type'] == 'match_confirm'
            else mode_labels.get(row['game_mode'])
        )
        if label is None and row['screen_type'] == 'hero_select_aram':
            label = 'bp_aram'
        if label is not None:
            labels[int(row['frame_id'])] = label
    reviewed = conn.execute(
        'SELECT k.frame_id, k.confirmed_label, k.visual_condition, f.frame_path '
        'FROM bp_review_items k JOIN frames f ON f.id = k.frame_id '
        "WHERE k.review_status = 'confirmed' AND k.confirmed_label IS NOT NULL"
    ).fetchall()
    for row in reviewed:
        frame_id = int(row['frame_id'])
        if (
            row['visual_condition'] == 'unreadable'
            or not managed_assets.frame_available(row['frame_path'])
        ):
            labels.pop(frame_id, None)
        else:
            labels[frame_id] = str(row['confirmed_label'])
    return labels


def _existing_key_screen_labels(conn: Any) -> Dict[int, str]:
    labels: Dict[int, str] = {}
    rows = conn.execute(
        'SELECT a.frame_id, a.screen_type, f.frame_path '
        'FROM annotations a JOIN frames f ON f.id = a.frame_id '
        "WHERE a.annotation_status = 'complete'"
    ).fetchall()
    for row in rows:
        if not managed_assets.frame_available(row['frame_path']):
            continue
        screen_type = row['screen_type']
        if screen_type == 'result_page':
            label = 'result_page'
        elif screen_type in ('scoreboard', 'death_scoreboard'):
            label = 'scoreboard'
        else:
            label = 'other'
        labels[int(row['frame_id'])] = label
    reviewed = conn.execute(
        'SELECT k.frame_id, k.confirmed_label, k.visual_condition, f.frame_path '
        'FROM key_screen_review_items k JOIN frames f ON f.id = k.frame_id '
        "WHERE k.review_status = 'confirmed' AND k.confirmed_label IS NOT NULL"
    ).fetchall()
    for row in reviewed:
        frame_id = int(row['frame_id'])
        if (
            row['visual_condition'] == 'unreadable'
            or not managed_assets.frame_available(row['frame_path'])
        ):
            labels.pop(frame_id, None)
        else:
            labels[frame_id] = str(row['confirmed_label'])
    return labels


def _existing_screen_state_labels(conn: Any) -> Dict[int, str]:
    labels: Dict[int, str] = {}
    rows = conn.execute(
        'SELECT a.*, f.frame_path FROM annotations a '
        'JOIN frames f ON f.id = a.frame_id '
        "WHERE a.annotation_status = 'complete'"
    ).fetchall()
    for row in rows:
        if not managed_assets.frame_available(row['frame_path']):
            continue
        label = export._screen_state_label(dict(row))
        if label is not None:
            labels[int(row['frame_id'])] = label
    reviewed = conn.execute(
        'SELECT c.frame_id, c.confirmed_label, c.visual_condition, f.frame_path '
        'FROM worker_candidate_items c JOIN frames f ON f.id = c.frame_id '
        "WHERE c.task = 'screen_state' AND c.review_status = 'confirmed' "
        'AND c.confirmed_label IS NOT NULL'
    ).fetchall()
    for row in reviewed:
        frame_id = int(row['frame_id'])
        if (
            row['visual_condition'] == 'unreadable'
            or not managed_assets.frame_available(row['frame_path'])
        ):
            labels.pop(frame_id, None)
        else:
            labels[frame_id] = str(row['confirmed_label'])
    return labels


def _classification_summary(
    labels: Dict[int, str], required: List[str]
) -> Dict[str, Any]:
    counts = {
        label: sum(1 for value in labels.values() if value == label)
        for label in required
    }
    return {'total': len(labels), 'by_label': counts}


def _frame_videos(conn: Any, frame_ids: List[int]) -> Dict[int, int]:
    """只读取目标帧所属视频，避免为几千个样本反复全表扫描。"""

    selected = sorted(set(int(frame_id) for frame_id in frame_ids))
    result: Dict[int, int] = {}
    for offset in range(0, len(selected), 800):
        chunk = selected[offset : offset + 800]
        placeholders = ','.join('?' for _ in chunk)
        for row in conn.execute(
            f'SELECT id, video_id FROM frames WHERE id IN ({placeholders})',
            tuple(chunk),
        ).fetchall():
            result[int(row['id'])] = int(row['video_id'])
    return result


def _video_count_for_frames(conn: Any, frame_ids: List[int]) -> int:
    return len(set(_frame_videos(conn, frame_ids).values()))


def _videos_by_label(
    conn: Any,
    labels: Dict[int, str],
    required: List[str],
    *,
    frame_videos: Optional[Dict[int, int]] = None,
) -> Dict[str, int]:
    videos = frame_videos or _frame_videos(conn, list(labels))
    return {
        label: len(
            {
                videos[frame_id]
                for frame_id, value in labels.items()
                if value == label and frame_id in videos
            }
        )
        for label in required
    }


def _training_review_labels(
    conn: Any, *, column: str, allowed: List[str]
) -> Dict[int, str]:
    if column not in {'match_flow_label', 'match_mode_label', 'hero_select_label'}:
        raise ValueError('未知统一复核标签列')
    labels: Dict[int, str] = {}
    rows = conn.execute(
        f'SELECT r.frame_id, r.{column} AS label, f.frame_path '
        'FROM training_review_items r '
        'JOIN frames f ON f.id = r.frame_id '
        "WHERE (r.review_status = 'confirmed' OR ("
        "r.review_status = 'partial' AND EXISTS ("
        'SELECT 1 FROM training_review_sources source '
        'WHERE source.frame_id = r.frame_id '
        "AND source.source_type = 'manual_correction'))) "
        f'AND r.{column} IS NOT NULL'
    ).fetchall()
    accepted = set(allowed)
    duplicate_results = db.training_review_duplicate_result_frame_ids(conn)
    for row in rows:
        if int(row['frame_id']) in duplicate_results:
            continue
        label = str(row['label'])
        if label in accepted and managed_assets.frame_available(row['frame_path']):
            labels[int(row['frame_id'])] = label
    return labels


def _result_detector_member_samples(
    conn: Any, *, max_negatives: int = 1_500
) -> List[Dict[str, Any]]:
    """按下一次结算检测快照的优先级返回确定成员，不写导出目录。"""
    members: Dict[int, Dict[str, Any]] = {}
    rows = conn.execute(
        'SELECT a.frame_id, a.screen_type, a.game_mode, f.video_id, '
        'f.sha256, f.frame_path, b.x AS result_x, b.y AS result_y, '
        'b.w AS result_w, b.h AS result_h '
        'FROM annotations a JOIN frames f ON f.id = a.frame_id '
        "LEFT JOIN boxes b ON b.frame_id = a.frame_id "
        "AND b.box_type = 'result_panel' "
        "WHERE a.annotation_status = 'complete'"
    ).fetchall()
    for raw_row in rows:
        row = dict(raw_row)
        if not managed_assets.frame_available(row['frame_path']):
            continue
        frame_id = int(row['frame_id'])
        box = _result_box_from_row(row)
        if row['screen_type'] == 'result_page':
            if not isinstance(box, dict):
                continue
            label = 'result_panel'
        else:
            label = 'no_result_panel'
            box = None
        members[frame_id] = {
            **row,
            'sample_id': f'f{frame_id:08d}',
            'label': label,
            'box': box,
            'label_source': 'existing_human_annotation',
            'scoreboard': row['screen_type'] in {'scoreboard', 'death_scoreboard'},
        }

    candidate_rows = conn.execute(
        'SELECT c.frame_id, c.confirmed_label, c.boxes_json, f.video_id, '
        'f.sha256, f.frame_path, a.screen_type, a.game_mode '
        'FROM worker_candidate_items c '
        'JOIN frames f ON f.id = c.frame_id '
        'LEFT JOIN annotations a ON a.frame_id = f.id '
        "WHERE c.task = 'result_detector' "
        "AND c.review_status = 'confirmed' "
        'AND c.confirmed_label IS NOT NULL '
        "AND c.visual_condition != 'unreadable'"
    ).fetchall()
    for raw_row in candidate_rows:
        row = dict(raw_row)
        if not managed_assets.frame_available(row['frame_path']):
            continue
        label = str(row['confirmed_label'])
        if label not in {'result_panel', 'no_result_panel'}:
            continue
        boxes = json.loads(row['boxes_json'] or '[]')
        result_box = next(
            (
                box
                for box in boxes
                if isinstance(box, dict)
                and (not box.get('type') or box.get('type') == 'result_panel')
            ),
            None,
        )
        if label == 'result_panel' and result_box is None:
            continue
        frame_id = int(row['frame_id'])
        members[frame_id] = {
            **row,
            'sample_id': f'f{frame_id:08d}',
            'label': label,
            'box': result_box if label == 'result_panel' else None,
            'label_source': 'worker_candidate_confirmed',
            'scoreboard': row['screen_type'] in {'scoreboard', 'death_scoreboard'},
        }

    unified_rows = conn.execute(
        'SELECT r.frame_id, r.result_panel_label, r.hero_layout_label, '
        'r.match_mode_label, f.video_id, f.sha256, f.frame_path, '
        'a.screen_type, a.game_mode, b.x AS result_x, b.y AS result_y, '
        'b.w AS result_w, b.h AS result_h '
        'FROM training_review_items r '
        'JOIN frames f ON f.id = r.frame_id '
        'LEFT JOIN annotations a ON a.frame_id = f.id '
        "LEFT JOIN boxes b ON b.frame_id = r.frame_id "
        "AND b.box_type = 'result_panel' "
        "WHERE r.review_status = 'confirmed' "
        'AND r.result_panel_label IS NOT NULL'
    ).fetchall()
    for raw_row in unified_rows:
        row = dict(raw_row)
        frame_id = int(row['frame_id'])
        label = str(row['result_panel_label'])
        if label == 'unreadable' or not managed_assets.frame_available(
            row['frame_path']
        ):
            members.pop(frame_id, None)
            continue
        box = _result_box_from_row(row)
        if label == 'result_panel' and not isinstance(box, dict):
            members.pop(frame_id, None)
            continue
        members[frame_id] = {
            **row,
            'sample_id': f'f{frame_id:08d}',
            'label': label,
            'box': box if label == 'result_panel' else None,
            'label_source': 'training_review_confirmed',
            'scoreboard': (
                row['hero_layout_label'] == 'scoreboard'
                or row['screen_type'] in {'scoreboard', 'death_scoreboard'}
            ),
        }

    for frame_id in db.training_review_duplicate_result_frame_ids(conn):
        if members.get(frame_id, {}).get('label') == 'result_panel':
            members.pop(frame_id, None)
    positives = [
        sample for sample in members.values() if sample['label'] == 'result_panel'
    ]
    negatives = [
        sample for sample in members.values() if sample['label'] == 'no_result_panel'
    ]
    if max_negatives > 0 and len(negatives) > max_negatives:
        negatives = sorted(
            negatives,
            key=lambda sample: (
                (
                    0
                    if sample['scoreboard']
                    else (
                        1
                        if sample['label_source']
                        in {'training_review_confirmed', 'worker_candidate_confirmed'}
                        else 2
                    )
                ),
                str(sample.get('sha256') or sample['sample_id']),
            ),
        )[:max_negatives]
    return positives + negatives


def _result_box_from_row(row: Dict[str, Any]) -> Optional[Dict[str, float]]:
    if row.get('result_x') is None:
        return None
    return {
        'x': float(row['result_x']),
        'y': float(row['result_y']),
        'w': float(row['result_w']),
        'h': float(row['result_h']),
    }


def _confirmed_hero_member_lineups(conn: Any) -> List[Dict[str, Any]]:
    """一次查询组装完整英雄阵容，避免每帧再查标注、框和头像槽位。"""

    rows = conn.execute(
        'SELECT lineup.frame_id, lineup.screen_type, lineup.team_size, '
        'f.video_id, f.frame_path, slot.side, slot.slot, slot.crop_x, '
        'slot.crop_y, slot.crop_w, slot.crop_h, slot.confirmed_label '
        'FROM training_review_hero_lineups lineup '
        'JOIN frames f ON f.id = lineup.frame_id '
        'JOIN training_review_items review ON review.frame_id = lineup.frame_id '
        'JOIN training_review_hero_slots slot ON slot.frame_id = lineup.frame_id '
        "WHERE lineup.review_status = 'confirmed' "
        "AND COALESCE(review.view_context_label, 'played') = 'played' "
        "AND COALESCE(review.match_kind_label, 'pvp') != 'practice' "
        "ORDER BY lineup.frame_id, CASE slot.side WHEN 'left' THEN 0 ELSE 1 END, "
        'slot.slot'
    ).fetchall()
    by_frame: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        frame_id = int(row['frame_id'])
        lineup = by_frame.setdefault(
            frame_id,
            {
                'sample_id': f'f{frame_id:08d}',
                'frame_id': frame_id,
                'video_id': int(row['video_id']),
                'frame_path': str(row['frame_path']),
                'hero_screen_type': str(row['screen_type']),
                'team_size': int(row['team_size']),
                'hero_slots': [],
            },
        )
        lineup['hero_slots'].append(
            {
                'side': str(row['side']),
                'slot': int(row['slot']),
                'crop': {
                    'x': float(row['crop_x']),
                    'y': float(row['crop_y']),
                    'w': float(row['crop_w']),
                    'h': float(row['crop_h']),
                },
                'confirmed_label': str(row['confirmed_label'] or ''),
            }
        )
    return [
        lineup
        for lineup in by_frame.values()
        if len(lineup['hero_slots']) == int(lineup['team_size']) * 2
        and managed_assets.frame_available(lineup['frame_path'])
    ]


def _confirmed_player_position_members(conn: Any) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT lineup.frame_id, lineup.screen_type, lineup.team_size, '
        'lineup.player_side, lineup.player_slot, f.video_id, f.frame_path '
        'FROM training_review_hero_lineups lineup '
        'JOIN frames f ON f.id = lineup.frame_id '
        'JOIN training_review_items review ON review.frame_id = lineup.frame_id '
        "WHERE lineup.review_status = 'confirmed' "
        "AND lineup.player_status = 'identified' "
        "AND COALESCE(review.view_context_label, 'played') = 'played' "
        "AND lineup.screen_type IN ('scoreboard', 'result_page') "
        "AND lineup.player_side IN ('left', 'right') "
        'AND lineup.player_slot BETWEEN 1 AND lineup.team_size '
        'AND EXISTS ('
        'SELECT 1 FROM training_review_hero_slots slot '
        'WHERE slot.frame_id = lineup.frame_id '
        'AND slot.side = lineup.player_side '
        'AND slot.slot = lineup.player_slot)'
    ).fetchall()
    duplicate_results = db.training_review_duplicate_result_frame_ids(conn)
    samples = []
    for row in rows:
        frame_id = int(row['frame_id'])
        label = '{}{}'.format(row['player_side'], int(row['player_slot']))
        if (
            frame_id in duplicate_results
            or label not in export.PLAYER_POSITION_LABELS
            or not managed_assets.frame_available(row['frame_path'])
        ):
            continue
        samples.append(
            {
                'sample_id': f'f{frame_id:08d}',
                'frame_id': frame_id,
                'video_id': int(row['video_id']),
                'label': label,
                'hero_screen_type': str(row['screen_type']),
                'team_size': int(row['team_size']),
            }
        )
    return samples


def _current_task_members(conn: Any, task_id: str) -> Dict[str, Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    if task_id in export.UNIFIED_CLASSIFICATION_LABELS:
        labels = _training_review_labels(
            conn,
            column={
                'match_flow': 'match_flow_label',
                'match_mode': 'match_mode_label',
                'hero_select': 'hero_select_label',
            }[task_id],
            allowed=list(export.UNIFIED_CLASSIFICATION_LABELS[task_id]),
        )
        if labels:
            frame_videos = _frame_videos(conn, list(labels))
            samples = [
                {
                    'sample_id': f'f{frame_id:08d}',
                    'video_id': video_id,
                    'label': labels[frame_id],
                }
                for frame_id, video_id in frame_videos.items()
            ]
    elif task_id == 'result_detector':
        samples = _result_detector_member_samples(conn)
    elif task_id in {'hero_avatar_detector', 'hero_identity'}:
        lineups = _confirmed_hero_member_lineups(conn)
        if task_id == 'hero_avatar_detector':
            samples = [
                {
                    'sample_id': str(lineup['sample_id']),
                    'video_id': int(lineup['video_id']),
                    'label': str(lineup['hero_screen_type']),
                    'avatar_boxes': [slot['crop'] for slot in lineup['hero_slots']],
                }
                for lineup in lineups
            ]
        else:
            for lineup in lineups:
                for slot in lineup['hero_slots']:
                    label = str(slot['confirmed_label'] or '')
                    if not label or label == 'unreadable':
                        continue
                    samples.append(
                        {
                            'sample_id': 'f{:08d}-{}-{}'.format(
                                int(lineup['frame_id']), slot['side'], slot['slot']
                            ),
                            'video_id': int(lineup['video_id']),
                            'label': label,
                            'crop': slot['crop'],
                        }
                    )
    elif task_id == 'player_position':
        samples = _confirmed_player_position_members(conn)
    elif task_id == 'afk_status':
        samples = export.confirmed_afk_status_samples(conn)
    else:
        return {}
    return {
        str(sample['sample_id']): {
            'video_id': int(sample['video_id']),
            'label': str(sample.get('label') or ''),
            'signature': _task_member_signature(task_id, sample),
        }
        for sample in samples
    }


def _rounded_box(box: Any) -> Any:
    if not isinstance(box, dict):
        return None
    try:
        return [round(float(box[key]), 6) for key in ('x', 'y', 'w', 'h')]
    except (KeyError, TypeError, ValueError):
        return None


def _task_member_signature(task_id: str, sample: Dict[str, Any]) -> str:
    value: Any = str(sample.get('label') or sample.get('detector_label') or '')
    if task_id == 'result_detector':
        value = {
            'label': value,
            'box': _rounded_box(
                sample.get('box') or (sample.get('boxes') or {}).get('result_panel')
            ),
        }
    elif task_id == 'hero_avatar_detector':
        boxes = sample.get('avatar_boxes') or []
        value = {
            'screen_type': str(
                sample.get('label') or sample.get('hero_screen_type') or ''
            ),
            'boxes': sorted(
                (box for box in (_rounded_box(box) for box in boxes) if box),
                key=lambda box: tuple(box),
            ),
        }
    elif task_id == 'hero_identity':
        value = {'label': value, 'crop': _rounded_box(sample.get('crop'))}
    elif task_id == 'afk_status':
        value = {'label': value, 'crop': _rounded_box(sample.get('crop'))}
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _latest_dataset_delta(conn: Any, task_id: str) -> Optional[Dict[str, Any]]:
    run = conn.execute(
        'SELECT r.id, r.dataset_version_id, d.manifest_path '
        'FROM training_runs r '
        'JOIN dataset_versions d ON d.id = r.dataset_version_id '
        "WHERE r.task_id = ? AND r.status = 'succeeded' "
        'ORDER BY r.created_at DESC, r.id DESC LIMIT 1',
        (task_id,),
    ).fetchone()
    if run is None:
        return None
    try:
        manifest = managed_assets.resolve_dataset_manifest(
            str(run['dataset_version_id']), Path(str(run['manifest_path']))
        )
    except (FileNotFoundError, RuntimeError, ValueError):
        return None
    baseline: Dict[str, Dict[str, Any]] = {}
    with manifest.open(encoding='utf-8') as handle:
        for line in handle:
            if not line.strip():
                continue
            sample = json.loads(line)
            sample_id = str(sample.get('sample_id') or '')
            if not sample_id:
                continue
            baseline[sample_id] = {
                'video_id': int(sample.get('video_id') or 0),
                'label': str(sample.get('label') or sample.get('detector_label') or ''),
                'signature': _task_member_signature(task_id, sample),
            }
    current = _current_task_members(conn, task_id)
    baseline_ids = set(baseline)
    current_ids = set(current)
    new_ids = current_ids - baseline_ids
    removed_ids = baseline_ids - current_ids
    changed_ids = {
        sample_id
        for sample_id in baseline_ids & current_ids
        if baseline[sample_id]['signature'] != current[sample_id]['signature']
    }
    baseline_videos = {int(sample['video_id']) for sample in baseline.values()}
    new_by_label: Dict[str, int] = {}
    for sample_id in new_ids:
        label = str(current[sample_id]['label'])
        new_by_label[label] = new_by_label.get(label, 0) + 1
    return {
        'run_id': str(run['id']),
        'dataset_version_id': str(run['dataset_version_id']),
        'baseline_total': len(baseline),
        'current_total': len(current),
        'new': len(new_ids),
        'removed': len(removed_ids),
        'changed': len(changed_ids),
        'net': len(current) - len(baseline),
        'new_videos': len(
            {
                int(current[sample_id]['video_id'])
                for sample_id in new_ids
                if int(current[sample_id]['video_id']) not in baseline_videos
            }
        ),
        'new_by_label': dict(sorted(new_by_label.items())),
    }


def _task_counts(conn: Any, task_id: str) -> Dict[str, Any]:
    if task_id in export.UNIFIED_CLASSIFICATION_LABELS:
        required = list(export.UNIFIED_CLASSIFICATION_LABELS[task_id])
        column = {
            'match_flow': 'match_flow_label',
            'match_mode': 'match_mode_label',
            'hero_select': 'hero_select_label',
        }[task_id]
        labels = _training_review_labels(conn, column=column, allowed=required)
        frame_videos = _frame_videos(conn, list(labels))
        counts = _classification_summary(labels, required)
        counts['videos_by_label'] = _videos_by_label(
            conn, labels, required, frame_videos=frame_videos
        )
        video_count = len(set(frame_videos.values()))
    elif task_id == 'screen_state':
        labels = _existing_screen_state_labels(conn)
        required = list(export.SCREEN_STATE_LABELS)
        frame_videos = _frame_videos(conn, list(labels))
        counts = _classification_summary(labels, required)
        counts['videos_by_label'] = _videos_by_label(
            conn, labels, required, frame_videos=frame_videos
        )
        video_count = len(set(frame_videos.values()))
    elif task_id == 'bp_review':
        labels = _existing_bp_labels(conn)
        required = ['bp_3v3', 'bp_aram', 'bp_5v5', 'not_bp']
        frame_videos = _frame_videos(conn, list(labels))
        counts = _classification_summary(labels, required)
        counts['videos_by_label'] = _videos_by_label(
            conn, labels, required, frame_videos=frame_videos
        )
        video_count = len(set(frame_videos.values()))
    elif task_id == 'key_screen_review':
        labels = _existing_key_screen_labels(conn)
        required = ['result_page', 'scoreboard', 'other']
        frame_videos = _frame_videos(conn, list(labels))
        counts = _classification_summary(labels, required)
        counts['videos_by_label'] = _videos_by_label(
            conn, labels, required, frame_videos=frame_videos
        )
        video_count = len(set(frame_videos.values()))
    elif task_id == 'mode_gate':
        evidence_by_frame = {
            int(row['frame_id']): str(row['evidence'])
            for row in conn.execute(
                'SELECT mga.frame_id, mga.evidence, f.frame_path '
                'FROM mode_gate_annotations mga JOIN frames f ON f.id = mga.frame_id '
                "WHERE mga.evidence IN ('blocked_gate', 'open_entrance') "
                "AND f.frame_path != ''"
            ).fetchall()
            if managed_assets.frame_available(row['frame_path'])
        }
        for row in conn.execute(
            'SELECT c.frame_id, c.confirmed_label, f.frame_path '
            'FROM worker_candidate_items c JOIN frames f ON f.id = c.frame_id '
            "WHERE c.task = 'mode_gate' AND c.review_status = 'confirmed' "
            "AND c.confirmed_label IN ('blocked_gate', 'open_entrance') "
            "AND c.visual_condition != 'unreadable'"
        ).fetchall():
            if managed_assets.frame_available(row['frame_path']):
                evidence_by_frame[int(row['frame_id'])] = str(row['confirmed_label'])
        by_evidence = {
            label: sum(1 for value in evidence_by_frame.values() if value == label)
            for label in ('blocked_gate', 'open_entrance')
        }
        counts = {
            'total': sum(by_evidence.values()),
            'positive': int(by_evidence.get('blocked_gate', 0)),
            'negative': int(by_evidence.get('open_entrance', 0)),
        }
        video_count = _video_count_for_frames(conn, list(evidence_by_frame))
    elif task_id == 'result_detector':
        samples = _result_detector_member_samples(conn, max_negatives=0)
        positive = sum(sample['label'] == 'result_panel' for sample in samples)
        negative = sum(sample['label'] == 'no_result_panel' for sample in samples)
        hard_negative = int(
            conn.execute(
                'SELECT COUNT(*) FROM annotations a '
                'JOIN frames f ON f.id = a.frame_id '
                "WHERE a.annotation_status = 'complete' "
                "AND a.screen_type IN ('scoreboard', 'death_scoreboard') "
                "AND f.frame_path != ''"
            ).fetchone()[0]
        )
        counts = {
            'total': positive + negative,
            'positive': positive,
            'negative': negative,
            'hard_negative': hard_negative,
        }
        video_count = len({int(sample['video_id']) for sample in samples})
    elif task_id == 'hero_avatar_detector':
        rows = [
            dict(row)
            for row in conn.execute(
                'SELECT lineup.frame_id, lineup.screen_type, lineup.team_size, '
                'f.video_id, f.frame_path, COUNT(slot.slot) AS slot_count '
                'FROM training_review_hero_lineups lineup '
                'JOIN training_review_hero_slots slot '
                'ON slot.frame_id = lineup.frame_id '
                'JOIN frames f ON f.id = lineup.frame_id '
                "WHERE lineup.review_status = 'confirmed' "
                'GROUP BY lineup.frame_id, lineup.screen_type, '
                'lineup.team_size, f.video_id, f.frame_path '
                'HAVING COUNT(slot.slot) = lineup.team_size * 2'
            ).fetchall()
            if managed_assets.frame_available(row['frame_path'])
        ]
        counts = {
            'total': len(rows),
            'positive': len(rows),
            'boxes': sum(int(row['slot_count']) for row in rows),
            'by_screen_type': {
                screen_type: sum(row['screen_type'] == screen_type for row in rows)
                for screen_type in ('gameplay_hud', 'scoreboard', 'result_page')
            },
            'by_team_size': {
                str(team_size): sum(int(row['team_size']) == team_size for row in rows)
                for team_size in (3, 5)
            },
        }
        video_count = len({int(row['video_id']) for row in rows})
    elif task_id == 'hero_identity':
        rows = [
            dict(row)
            for row in conn.execute(
                'SELECT slot.confirmed_label, slot.crop_w, slot.crop_h, '
                'lineup.screen_type, f.video_id, f.width, f.height, f.frame_path '
                'FROM training_review_hero_slots slot '
                'JOIN training_review_hero_lineups lineup '
                'ON lineup.frame_id = slot.frame_id '
                'JOIN frames f ON f.id = slot.frame_id '
                "WHERE lineup.review_status = 'confirmed' "
                "AND COALESCE(slot.confirmed_label, '') NOT IN ('', 'unreadable')"
            ).fetchall()
            if managed_assets.frame_available(row['frame_path'])
        ]
        labels = sorted({str(row['confirmed_label']) for row in rows})
        counts = {
            'total': len(rows),
            'classes': len(labels),
            'by_label': {
                label: sum(row['confirmed_label'] == label for row in rows)
                for label in labels
            },
            'videos_by_label': {
                label: len(
                    {
                        int(row['video_id'])
                        for row in rows
                        if row['confirmed_label'] == label
                    }
                )
                for label in labels
            },
            'by_screen_type': {
                screen_type: sum(row['screen_type'] == screen_type for row in rows)
                for screen_type in ('gameplay_hud', 'scoreboard', 'result_page')
            },
            'under_24px': sum(
                min(
                    float(row['crop_w']) * int(row['width']),
                    float(row['crop_h']) * int(row['height']),
                )
                < 24
                for row in rows
            ),
            'under_48px': sum(
                min(
                    float(row['crop_w']) * int(row['width']),
                    float(row['crop_h']) * int(row['height']),
                )
                < 48
                for row in rows
            ),
        }
        video_count = len({int(row['video_id']) for row in rows})
    elif task_id == 'player_position':
        rows = _confirmed_player_position_members(conn)
        counts = {
            'total': len(rows),
            'classes': len({str(row['label']) for row in rows}),
            'by_label': {
                label: sum(row['label'] == label for row in rows)
                for label in export.PLAYER_POSITION_LABELS
            },
            'videos_by_label': {
                label: len(
                    {int(row['video_id']) for row in rows if row['label'] == label}
                )
                for label in export.PLAYER_POSITION_LABELS
            },
            'by_screen_type': {
                screen_type: sum(row['hero_screen_type'] == screen_type for row in rows)
                for screen_type in ('scoreboard', 'result_page')
            },
            'by_team_size': {
                str(team_size): sum(int(row['team_size']) == team_size for row in rows)
                for team_size in (3, 5)
            },
            'excluded_unreadable': int(
                conn.execute(
                    'SELECT COUNT(*) FROM training_review_hero_lineups '
                    "WHERE review_status = 'confirmed' "
                    "AND screen_type IN ('scoreboard', 'result_page') "
                    "AND player_status = 'unreadable'"
                ).fetchone()[0]
            ),
            'excluded_hud': int(
                conn.execute(
                    'SELECT COUNT(*) FROM training_review_hero_lineups '
                    "WHERE review_status = 'confirmed' "
                    "AND screen_type = 'gameplay_hud'"
                ).fetchone()[0]
            ),
        }
        video_count = len({int(row['video_id']) for row in rows})
    elif task_id == 'afk_status':
        rows = export.confirmed_afk_status_samples(conn)
        counts = {
            'total': len(rows),
            'by_label': {
                label: sum(row['label'] == label for row in rows)
                for label in export.AFK_STATUS_LABELS
            },
            'videos_by_label': {
                label: len(
                    {int(row['video_id']) for row in rows if row['label'] == label}
                )
                for label in export.AFK_STATUS_LABELS
            },
            'excluded_scoreboard': int(
                conn.execute(
                    'SELECT COUNT(*) FROM training_review_hero_slots slot '
                    'JOIN training_review_hero_lineups lineup '
                    'ON lineup.frame_id = slot.frame_id '
                    "WHERE lineup.review_status = 'confirmed' "
                    "AND lineup.screen_type = 'scoreboard' "
                    'AND slot.is_afk IS NOT NULL'
                ).fetchone()[0]
            ),
        }
        video_count = len({int(row['video_id']) for row in rows})
    else:
        raise ValueError(f'未知训练任务: {task_id}')
    counts['videos'] = video_count
    return counts


def _blocking_reasons(task_id: str, counts: Dict[str, Any]) -> List[str]:
    reasons = []
    if int(counts.get('videos', 0)) < 2:
        reasons.append('至少需要 2 个视频，才能把训练集和验证集按视频分开')
    if task_id in export.UNIFIED_CLASSIFICATION_LABELS:
        labels = counts.get('by_label', {})
        display_names = {
            'match_flow': '对局流程中',
            'not_match_flow': '非对局画面',
            '3v3': '3V3',
            'aram': '大乱斗',
            '5v5': '5V5',
            'not_select': '非英雄选择',
            'select_3v3': '3V3 英雄选择',
            'select_aram': '大乱斗英雄选择',
            'select_5v5': '5V5 英雄选择',
        }
        for label in export.UNIFIED_CLASSIFICATION_LABELS[task_id]:
            name = display_names[label]
            if int(labels.get(label, 0)) < 2:
                reasons.append(f'{name}至少需要 2 张有效图片')
            elif int(counts.get('videos_by_label', {}).get(label, 0)) < 2:
                reasons.append(f'{name}至少需要来自 2 个不同视频')
    elif task_id == 'hero_avatar_detector':
        if int(counts.get('total', 0)) < 2:
            reasons.append('至少需要 2 张完整人工头像布局')
        for screen_type, name in {
            'gameplay_hud': 'HUD',
            'scoreboard': '积分板',
            'result_page': '结算界面',
        }.items():
            if int(counts.get('by_screen_type', {}).get(screen_type, 0)) < 2:
                reasons.append(f'{name}至少需要 2 张完整人工头像布局')
    elif task_id == 'hero_identity':
        if int(counts.get('classes', 0)) < 2:
            reasons.append('至少需要 2 位不同英雄')
        for label, count in counts.get('by_label', {}).items():
            if int(count) < 2:
                reasons.append(f'{label}至少需要 2 个可读头像')
            elif int(counts.get('videos_by_label', {}).get(label, 0)) < 2:
                reasons.append(f'{label}至少需要来自 2 个不同视频')
    elif task_id == 'player_position':
        names = {
            label: '{}队第 {} 位'.format(
                '左' if label.startswith('left') else '右', label[-1]
            )
            for label in export.PLAYER_POSITION_LABELS
        }
        for label in export.PLAYER_POSITION_LABELS:
            count = int(counts.get('by_label', {}).get(label, 0))
            if count < 2:
                reasons.append(f'{names[label]}至少需要 2 张有效图片')
            elif int(counts.get('videos_by_label', {}).get(label, 0)) < 2:
                reasons.append(f'{names[label]}至少需要来自 2 个不同视频')
    elif task_id == 'afk_status':
        names = {'active': '正常', 'afk': '挂机'}
        for label in export.AFK_STATUS_LABELS:
            count = int(counts.get('by_label', {}).get(label, 0))
            if count < 2:
                reasons.append(f'{names[label]}至少需要 2 个有效头像区域')
            elif int(counts.get('videos_by_label', {}).get(label, 0)) < 2:
                reasons.append(f'{names[label]}至少需要来自 2 个不同视频')
    elif task_id == 'screen_state':
        labels = counts.get('by_label', {})
        names = {
            'not_vainglory': '非虚荣',
            'out_of_match': '游戏外',
            'pre_match': '对局前',
            'in_match': '对局中',
            'talent_select': '天赋选择',
            'post_match': '赛后',
            'transition': '转场',
        }
        for label, name in names.items():
            if int(labels.get(label, 0)) < 2:
                reasons.append(f'{name}至少需要 2 张有效图片')
            elif int(counts.get('videos_by_label', {}).get(label, 0)) < 2:
                reasons.append(f'{name}至少需要来自 2 个不同视频')
    elif task_id == 'bp_review':
        labels = counts.get('by_label', {})
        names = {
            'bp_3v3': '3V3 BP',
            'bp_aram': '大乱斗 BP',
            'bp_5v5': '5V5 BP',
            'not_bp': '非 BP',
        }
        for label, name in names.items():
            if int(labels.get(label, 0)) < 2:
                reasons.append(f'{name} 至少需要 2 张有效图片')
            elif int(counts.get('videos_by_label', {}).get(label, 0)) < 2:
                reasons.append(f'{name} 至少需要来自 2 个不同视频')
    elif task_id == 'key_screen_review':
        labels = counts.get('by_label', {})
        names = {'result_page': '结算页', 'scoreboard': '计分板', 'other': '其他画面'}
        for label, name in names.items():
            if int(labels.get(label, 0)) < 2:
                reasons.append(f'{name} 至少需要 2 张有效图片')
            elif int(counts.get('videos_by_label', {}).get(label, 0)) < 2:
                reasons.append(f'{name} 至少需要来自 2 个不同视频')
    else:
        if int(counts.get('positive', 0)) < 2:
            reasons.append('正样本至少需要 2 张有效图片')
        if int(counts.get('negative', 0)) < 2:
            reasons.append('负样本至少需要 2 张有效图片')
    return reasons


def _quality_warnings(task_id: str, counts: Dict[str, Any]) -> List[str]:
    """已经能跑训练，但还没达到第一轮正式模型的建议量。"""
    if task_id == 'match_flow':
        values = counts.get('by_label', {})
        targets = {
            'match_flow': ('对局流程中', 300),
            'not_match_flow': ('非对局画面', 300),
        }
    elif task_id == 'match_mode':
        values = counts.get('by_label', {})
        targets = {'3v3': ('3V3', 200), 'aram': ('大乱斗', 200), '5v5': ('5V5', 200)}
    elif task_id == 'hero_select':
        values = counts.get('by_label', {})
        targets = {
            'not_select': ('非英雄选择', 300),
            'select_3v3': ('3V3 英雄选择', 100),
            'select_aram': ('大乱斗英雄选择', 100),
            'select_5v5': ('5V5 英雄选择', 100),
        }
    elif task_id == 'screen_state':
        values = counts.get('by_label', {})
        targets = {
            'not_vainglory': ('非虚荣', 100),
            'out_of_match': ('游戏外', 100),
            'pre_match': ('对局前', 100),
            'in_match': ('对局中', 300),
            'talent_select': ('天赋选择', 100),
            'post_match': ('赛后', 100),
            'transition': ('转场', 100),
        }
    elif task_id == 'bp_review':
        values = counts.get('by_label', {})
        targets = {
            'bp_3v3': ('3V3 BP', 100),
            'bp_aram': ('大乱斗 BP', 100),
            'bp_5v5': ('5V5 BP', 100),
            'not_bp': ('非 BP', 200),
        }
    elif task_id == 'key_screen_review':
        values = counts.get('by_label', {})
        targets = {
            'result_page': ('结算页', 100),
            'scoreboard': ('计分板', 100),
            'other': ('其他画面', 300),
        }
    elif task_id == 'hero_avatar_detector':
        values = counts.get('by_screen_type', {})
        targets = {
            'gameplay_hud': ('HUD', 100),
            'scoreboard': ('积分板', 100),
            'result_page': ('结算界面', 100),
        }
    elif task_id == 'hero_identity':
        values = counts.get('by_label', {})
        return [
            f'{label} {int(value)}/50'
            for label, value in sorted(
                values.items(), key=lambda item: (int(item[1]), item[0])
            )
            if int(value) < 50
        ]
    elif task_id == 'player_position':
        values = counts.get('by_label', {})
        return [
            '{}队第 {} 位 {}/30'.format(
                '左' if label.startswith('left') else '右',
                label[-1],
                int(values.get(label, 0)),
            )
            for label in export.PLAYER_POSITION_LABELS
            if int(values.get(label, 0)) < 30
        ]
    elif task_id == 'afk_status':
        values = counts.get('by_label', {})
        targets = {'active': ('正常', 500), 'afk': ('挂机', 200)}
    elif task_id == 'mode_gate':
        values = counts
        targets = {'positive': ('有光栅', 100), 'negative': ('开放入口', 100)}
    else:
        values = counts
        targets = {
            'positive': ('有结算面板', 120),
            'negative': ('负样本', 800),
            'hard_negative': ('计分板 hard negative', 100),
        }
    return [
        f'{name} {int(values.get(key, 0))}/{target}'
        for key, (name, target) in targets.items()
        if int(values.get(key, 0)) < target
    ]


def task_summary(conn: Any, task_id: str) -> Dict[str, Any]:
    definition = TRAINING_TASKS.get(task_id)
    if definition is None:
        raise ValueError(f'未知训练任务: {task_id}')
    counts = _task_counts(conn, task_id)
    reasons = _blocking_reasons(task_id, counts)
    return {
        'id': task_id,
        **definition,
        'counts': counts,
        'dataset_delta': _latest_dataset_delta(conn, task_id),
        'ready': not reasons,
        'blocking_reasons': reasons,
        'quality_warnings': _quality_warnings(task_id, counts),
    }


def task_summaries(conn: Any, *, include_legacy: bool = False) -> List[Dict[str, Any]]:
    return [
        task_summary(conn, task_id)
        for task_id, definition in TRAINING_TASKS.items()
        if include_legacy or definition.get('active', True)
    ]


def export_snapshot(
    conn: Any, task_id: str, *, materialize: bool = True
) -> Dict[str, Any]:
    if task_id in export.UNIFIED_CLASSIFICATION_LABELS:
        return export.export_training_review_classifier(
            conn, task_id, materialize=materialize
        )
    if task_id == 'screen_state':
        return export.export_screen_state_classifier(conn)
    if task_id == 'bp_review':
        return export.export_bp_classifier(conn)
    if task_id == 'key_screen_review':
        return export.export_key_screen_classifier(conn)
    if task_id == 'mode_gate':
        return export.export_mode_gate_detector(conn)
    if task_id == 'result_detector':
        return export.export_result_detector(
            conn, include_negatives=True, max_negatives=1_500, materialize=materialize
        )
    if task_id == 'hero_avatar_detector':
        return export.export_hero_avatar_detector(conn, materialize=materialize)
    if task_id == 'hero_identity':
        return export.export_hero_identity_classifier(conn, materialize=materialize)
    if task_id == 'player_position':
        return export.export_player_position_classifier(conn, materialize=materialize)
    if task_id == 'afk_status':
        return export.export_afk_status_classifier(conn, materialize=materialize)
    raise ValueError(f'未知训练任务: {task_id}')


def new_run_id(task_id: str) -> str:
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    return '{}-{}-{}'.format(task_id.replace('_', '-'), stamp, uuid4().hex[:6])


def interrupted_run_checkpoint(run: Dict[str, Any]) -> Path:
    if run.get('status') != 'interrupted':
        raise ValueError('只有已中断的训练才能从断点恢复')
    checkpoint = (
        config.WORK_DIR
        / 'training-runs'
        / str(run.get('id') or '')
        / 'ultralytics'
        / 'weights'
        / 'last.pt'
    )
    if not checkpoint.is_file() or checkpoint.stat().st_size <= 0:
        raise FileNotFoundError('中断训练没有可用的 last.pt 断点')
    return checkpoint


class TrainingManager:
    """一次只运行一个训练进程，避免多个任务争抢 MPS/内存。"""

    def __init__(self, db_path: Path = config.DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._active_run_id: Optional[str] = None
        self._process: Optional[subprocess.Popen[str]] = None
        self._cancelled = set()

    def active_run_id(self) -> Optional[str]:
        with self._lock:
            return self._active_run_id

    def start(self, run_id: str) -> None:
        with self._lock:
            if self._active_run_id is not None:
                raise RuntimeError(f'已有训练正在运行: {self._active_run_id}')
            self._active_run_id = run_id
        threading.Thread(
            target=self._run, args=(run_id,), name=f'training-{run_id}', daemon=True
        ).start()

    def cancel(self, run_id: str) -> None:
        with self._lock:
            if self._active_run_id != run_id:
                raise KeyError(f'训练任务未在运行: {run_id}')
            self._cancelled.add(run_id)
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def shutdown(self) -> None:
        """标注服务退出时停止训练子进程，避免留下孤儿进程。"""
        with self._lock:
            run_id = self._active_run_id
            process = self._process
            if run_id is not None:
                self._cancelled.add(run_id)
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _update(self, run_id: str, **values: Any) -> None:
        conn = db.connect(self._db_path)
        try:
            db.update_training_run(conn, run_id, **values)
        finally:
            conn.close()

    def _run(self, run_id: str) -> None:
        conn = db.connect(self._db_path)
        try:
            run = db.get_training_run(conn, run_id)
            if run is None:
                raise KeyError(f'训练记录不存在: {run_id}')
            dataset = conn.execute(
                'SELECT manifest_path FROM dataset_versions WHERE id = ?',
                (run['dataset_version_id'],),
            ).fetchone()
            if dataset is None:
                raise KeyError(f'数据集版本不存在: {run["dataset_version_id"]}')
        finally:
            conn.close()
        definition = TRAINING_TASKS[run['task_id']]
        dataset_dir = Path(dataset['manifest_path']).parent
        run_dir = config.WORK_DIR / 'training-runs' / run_id
        resume_checkpoint = (
            interrupted_run_checkpoint(run) if run['status'] == 'interrupted' else None
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = run_dir / 'model.onnx'
        log_path = Path(run['log_path'])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            '-m',
            'labeler.training_runner',
            '--task-id',
            run['task_id'],
            '--kind',
            definition['kind'],
            '--dataset-dir',
            str(dataset_dir),
            '--run-dir',
            str(run_dir),
            '--artifact',
            str(artifact_path),
            '--base-model',
            str(config.MODELS_DIR / 'base' / definition['base_model']),
            '--epochs',
            str(run['epochs']),
            '--imgsz',
            str(definition['imgsz']),
        ]
        if definition['kind'] == 'classify':
            command.extend(
                [
                    '--input-width',
                    str(definition['input_width']),
                    '--input-height',
                    str(definition['input_height']),
                ]
            )
        if resume_checkpoint is not None:
            command.extend(['--resume-checkpoint', str(resume_checkpoint)])
        metrics: Dict[str, Any] = {}
        try:
            if run_id in self._cancelled:
                self._update(
                    run_id, status='cancelled', finished_at=db.now(), error='用户取消'
                )
                return
            running_values: Dict[str, Any] = {
                'status': 'running',
                'error': '',
                'finished_at': None,
            }
            if resume_checkpoint is None:
                running_values['started_at'] = db.now()
            self._update(run_id, **running_values)
            environment = dict(os.environ)
            environment['PYTHONUNBUFFERED'] = '1'
            with log_path.open('a', encoding='utf-8') as log_handle:
                process = subprocess.Popen(
                    command,
                    cwd=str(Path(__file__).resolve().parent.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=environment,
                )
                with self._lock:
                    self._process = process
                assert process.stdout is not None
                for line in process.stdout:
                    log_handle.write(line)
                    log_handle.flush()
                    stripped = line.strip()
                    if stripped.startswith(PROGRESS_PREFIX):
                        payload = json.loads(stripped[len(PROGRESS_PREFIX) :])
                        metrics = payload.get('metrics') or metrics
                        self._update(
                            run_id,
                            current_epoch=int(payload.get('epoch', 0)),
                            progress=float(payload.get('progress', 0)),
                            metrics=metrics,
                        )
                    elif stripped.startswith(RESULT_PREFIX):
                        payload = json.loads(stripped[len(RESULT_PREFIX) :])
                        metrics = payload.get('metrics') or metrics
                return_code = process.wait()
            if run_id in self._cancelled:
                self._update(
                    run_id, status='cancelled', finished_at=db.now(), error='用户取消'
                )
            elif return_code != 0:
                self._update(
                    run_id,
                    status='failed',
                    finished_at=db.now(),
                    error=f'训练进程退出码 {return_code}，请查看日志',
                )
            elif not artifact_path.is_file():
                self._update(
                    run_id,
                    status='failed',
                    finished_at=db.now(),
                    error='训练完成但没有生成 ONNX 模型',
                )
            else:
                self._update(
                    run_id,
                    status='succeeded',
                    current_epoch=int(run['epochs']),
                    progress=1.0,
                    metrics=metrics,
                    artifact_path=str(artifact_path),
                    finished_at=db.now(),
                )
        except Exception as error:  # noqa: BLE001
            try:
                self._update(
                    run_id,
                    status='failed',
                    finished_at=db.now(),
                    error=str(error)[:500],
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            with self._lock:
                self._process = None
                self._active_run_id = None
                self._cancelled.discard(run_id)


def publish_local_model(conn: Any, run_id: str) -> Dict[str, str]:
    """把成功模型设为标注工作台当前测试模型；不触碰 NAS 或 MacBook。"""
    run = db.get_training_run(conn, run_id)
    if run is None:
        raise KeyError(f'训练记录不存在: {run_id}')
    if run['status'] != 'succeeded':
        raise ValueError('只有训练成功的模型才能发布到本机测试区')
    source, metadata_source = managed_assets.resolve_model_run(
        run_id, Path(run['artifact_path'])
    )
    definition = TRAINING_TASKS[run['task_id']]
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    destination = config.MODELS_DIR / definition['publish_name']
    if destination.is_file():
        backup_dir = config.WORK_DIR / 'model-backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        shutil.copy2(
            destination, backup_dir / f'{destination.stem}-{stamp}{destination.suffix}'
        )
        current_metadata = destination.with_suffix('.json')
        if current_metadata.is_file():
            shutil.copy2(
                current_metadata, backup_dir / f'{destination.stem}-{stamp}.json'
            )
    shutil.copy2(source, destination)
    if metadata_source.is_file():
        shutil.copy2(metadata_source, destination.with_suffix('.json'))
    db.update_training_run(conn, run_id, published_path=str(destination))
    return {'run_id': run_id, 'path': str(destination)}
