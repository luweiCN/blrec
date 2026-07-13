from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.setting.models import LiveMonitorSettings, SettingsOut
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


def test_packaged_webapp_contains_live_monitor_ui() -> None:
    webapp_dir = Path(__file__).resolve().parents[2] / 'src/blrec/data/webapp'
    javascript = ''.join(path.read_text() for path in webapp_dir.glob('*.js'))

    assert 'liveMonitor' in javascript
    assert '/api/v1/live-status' in javascript
