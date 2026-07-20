from dataclasses import dataclass
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.bili_upload.retention import RetentionStatus
from blrec.web import security
from blrec.web.routers import recording_retention


@dataclass
class FakeRetentionManager:
    status_calls: int = 0

    async def status(self) -> RetentionStatus:
        self.status_calls += 1
        return RetentionStatus(
            managed_video_bytes=480,
            capacity_bytes=500,
            remaining_bytes=20,
            warning_threshold_bytes=20,
            warning=True,
        )


@pytest.fixture(autouse=True)
def restore_state() -> Iterator[None]:
    old_manager = recording_retention.manager
    old_reason = recording_retention.unavailable_reason
    old_key = security.api_key
    yield
    recording_retention.manager = old_manager
    recording_retention.unavailable_reason = old_reason
    security.api_key = old_key


def test_retention_status_reports_capacity_warning() -> None:
    manager = FakeRetentionManager()
    recording_retention.manager = manager  # type: ignore[assignment]
    recording_retention.unavailable_reason = None
    security.api_key = 'test-key'
    api = FastAPI()
    api.include_router(recording_retention.router, prefix='/api/v1')

    with TestClient(api) as client:
        response = client.get(
            '/api/v1/recording-retention/status', headers={'x-api-key': 'test-key'}
        )

    assert response.status_code == 200
    assert response.json() == {
        'managedVideoBytes': 480,
        'capacityBytes': 500,
        'remainingBytes': 20,
        'warningThresholdBytes': 20,
        'warning': True,
    }
    assert manager.status_calls == 1
