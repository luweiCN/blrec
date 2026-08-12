#!/usr/bin/env python3
"""Queue the 2026-08-12 Vainglory recovery audit for reanalysis.

The bundled manifest is a read-only snapshot of the affected production rows.
Running this script without ``--execute`` only prints the plan. Execution talks
to the BLREC administrator API and persists one result after every request so a
failed or interrupted run can be resumed safely.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import http.cookiejar
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

DEFAULT_MANIFEST = Path(__file__).with_suffix('.jsonl')
DEFAULT_REPORT = Path('/cfg/vainglory-reanalysis-recovery-20260812-report.json')
MISSING_PAGES_REASON = 'missing_archive_pages'
FIVE_V_FIVE_REASON = 'five_v_five'
MODE_LINEUP_CONFLICT_REASON = 'mode_lineup_conflict'


@dataclass(frozen=True)
class RecoveryCandidate:
    kind: str
    item_id: int
    title: str
    reasons: Tuple[str, ...]

    @property
    def key(self) -> str:
        return '{}:{}'.format(self.kind, self.item_id)

    @property
    def api_path(self) -> str:
        if self.kind == 'session':
            return '/api/v1/vainglory/sessions/{}/scan'.format(self.item_id)
        if self.kind == 'archive_import':
            return '/api/v1/vainglory/archive-imports/{}/scan'.format(self.item_id)
        raise ValueError('unsupported candidate kind: {}'.format(self.kind))


@dataclass(frozen=True)
class RecoveryPlan:
    manifest_path: Path
    manifest_sha256: str
    metadata: Mapping[str, Any]
    candidates: Tuple[RecoveryCandidate, ...]
    missing_archive_count: int
    five_v_five_session_count: int
    mode_lineup_conflict_session_count: int = 0
    mode_lineup_conflict_match_count: int = 0

    @property
    def session_count(self) -> int:
        return sum(candidate.kind == 'session' for candidate in self.candidates)

    @property
    def import_count(self) -> int:
        return sum(candidate.kind == 'archive_import' for candidate in self.candidates)

    @property
    def overlap_count(self) -> int:
        return sum(
            MISSING_PAGES_REASON in candidate.reasons
            and FIVE_V_FIVE_REASON in candidate.reasons
            for candidate in self.candidates
        )


@dataclass(frozen=True)
class ExecutionResult:
    accepted: int
    failed: int
    pending: int
    fatal_error: Optional[str]


class RecoveryApiError(RuntimeError):
    def __init__(
        self, message: str, *, status_code: Optional[int] = None, fatal: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.fatal = fatal


def load_recovery_plan(manifest_path: Path) -> RecoveryPlan:
    raw = manifest_path.read_bytes()
    metadata: Optional[Mapping[str, Any]] = None
    imports: Dict[int, RecoveryCandidate] = {}
    sessions: Dict[int, RecoveryCandidate] = {}
    missing_archive_count = 0
    five_v_five_session_count = 0
    mode_lineup_conflict_session_count = 0
    mode_lineup_conflict_match_count = 0

    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(
                'manifest line {} is not valid JSON'.format(line_number)
            ) from error
        if not isinstance(record, dict):
            raise ValueError('manifest line {} must be an object'.format(line_number))
        record_type = record.get('type')
        if record_type == 'metadata':
            if metadata is not None or line_number != 1:
                raise ValueError('manifest metadata must be the first record')
            metadata = record
            continue
        if metadata is None:
            raise ValueError('manifest metadata is missing')
        if record_type == MISSING_PAGES_REASON:
            missing_archive_count += 1
            import_id = _positive_int(record, 'importId', line_number)
            session_id = _optional_positive_int(record, 'sessionId', line_number)
            title = _title(record, line_number)
            if int(record.get('missingPageCount', 0)) <= 0:
                raise ValueError(
                    'manifest line {} has no missing pages'.format(line_number)
                )
            if session_id is None:
                if import_id in imports:
                    raise ValueError(
                        'manifest contains duplicate import {}'.format(import_id)
                    )
                imports[import_id] = RecoveryCandidate(
                    kind='archive_import',
                    item_id=import_id,
                    title=title,
                    reasons=(MISSING_PAGES_REASON,),
                )
            else:
                _merge_session(
                    sessions,
                    session_id=session_id,
                    title=title,
                    reason=MISSING_PAGES_REASON,
                )
            continue
        if record_type == FIVE_V_FIVE_REASON:
            five_v_five_session_count += 1
            session_id = _positive_int(record, 'sessionId', line_number)
            _merge_session(
                sessions,
                session_id=session_id,
                title=_title(record, line_number),
                reason=FIVE_V_FIVE_REASON,
            )
            conflict_match_count = int(record.get('contradictoryMatchCount', 0))
            if conflict_match_count < 0:
                raise ValueError(
                    'manifest line {} has invalid contradictoryMatchCount'.format(
                        line_number
                    )
                )
            if conflict_match_count:
                mode_lineup_conflict_session_count += 1
                mode_lineup_conflict_match_count += conflict_match_count
                _merge_session(
                    sessions,
                    session_id=session_id,
                    title=_title(record, line_number),
                    reason=MODE_LINEUP_CONFLICT_REASON,
                )
            continue
        raise ValueError(
            'manifest line {} has unsupported type {!r}'.format(
                line_number, record_type
            )
        )

    if metadata is None:
        raise ValueError('manifest metadata is missing')
    _validate_manifest_counts(
        metadata,
        missing_archive_count=missing_archive_count,
        five_v_five_session_count=five_v_five_session_count,
        mode_lineup_conflict_session_count=mode_lineup_conflict_session_count,
        mode_lineup_conflict_match_count=mode_lineup_conflict_match_count,
    )
    return RecoveryPlan(
        manifest_path=manifest_path,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        metadata=metadata,
        candidates=tuple(imports.values()) + tuple(sessions.values()),
        missing_archive_count=missing_archive_count,
        five_v_five_session_count=five_v_five_session_count,
        mode_lineup_conflict_session_count=mode_lineup_conflict_session_count,
        mode_lineup_conflict_match_count=mode_lineup_conflict_match_count,
    )


def _positive_int(record: Mapping[str, Any], key: str, line_number: int) -> int:
    value = record.get(key)
    if value is None or isinstance(value, bool):
        raise ValueError('manifest line {} has invalid {}'.format(line_number, key))
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            'manifest line {} has invalid {}'.format(line_number, key)
        ) from error
    if result <= 0:
        raise ValueError('manifest line {} has invalid {}'.format(line_number, key))
    return result


def _optional_positive_int(
    record: Mapping[str, Any], key: str, line_number: int
) -> Optional[int]:
    if record.get(key) is None:
        return None
    return _positive_int(record, key, line_number)


def _title(record: Mapping[str, Any], line_number: int) -> str:
    title = str(record.get('title') or '').strip()
    if not title:
        raise ValueError('manifest line {} has no title'.format(line_number))
    return title


def _merge_session(
    sessions: Dict[int, RecoveryCandidate], *, session_id: int, title: str, reason: str
) -> None:
    existing = sessions.get(session_id)
    if existing is None:
        sessions[session_id] = RecoveryCandidate(
            kind='session', item_id=session_id, title=title, reasons=(reason,)
        )
        return
    if reason in existing.reasons:
        return
    sessions[session_id] = RecoveryCandidate(
        kind=existing.kind,
        item_id=existing.item_id,
        title=existing.title,
        reasons=existing.reasons + (reason,),
    )


def _validate_manifest_counts(
    metadata: Mapping[str, Any],
    *,
    missing_archive_count: int,
    five_v_five_session_count: int,
    mode_lineup_conflict_session_count: int,
    mode_lineup_conflict_match_count: int,
) -> None:
    missing = metadata.get('missingArchivePages')
    five = metadata.get('fiveVsFiveSessions')
    if not isinstance(missing, Mapping) or not isinstance(five, Mapping):
        raise ValueError('manifest metadata counts are missing')
    if int(missing.get('affectedArchives', -1)) != missing_archive_count:
        raise ValueError('manifest missing-page count does not match its metadata')
    if int(five.get('sessionCount', -1)) != five_v_five_session_count:
        raise ValueError('manifest 5V5 count does not match its metadata')
    if (
        int(five.get('contradictorySessionCount', -1))
        != mode_lineup_conflict_session_count
    ):
        raise ValueError('manifest conflict-session count does not match its metadata')
    if int(five.get('contradictoryMatchCount', -1)) != mode_lineup_conflict_match_count:
        raise ValueError('manifest conflict-match count does not match its metadata')


class RecoveryClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 30) -> None:
        parsed = urlsplit(base_url.strip())
        if (
            parsed.scheme not in ('http', 'https')
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError('base URL must be an HTTP(S) server URL')
        self._base_url = base_url.strip().rstrip('/')
        self._origin = '{}://{}'.format(parsed.scheme, parsed.netloc)
        self._timeout_seconds = timeout_seconds
        self._csrf_token = ''
        self._opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def login(self, username: str, password: str) -> None:
        response = self._request_json(
            'POST',
            '/api/v1/auth/login',
            payload={'username': username, 'password': password},
            csrf=False,
        )
        if not isinstance(response, Mapping):
            raise RecoveryApiError('登录响应不是 JSON 对象', fatal=True)
        token = str(response.get('csrfToken') or '')
        if not token:
            raise RecoveryApiError('登录响应缺少 CSRF token', fatal=True)
        self._csrf_token = token

    def queue(self, candidate: RecoveryCandidate) -> None:
        self._request_json('POST', candidate.api_path, payload=None, csrf=True)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]],
        csrf: bool,
    ) -> Any:
        body = b'' if payload is None else json.dumps(payload).encode('utf8')
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Origin': self._origin,
            'User-Agent': 'blrec-vainglory-reanalysis-recovery/20260812',
        }
        if csrf:
            headers['X-CSRF-Token'] = self._csrf_token
        request = Request(
            self._base_url + path, data=body, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except HTTPError as error:
            detail = _response_error_detail(error.read())
            raise RecoveryApiError(
                'HTTP {}: {}'.format(error.code, detail),
                status_code=error.code,
                fatal=error.code in (401, 403, 429) or error.code >= 500,
            ) from error
        except (OSError, URLError) as error:
            raise RecoveryApiError(
                '无法连接 BLREC API: {}'.format(error), fatal=True
            ) from error
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecoveryApiError('BLREC API 返回了无效 JSON', fatal=True) from error


def _response_error_detail(raw: bytes) -> str:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode('utf8', errors='replace')[:500] or '请求失败'
    if isinstance(payload, Mapping):
        return str(payload.get('detail') or payload)[:500]
    return str(payload)[:500]


def execute_plan(
    plan: RecoveryPlan,
    report_path: Path,
    queue: Callable[[RecoveryCandidate], None],
    *,
    delay_seconds: float,
    output: Callable[[str], None] = print,
    sleeper: Callable[[float], None] = time.sleep,
) -> ExecutionResult:
    report = _load_or_create_report(plan, report_path)
    items = report['items']
    assert isinstance(items, dict)
    fatal_error: Optional[str] = None

    for position, candidate in enumerate(plan.candidates, start=1):
        previous = items.get(candidate.key)
        if isinstance(previous, Mapping) and previous.get('state') == 'accepted':
            continue
        attempts = (
            int(previous.get('attemptCount', 0)) if isinstance(previous, Mapping) else 0
        )
        try:
            queue(candidate)
        except RecoveryApiError as error:
            items[candidate.key] = _report_item(
                candidate, state='failed', attempt_count=attempts + 1, error=str(error)
            )
            output(
                '[{}/{}] 失败 {}：{}'.format(
                    position, len(plan.candidates), candidate.key, error
                )
            )
            fatal = error.fatal
        except Exception as error:
            items[candidate.key] = _report_item(
                candidate,
                state='failed',
                attempt_count=attempts + 1,
                error='{}: {}'.format(type(error).__name__, error)[:500],
            )
            output(
                '[{}/{}] 失败 {}：{}'.format(
                    position, len(plan.candidates), candidate.key, error
                )
            )
            fatal = True
        else:
            items[candidate.key] = _report_item(
                candidate, state='accepted', attempt_count=attempts + 1, error=None
            )
            output(
                '[{}/{}] 已入队 {}'.format(
                    position, len(plan.candidates), candidate.key
                )
            )
            fatal = False
        report['updatedAt'] = int(time.time())
        _write_report(report_path, report)
        if fatal:
            fatal_error = str(items[candidate.key].get('error') or '请求失败')
            break
        if delay_seconds > 0 and position < len(plan.candidates):
            sleeper(delay_seconds)

    accepted = sum(
        isinstance(items.get(candidate.key), Mapping)
        and items[candidate.key].get('state') == 'accepted'
        for candidate in plan.candidates
    )
    failed = sum(
        isinstance(items.get(candidate.key), Mapping)
        and items[candidate.key].get('state') == 'failed'
        for candidate in plan.candidates
    )
    return ExecutionResult(
        accepted=accepted,
        failed=failed,
        pending=len(plan.candidates) - accepted - failed,
        fatal_error=fatal_error,
    )


def _report_item(
    candidate: RecoveryCandidate,
    *,
    state: str,
    attempt_count: int,
    error: Optional[str],
) -> Dict[str, Any]:
    return {
        'kind': candidate.kind,
        'itemId': candidate.item_id,
        'title': candidate.title,
        'reasons': list(candidate.reasons),
        'state': state,
        'attemptCount': attempt_count,
        'attemptedAt': int(time.time()),
        'error': error,
    }


def _load_or_create_report(plan: RecoveryPlan, report_path: Path) -> Dict[str, Any]:
    if report_path.exists():
        value = json.loads(report_path.read_text(encoding='utf8'))
        if not isinstance(value, dict) or not isinstance(value.get('items'), dict):
            raise ValueError('existing report has an invalid structure')
        if value.get('manifestSha256') != plan.manifest_sha256:
            raise ValueError('existing report belongs to a different manifest')
        return value
    now = int(time.time())
    return {
        'version': 1,
        'manifest': str(plan.manifest_path),
        'manifestSha256': plan.manifest_sha256,
        'createdAt': now,
        'updatedAt': now,
        'items': {},
    }


def _write_report(report_path: Path, report: Mapping[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=report_path.name + '.', suffix='.tmp', dir=str(report_path.parent)
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf8') as temporary:
            json.dump(report, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write('\n')
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, report_path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _print_summary(plan: RecoveryPlan) -> None:
    print('本次只读审计清单：')
    print('  缺失分 P 稿件：{} 个'.format(plan.missing_archive_count))
    print('  5V5 直播场次：{} 场'.format(plan.five_v_five_session_count))
    print(
        '  模式/阵容冲突：{} 场、{} 局（已与上述场次去重）'.format(
            plan.mode_lineup_conflict_session_count,
            plan.mode_lineup_conflict_match_count,
        )
    )
    print('  两类重叠场次：{} 场'.format(plan.overlap_count))
    print('  去重后整场重扫：{} 场'.format(plan.session_count))
    print('  尚未生成场次、先恢复稿件：{} 个'.format(plan.import_count))
    print('  合计 API 入队请求：{} 次'.format(len(plan.candidates)))


def _print_candidates(plan: RecoveryPlan) -> None:
    for candidate in plan.candidates:
        reasons = ','.join(candidate.reasons)
        title = ' '.join(candidate.title.splitlines())
        print('{} [{}] {}'.format(candidate.key, reasons, title))


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='批量恢复 2026-08-12 审计发现的缺分 P 和 5V5 对局分析任务'
    )
    parser.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument('--report', type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        '--base-url',
        default=os.environ.get('BLREC_REANALYSIS_BASE_URL', 'http://127.0.0.1:2233'),
    )
    parser.add_argument(
        '--username', default=os.environ.get('BLREC_REANALYSIS_USERNAME', '')
    )
    parser.add_argument('--delay-seconds', type=float, default=1.0)
    parser.add_argument('--timeout-seconds', type=float, default=30.0)
    parser.add_argument('--summary-only', action='store_true')
    parser.add_argument(
        '--execute',
        action='store_true',
        help='实际调用管理 API；不提供时只预览，不修改任何线上状态',
    )
    args = parser.parse_args(argv)
    if args.delay_seconds < 0:
        parser.error('--delay-seconds must not be negative')
    if args.timeout_seconds <= 0:
        parser.error('--timeout-seconds must be positive')
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    plan = load_recovery_plan(args.manifest)
    _print_summary(plan)
    if not args.execute:
        if not args.summary_only:
            _print_candidates(plan)
        print('当前为预览模式；没有发送请求。明确传入 --execute 才会入队。')
        return 0

    username = str(args.username).strip()
    if not username:
        username = input('BLREC 管理员用户名：').strip()
    password = os.environ.get('BLREC_REANALYSIS_PASSWORD', '')
    if not password:
        password = getpass.getpass('BLREC 管理员密码：')
    if not username or not password:
        raise ValueError('管理员用户名和密码不能为空')

    client = RecoveryClient(args.base_url, timeout_seconds=args.timeout_seconds)
    client.login(username, password)
    result = execute_plan(
        plan, args.report, client.queue, delay_seconds=args.delay_seconds
    )
    print(
        '执行结果：已接受 {}，失败 {}，尚未执行 {}；报告 {}'.format(
            result.accepted, result.failed, result.pending, args.report
        )
    )
    if result.fatal_error:
        print(
            '已因致命错误停止，可修复后使用同一命令续跑：{}'.format(result.fatal_error)
        )
    return 0 if result.failed == 0 and result.pending == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
