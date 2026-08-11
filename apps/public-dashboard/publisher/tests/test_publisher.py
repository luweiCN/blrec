import hashlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from blrec_dashboard_publisher import publisher
from blrec_dashboard_publisher.publisher import (
    DashboardPublishError,
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
    content_revision: str = '',
) -> bytes:
    revision = content_revision or hashlib.sha256(snapshot_content).hexdigest()
    return json_bytes(
        {
            'schemaVersion': 1,
            'snapshotId': snapshot_id,
            'snapshotPath': 'snapshots/{}.json'.format(snapshot_id),
            'publicationDate': publication_date,
            'generatedAt': '{}T00:05:00Z'.format(publication_date),
            'sourceLastMatchId': source_last_match_id,
            'contentRevision': revision,
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
        self,
        snapshot_id='snapshot-new',
        source_last_match_id=20,
        players=None,
        content_revision=None,
    ) -> None:
        self.snapshot_id = snapshot_id
        self.source_last_match_id = source_last_match_id
        self.players = players or []
        self.content_revision = (
            content_revision
            or hashlib.sha256(
                json_bytes(
                    {'sourceLastMatchId': source_last_match_id, 'players': self.players}
                )
            ).hexdigest()
        )
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
                'contentRevision': self.content_revision,
                'standings': {'2026-summer': {'players': self.players, 'heroes': []}},
            }
        )
        manifest = manifest_bytes(
            self.snapshot_id,
            publication_date,
            self.source_last_match_id,
            snapshot,
            self.content_revision,
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


def test_unchanged_content_is_checked_then_skips_upload(tmp_path: Path) -> None:
    snapshot = json_bytes({'snapshotId': 'snapshot-current'})
    revision = 'a' * 64
    remote = manifest_bytes('snapshot-current', '2026-08-08', 30, snapshot, revision)
    store = FakeStore(remote)
    exporter = Exporter(source_last_match_id=30, content_revision=revision)

    result = publish_dashboard_once(
        tmp_path / 'database.sqlite3',
        tmp_path / 'state',
        store,
        now=datetime(2026, 8, 8, 10, tzinfo=SHANGHAI),
        exporter=exporter,
    )

    assert result.published is False
    assert result.snapshot_id == 'snapshot-current'
    assert result.source_match_count == 42
    assert exporter.calls == 1
    assert store.events == ['load-manifest']


def test_changed_content_is_published_again_on_the_same_day(tmp_path: Path) -> None:
    snapshot = json_bytes({'snapshotId': 'snapshot-current'})
    remote = manifest_bytes('snapshot-current', '2026-08-08', 30, snapshot, 'a' * 64)
    store = FakeStore(remote)
    exporter = Exporter(
        snapshot_id='snapshot-updated',
        source_last_match_id=31,
        content_revision='b' * 64,
    )

    result = publish_dashboard_once(
        tmp_path / 'database.sqlite3',
        tmp_path / 'state',
        store,
        now=datetime(2026, 8, 8, 10, 15, tzinfo=SHANGHAI),
        exporter=exporter,
    )

    assert result.published is True
    assert result.snapshot_id == 'snapshot-updated'
    assert exporter.calls == 1


def test_force_republishes_a_current_remote_manifest(tmp_path: Path) -> None:
    snapshot = json_bytes({'snapshotId': 'snapshot-current'})
    revision = 'a' * 64
    remote = manifest_bytes('snapshot-current', '2026-08-08', 30, snapshot, revision)
    store = FakeStore(remote)
    exporter = Exporter(
        snapshot_id='snapshot-recalculated',
        source_last_match_id=30,
        content_revision=revision,
    )

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


def test_worker_debounces_and_syncs_when_source_revision_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = publisher._WorkerConfiguration(
        database=tmp_path / 'database.sqlite3',
        settings=tmp_path / 'settings.toml',
        state=tmp_path / 'state',
        endpoint='https://example.invalid',
        bucket='bucket',
        prefix='data',
        watch_seconds=1,
        debounce_seconds=2,
        reconcile_seconds=24 * 60 * 60,
        retry_seconds=60,
    )
    delays = []
    publications = []
    revisions = iter((10, 10, 11, 11, 11))
    monkeypatch.setattr(
        publisher, '_read_source_revision', lambda _database: next(revisions)
    )
    monkeypatch.setattr(
        publisher, '_publish', lambda configuration, now: publications.append(now)
    )

    def stop_after_first_delay(seconds: int) -> None:
        delays.append(seconds)
        if delays == [1, 2, 1]:
            raise KeyboardInterrupt('stop worker loop')

    monkeypatch.setattr(publisher.time, 'sleep', stop_after_first_delay)

    with pytest.raises(KeyboardInterrupt, match='stop worker loop'):
        publisher._worker_loop(configuration)

    assert len(publications) == 2
    assert delays == [1, 2, 1]


def test_worker_can_sync_api_without_republishing_static_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection = SimpleNamespace(
        interface_name=None, source_address=None, role='system-default'
    )

    class Store:
        def __init__(self) -> None:
            self.selection = selection
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class ApiClient:
        def __init__(self) -> None:
            self.selection = selection
            self.closed = False

        def post_batch(self, _key: str, _content: bytes):
            return {'status': 'applied'}

        def close(self) -> None:
            self.closed = True

    store = Store()
    api_client = ApiClient()
    monkeypatch.setenv('ALIBABA_CLOUD_ACCESS_KEY_ID', 'id')
    monkeypatch.setenv('ALIBABA_CLOUD_ACCESS_KEY_SECRET', 'secret')
    monkeypatch.setenv('DASHBOARD_API_TOKEN', 'token')
    monkeypatch.setattr(publisher, 'load_network_settings', lambda _path: object())
    monkeypatch.setattr(publisher, 'NetworkRouteManager', lambda _loader: object())
    monkeypatch.setattr(publisher, 'OssDashboardStore', lambda **_kwargs: store)
    monkeypatch.setattr(publisher, 'DashboardApiClient', lambda **_kwargs: api_client)
    monkeypatch.setattr(
        publisher,
        'publish_dashboard_once',
        lambda *_args, **_kwargs: pytest.fail('static JSON should stay unchanged'),
    )
    monkeypatch.setattr(
        publisher,
        'sync_dashboard_api_once',
        lambda **_kwargs: SimpleNamespace(
            synced=True,
            batch_id='batch-1',
            match_count=1,
            removed_match_count=0,
            uploaded_image_bytes=0,
        ),
    )
    configuration = publisher._WorkerConfiguration(
        database=tmp_path / 'database.sqlite3',
        settings=tmp_path / 'settings.toml',
        state=tmp_path / 'state',
        endpoint='https://example.invalid',
        bucket='bucket',
        prefix='data',
        watch_seconds=1,
        debounce_seconds=2,
        reconcile_seconds=24 * 60 * 60,
        retry_seconds=60,
        api_url='https://vg-api.example',
        publish_static_data=False,
    )

    result = publisher._publish(
        configuration, datetime(2026, 8, 11, 10, 15, tzinfo=SHANGHAI)
    )

    assert result is None
    assert store.closed is True
    assert api_client.closed is True
