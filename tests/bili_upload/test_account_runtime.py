import asyncio
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import AsyncMock

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.journal import RecordingJournalBridge
from blrec.bili_upload.runtime import BiliAccountRuntime
from blrec.setting.models import BiliUploadSettings


class FakeClock:
    def __init__(self, value: int = 1_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return float(self.value)


def confirmed_response() -> Mapping[str, Any]:
    return {
        'code': 0,
        'data': {
            'token_info': {
                'access_token': 'access-new',
                'refresh_token': 'refresh-new',
                'mid': 42,
                'expires_in': 180 * 24 * 3600,
            },
            'cookie_info': {
                'cookies': [
                    {'name': 'DedeUserID', 'value': '42'},
                    {'name': 'SESSDATA', 'value': 'sess-secret', 'http_only': 1},
                    {'name': 'bili_jct', 'value': 'csrf-secret'},
                ]
            },
        },
    }


class IdentityProtocol:
    def __init__(self) -> None:
        self.oauth_calls = 0

    async def oauth_info(self, _bundle: Any) -> Mapping[str, Any]:
        self.oauth_calls += 1
        return {'code': 0, 'data': {'mid': 42, 'refresh': False}}

    async def web_nav(self, _bundle: Any) -> Mapping[str, Any]:
        return {'code': 0, 'data': {'isLogin': True, 'mid': 42, 'uname': 'fixture'}}


@pytest.mark.asyncio
async def test_disabled_runtime_uses_no_database_or_protocol(tmp_path: Path) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(
            enabled=False, database_path=str(tmp_path / 'unused.sqlite3')
        ),
        api_key=None,
        credential_key=None,
    )

    assert not await runtime.start()
    assert runtime.manager is None
    assert runtime.journal is None
    assert runtime.unavailable_reason == 'Bilibili account management is not enabled'
    assert not (tmp_path / 'unused.sqlite3').exists()
    assert await runtime.primary_cookie_header('https://api.bilibili.com/') is None


@pytest.mark.asyncio
async def test_missing_security_configuration_fails_closed_without_database(
    tmp_path: Path,
) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(
            enabled=True, database_path=str(tmp_path / 'unused.sqlite3')
        ),
        api_key=None,
        credential_key=b'k' * 32,
    )

    assert not await runtime.start()
    assert runtime.manager is None
    assert runtime.unavailable_reason == 'BLREC_API_KEY is required'
    assert not (tmp_path / 'unused.sqlite3').exists()


@pytest.mark.asyncio
async def test_enabled_runtime_starts_manager_and_periodic_health_check(
    tmp_path: Path,
) -> None:
    protocol = IdentityProtocol()
    clock = FakeClock()
    settings = BiliUploadSettings(
        enabled=True, database_path=str(tmp_path / 'blrec.sqlite3')
    )
    seed_runtime = BiliAccountRuntime(
        settings,
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=protocol,
        clock=clock,
    )
    assert await seed_runtime.start()
    assert seed_runtime.manager is not None
    await seed_runtime.manager.finish_confirmed_login(confirmed_response())
    await seed_runtime.close()
    protocol.oauth_calls = 0

    runtime = BiliAccountRuntime(
        settings,
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=protocol,
        clock=clock,
        refresh_interval_seconds=0.01,
    )
    try:
        assert await runtime.start()
        assert runtime.manager is not None
        assert runtime.journal is not None
        assert runtime.coordinator is not None
        assert runtime.policy_manager is not None

        for _ in range(100):
            if protocol.oauth_calls:
                break
            await asyncio.sleep(0.01)

        assert protocol.oauth_calls == 1
        assert runtime.unavailable_reason is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_close_is_idempotent(tmp_path: Path) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(enabled=True, database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=IdentityProtocol(),
    )

    assert await runtime.start()
    await runtime.close()
    await runtime.close()

    assert runtime.manager is None
    assert runtime.coordinator is None
    assert runtime.policy_manager is None


@pytest.mark.asyncio
async def test_runtime_reconciles_crash_interrupted_recording_before_use(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'interrupted.flv'
    source.write_bytes(b'partial recording')
    database_path = str(tmp_path / 'blrec.sqlite3')
    database = BiliUploadDatabase(database_path)
    await database.open()
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await database.close()

    runtime = BiliAccountRuntime(
        BiliUploadSettings(enabled=True, database_path=database_path),
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=IdentityProtocol(),
        clock=lambda: 2_000,
    )
    try:
        assert await runtime.start()
        assert runtime.journal is not None
        session = await runtime.journal.session_for_run(run_id)
        assert session.state == 'manual_review'
        assert session.parts[0].artifact_state == 'manual_review'
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_exposes_primary_cookie_and_forwards_auth_failures(
    tmp_path: Path,
) -> None:
    changed = AsyncMock()
    runtime = BiliAccountRuntime(
        BiliUploadSettings(enabled=True, database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=IdentityProtocol(),
        on_primary_credential_changed=changed,
    )
    try:
        assert await runtime.start()
        assert runtime.manager is not None
        await runtime.manager.finish_confirmed_login(confirmed_response())

        header = await runtime.primary_cookie_header(
            'https://api.live.bilibili.com/x/test'
        )
        assert 'SESSDATA=sess-secret' in header
        changed.assert_awaited_once_with()

        runtime.manager.report_primary_auth_failure = AsyncMock()
        await runtime.report_primary_auth_failure()
        runtime.manager.report_primary_auth_failure.assert_awaited_once_with()
    finally:
        await runtime.close()
