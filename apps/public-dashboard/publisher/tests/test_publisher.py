import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from blrec_dashboard_publisher.publisher import (
    DashboardPublishError,
    next_publication_at,
    publish_dashboard_once,
)
from blrec_dashboard_publisher.snapshot import SHANGHAI, DashboardExportResult


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        + '\n'
    ).encode('utf-8')


def manifest_bytes(
    snapshot_id: str,
    publication_date: str,
    source_last_match_id: int,
    snapshot_content: bytes,
) -> bytes:
    return json_bytes(
        {
            'schemaVersion': 1,
            'snapshotId': snapshot_id,
            'snapshotPath': 'snapshots/{}.json'.format(snapshot_id),
            'publicationDate': publication_date,
            'generatedAt': '{}T00:05:00Z'.format(publication_date),
            'sourceLastMatchId': source_last_match_id,
            'sha256': hashlib.sha256(snapshot_content).hexdigest(),
            'bytes': len(snapshot_content),
        }
    )


class FakeStore:
    def __init__(self, manifest=None, fail_manifest=False) -> None:
        self.manifest = manifest
        self.fail_manifest = fail_manifest
        self.events = []

    def load_manifest(self):
        self.events.append('load-manifest')
        return self.manifest

    def put_snapshot(self, path, content, sha256):
        self.events.append('put-snapshot:{}'.format(path))
        assert hashlib.sha256(content).hexdigest() == sha256
        return len(content)

    def put_manifest(self, content):
        self.events.append('put-manifest')
        if self.fail_manifest:
            raise OSError('temporary failure')
        self.manifest = content
        return len(content)


class Exporter:
    def __init__(self, snapshot_id='snapshot-new', source_last_match_id=20) -> None:
        self.snapshot_id = snapshot_id
        self.source_last_match_id = source_last_match_id
        self.calls = 0

    def __call__(self, database, output, *, now):
        self.calls += 1
        publication_date = now.astimezone(SHANGHAI).date().isoformat()
        snapshot = json_bytes(
            {
                'schemaVersion': 2,
                'snapshotId': self.snapshot_id,
                'publicationDate': publication_date,
                'sourceLastMatchId': self.source_last_match_id,
                'sourceMatchCount': 42,
            }
        )
        manifest = manifest_bytes(
            self.snapshot_id, publication_date, self.source_last_match_id, snapshot
        )
        snapshot_path = output / 'snapshots' / '{}.json'.format(self.snapshot_id)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_bytes(snapshot)
        manifest_path = output / 'manifest.json'
        manifest_path.write_bytes(manifest)
        return DashboardExportResult(
            manifest_path=manifest_path,
            snapshot_path=snapshot_path,
            manifest=json.loads(manifest),
            sha256=hashlib.sha256(snapshot).hexdigest(),
        )


def test_current_remote_manifest_skips_export_and_upload(tmp_path: Path) -> None:
    snapshot = json_bytes({'snapshotId': 'snapshot-current'})
    remote = manifest_bytes('snapshot-current', '2026-08-08', 30, snapshot)
    store = FakeStore(remote)
    exporter = Exporter()

    result = publish_dashboard_once(
        tmp_path / 'database.sqlite3',
        tmp_path / 'state',
        store,
        now=datetime(2026, 8, 8, 10, tzinfo=SHANGHAI),
        exporter=exporter,
    )

    assert result.published is False
    assert result.snapshot_id == 'snapshot-current'
    assert exporter.calls == 0
    assert store.events == ['load-manifest']


def test_force_republishes_a_current_remote_manifest(tmp_path: Path) -> None:
    snapshot = json_bytes({'snapshotId': 'snapshot-current'})
    remote = manifest_bytes('snapshot-current', '2026-08-08', 30, snapshot)
    store = FakeStore(remote)
    exporter = Exporter(snapshot_id='snapshot-recalculated', source_last_match_id=30)

    result = publish_dashboard_once(
        tmp_path / 'database.sqlite3',
        tmp_path / 'state',
        store,
        now=datetime(2026, 8, 8, 10, tzinfo=SHANGHAI),
        exporter=exporter,
        force=True,
    )

    assert result.published is True
    assert result.snapshot_id == 'snapshot-recalculated'
    assert exporter.calls == 1
    assert store.events == [
        'load-manifest',
        'put-snapshot:snapshots/snapshot-recalculated.json',
        'put-manifest',
        'load-manifest',
    ]


def test_snapshot_is_uploaded_before_manifest_commit(tmp_path: Path) -> None:
    old_snapshot = json_bytes({'snapshotId': 'snapshot-old'})
    store = FakeStore(manifest_bytes('snapshot-old', '2026-08-07', 10, old_snapshot))
    exporter = Exporter()

    result = publish_dashboard_once(
        tmp_path / 'database.sqlite3',
        tmp_path / 'state',
        store,
        now=datetime(2026, 8, 8, 0, 5, tzinfo=SHANGHAI),
        exporter=exporter,
    )

    assert result.published is True
    assert result.source_match_count == 42
    assert store.events == [
        'load-manifest',
        'put-snapshot:snapshots/snapshot-new.json',
        'put-manifest',
        'load-manifest',
    ]


def test_retry_reuses_pending_snapshot_after_manifest_failure(tmp_path: Path) -> None:
    old_snapshot = json_bytes({'snapshotId': 'snapshot-old'})
    remote = manifest_bytes('snapshot-old', '2026-08-07', 10, old_snapshot)
    store = FakeStore(remote, fail_manifest=True)
    exporter = Exporter()
    arguments = (tmp_path / 'database.sqlite3', tmp_path / 'state', store)
    now = datetime(2026, 8, 8, 0, 5, tzinfo=SHANGHAI)

    with pytest.raises(OSError, match='temporary failure'):
        publish_dashboard_once(*arguments, now=now, exporter=exporter)

    store.fail_manifest = False
    result = publish_dashboard_once(*arguments, now=now, exporter=exporter)

    assert result.published is True
    assert exporter.calls == 1


def test_source_progress_regression_stops_publication(tmp_path: Path) -> None:
    old_snapshot = json_bytes({'snapshotId': 'snapshot-old'})
    store = FakeStore(manifest_bytes('snapshot-old', '2026-08-07', 30, old_snapshot))

    with pytest.raises(DashboardPublishError, match='进度发生回退'):
        publish_dashboard_once(
            tmp_path / 'database.sqlite3',
            tmp_path / 'state',
            store,
            now=datetime(2026, 8, 8, 0, 5, tzinfo=SHANGHAI),
            exporter=Exporter(source_last_match_id=20),
        )

    assert 'put-manifest' not in store.events


def test_next_publication_rolls_forward_after_schedule() -> None:
    assert next_publication_at(
        datetime(2026, 8, 8, 0, 6, tzinfo=SHANGHAI), 0, 5
    ) == datetime(2026, 8, 9, 0, 5, tzinfo=SHANGHAI)
