"""BP 主动学习候选选择。

现有 multi-v2 在未见过的主播上会高置信度把 5V5 / 大乱斗 BP 判成 3V3，
所以不能只收集低置信度样本。候选同时来自：

- 模型认为是 pre_match 的代表帧；
- 从游戏外/转场进入 gameplay 前的帧；
- pre_match 概率不高但进入候选区间的边界帧。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Sequence

from .inference import MULTI_CLASSES

_MODE_TO_LABEL = {
    '3v3': 'bp_3v3',
    'aram': 'bp_aram',
    '5v5': 'bp_5v5',
}
_ENTRY_PREVIOUS_STAGES = {'out_of_match', 'transition', 'pre_match'}


def balanced_frame_rows(
        rows: Sequence[Dict[str, Any]], *, maximum: int) -> List[Dict[str, Any]]:
    """按视频均匀抽取待推理帧，避免长视频或单一主播淹没候选。"""
    if maximum <= 0:
        raise ValueError('maximum 必须为正数')
    by_video: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_video[int(row['video_id'])].append(row)
    if not by_video:
        return []
    for items in by_video.values():
        items.sort(key=lambda item: int(item['timestamp_ms']))

    per_video = max(1, maximum // len(by_video))
    selected: List[Dict[str, Any]] = []
    leftovers: List[Dict[str, Any]] = []
    for video_id in sorted(by_video):
        items = by_video[video_id]
        count = min(len(items), per_video)
        if count == len(items):
            selected.extend(items)
            continue
        indices = {
            round(index * (len(items) - 1) / max(1, count - 1))
            for index in range(count)
        }
        selected.extend(items[index] for index in sorted(indices))
        leftovers.extend(
            item for index, item in enumerate(items) if index not in indices)
    if len(selected) < maximum:
        leftovers.sort(key=lambda item: (int(item['video_id']),
                                         int(item['timestamp_ms'])))
        selected.extend(leftovers[:maximum - len(selected)])
    return sorted(
        selected[:maximum],
        key=lambda item: (int(item['video_id']), int(item['timestamp_ms'])),
    )


def observation_from_prediction(
        frame: Dict[str, Any], prediction: Dict[str, Any]) -> Dict[str, Any]:
    """把 multi-v2 API 输出转换为候选选择所需的稳定字段。"""
    if prediction.get('task') != 'multi':
        raise ValueError('BP 候选收集只支持 multi 多头模型')
    stage = prediction['stage']
    mode = prediction['mode']
    stage_probs = stage.get('raw_probs') or []
    try:
        pre_match_index = MULTI_CLASSES['stage'].index('pre_match')
        pre_match_confidence = float(stage_probs[pre_match_index])
    except (IndexError, ValueError):
        pre_match_confidence = 0.0
    mode_top = mode['top5']
    mode_margin = (
        float(mode_top[0]['prob']) - float(mode_top[1]['prob'])
        if len(mode_top) > 1 else float(mode_top[0]['prob'])
    )
    return {
        **frame,
        'stage_class': stage['top1']['class'],
        'stage_confidence': float(stage['top1']['prob']),
        'pre_match_confidence': pre_match_confidence,
        'mode_class': mode['top1']['class'],
        'mode_confidence': float(mode['top1']['prob']),
        'mode_margin': max(0.0, mode_margin),
        'raw_prediction': prediction,
    }


def _episodes(
        observations: Sequence[Dict[str, Any]], *, minimum_pre_match: float = 0.25,
        maximum_gap_ms: int = 20_000) -> List[List[Dict[str, Any]]]:
    episodes: List[List[Dict[str, Any]]] = []
    for observation in observations:
        is_candidate = (
            observation['stage_class'] == 'pre_match'
            or observation['pre_match_confidence'] >= minimum_pre_match
        )
        if not is_candidate:
            continue
        if (
            episodes
            and observation['timestamp_ms'] - episodes[-1][-1]['timestamp_ms']
            <= maximum_gap_ms
        ):
            episodes[-1].append(observation)
        else:
            episodes.append([observation])
    return episodes


def _representatives(episode: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """每个 BP 候选段最多取三张：最像、模式最有争议、时间中点。"""
    if not episode:
        return []
    candidates = [
        max(episode, key=lambda item: item['pre_match_confidence']),
        min(episode, key=lambda item: item['mode_margin']),
        episode[len(episode) // 2],
    ]
    result = []
    seen = set()
    for item in candidates:
        if item['frame_id'] in seen:
            continue
        seen.add(item['frame_id'])
        result.append(item)
    return result


def select_candidates(
        observations: Iterable[Dict[str, Any]], *, maximum: int = 300,
        maximum_per_video: int = 24) -> List[Dict[str, Any]]:
    """从模型观察中选择少量高价值、可人工复核的 BP 候选。"""
    if maximum <= 0 or maximum_per_video <= 0:
        raise ValueError('候选数量限制必须为正数')
    by_video: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        by_video[int(observation['video_id'])].append(observation)
    selected_by_video: Dict[int, Dict[int, Dict[str, Any]]] = defaultdict(dict)

    def add(observation: Dict[str, Any], reason: str, priority: float) -> None:
        video_items = selected_by_video[int(observation['video_id'])]
        current = video_items.get(int(observation['frame_id']))
        if current is None:
            current = {**observation, 'reasons': [], 'priority': priority}
            video_items[int(observation['frame_id'])] = current
        current['priority'] = max(float(current['priority']), priority)
        if reason not in current['reasons']:
            current['reasons'].append(reason)

    for video_id, items in by_video.items():
        items.sort(key=lambda item: int(item['timestamp_ms']))
        for episode in _episodes(items):
            for representative in _representatives(episode):
                add(
                    representative,
                    '模型认为接近选英雄界面',
                    100.0 + representative['pre_match_confidence'],
                )

        for index, current in enumerate(items):
            if current['stage_class'] != 'gameplay' or index == 0:
                continue
            previous = items[index - 1]
            if previous['stage_class'] not in _ENTRY_PREVIOUS_STAGES:
                continue
            for candidate in items[max(0, index - 3):index]:
                add(
                    candidate,
                    '游戏外/转场进入游戏前的代表帧',
                    80.0 + candidate['pre_match_confidence'],
                )

        for item in items:
            if (
                item['stage_class'] != 'pre_match'
                and item['pre_match_confidence'] >= 0.08
                and item['stage_confidence'] < 0.80
            ):
                add(
                    item,
                    '阶段模型在选英雄附近存在争议',
                    60.0 + item['pre_match_confidence'],
                )

    limited_by_video: Dict[int, List[Dict[str, Any]]] = {}
    for video_id, candidates in selected_by_video.items():
        ordered = sorted(
            candidates.values(),
            key=lambda item: (-float(item['priority']), int(item['timestamp_ms'])),
        )[:maximum_per_video]
        for item in ordered:
            likely_bp = (
                item['stage_class'] == 'pre_match'
                or item['pre_match_confidence'] >= 0.20
            )
            item['suggested_label'] = (
                _MODE_TO_LABEL.get(item['mode_class'], 'not_bp')
                if likely_bp else 'not_bp'
            )
            item['suggestion_confidence'] = (
                min(item['pre_match_confidence'], item['mode_confidence'])
                if likely_bp else item['stage_confidence']
            )
            item['selection_reason'] = '；'.join(item.pop('reasons'))
        limited_by_video[video_id] = ordered

    result: List[Dict[str, Any]] = []
    while len(result) < maximum:
        added = False
        for video_id in sorted(limited_by_video):
            items = limited_by_video[video_id]
            if not items:
                continue
            result.append(items.pop(0))
            added = True
            if len(result) >= maximum:
                break
        if not added:
            break
    return result
