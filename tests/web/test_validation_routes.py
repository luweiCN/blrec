from typing import Any, Mapping

from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.web.routers import validation


class _Application:
    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = result
        self.cookies = []

    async def validate_bili_cookie(self, cookie: str) -> Mapping[str, Any]:
        self.cookies.append(cookie)
        return self.result

    async def validate_directory(self, path: str):
        if path == 'saturated':
            from blrec.setting.file_work import SettingsFileWorkSaturated

            raise SettingsFileWorkSaturated('full')
        return (0, 'ok')


def test_cookie_validation_uses_application_owned_transport() -> None:
    application = _Application({'code': 0, 'message': 'ok', 'data': {'mid': 1}})
    validation.app = application  # type: ignore[assignment]
    api = FastAPI()
    api.include_router(validation.router)

    response = TestClient(api).post(
        '/api/v1/validation/cookie', json={'cookie': 'SESSDATA=route-secret'}
    )

    assert response.status_code == 200
    assert response.json() == {'code': 0, 'message': 'ok', 'data': {'mid': 1}}
    assert application.cookies == ['SESSDATA=route-secret']


def test_directory_validation_maps_saturation_to_retryable_503() -> None:
    application = _Application({'code': 0, 'message': 'ok', 'data': {}})
    validation.app = application  # type: ignore[assignment]
    api = FastAPI()
    api.include_router(validation.router)

    response = TestClient(api).post(
        '/api/v1/validation/dir', json={'path': 'saturated'}
    )

    assert response.status_code == 503
    assert response.headers['Retry-After'] == '1'
