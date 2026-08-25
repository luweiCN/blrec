from dataclasses import replace

import pytest

from blrec.vainglory.analysis_protocol import (
    decode_afk_statuses,
    decode_match,
    encode_match,
)
from blrec.vainglory.analyzer import AnalyzedAfkStatus, AnalyzedMatch
from blrec.vainglory.ocr import ResultHeader, ResultOcr
from blrec.vainglory.vision import ResultLayout


def _match() -> AnalyzedMatch:
    return AnalyzedMatch(
        part_id=1,
        part_index=1,
        result_at_ms=90_000,
        layout=ResultLayout('teal', 'orange', 'teal', 'left', 0.99),
        ocr=ResultOcr(ResultHeader('胜利', 'normal', 900, 8, 4, 20_000, 18_000), ()),
        heroes=(),
        confidence=0.95,
        afk_statuses=tuple(
            AnalyzedAfkStatus(
                side=side,
                slot=slot,
                status='afk' if (side, slot) == ('right', 2) else 'active',
                probability=0.9 if (side, slot) == ('right', 2) else 0.1,
                model_version='afk-run-1',
            )
            for side in ('left', 'right')
            for slot in range(1, 4)
        ),
    )


def test_match_protocol_round_trips_complete_afk_slot_predictions() -> None:
    match = _match()

    restored = decode_match(encode_match(match))

    assert restored.afk_statuses == match.afk_statuses


def test_match_protocol_keeps_older_worker_payload_compatible() -> None:
    payload = encode_match(replace(_match(), afk_statuses=()))
    payload.pop('afk_statuses')

    assert decode_match(payload).afk_statuses == ()


def test_match_protocol_rejects_partial_afk_slot_predictions() -> None:
    payload = encode_match(_match())
    payload['afk_statuses'] = payload['afk_statuses'][:-1]

    with pytest.raises(ValueError, match='完整覆盖'):
        decode_match(payload)


def test_standalone_afk_protocol_keeps_low_positive_probability_for_review() -> None:
    payload = encode_match(_match())['afk_statuses']
    payload[4] = {
        **payload[4],
        'status': 'unknown',
        'probability': 0.527,
        'gate_reason': 'model_low_positive_probability',
    }

    statuses = decode_afk_statuses(payload)

    assert statuses[4].status == 'unknown'
    assert statuses[4].probability == pytest.approx(0.527)
    assert statuses[4].gate_reason == 'model_low_positive_probability'
