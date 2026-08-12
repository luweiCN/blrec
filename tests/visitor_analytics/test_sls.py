from datetime import datetime, timedelta, timezone
from typing import List, Mapping, Sequence, Tuple

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.visitor_analytics.archive import (
    VisitorAnalyticsArchive,
    VisitorAnalyticsSynchronizer,
)
from blrec.visitor_analytics.sls import (
    VisitorAnalyticsConfig,
    VisitorAnalyticsQuery,
    VisitorAnalyticsService,
    _where_clause,
    build_sls_authorization,
)


class FakeSlsClient:
    def __init__(self) -> None:
        self.calls: List[Tuple[int, int, str, int]] = []

    async def query(
        self, from_time: int, to_time: int, query: str, *, line: int = 100
    ) -> Sequence[Mapping[str, object]]:
        self.calls.append((from_time, to_time, query, line))
        if 'AS page_views' in query:
            return [
                {'visitors': '7', 'events': '12', 'page_views': '10', 'heartbeats': '2'}
            ]
        if 'AS bucket' in query:
            return [
                {'bucket': '2026-08-12 10:00', 'visitors': '4', 'events': '6'},
                {'bucket': '2026-08-12 09:00', 'visitors': '3', 'events': '6'},
            ]
        if 'AS occurred_at' in query:
            return [
                {
                    'occurred_at': '2026-08-12 10:20:00',
                    'visitor': 'f71877fd-1665-4635-8f93-31558a3ad9ee',
                    'page': 'players',
                    'source': 'direct',
                    'device': 'mobile',
                    'browser': 'Safari',
                    'country': '中国',
                    'province': '北京',
                    'city': '北京',
                }
            ]
        return [{'dimension': '测试', 'visitors': '5', 'events': '8'}]


def analytics_query() -> VisitorAnalyticsQuery:
    return VisitorAnalyticsQuery(
        start_at=datetime(2026, 8, 12, 8, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 12, 10, tzinfo=timezone.utc),
    )


def configured() -> VisitorAnalyticsConfig:
    return VisitorAnalyticsConfig(
        access_key_id='test-id', access_key_secret='test-secret', cache_seconds=300
    )


def test_sls_signature_is_stable_and_excludes_late_x_log_date() -> None:
    authorization = build_sls_authorization(
        method='GET',
        resource='/logstores/example',
        parameters={'from': 1, 'query': '* | select count(*)', 'to': 2},
        headers={
            'Date': 'Wed, 12 Aug 2026 02:00:00 GMT',
            'Host': 'project.cn-beijing.log.aliyuncs.com',
            'x-log-apiversion': '0.6.0',
            'x-log-bodyrawsize': '0',
            'x-log-signaturemethod': 'hmac-sha1',
        },
        access_key_id='test-id',
        access_key_secret='test-secret',
    )

    assert authorization == 'LOG test-id:en5e/tMbR/ypOVzMG6xWIzzOwnA='


@pytest.mark.asyncio
async def test_summary_queries_distributions_masks_visitors_and_caches() -> None:
    client = FakeSlsClient()
    service = VisitorAnalyticsService(
        configured(),
        client,
        now=lambda: datetime(2026, 8, 12, 10, tzinfo=timezone.utc),
        clock=lambda: 100,
    )

    summary = await service.summary(analytics_query())

    assert summary.status == 'ready'
    assert summary.totals.visitors == 7
    assert summary.totals.page_views == 10
    assert summary.trend_granularity == 'hour'
    assert [item.bucket for item in summary.trend] == [
        '2026-08-12 09:00',
        '2026-08-12 10:00',
    ]
    assert summary.pages[0].visitors == 5
    assert summary.recent_visits[0].visitor.startswith('#')
    assert 'f71877fd' not in summary.recent_visits[0].visitor
    assert len(client.calls) == 11

    assert await service.summary(analytics_query()) is summary
    assert len(client.calls) == 11


@pytest.mark.asyncio
async def test_unconfigured_summary_never_queries_sls() -> None:
    client = FakeSlsClient()
    service = VisitorAnalyticsService(
        VisitorAnalyticsConfig(access_key_id=None, access_key_secret=None), client
    )

    summary = await service.summary(analytics_query())

    assert summary.status == 'not_configured'
    assert summary.configured is False
    assert client.calls == []


@pytest.mark.asyncio
async def test_query_range_cannot_exceed_log_retention() -> None:
    client = FakeSlsClient()
    service = VisitorAnalyticsService(configured(), client)
    query = analytics_query()

    with pytest.raises(ValueError, match='最近 7 天'):
        await service.summary(
            VisitorAnalyticsQuery(
                start_at=query.start_at,
                end_at=query.start_at + timedelta(days=7, seconds=1),
            )
        )


def test_filter_values_are_sql_escaped() -> None:
    query = VisitorAnalyticsQuery(
        start_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        province="北'京",
    ).normalized()

    where = _where_clause(query)

    assert "'北''京'" in where
    assert "'北'京'" not in where
    assert "event=detail" in where
    assert "kind=([^&]*)" in where


class FakeArchiveSource:
    def __init__(self, values: Sequence[Mapping[str, object]]) -> None:
        self.values = list(values)
        self.calls: List[Tuple[int, int, int, int]] = []

    async def archive_page(
        self, from_time: int, to_time: int, *, offset: int, limit: int
    ) -> Sequence[Mapping[str, object]]:
        self.calls.append((from_time, to_time, offset, limit))
        matching = [
            value
            for value in self.values
            if from_time <= int(value['occurred_at']) < to_time
        ]
        return matching[offset : offset + limit]


@pytest.mark.asyncio
async def test_archive_syncs_deduplicates_and_serves_long_ranges(tmp_path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        archive = VisitorAnalyticsArchive(database)
        first_at = int(datetime(2026, 8, 6, 8, tzinfo=timezone.utc).timestamp())
        second_at = int(datetime(2026, 8, 10, 8, tzinfo=timezone.utc).timestamp())
        source = FakeArchiveSource(
            [
                {
                    'request_id': 'request-1',
                    'occurred_at': first_at,
                    'event': 'pageview',
                    'visitor': 'visitor-a',
                    'page': 'players',
                    'source': 'direct',
                    'device': 'mobile',
                    'browser': 'Safari',
                    'country': '中国',
                    'province': '北京',
                    'city': '北京',
                    'provider': '联通',
                },
                {
                    'request_id': 'request-2',
                    'occurred_at': second_at,
                    'event': 'heartbeat',
                    'visitor': 'visitor-a',
                    'page': 'players',
                    'source': 'internal',
                    'device': 'mobile',
                    'browser': 'Safari',
                    'country': '中国',
                    'province': '北京',
                    'city': '北京',
                    'provider': '联通',
                },
                {
                    'request_id': 'request-3',
                    'occurred_at': second_at + 1,
                    'event': 'pageview',
                    'visitor': 'visitor-b',
                    'page': 'matches',
                    'source': 'direct',
                    'device': 'desktop',
                    'browser': 'Chrome',
                    'country': '中国',
                    'province': '上海',
                    'city': '上海',
                    'provider': '电信',
                },
            ]
        )
        current = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        synchronizer = VisitorAnalyticsSynchronizer(
            configured(),
            source,
            archive,
            now=lambda: current,
            ingestion_delay_seconds=0,
            window_seconds=8 * 86400,
            page_size=2,
        )

        assert await synchronizer.sync_once() == 3
        assert await synchronizer.sync_once() == 0

        client = FakeSlsClient()
        service = VisitorAnalyticsService(
            configured(), client, archive=archive, now=lambda: current
        )
        result = await service.summary(
            VisitorAnalyticsQuery(
                start_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                end_at=current,
                event='all',
            )
        )

        assert result.archive_enabled is True
        assert result.archive_initial_sync_complete is True
        assert result.totals.events == 3
        assert result.totals.visitors == 2
        assert result.totals.page_views == 2
        assert result.totals.heartbeats == 1
        assert [item.value for item in result.pages] == ['players', 'matches']
        assert client.calls == []
        stored_visitor = await database.scalar(
            'SELECT visitor_hash FROM visitor_analytics_events '
            'WHERE request_id=\'request-1\''
        )
        assert stored_visitor != 'visitor-a'
        assert len(str(stored_visitor)) == 64
    finally:
        await database.close()
