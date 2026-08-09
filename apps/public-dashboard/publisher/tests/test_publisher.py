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
    def __init__(
        self, manifest=None, trends=None, fail_manifest=False, fail_trends=False
    ) -> None:
        self.manifest = manifest
        self.trends = trends
        self.fail_manifest = fail_manifest
        self.fail_trends = fail_trends
        self.events = []

    def load_manifest(self):
        self.events.append('load-manifest')
        return self.manifest

    def put_snapshot(self, path, content, sha256):
        self.events.append('put-snapshot:{}'.format(path))
        assert hashlib.sha256(content).hexdigest() == sha256
        return len(content)

    def load_trends(self):
        self.events.append('load-trends')
        return self.trends

    def put_trends(self, content):
        self.events.append('put-trends')
        if self.fail_trends:
            raise OSError('trend upload failure')
        self.trends = content
        return len(content)

    def put_manifest(self, content):
        self.events.append('put-manifest')
        if self.fail_manifest:
            raise OSError('temporary failure')
        self.manifest = content
        return len(content)


class Exporter:
    def __init__(
        self, snapshot_id='snapshot-new', source_last_match_id=20, players=None
    ) -> None:
        self.snapshot_id = snapshot_id
        self.source_last_match_id = source_last_match_id
        self.players = players or []
        self.calls = 0

    def __call__(self, database, output, *, now):
        self.calls += 1
        publication_date = now.astimezone(SHANGHAI).date().isoformat()
        snapshot = json_bytes(
            {
                'schemaVersion': 2,
                'snapshotId': self.snapshot_id,
                'publicationDate': publication_date,
                'generatedAt': now.isoformat(),
                'sourceLastMatchId': self.source_last_match_id,
                'sourceMatchCount': 42,
                'standings': {'2026-summer': {'players': self.players, 'heroes': []}},
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
        'load-trends',
        'put-snapshot:snapshots/snapshot-recalculated.json',
        'put-trends',
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
        'load-trends',
        'put-snapshot:snapshots/snapshot-new.json',
        'put-trends',
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
    trends = json.loads(store.trends)
    assert len(trends['publications']) == 1


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


def performance(score, matches, wins):
    return {'ratingScore': score, 'matches': matches, 'wins': wins}


def trend_player(player_id, score, matches, wins):
    return {
        'id': player_id,
        'modes': {
            'all': performance(score, matches, wins),
            '3v3': performance(score, matches, wins),
            'brawl': performance(None, 0, 0),
            '5v5': performance(None, 0, 0),
        },
    }


def test_publication_records_ranked_scores_for_each_season_and_mode(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    exporter = Exporter(
        players=[
            trend_player(7, 620, 20, 12),
            trend_player(3, 620, 30, 17),
            trend_player(9, 590, 50, 35),
        ]
    )

    publish_dashboard_once(
        tmp_path / 'database.sqlite3',
        tmp_path / 'state',
        store,
        now=datetime(2026, 8, 8, 0, 5, tzinfo=SHANGHAI),
        exporter=exporter,
    )

    trends = json.loads(store.trends)
    assert trends['schemaVersion'] == 1
    assert len(trends['publications']) == 1
    publication = trends['publications'][0]
    assert publication['snapshotId'] == 'snapshot-new'
    assert publication['standings']['2026-summer']['3v3'] == [
        {'playerId': 3, 'rank': 1, 'ratingScore': 620},
        {'playerId': 7, 'rank': 2, 'ratingScore': 620},
        {'playerId': 9, 'rank': 3, 'ratingScore': 590},
    ]
    assert publication['standings']['2026-summer']['brawl'] == []


def test_force_publication_replaces_the_same_day_trend_point(tmp_path: Path) -> None:
    store = FakeStore()
    arguments = (tmp_path / 'database.sqlite3', tmp_path / 'state', store)
    now = datetime(2026, 8, 8, 0, 5, tzinfo=SHANGHAI)

    publish_dashboard_once(
        *arguments, now=now, exporter=Exporter(players=[trend_player(7, 610, 20, 12)])
    )
    publish_dashboard_once(
        *arguments,
        now=now,
        exporter=Exporter(
            snapshot_id='snapshot-recalculated', players=[trend_player(7, 625, 21, 13)]
        ),
        force=True,
    )

    trends = json.loads(store.trends)
    assert len(trends['publications']) == 1
    publication = trends['publications'][0]
    assert publication['snapshotId'] == 'snapshot-recalculated'
    assert publication['standings']['2026-summer']['3v3'][0]['ratingScore'] == 625


def test_trend_history_keeps_the_latest_thirty_publications(tmp_path: Path) -> None:
    store = FakeStore()
    arguments = (tmp_path / 'database.sqlite3', tmp_path / 'state', store)

    for day in range(1, 32):
        publish_dashboard_once(
            *arguments,
            now=datetime(2026, 7, day, 0, 5, tzinfo=SHANGHAI),
            exporter=Exporter(
                snapshot_id='snapshot-{:02d}'.format(day),
                source_last_match_id=day,
                players=[trend_player(7, 600 + day, day, day // 2)],
            ),
        )

    trends = json.loads(store.trends)
    assert len(trends['publications']) == 30
    assert trends['publications'][0]['publicationDate'] == '2026-07-02'
    assert trends['publications'][-1]['publicationDate'] == '2026-07-31'


def test_trends_are_committed_before_the_manifest(tmp_path: Path) -> None:
    store = FakeStore(fail_trends=True)

    with pytest.raises(OSError, match='trend upload failure'):
        publish_dashboard_once(
            tmp_path / 'database.sqlite3',
            tmp_path / 'state',
            store,
            now=datetime(2026, 8, 8, 0, 5, tzinfo=SHANGHAI),
            exporter=Exporter(),
        )

    assert store.manifest is None
    assert 'put-manifest' not in store.events


def test_next_publication_rolls_forward_after_schedule() -> None:
    assert next_publication_at(
        datetime(2026, 8, 8, 0, 6, tzinfo=SHANGHAI), 0, 5
    ) == datetime(2026, 8, 9, 0, 5, tzinfo=SHANGHAI)
