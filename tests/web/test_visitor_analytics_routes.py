from datetime import datetime, timezone
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.visitor_analytics import VisitorAnalyticsQuery
from blrec.visitor_analytics.models import (
    VisitorAnalyticsFilters,
    VisitorAnalyticsSummary,
    VisitorAnalyticsTotals,
)
from blrec.web.routers import visitor_analytics


class FakeVisitorAnalyticsService:
    def __init__(self) -> None:
        self.query: Optional[VisitorAnalyticsQuery] = None
        self.force_refresh = False

    async def summary(
        self, query: VisitorAnalyticsQuery, *, force_refresh: bool = False
    ) -> VisitorAnalyticsSummary:
        self.query = query
        self.force_refresh = force_refresh
        return VisitorAnalyticsSummary(
            provider='aliyun-sls',
            status='ready',
            configured=True,
            generated_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
            retention_days=7,
            cache_seconds=300,
            filters=VisitorAnalyticsFilters(
                start_at=query.start_at,
                end_at=query.end_at,
                event=query.event,
                province=query.province,
            ),
            totals=VisitorAnalyticsTotals(visitors=3, page_views=9, events=9),
        )


def test_summary_forwards_filters_and_serializes_camel_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeVisitorAnalyticsService()
    monkeypatch.setattr(visitor_analytics, 'service', service)
    app = FastAPI()
    app.include_router(visitor_analytics.router, prefix='/api/v1')

    response = TestClient(app).get(
        '/api/v1/visitor-analytics/summary',
        params={
            'startAt': '2026-08-11T00:00:00+08:00',
            'endAt': '2026-08-12T00:00:00+08:00',
            'province': '北京',
            'refresh': 'true',
        },
    )

    assert response.status_code == 200
    assert response.json()['retentionDays'] == 7
    assert response.json()['totals']['pageViews'] == 9
    assert service.query is not None
    assert service.query.province == '北京'
    assert service.force_refresh is True


def test_missing_service_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visitor_analytics, 'service', None)
    app = FastAPI()
    app.include_router(visitor_analytics.router, prefix='/api/v1')

    response = TestClient(app).get('/api/v1/visitor-analytics/summary')

    assert response.status_code == 503
