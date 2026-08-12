import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pytest

SCRIPT_PATH = (
    Path(__file__).parents[2]
    / 'public-dashboard'
    / 'deploy'
    / 'aliyun'
    / 'publish_site_stats.py'
)
SPEC = importlib.util.spec_from_file_location('site_stats_publisher', SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
site_stats_publisher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = site_stats_publisher
SPEC.loader.exec_module(site_stats_publisher)


SHANGHAI = ZoneInfo('Asia/Shanghai')


def test_sls_parameter_patterns_accept_legacy_events_and_ignore_details() -> None:
    visitor = 'f71877fd-1665-4635-8f93-31558a3ad9ee'

    assert re.search(
        site_stats_publisher.PAGEVIEW_PARAM_PATTERN,
        '?event=pageview&visitor={}'.format(visitor),
    )
    assert re.search(
        site_stats_publisher.ACTIVE_PARAM_PATTERN,
        '?event=heartbeat&visitor={}&page=players&source=direct&device=mobile'.format(
            visitor
        ),
    )
    assert re.search(
        site_stats_publisher.VISITOR_PARAM_PATTERN,
        '?event=pageview&visitor={}&page=overview'.format(visitor),
    )
    detail = '?event=detail&kind=pageview&visitor={}&page=overview'.format(visitor)
    assert not re.search(site_stats_publisher.PAGEVIEW_PARAM_PATTERN, detail)
    assert not re.search(site_stats_publisher.ACTIVE_PARAM_PATTERN, detail)


class FakeAnalytics:
    def __init__(self, counts, active: int) -> None:
        self.counts = list(counts)
        self.active = active
        self.daily_queries = []

    def daily_counts(self, from_time: int, to_time: int):
        self.daily_queries.append((from_time, to_time))
        return self.counts.pop(0)

    def active_visitors(self, from_time: int, to_time: int) -> int:
        return self.active


class FakeStore:
    def __init__(self, history: Optional[bytes] = None) -> None:
        self.history = history
        self.public_stats: Optional[bytes] = None

    def load_history(self) -> Optional[bytes]:
        return self.history

    def publish_history(self, contents: bytes) -> None:
        self.history = contents

    def publish_public_stats(self, contents: bytes) -> None:
        self.public_stats = contents


def test_refresh_publishes_four_public_counts_and_persists_daily_history() -> None:
    analytics = FakeAnalytics(
        [site_stats_publisher.Counts(page_views=63, visitors=18)], active=4
    )
    store = FakeStore()
    started_at = datetime(2026, 8, 4, tzinfo=SHANGHAI)
    now = datetime(2026, 8, 4, 10, 5, tzinfo=SHANGHAI)

    stats = site_stats_publisher.refresh_site_stats(analytics, store, started_at, now)

    assert stats == {
        'schemaVersion': 1,
        'generatedAt': '2026-08-04T10:05:00+08:00',
        'timezone': 'Asia/Shanghai',
        'trackingStartedAt': '2026-08-04T00:00:00+08:00',
        'activeWindowMinutes': 5,
        'today': {'date': '2026-08-04', 'visitors': 18, 'pageViews': 63},
        'activeVisitors': 4,
        'totalPageViews': 63,
    }
    assert json.loads(store.history)['days']['2026-08-04'] == {
        'pageViews': 63,
        'visitors': 18,
    }
    assert json.loads(store.public_stats) == stats


def test_refresh_recalculates_the_retained_week_after_a_restart() -> None:
    started_at = datetime(2026, 8, 1, tzinfo=SHANGHAI)
    existing = site_stats_publisher.StatsHistory(
        tracking_started_at=started_at,
        days={'2026-08-03': site_stats_publisher.Counts(9, 3)},
    )
    store = FakeStore(site_stats_publisher.serialize_history(existing))
    daily_counts = [
        site_stats_publisher.Counts(page_views=index * 10, visitors=index)
        for index in range(1, 8)
    ]
    analytics = FakeAnalytics(daily_counts, active=2)

    stats = site_stats_publisher.refresh_site_stats(
        analytics, store, started_at, datetime(2026, 8, 10, 12, tzinfo=SHANGHAI)
    )

    assert len(analytics.daily_queries) == 7
    assert stats['today']['pageViews'] == 70
    assert stats['totalPageViews'] == 9 + sum(index * 10 for index in range(1, 8))


def test_refresh_refuses_to_replace_history_with_lower_counts() -> None:
    started_at = datetime(2026, 8, 4, tzinfo=SHANGHAI)
    existing = site_stats_publisher.StatsHistory(
        tracking_started_at=started_at,
        days={'2026-08-04': site_stats_publisher.Counts(20, 8)},
    )
    store = FakeStore(site_stats_publisher.serialize_history(existing))
    analytics = FakeAnalytics(
        [site_stats_publisher.Counts(page_views=19, visitors=8)], active=1
    )

    with pytest.raises(site_stats_publisher.SiteStatsError, match='停止发布'):
        site_stats_publisher.refresh_site_stats(
            analytics, store, started_at, datetime(2026, 8, 4, 12, tzinfo=SHANGHAI)
        )

    assert store.public_stats is None
