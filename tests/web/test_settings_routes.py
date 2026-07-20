from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.setting.models import LiveMonitorSettings, SettingsOut, TaskOptions
from blrec.setting.typing import KeySetOfSettings
from blrec.web.routers import settings


class SettingsApplication:
    def __init__(self) -> None:
        self.include: Optional[KeySetOfSettings] = None

    def get_settings(
        self, include: Optional[KeySetOfSettings], exclude: Optional[KeySetOfSettings]
    ) -> SettingsOut:
        self.include = include
        return SettingsOut(live_monitor=LiveMonitorSettings(batch_size=29))

    async def change_settings_with_operations(self, value):
        return (
            SettingsOut(live_monitor=LiveMonitorSettings(batch_size=17)),
            ('settings-operation',),
        )

    async def change_task_options_with_operations(self, room_id, value):
        return (
            TaskOptions.parse_obj({'recorder': {'readTimeout': 5}}),
            ('task-settings-operation',),
        )


def test_get_live_monitor_settings_by_alias() -> None:
    application = SettingsApplication()
    api = FastAPI()
    settings.app = application  # type: ignore[assignment]
    api.include_router(settings.router)

    with TestClient(api) as client:
        response = client.get('/api/v1/settings', params={'include': 'liveMonitor'})

    assert response.status_code == 200
    assert response.json()['liveMonitor']['batchSize'] == 29
    assert application.include == frozenset({'live_monitor'})


def test_get_operational_notifications_settings_by_alias() -> None:
    application = SettingsApplication()
    api = FastAPI()
    settings.app = application  # type: ignore[assignment]
    api.include_router(settings.router)

    with TestClient(api) as client:
        response = client.get(
            '/api/v1/settings', params={'include': 'operationalNotifications'}
        )

    assert response.status_code == 200
    assert application.include == frozenset({'operational_notifications'})


def test_packaged_webapp_contains_live_monitor_ui() -> None:
    webapp_dir = Path(__file__).resolve().parents[2] / 'src/blrec/data/webapp'
    javascript = ''.join(path.read_text() for path in webapp_dir.glob('*.js'))

    assert 'liveMonitor' in javascript
    assert '/api/v1/live-status' in javascript


def test_patch_keeps_body_and_exposes_apply_operation_header() -> None:
    application = SettingsApplication()
    api = FastAPI()
    settings.app = application  # type: ignore[assignment]
    api.include_router(settings.router)

    response = TestClient(api).patch(
        '/api/v1/settings', json={'liveMonitor': {'batchSize': 17}}
    )

    assert response.status_code == 200
    assert response.json()['liveMonitor']['batchSize'] == 17
    assert response.headers['X-BLREC-Operation-ID'] == 'settings-operation'


def test_task_patch_keeps_body_and_exposes_apply_operation_header() -> None:
    application = SettingsApplication()
    api = FastAPI()
    settings.app = application  # type: ignore[assignment]
    api.include_router(settings.router)

    response = TestClient(api).patch(
        '/api/v1/settings/tasks/100', json={'recorder': {'readTimeout': 5}}
    )

    assert response.status_code == 200
    assert response.json()['recorder']['readTimeout'] == 5
    assert response.headers['X-BLREC-Operation-ID'] == 'task-settings-operation'
