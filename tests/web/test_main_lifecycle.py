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
