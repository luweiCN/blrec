from __future__ import annotations

import base64
import math
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, cast

from blrec.vainglory.analyzer import (
    AnalyzedAfkStatus,
    AnalyzedHero,
    AnalyzedMatch,
    TrainingCandidate,
    TrainingCandidateBox,
    TrainingCandidateLabel,
    TrainingCandidateTask,
)
from blrec.vainglory.ocr import OcrPlayer, PlayerStats, ResultHeader, ResultOcr
from blrec.vainglory.vision import RecordedPlayer, ResultLayout, ViewportTransform


def _encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode('ascii') if value else ''


def _decode_bytes(value: object) -> bytes:
    return base64.b64decode(str(value), validate=True) if value else b''


_TRAINING_CANDIDATE_LABELS = {
    'screen_state': {
        'not_vainglory',
        'out_of_match',
        'pre_match',
        'in_match',
        'talent_select',
        'post_match',
        'transition',
    },
    'bp_review': {'bp_3v3', 'bp_aram', 'bp_5v5', 'not_bp'},
    'key_screen_review': {'result_page', 'scoreboard', 'other'},
    'result_detector': {'result_panel', 'no_result_panel'},
    'mode_gate': {'blocked_gate', 'open_entrance', 'no_evidence'},
    'match_flow': {'match_flow', 'not_match_flow'},
    'hero_select': {'not_select', 'select_3v3', 'select_aram', 'select_5v5'},
    'match_mode': {'3v3', 'aram', '5v5'},
}


def encode_training_candidate(candidate: TrainingCandidate) -> Dict[str, Any]:
    return {
        'task': candidate.task,
        'at_ms': candidate.at_ms,
        'segment_start_ms': candidate.segment_start_ms,
        'image_jpeg': _encode_bytes(candidate.image_jpeg),
        'image_width': candidate.image_width,
        'image_height': candidate.image_height,
        'model_version': candidate.model_version,
        'suggested_label': candidate.suggested_label,
        'suggestion_confidence': candidate.suggestion_confidence,
        'stage_class': candidate.stage_class,
        'stage_confidence': candidate.stage_confidence,
        'mode_class': candidate.mode_class,
        'mode_confidence': candidate.mode_confidence,
        'selection_reason': candidate.selection_reason,
        'suggested_boxes': [
            {'type': box.box_type, 'x': box.x, 'y': box.y, 'w': box.w, 'h': box.h}
            for box in candidate.suggested_boxes
        ],
    }


def decode_training_candidate(payload: Mapping[str, Any]) -> TrainingCandidate:
    task = str(payload.get('task', 'bp_review'))
    if task not in _TRAINING_CANDIDATE_LABELS:
        raise ValueError('unknown training candidate task')
    suggested_label = str(payload['suggested_label'])
    if suggested_label not in _TRAINING_CANDIDATE_LABELS[task]:
        raise ValueError('unknown training candidate label')
    image_jpeg = _decode_bytes(payload.get('image_jpeg'))
    if not image_jpeg.startswith(b'\xff\xd8') or not image_jpeg.endswith(b'\xff\xd9'):
        raise ValueError('training candidate image is not JPEG')
    if len(image_jpeg) > 1_000_000:
        raise ValueError('training candidate image is too large')

    def confidence(name: str) -> float:
        value = float(payload[name])
        if not 0 <= value <= 1:
            raise ValueError('{} must be between 0 and 1'.format(name))
        return value

    at_ms = int(payload['at_ms'])
    segment_start_ms = int(payload['segment_start_ms'])
    if at_ms < 0 or segment_start_ms < 0:
        raise ValueError('training candidate timestamps must not be negative')
    boxes = []
    for raw_box in payload.get('suggested_boxes') or ():
        if not isinstance(raw_box, Mapping):
            raise ValueError('training candidate box must be an object')
        boxes.append(
            TrainingCandidateBox(
                box_type=str(raw_box.get('type', ''))[:80],
                x=float(raw_box['x']),
                y=float(raw_box['y']),
                w=float(raw_box['w']),
                h=float(raw_box['h']),
            )
        )
    return TrainingCandidate(
        at_ms=at_ms,
        segment_start_ms=segment_start_ms,
        image_jpeg=image_jpeg,
        model_version=str(payload['model_version'])[:80],
        suggested_label=cast(TrainingCandidateLabel, suggested_label),
        suggestion_confidence=confidence('suggestion_confidence'),
        stage_class=str(payload['stage_class'])[:80],
        stage_confidence=confidence('stage_confidence'),
        mode_class=str(payload['mode_class'])[:80],
        mode_confidence=confidence('mode_confidence'),
        selection_reason=str(payload['selection_reason'])[:500],
        image_width=max(0, int(payload.get('image_width', 0))),
        image_height=max(0, int(payload.get('image_height', 0))),
        task=cast(TrainingCandidateTask, task),
        suggested_boxes=tuple(boxes),
    )


def decode_training_candidates(
    payloads: Sequence[Mapping[str, Any]]
) -> Tuple[TrainingCandidate, ...]:
    return tuple(decode_training_candidate(payload) for payload in payloads)


def encode_hero(hero: AnalyzedHero) -> Dict[str, Any]:
    return {
        'side': hero.side,
        'slot': hero.slot,
        'fingerprint': hero.fingerprint,
        'thumbnail_png': _encode_bytes(hero.thumbnail_png),
        'label': hero.label,
        'confidence': hero.confidence,
    }


def decode_hero(payload: Mapping[str, Any]) -> AnalyzedHero:
    return AnalyzedHero(
        side=cast(Any, str(payload['side'])),
        slot=int(payload['slot']),
        fingerprint=str(payload['fingerprint']),
        thumbnail_png=_decode_bytes(payload.get('thumbnail_png')),
        label=str(payload.get('label', '')),
        confidence=float(payload.get('confidence', 0)),
    )


def encode_recorded_player(
    player: Optional[RecordedPlayer],
) -> Optional[Dict[str, Any]]:
    if player is None:
        return None
    return {'side': player.side, 'slot': player.slot, 'confidence': player.confidence}


def decode_recorded_player(
    payload: Optional[Mapping[str, Any]]
) -> Optional[RecordedPlayer]:
    if payload is None:
        return None
    return RecordedPlayer(
        side=cast(Any, str(payload['side'])),
        slot=int(payload['slot']),
        confidence=float(payload['confidence']),
    )


def encode_match(match: AnalyzedMatch) -> Dict[str, Any]:
    viewport = match.layout.viewport
    header = match.ocr.header
    return {
        'part_id': match.part_id,
        'part_index': match.part_index,
        'result_at_ms': match.result_at_ms,
        'layout': {
            'left_color': match.layout.left_color,
            'right_color': match.layout.right_color,
            'winner_color': match.layout.winner_color,
            'winner_side': match.layout.winner_side,
            'confidence': match.layout.confidence,
            'team_size': match.layout.team_size,
            'viewport': {
                'name': viewport.name,
                'left': viewport.left,
                'top': viewport.top,
                'width': viewport.width,
                'height': viewport.height,
                'ocr_profile': viewport.ocr_profile,
            },
        },
        'ocr': {
            'header': {
                'result_text': header.result_text,
                'end_reason': header.end_reason,
                'duration_seconds': header.duration_seconds,
                'left_kills': header.left_kills,
                'right_kills': header.right_kills,
                'left_economy': header.left_economy,
                'right_economy': header.right_economy,
            },
            'players': [
                {
                    'side': player.side,
                    'slot': player.slot,
                    'name': player.name,
                    'normalized_name': player.normalized_name,
                    'confidence': player.confidence,
                    'raw_name': player.raw_name,
                    'stats': {
                        'kills': player.stats.kills,
                        'deaths': player.stats.deaths,
                        'assists': player.stats.assists,
                        'economy': player.stats.economy,
                        'last_hits': player.stats.last_hits,
                    },
                }
                for player in match.ocr.players
            ],
            'raw_text': match.ocr.raw_text,
        },
        'heroes': [encode_hero(hero) for hero in match.heroes],
        'afk_statuses': [
            {
                'side': value.side,
                'slot': value.slot,
                'status': value.status,
                'probability': value.probability,
                'model_version': value.model_version,
                'gate_reason': value.gate_reason,
            }
            for value in match.afk_statuses
        ],
        'confidence': match.confidence,
        'result_frame_png': _encode_bytes(match.result_frame_png),
        'game_mode': match.game_mode,
        'recorded_player': encode_recorded_player(match.recorded_player),
        'match_kind': match.match_kind,
        'view_context': match.view_context,
        'stats_eligible': match.stats_eligible,
        'stats_exclusion_reason': match.stats_exclusion_reason,
    }


def _optional_int(value: object) -> Optional[int]:
    return None if value is None else int(cast(Any, value))


def decode_match(payload: Mapping[str, Any]) -> AnalyzedMatch:
    layout_payload = cast(Mapping[str, Any], payload['layout'])
    viewport_payload = cast(Mapping[str, Any], layout_payload['viewport'])
    ocr_payload = cast(Mapping[str, Any], payload['ocr'])
    header_payload = cast(Mapping[str, Any], ocr_payload['header'])
    player_payloads = cast(Sequence[Mapping[str, Any]], ocr_payload['players'])
    hero_payloads = cast(Sequence[Mapping[str, Any]], payload['heroes'])
    recorded_payload = cast(Optional[Mapping[str, Any]], payload.get('recorded_player'))
    afk_payloads = cast(Sequence[Mapping[str, Any]], payload.get('afk_statuses') or ())
    viewport = ViewportTransform(
        name=str(viewport_payload['name']),
        left=float(viewport_payload['left']),
        top=float(viewport_payload['top']),
        width=float(viewport_payload['width']),
        height=float(viewport_payload['height']),
        ocr_profile=cast(Any, str(viewport_payload['ocr_profile'])),
    )
    layout = ResultLayout(
        left_color=cast(Any, str(layout_payload['left_color'])),
        right_color=cast(Any, str(layout_payload['right_color'])),
        winner_color=cast(Any, str(layout_payload['winner_color'])),
        winner_side=cast(Any, str(layout_payload['winner_side'])),
        confidence=float(layout_payload['confidence']),
        team_size=cast(Any, int(layout_payload['team_size'])),
        viewport=viewport,
    )
    header = ResultHeader(
        result_text=str(header_payload['result_text']),
        end_reason=str(header_payload['end_reason']),
        duration_seconds=_optional_int(header_payload.get('duration_seconds')),
        left_kills=_optional_int(header_payload.get('left_kills')),
        right_kills=_optional_int(header_payload.get('right_kills')),
        left_economy=_optional_int(header_payload.get('left_economy')),
        right_economy=_optional_int(header_payload.get('right_economy')),
    )
    players = tuple(
        OcrPlayer(
            side=str(player['side']),
            slot=int(player['slot']),
            name=str(player['name']),
            normalized_name=str(player['normalized_name']),
            stats=PlayerStats(
                kills=_optional_int(
                    cast(Mapping[str, Any], player['stats']).get('kills')
                ),
                deaths=_optional_int(
                    cast(Mapping[str, Any], player['stats']).get('deaths')
                ),
                assists=_optional_int(
                    cast(Mapping[str, Any], player['stats']).get('assists')
                ),
                economy=_optional_int(
                    cast(Mapping[str, Any], player['stats']).get('economy')
                ),
                last_hits=None,
            ),
            confidence=float(player.get('confidence', 0)),
            raw_name=str(player.get('raw_name', '')),
        )
        for player in player_payloads
    )
    afk_statuses = []
    for value in afk_payloads:
        side = str(value.get('side') or '')
        slot = int(value.get('slot') or 0)
        status = str(value.get('status') or 'unknown')
        probability_value = value.get('probability')
        probability = None if probability_value is None else float(probability_value)
        if side not in ('left', 'right') or not 1 <= slot <= 5:
            raise ValueError('挂机预测槽位无效')
        if status not in ('unknown', 'active', 'afk'):
            raise ValueError('挂机预测状态无效')
        if probability is not None and (
            not math.isfinite(probability) or not 0 <= probability <= 1
        ):
            raise ValueError('挂机预测概率必须在零和一之间')
        afk_statuses.append(
            AnalyzedAfkStatus(
                side=cast(Any, side),
                slot=slot,
                status=cast(Any, status),
                probability=probability,
                model_version=str(value.get('model_version') or '')[:200],
                gate_reason=str(value.get('gate_reason') or '')[:200],
            )
        )
    if afk_statuses:
        positions = {(value.side, value.slot) for value in afk_statuses}
        expected_positions = {
            (side, slot)
            for side in ('left', 'right')
            for slot in range(1, layout.team_size + 1)
        }
        if len(afk_statuses) != len(positions) or positions != expected_positions:
            raise ValueError('挂机预测必须完整覆盖结算页槽位')
    return AnalyzedMatch(
        part_id=int(payload['part_id']),
        part_index=int(payload['part_index']),
        result_at_ms=int(payload['result_at_ms']),
        layout=layout,
        ocr=ResultOcr(
            header=header,
            players=players,
            raw_text=str(ocr_payload.get('raw_text', '')),
        ),
        heroes=tuple(decode_hero(hero) for hero in hero_payloads),
        confidence=float(payload['confidence']),
        result_frame_png=_decode_bytes(payload.get('result_frame_png')),
        game_mode=str(payload.get('game_mode', 'unknown')),
        recorded_player=decode_recorded_player(recorded_payload),
        match_kind=cast(Any, str(payload.get('match_kind', 'unknown'))),
        view_context=cast(Any, str(payload.get('view_context', 'unknown'))),
        stats_eligible=bool(payload.get('stats_eligible', True)),
        stats_exclusion_reason=str(payload.get('stats_exclusion_reason', '')),
        afk_statuses=tuple(afk_statuses),
    )


def decode_matches(payloads: Sequence[Mapping[str, Any]]) -> Tuple[AnalyzedMatch, ...]:
    return tuple(decode_match(payload) for payload in payloads)
