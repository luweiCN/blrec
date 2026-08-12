from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.cloud_cost.models import CloudCostSummary, CostTotals
from blrec.web.routers import cloud_cost


class FakeCloudCostService:
    def __init__(self) -> None:
        self.force_refresh = False

    async def summary(self, *, force_refresh: bool = False) -> CloudCostSummary:
        self.force_refresh = force_refresh
        return CloudCostSummary(
            provider='aliyun',
            status='ready',
            configured=True,
            generated_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
            billing_cycle='2026-08',
            currency='CNY',
            cache_seconds=600,
            totals=CostTotals(),
        )


def test_get_summary_serializes_camel_case_and_forwards_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeCloudCostService()
    monkeypatch.setattr(cloud_cost, 'service', service)
    app = FastAPI()
    app.include_router(cloud_cost.router, prefix='/api/v1')

    response = TestClient(app).get('/api/v1/cloud-cost/summary?refresh=true')

    assert response.status_code == 200
    assert response.json()['billingCycle'] == '2026-08'
    assert response.json()['cacheSeconds'] == 600
    assert service.force_refresh is True


def test_missing_service_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_cost, 'service', None)
    app = FastAPI()
    app.include_router(cloud_cost.router, prefix='/api/v1')

    response = TestClient(app).get('/api/v1/cloud-cost/summary')

    assert response.status_code == 503
