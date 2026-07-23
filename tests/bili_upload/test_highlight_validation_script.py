from pathlib import Path

from scripts.validate_highlight_cuts import (
    _case_duration_ms,
    _case_start_ms,
    _sample_sources,
)


def test_nas_validation_samples_five_positions_per_selected_source() -> None:
    eligible = tuple((Path(str(index)), 100_000) for index in range(108))

    sampled = _sample_sources(eligible, 300)

    assert len(sampled) == 60
    assert sampled[0] == eligible[0]
    assert sampled[-1] == eligible[-1]
    assert len(set(sampled)) == len(sampled)
    cases_by_source = {source: [] for source in sampled}
    for case_index in range(300):
        source, duration_ms = sampled[case_index % len(sampled)]
        round_index = case_index // len(sampled)
        clip_duration_ms = _case_duration_ms(5_000, round_index)
        cases_by_source[(source, duration_ms)].append(
            (
                _case_start_ms(duration_ms, clip_duration_ms, round_index),
                clip_duration_ms,
            )
        )
    assert set(tuple(cases) for cases in cases_by_source.values()) == {
        (
            (1_100, 5_000),
            (22_437, 10_000),
            (42_375, 15_000),
            (59_812, 20_000),
            (62_775, 30_000),
        )
    }
