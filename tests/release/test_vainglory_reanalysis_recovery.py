import json
from pathlib import Path

from scripts.vainglory_reanalysis_recovery_20260812 import (
    FIVE_V_FIVE_REASON,
    MISSING_PAGES_REASON,
    RecoveryApiError,
    RecoveryCandidate,
    RecoveryPlan,
    execute_plan,
    load_recovery_plan,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / 'scripts/vainglory_reanalysis_recovery_20260812.jsonl'


def test_production_audit_manifest_builds_the_expected_deduplicated_plan() -> None:
    plan = load_recovery_plan(MANIFEST)

    assert plan.missing_archive_count == 131
    assert plan.five_v_five_session_count == 165
    assert plan.import_count == 44
    assert plan.session_count == 231
    assert plan.overlap_count == 21
    assert len(plan.candidates) == 275
    assert len({candidate.key for candidate in plan.candidates}) == 275
    assert plan.metadata['missingArchivePages']['missingPages'] == 238


def test_failed_items_are_retried_without_requeueing_accepted_items(
    tmp_path: Path,
) -> None:
    candidates = (
        RecoveryCandidate(
            kind='archive_import',
            item_id=1,
            title='缺少分 P',
            reasons=(MISSING_PAGES_REASON,),
        ),
        RecoveryCandidate(
            kind='session', item_id=2, title='5V5 直播', reasons=(FIVE_V_FIVE_REASON,)
        ),
        RecoveryCandidate(
            kind='session',
            item_id=3,
            title='重叠直播',
            reasons=(MISSING_PAGES_REASON, FIVE_V_FIVE_REASON),
        ),
    )
    plan = RecoveryPlan(
        manifest_path=tmp_path / 'manifest.jsonl',
        manifest_sha256='audit-sha256',
        metadata={},
        candidates=candidates,
        missing_archive_count=2,
        five_v_five_session_count=2,
    )
    report_path = tmp_path / 'report.json'
    first_calls = []

    def first_queue(candidate: RecoveryCandidate) -> None:
        first_calls.append(candidate.key)
        if candidate.key == 'session:2':
            raise RecoveryApiError('任务正在运行', status_code=409)

    first = execute_plan(
        plan, report_path, first_queue, delay_seconds=0, output=lambda _message: None
    )

    assert first_calls == ['archive_import:1', 'session:2', 'session:3']
    assert first.accepted == 2
    assert first.failed == 1
    assert first.pending == 0

    second_calls = []
    second = execute_plan(
        plan,
        report_path,
        lambda candidate: second_calls.append(candidate.key),
        delay_seconds=0,
        output=lambda _message: None,
    )

    assert second_calls == ['session:2']
    assert second.accepted == 3
    assert second.failed == 0
    assert second.pending == 0
    report = json.loads(report_path.read_text(encoding='utf8'))
    assert report['items']['archive_import:1']['attemptCount'] == 1
    assert report['items']['session:2']['attemptCount'] == 2
    assert report['items']['session:3']['attemptCount'] == 1
