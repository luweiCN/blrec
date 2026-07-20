from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.web.routers import update


class _UpdateApplication:
    def __init__(self, value: Optional[str] = None, error: Optional[Exception] = None):
        self.value = value
        self.error = error
        self.calls = 0

    async def get_latest_version_string(self, _project_name: str) -> Optional[str]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


def _client(application: _UpdateApplication) -> TestClient:
    update.app = application  # type: ignore[assignment]
    api = FastAPI()
    api.include_router(update.router)
    return TestClient(api)


def test_update_route_uses_application_owned_client() -> None:
    application = _UpdateApplication('9.9.9')

    response = _client(application).get('/api/v1/update/version/latest')

    assert response.status_code == 200
    assert response.json() == '9.9.9'
    assert application.calls == 1


def test_update_route_returns_empty_value_without_exposing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = []

    class _Logger:
        @staticmethod
        def warning(message: str, *values: object) -> None:
            messages.append(message.format(*values))

    monkeypatch.setattr(update, 'logger', _Logger())
    application = _UpdateApplication(error=OSError('private response body'))

    response = _client(application).get('/api/v1/update/version/latest')

    assert response.status_code == 200
    assert response.json() == ''
    assert 'private response body' not in response.text
    assert 'private response body' not in '\n'.join(messages)
