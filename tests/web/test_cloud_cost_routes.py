from datetime import datetime, timezone
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.cloud_cost.models import CloudCostSummary, CostTotals
from blrec.web.routers import cloud_cost


class FakeCloudCostService:
    def __init__(self) -> None:
        self.force_refresh = False
        self.billing_cycle = None

    async def summary(
        self, *, force_refresh: bool = False, billing_cycle: Optional[str] = None
    ) -> CloudCostSummary:
        self.force_refresh = force_refresh
        self.billing_cycle = billing_cycle
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

    response = TestClient(app).get(
        '/api/v1/cloud-cost/summary?refresh=true&billing_cycle=2026-07'
    )

    assert response.status_code == 200
    assert response.json()['billingCycle'] == '2026-08'
    assert response.json()['cacheSeconds'] == 600
    assert service.force_refresh is True
    assert service.billing_cycle == '2026-07'


def test_missing_service_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_cost, 'service', None)
    app = FastAPI()
    app.include_router(cloud_cost.router, prefix='/api/v1')

    response = TestClient(app).get('/api/v1/cloud-cost/summary')

    assert response.status_code == 503


def test_invalid_billing_cycle_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeCloudCostService()
    monkeypatch.setattr(cloud_cost, 'service', service)
    app = FastAPI()
    app.include_router(cloud_cost.router, prefix='/api/v1')

    response = TestClient(app).get(
        '/api/v1/cloud-cost/summary?billing_cycle=not-a-month'
    )

    assert response.status_code == 422
