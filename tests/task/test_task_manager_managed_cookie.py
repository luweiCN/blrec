from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import blrec.application  # noqa: F401 - initializes the package import cycle
from blrec.task.task_manager import RecordTaskManager


class FakeTask:
    def __init__(self) -> None:
        self.user_agent = ''
        self.cookie = ''
        self.ready = True
        self.restart_danmaku_client = AsyncMock()


@pytest.mark.asyncio
async def test_managed_primary_cookie_overrides_manual_header_setting() -> None:
    provider = AsyncMock(return_value='SESSDATA=managed')
    manager = RecordTaskManager(
        object(), managed_cookie_provider=provider  # type: ignore[arg-type]
    )
    task = FakeTask()
    manager._tasks[100] = task  # type: ignore[assignment]
    settings = SimpleNamespace(user_agent='fixture-agent', cookie='manual-cookie')

    await manager.apply_task_header_settings(100, settings)  # type: ignore[arg-type]

    provider.assert_awaited_once_with('https://api.bilibili.com/')
    assert task.user_agent == 'fixture-agent'
    assert task.cookie == 'SESSDATA=managed'
    task.restart_danmaku_client.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_refresh_managed_cookie_updates_every_loaded_task() -> None:
    provider = AsyncMock(return_value='SESSDATA=first')
    manager = RecordTaskManager(
        object(), managed_cookie_provider=provider  # type: ignore[arg-type]
    )
    first = FakeTask()
    second = FakeTask()
    first.cookie = 'SESSDATA=old'
    second.cookie = 'SESSDATA=old'
    manager._tasks = {100: first, 200: second}  # type: ignore[assignment]

    await manager.refresh_managed_cookie()

    assert first.cookie == 'SESSDATA=first'
    assert second.cookie == 'SESSDATA=first'
    first.restart_danmaku_client.assert_awaited_once_with()
    second.restart_danmaku_client.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_manual_cookie_remains_available_when_account_manager_is_disabled() -> (
    None
):
    provider = AsyncMock(return_value=None)
    manager = RecordTaskManager(
        object(), managed_cookie_provider=provider  # type: ignore[arg-type]
    )
    task = FakeTask()
    manager._tasks[100] = task  # type: ignore[assignment]
    settings = SimpleNamespace(user_agent='fixture-agent', cookie='manual-cookie')

    await manager.apply_task_header_settings(100, settings)  # type: ignore[arg-type]

    assert task.cookie == 'manual-cookie'
