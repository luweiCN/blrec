import json
import sqlite3
from pathlib import Path

from scripts.vainglory_tail_reanalysis_20260812 import (
    build_tail_candidate,
    compare_tail_candidate,
    discover_tail_candidates,
)


def _summary(*, terminal_window: bool) -> dict:
    windows = [
        {'startMs': 1_132_956, 'endMs': 1_197_956, 'focusMs': 1_172_956},
        {'startMs': 2_352_913, 'endMs': 2_417_913, 'focusMs': 2_392_913},
    ]
    if terminal_window:
        windows.append({'startMs': 3_497_869, 'endMs': 3_566_000, 'focusMs': 3_537_869})
    return {
        'pipeline': 'timeline-v2',
        'modelPackageId': 'vg-vision-v1-20260812',
        'timelineSegments': [
            {'startMs': 120_000, 'endMs': 1_172_956, 'mode': '3v3'},
            {'startMs': 1_377_956, 'endMs': 2_392_913, 'mode': '3v3'},
            {'startMs': 2_577_898, 'endMs': 3_537_869, 'mode': '3v3'},
        ],
        'resultWindows': windows,
    }


def _row(**overrides: object) -> dict:
    value = {
        'part_id': 1181,
        'session_id': 416,
        'part_index': 1,
        'title': '东南亚分边',
        'record_duration_seconds': 3566,
        'state': 'ready',
        'algorithm_version': 18,
        'candidate_count': 2,
        'match_count': 2,
        'completed_at': 100,
    }
    value.update(overrides)
    return value


def test_builds_baseline_for_terminal_segment_without_a_window() -> None:
    candidate = build_tail_candidate(
        _row(),
        _summary(terminal_window=False),
        (
            {'resultAtMs': 1_174_206, 'durationSeconds': 926, 'gameMode': '3v3'},
            {'resultAtMs': 2_391_163, 'durationSeconds': 959, 'gameMode': '3v3'},
        ),
    )

    assert candidate is not None
    assert candidate['issue']['code'] == 'terminal_segment_without_result_window'
    assert candidate['issue']['tailGapMs'] == 28_131
    assert candidate['old']['matchCount'] == 2
    assert candidate['old']['resultWindowCount'] == 2
    assert candidate['old']['timelineSegmentCount'] == 3


def test_does_not_select_terminal_segment_that_already_has_a_window() -> None:
    assert build_tail_candidate(_row(), _summary(terminal_window=True), ()) is None


def test_discovers_candidates_from_the_database(tmp_path: Path) -> None:
    database = tmp_path / 'blrec.sqlite3'
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE recording_sessions(id INTEGER PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE recording_parts(
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            part_index INTEGER NOT NULL,
            record_duration_seconds INTEGER
        );
        CREATE TABLE vainglory_part_jobs(
            part_id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            algorithm_version INTEGER NOT NULL,
            candidate_count INTEGER,
            match_count INTEGER NOT NULL,
            completed_at INTEGER,
            analysis_summary_json TEXT
        );
        CREATE TABLE vainglory_matches(
            id INTEGER PRIMARY KEY,
            result_part_id INTEGER NOT NULL,
            result_at_ms INTEGER NOT NULL,
            duration_seconds INTEGER,
            game_mode TEXT NOT NULL,
            team_size INTEGER
        );
        """
    )
    connection.execute(
        'INSERT INTO recording_sessions(id,title) VALUES(416,?)', ('东南亚分边',)
    )
    connection.execute('INSERT INTO recording_parts VALUES(1181,416,1,3566)')
    connection.execute(
        'INSERT INTO vainglory_part_jobs VALUES(1181,416,\'ready\',18,2,2,100,?)',
        (json.dumps(_summary(terminal_window=False)),),
    )
    connection.execute(
        "INSERT INTO vainglory_matches VALUES(1,1181,1174206,926,'3v3',3)"
    )
    connection.commit()

    candidates = discover_tail_candidates(connection)

    assert len(candidates) == 1
    assert candidates[0]['partId'] == 1181
    assert candidates[0]['old']['matches'][0]['resultAtMs'] == 1_174_206


def test_comparison_reports_recovered_terminal_match() -> None:
    baseline = build_tail_candidate(_row(), _summary(terminal_window=False), ())
    assert baseline is not None
    current = {
        'state': 'ready',
        'algorithmVersion': 18,
        'completedAt': 200,
        'candidateCount': 3,
        'matchCount': 3,
        'analysisSummary': _summary(terminal_window=True),
        'matches': (),
    }

    comparison = compare_tail_candidate(baseline, current, queued_at=150)

    assert comparison['status'] == 'recovered_match'
    assert comparison['terminalWindowPresent'] is True
    assert comparison['matchCountDelta'] == 1


def test_comparison_distinguishes_no_result_from_still_missing_window() -> None:
    baseline = build_tail_candidate(_row(), _summary(terminal_window=False), ())
    assert baseline is not None
    covered = {
        'state': 'ready',
        'algorithmVersion': 18,
        'completedAt': 200,
        'candidateCount': 2,
        'matchCount': 2,
        'analysisSummary': _summary(terminal_window=True),
        'matches': (),
    }
    missing = dict(covered, analysisSummary=_summary(terminal_window=False))

    assert (
        compare_tail_candidate(baseline, covered, queued_at=150)['status']
        == 'verified_without_result'
    )
    assert (
        compare_tail_candidate(baseline, missing, queued_at=150)['status']
        == 'terminal_window_missing'
    )


def test_comparison_stays_pending_until_a_new_run_finishes() -> None:
    baseline = build_tail_candidate(_row(), _summary(terminal_window=False), ())
    assert baseline is not None
    current = {
        'state': 'analyzing',
        'algorithmVersion': 18,
        'completedAt': None,
        'candidateCount': 0,
        'matchCount': 0,
        'analysisSummary': None,
        'matches': (),
    }

    assert (
        compare_tail_candidate(baseline, current, queued_at=150)['status'] == 'pending'
    )
