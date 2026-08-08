from __future__ import annotations

import base64
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, cast

from blrec.vainglory.analyzer import AnalyzedHero, AnalyzedMatch
from blrec.vainglory.ocr import OcrPlayer, PlayerStats, ResultHeader, ResultOcr
from blrec.vainglory.vision import RecordedPlayer, ResultLayout, ViewportTransform


def _encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode('ascii') if value else ''


def _decode_bytes(value: object) -> bytes:
    return base64.b64decode(str(value), validate=True) if value else b''


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
                last_hits=_optional_int(
                    cast(Mapping[str, Any], player['stats']).get('last_hits')
                ),
            ),
            confidence=float(player.get('confidence', 0)),
            raw_name=str(player.get('raw_name', '')),
        )
        for player in player_payloads
    )
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
    )


def decode_matches(payloads: Sequence[Mapping[str, Any]]) -> Tuple[AnalyzedMatch, ...]:
    return tuple(decode_match(payload) for payload in payloads)
