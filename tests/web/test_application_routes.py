from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.web.routers import application


class _Application:
    async def submit_restart(self) -> SimpleNamespace:
        return SimpleNamespace(id='restart-operation')


def test_restart_returns_durable_operation_without_waiting() -> None:
    application.app = _Application()  # type: ignore[assignment]
    api = FastAPI()
    api.include_router(application.router)

    response = TestClient(api).post('/api/v1/app/restart')

    assert response.status_code == 202
    assert response.headers['X-BLREC-Operation-ID'] == 'restart-operation'
    assert response.json() == {
        'code': 0,
        'message': 'Application restart accepted',
        'data': None,
    }
