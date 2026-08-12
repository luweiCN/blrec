#!/usr/bin/env python3
"""Reanalyse and monitor timeline-v2 parts skipped at the end of a video.

The script keeps the pre-reanalysis rows in a persistent JSON state file before
it queues anything.  Later ``check`` and ``monitor`` runs compare the same part
IDs with their new timeline, result-window, candidate and match counts.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    from vainglory_reanalysis_recovery_20260812 import (
        RecoveryApiError,
        RecoveryCandidate,
        RecoveryClient,
    )
except ImportError:  # Imported as ``scripts.*`` by the test suite.
    from scripts.vainglory_reanalysis_recovery_20260812 import (
        RecoveryApiError,
        RecoveryCandidate,
        RecoveryClient,
    )

DEFAULT_DATABASE = Path('/cfg/blrec.sqlite3')
DEFAULT_STATE = Path('/cfg/vainglory-tail-reanalysis-20260812.json')
ISSUE_CODE = 'terminal_segment_without_result_window'
TAIL_GAP_LIMIT_MS = 120_000


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mapping_list(value: Any) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _terminal_window_present(summary: Mapping[str, Any]) -> bool:
    segments = _mapping_list(summary.get('timelineSegments'))
    windows = _mapping_list(summary.get('resultWindows'))
    if not segments:
        return False
    terminal = segments[-1]
    segment_start = _integer(terminal.get('startMs'), -1)
    segment_end = _integer(terminal.get('endMs'), -1)
    if segment_start < 0 or segment_end < segment_start:
        return False
    for window in windows:
        start_ms = _integer(window.get('startMs'), -1)
        end_ms = _integer(window.get('endMs'), -1)
        focus_value = window.get('focusMs')
        focus_ms = None if focus_value is None else _integer(focus_value, -1)
        if focus_ms is not None and segment_start <= focus_ms <= segment_end:
            return True
        if start_ms <= segment_end <= end_ms:
            return True
    return False


def _normalized_matches(matches: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    for match in matches:
        normalized.append(
            {
                'resultAtMs': _integer(
                    match.get('resultAtMs', match.get('result_at_ms'))
                ),
                'durationSeconds': (
                    None
                    if match.get('durationSeconds', match.get('duration_seconds'))
                    is None
                    else _integer(
                        match.get('durationSeconds', match.get('duration_seconds'))
                    )
                ),
                'gameMode': str(match.get('gameMode', match.get('game_mode')) or ''),
                'teamSize': (
                    None
                    if match.get('teamSize', match.get('team_size')) is None
                    else _integer(match.get('teamSize', match.get('team_size')))
                ),
            }
        )
    return normalized


def build_tail_candidate(
    row: Mapping[str, Any],
    summary: Mapping[str, Any],
    matches: Sequence[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    if str(row.get('state') or '') != 'ready':
        return None
    if summary.get('pipeline') != 'timeline-v2':
        return None
    segments = _mapping_list(summary.get('timelineSegments'))
    if not segments or _terminal_window_present(summary):
        return None
    duration_ms = _integer(row.get('record_duration_seconds')) * 1_000
    terminal_end_ms = _integer(segments[-1].get('endMs'), -1)
    tail_gap_ms = duration_ms - terminal_end_ms
    if duration_ms <= 0 or not -5_000 <= tail_gap_ms <= TAIL_GAP_LIMIT_MS:
        return None
    windows = _mapping_list(summary.get('resultWindows'))
    old_matches = _normalized_matches(matches)
    return {
        'sessionId': _integer(row.get('session_id')),
        'partId': _integer(row.get('part_id')),
        'partIndex': _integer(row.get('part_index')),
        'title': str(row.get('title') or ''),
        'durationMs': duration_ms,
        'issue': {
            'code': ISSUE_CODE,
            'tailGapMs': tail_gap_ms,
            'terminalSegment': dict(segments[-1]),
        },
        'old': {
            'state': str(row.get('state') or ''),
            'algorithmVersion': _integer(row.get('algorithm_version')),
            'completedAt': (
                None
                if row.get('completed_at') is None
                else _integer(row.get('completed_at'))
            ),
            'candidateCount': _integer(row.get('candidate_count')),
            'matchCount': _integer(row.get('match_count')),
            'pipeline': str(summary.get('pipeline') or ''),
            'modelPackageId': str(summary.get('modelPackageId') or ''),
            'timelineSegmentCount': len(segments),
            'resultWindowCount': len(windows),
            'timelineSegments': [dict(item) for item in segments],
            'resultWindows': [dict(item) for item in windows],
            'matches': old_matches,
        },
    }


def _part_matches(
    connection: sqlite3.Connection, part_id: int
) -> Tuple[Mapping[str, Any], ...]:
    return tuple(
        {
            'result_at_ms': row['result_at_ms'],
            'duration_seconds': row['duration_seconds'],
            'game_mode': row['game_mode'],
            'team_size': row['team_size'],
        }
        for row in connection.execute(
            'SELECT result_at_ms,duration_seconds,game_mode,team_size '
            'FROM vainglory_matches WHERE result_part_id=? '
            'ORDER BY result_at_ms,id',
            (part_id,),
        ).fetchall()
    )


def discover_tail_candidates(
    connection: sqlite3.Connection,
) -> Tuple[Dict[str, Any], ...]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        'SELECT job.part_id,job.session_id,job.state,job.algorithm_version,'
        'job.candidate_count,job.match_count,job.completed_at,'
        'job.analysis_summary_json,session.title,part.part_index,'
        'part.record_duration_seconds '
        'FROM vainglory_part_jobs job '
        'JOIN recording_sessions session ON session.id=job.session_id '
        'JOIN recording_parts part ON part.id=job.part_id '
        "WHERE job.state='ready' AND job.analysis_summary_json IS NOT NULL "
        'ORDER BY job.session_id,part.part_index,job.part_id'
    ).fetchall()
    candidates = []
    for raw_row in rows:
        row = dict(raw_row)
        try:
            summary = json.loads(str(row['analysis_summary_json']))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(summary, Mapping):
            continue
        candidate = build_tail_candidate(
            row, summary, _part_matches(connection, _integer(row['part_id']))
        )
        if candidate is not None:
            candidates.append(candidate)
    return tuple(candidates)


def _current_part_snapshot(
    connection: sqlite3.Connection, part_id: int
) -> Optional[Dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        'SELECT state,algorithm_version,candidate_count,match_count,completed_at,'
        'analysis_summary_json FROM vainglory_part_jobs WHERE part_id=?',
        (part_id,),
    ).fetchone()
    if row is None:
        return None
    summary = None
    if row['analysis_summary_json']:
        try:
            parsed = json.loads(str(row['analysis_summary_json']))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, Mapping):
            summary = dict(parsed)
    return {
        'state': str(row['state']),
        'algorithmVersion': _integer(row['algorithm_version']),
        'completedAt': (
            None if row['completed_at'] is None else _integer(row['completed_at'])
        ),
        'candidateCount': _integer(row['candidate_count']),
        'matchCount': _integer(row['match_count']),
        'analysisSummary': summary,
        'matches': _normalized_matches(_part_matches(connection, part_id)),
    }


def compare_tail_candidate(
    baseline: Mapping[str, Any], current: Optional[Mapping[str, Any]], *, queued_at: int
) -> Dict[str, Any]:
    old = baseline.get('old')
    if not isinstance(old, Mapping):
        raise ValueError('baseline candidate has no old snapshot')
    old_completed_at = _integer(old.get('completedAt'))
    old_candidate_count = _integer(old.get('candidateCount'))
    old_match_count = _integer(old.get('matchCount'))
    if current is None:
        return {'status': 'part_missing', 'terminalWindowPresent': False}
    state = str(current.get('state') or '')
    completed_at = _integer(current.get('completedAt'))
    if state == 'failed':
        return {'status': 'failed', 'terminalWindowPresent': False, 'state': state}
    if state != 'ready' or completed_at <= old_completed_at or completed_at < queued_at:
        return {'status': 'pending', 'terminalWindowPresent': False, 'state': state}
    summary = current.get('analysisSummary')
    terminal_window_present = (
        _terminal_window_present(summary) if isinstance(summary, Mapping) else False
    )
    new_candidate_count = _integer(current.get('candidateCount'))
    new_match_count = _integer(current.get('matchCount'))
    comparison = {
        'status': '',
        'state': state,
        'completedAt': completed_at,
        'algorithmVersion': _integer(current.get('algorithmVersion')),
        'terminalWindowPresent': terminal_window_present,
        'candidateCount': new_candidate_count,
        'candidateCountDelta': new_candidate_count - old_candidate_count,
        'matchCount': new_match_count,
        'matchCountDelta': new_match_count - old_match_count,
        'matches': list(current.get('matches') or ()),
        'analysisSummary': summary,
    }
    if not terminal_window_present:
        comparison['status'] = 'terminal_window_missing'
    elif new_match_count > old_match_count:
        comparison['status'] = 'recovered_match'
    else:
        comparison['status'] = 'verified_without_result'
    return comparison


def _connect_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    connection = sqlite3.connect(
        'file:{}?mode=ro'.format(resolved), uri=True, timeout=30
    )
    connection.row_factory = sqlite3.Row
    return connection


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + '.', suffix='.tmp', dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf8') as temporary:
            json.dump(value, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write('\n')
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _load_state(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding='utf8'))
    if not isinstance(value, dict) or not isinstance(value.get('candidates'), list):
        raise ValueError('state file has an invalid structure')
    return value


def _candidate_sessions(
    candidates: Sequence[Mapping[str, Any]]
) -> Tuple[Dict[str, Any], ...]:
    sessions: Dict[int, Dict[str, Any]] = {}
    for candidate in candidates:
        session_id = _integer(candidate.get('sessionId'))
        if session_id <= 0:
            continue
        sessions.setdefault(
            session_id,
            {
                'sessionId': session_id,
                'title': str(candidate.get('title') or ''),
                'affectedPartIds': [],
            },
        )['affectedPartIds'].append(_integer(candidate.get('partId')))
    return tuple(sessions[key] for key in sorted(sessions))


def _print_candidate_summary(candidates: Sequence[Mapping[str, Any]]) -> None:
    sessions = _candidate_sessions(candidates)
    print('片尾边界问题：{} 个分批，{} 场直播'.format(len(candidates), len(sessions)))
    for candidate in candidates:
        print(
            '  session={} P{} part={}：旧段/窗口/候选/局数={}/{}/{}/{}，片尾差 {:.1f}s，{}'.format(
                candidate['sessionId'],
                candidate['partIndex'],
                candidate['partId'],
                candidate['old']['timelineSegmentCount'],
                candidate['old']['resultWindowCount'],
                candidate['old']['candidateCount'],
                candidate['old']['matchCount'],
                candidate['issue']['tailGapMs'] / 1_000,
                candidate['title'],
            )
        )


def _prepare(database: Path, state_path: Path, *, force: bool) -> int:
    if state_path.exists() and not force:
        raise ValueError('state file already exists; pass --force to replace it')
    with _connect_read_only(database) as connection:
        candidates = discover_tail_candidates(connection)
    now = int(time.time())
    state = {
        'version': 1,
        'issueCode': ISSUE_CODE,
        'createdAt': now,
        'database': str(database),
        'candidates': list(candidates),
        'sessions': list(_candidate_sessions(candidates)),
        'queue': {},
        'comparisons': {},
        'updatedAt': now,
    }
    _write_json(state_path, state)
    _print_candidate_summary(candidates)
    print('旧结果基线已保存：{}'.format(state_path))
    return 0


def _queue(
    state_path: Path,
    *,
    execute: bool,
    base_url: str,
    username: str,
    timeout_seconds: float,
    delay_seconds: float,
) -> int:
    state = _load_state(state_path)
    sessions = tuple(
        item for item in state.get('sessions', ()) if isinstance(item, Mapping)
    )
    print('准备将 {} 场直播加入整场重新分析。'.format(len(sessions)))
    if not execute:
        print('当前为预览模式；明确传入 --execute 才会入队。')
        return 0
    login = username.strip() or input('BLREC 管理员用户名：').strip()
    password = os.environ.get('BLREC_REANALYSIS_PASSWORD', '')
    if not password:
        password = getpass.getpass('BLREC 管理员密码：')
    if not login or not password:
        raise ValueError('管理员用户名和密码不能为空')
    client = RecoveryClient(base_url, timeout_seconds=timeout_seconds)
    client.login(login, password)
    queue_state = state.setdefault('queue', {})
    assert isinstance(queue_state, dict)
    failed = 0
    for position, session in enumerate(sessions, start=1):
        session_id = _integer(session.get('sessionId'))
        key = str(session_id)
        previous = queue_state.get(key)
        if isinstance(previous, Mapping) and previous.get('state') == 'accepted':
            continue
        candidate = RecoveryCandidate(
            kind='session',
            item_id=session_id,
            title=str(session.get('title') or ''),
            reasons=(ISSUE_CODE,),
        )
        attempted_at = int(time.time())
        try:
            client.queue(candidate)
        except RecoveryApiError as error:
            failed += 1
            queue_state[key] = {
                'state': 'failed',
                'attemptedAt': attempted_at,
                'error': str(error),
            }
            print(
                '[{}/{}] 入队失败 session={}：{}'.format(
                    position, len(sessions), session_id, error
                )
            )
            fatal = error.fatal
        else:
            queue_state[key] = {
                'state': 'accepted',
                'queuedAt': attempted_at,
                'error': None,
            }
            print(
                '[{}/{}] 已入队 session={}'.format(position, len(sessions), session_id)
            )
            fatal = False
        state['updatedAt'] = int(time.time())
        _write_json(state_path, state)
        if fatal:
            break
        if delay_seconds > 0 and position < len(sessions):
            time.sleep(delay_seconds)
    accepted = sum(
        isinstance(value, Mapping) and value.get('state') == 'accepted'
        for value in queue_state.values()
    )
    print(
        '入队结果：成功 {}，失败 {}，状态文件 {}'.format(accepted, failed, state_path)
    )
    return 0 if failed == 0 and accepted == len(sessions) else 1


def _refresh_comparisons(database: Path, state: Dict[str, Any]) -> Counter[str]:
    queue_state = state.get('queue')
    if not isinstance(queue_state, Mapping):
        queue_state = {}
    comparisons = state.setdefault('comparisons', {})
    if not isinstance(comparisons, dict):
        raise ValueError('state comparisons must be an object')
    with _connect_read_only(database) as connection:
        for candidate in state['candidates']:
            if not isinstance(candidate, Mapping):
                continue
            part_id = _integer(candidate.get('partId'))
            session_queue = queue_state.get(str(_integer(candidate.get('sessionId'))))
            queued_at = (
                _integer(session_queue.get('queuedAt'))
                if isinstance(session_queue, Mapping)
                else 0
            )
            if queued_at <= 0:
                comparison = {'status': 'not_queued', 'terminalWindowPresent': False}
            else:
                comparison = compare_tail_candidate(
                    candidate,
                    _current_part_snapshot(connection, part_id),
                    queued_at=queued_at,
                )
            comparison['checkedAt'] = int(time.time())
            comparisons[str(part_id)] = comparison
    state['updatedAt'] = int(time.time())
    return Counter(
        str(value.get('status') or 'unknown')
        for value in comparisons.values()
        if isinstance(value, Mapping)
    )


def _print_comparison_summary(counts: Mapping[str, int]) -> None:
    order = (
        'recovered_match',
        'verified_without_result',
        'terminal_window_missing',
        'pending',
        'failed',
        'part_missing',
        'not_queued',
        'unknown',
    )
    labels = {
        'recovered_match': '已找回比赛',
        'verified_without_result': '已扫片尾但没有新增比赛',
        'terminal_window_missing': '仍未生成片尾窗口',
        'pending': '重分析未完成',
        'failed': '重分析失败',
        'part_missing': '分批记录缺失',
        'not_queued': '尚未入队',
        'unknown': '未知',
    }
    print('前后对比：')
    for status in order:
        if counts.get(status):
            print('  {}：{}'.format(labels[status], counts[status]))


def _check(database: Path, state_path: Path) -> Counter[str]:
    state = _load_state(state_path)
    counts = _refresh_comparisons(database, state)
    _write_json(state_path, state)
    _print_comparison_summary(counts)
    print('详细新旧结果已写回：{}'.format(state_path))
    return counts


def _monitor(
    database: Path, state_path: Path, *, interval_seconds: float, timeout_seconds: float
) -> int:
    started = time.monotonic()
    while True:
        counts = _check(database, state_path)
        unfinished = sum(counts.get(status, 0) for status in ('pending', 'not_queued'))
        if unfinished == 0:
            return 0 if not counts.get('terminal_window_missing') else 1
        if timeout_seconds > 0 and time.monotonic() - started >= timeout_seconds:
            print('监控超时，仍有 {} 个分批未完成。'.format(unfinished))
            return 2
        time.sleep(interval_seconds)


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='片尾对局漏扫批量恢复及前后监控')
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--state', type=Path, default=DEFAULT_STATE)
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('preview')
    prepare = subparsers.add_parser('prepare')
    prepare.add_argument('--force', action='store_true')
    queue = subparsers.add_parser('queue')
    queue.add_argument('--execute', action='store_true')
    queue.add_argument(
        '--base-url',
        default=os.environ.get('BLREC_REANALYSIS_BASE_URL', 'http://127.0.0.1:2233'),
    )
    queue.add_argument(
        '--username', default=os.environ.get('BLREC_REANALYSIS_USERNAME', '')
    )
    queue.add_argument('--timeout-seconds', type=float, default=30.0)
    queue.add_argument('--delay-seconds', type=float, default=1.0)
    subparsers.add_parser('check')
    monitor = subparsers.add_parser('monitor')
    monitor.add_argument('--interval-seconds', type=float, default=30.0)
    monitor.add_argument('--timeout-seconds', type=float, default=0.0)
    args = parser.parse_args(argv)
    if getattr(args, 'timeout_seconds', 1) < 0:
        parser.error('--timeout-seconds must not be negative')
    if getattr(args, 'interval_seconds', 1) <= 0:
        parser.error('--interval-seconds must be positive')
    if getattr(args, 'delay_seconds', 0) < 0:
        parser.error('--delay-seconds must not be negative')
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    if args.command == 'preview':
        with _connect_read_only(args.database) as connection:
            _print_candidate_summary(discover_tail_candidates(connection))
        print('当前为只读预览，没有保存状态或修改数据库。')
        return 0
    if args.command == 'prepare':
        return _prepare(args.database, args.state, force=args.force)
    if args.command == 'queue':
        return _queue(
            args.state,
            execute=args.execute,
            base_url=args.base_url,
            username=args.username,
            timeout_seconds=args.timeout_seconds,
            delay_seconds=args.delay_seconds,
        )
    if args.command == 'check':
        _check(args.database, args.state)
        return 0
    if args.command == 'monitor':
        return _monitor(
            args.database,
            args.state,
            interval_seconds=args.interval_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    raise AssertionError('unreachable command')


if __name__ == '__main__':
    raise SystemExit(main())
