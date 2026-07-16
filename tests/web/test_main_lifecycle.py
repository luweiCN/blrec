from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import blrec.web.main as web_main


@pytest.mark.asyncio
async def test_credential_callback_waits_until_application_is_started(
    monkeypatch,
) -> None:
    refresh = AsyncMock()
    monkeypatch.setattr(
        web_main, 'app', SimpleNamespace(refresh_managed_cookie=refresh)
    )
    monkeypatch.setattr(web_main, '_application_started', False, raising=False)

    await web_main._apply_primary_credential()

    refresh.assert_not_awaited()

    web_main._application_started = True
    await web_main._apply_primary_credential()

    refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_realtime_highlight_snapshot_uses_runtime_worker(monkeypatch) -> None:
    progress = AsyncMock(return_value=({'id': 3, 'state': 'processing'},))
    monkeypatch.setattr(
        web_main,
        '_bili_account_runtime',
        SimpleNamespace(highlight_worker=SimpleNamespace(progress=progress)),
    )

    assert await web_main._realtime_highlight_snapshot() == [
        {'id': 3, 'state': 'processing'}
    ]
    progress.assert_awaited_once_with()
