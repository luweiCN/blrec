from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.application import CoordinatorMetrics
from blrec.bili.live_status import BreakerState
from blrec.web import security
from blrec.web.routers import live_status


class StatusApplication:
    def __init__(self) -> None:
        self.resumed = False

    def get_live_status_metrics(self) -> CoordinatorMetrics:
        return CoordinatorMetrics(
            mode='batch',
            interval_seconds=30,
            batch_size=29,
            registered_rooms=58,
            active_websockets=0,
            last_success_at=100.0,
            snapshot_max_age_seconds=2.0,
            missing_results=1,
            fallback_requests=3,
            breaker_state=BreakerState.CLOSED,
            breaker_reason=None,
        )

    def resume_live_status_coordinator(self) -> None:
        self.resumed = True


@pytest.fixture
def client() -> Iterator[TestClient]:
    application = StatusApplication()
    api = FastAPI()
    api.dependency_overrides[live_status.get_application] = lambda: application
    api.include_router(live_status.router, prefix='/api/v1')
    security.api_key = 'test-api-key'
    security.whitelist.clear()
    security.blacklist.clear()
    security.attempting_clients.clear()
    with TestClient(api) as test_client:
        yield test_client
    security.api_key = ''
    security.whitelist.clear()
    security.blacklist.clear()
    security.attempting_clients.clear()


def test_get_live_status(client: TestClient) -> None:
    response = client.get('/api/v1/live-status')
    assert response.status_code == 200
    assert response.json()['activeWebsockets'] == 0
    assert response.json()['registeredRooms'] == 58


def test_resume_live_status_requires_api_key(client: TestClient) -> None:
    response = client.post('/api/v1/live-status/resume')
    assert response.status_code == 401


def test_resume_live_status_accepts_existing_api_key(client: TestClient) -> None:
    response = client.post(
        '/api/v1/live-status/resume', headers={'x-api-key': 'test-api-key'}
    )
    assert response.status_code == 204
