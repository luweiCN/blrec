from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.notification.operational import (
    OperationalHealthScanner,
    OperationalNotificationCenter,
)
from blrec.setting.models import (
    OperationalNotificationSettings,
    OperationalNotificationTarget,
)


class FakeSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: List[Tuple[str, str, str]] = []

    async def send_message(self, title: str, content: str, message_type: str) -> None:
        self.calls.append((title, content, message_type))
        if self.fail:
            raise RuntimeError('channel unavailable')


def configured_settings() -> OperationalNotificationSettings:
    settings = OperationalNotificationSettings()
    route = settings.route_for('account_unavailable')
    route.targets = [
        OperationalNotificationTarget(channel='email', message_type='html'),
        OperationalNotificationTarget(channel='pushdeer', message_type='markdown'),
    ]
    return settings


@pytest.mark.asyncio
async def test_operational_notifications_send_problem_and_recovery_once(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    email = FakeSender()
    pushdeer = FakeSender(fail=True)
    settings = configured_settings()
    center = OperationalNotificationCenter(
        database,
        settings_provider=lambda: settings,
        senders={'email': email, 'pushdeer': pushdeer},
        channel_enabled=lambda _channel: True,
        clock=lambda: 1_000,
    )
    try:
        assert not await center.report(
            'account_unavailable',
            'account:1',
            healthy=True,
            title='投稿账号失效',
            detail='账号一',
        )
        assert await center.report(
            'account_unavailable',
            'account:1',
            healthy=False,
            title='投稿账号失效',
            detail='账号一：Cookie 已失效',
        )
        assert not await center.report(
            'account_unavailable',
            'account:1',
            healthy=False,
            title='投稿账号失效',
            detail='原因文案变化也不重复轰炸',
        )
        assert await center.report(
            'account_unavailable',
            'account:1',
            healthy=True,
            title='投稿账号恢复',
            detail='账号一已经恢复可用',
        )

        assert [call[0] for call in email.calls] == ['投稿账号失效', '投稿账号恢复']
        assert email.calls[0][2] == 'html'
        assert len(pushdeer.calls) == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_operational_notification_state_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / 'db.sqlite3'
    settings = configured_settings()
    first_sender = FakeSender()
    database = BiliUploadDatabase(str(path))
    await database.open()
    center = OperationalNotificationCenter(
        database,
        settings_provider=lambda: settings,
        senders={'email': first_sender},
        channel_enabled=lambda _channel: True,
    )
    await center.report(
        'account_unavailable',
        'account:1',
        healthy=False,
        title='投稿账号失效',
        detail='首次观察只建立基线',
    )
    assert first_sender.calls == []
    await database.close()

    second_sender = FakeSender()
    database = BiliUploadDatabase(str(path))
    await database.open()
    try:
        center = OperationalNotificationCenter(
            database,
            settings_provider=lambda: settings,
            senders={'email': second_sender},
            channel_enabled=lambda _channel: True,
        )
        assert not await center.report(
            'account_unavailable',
            'account:1',
            healthy=False,
            title='投稿账号失效',
            detail='重启后仍然失败',
        )
        assert second_sender.calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_disabled_channel_is_not_dispatched(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    sender = FakeSender()
    settings = configured_settings()
    center = OperationalNotificationCenter(
        database,
        settings_provider=lambda: settings,
        senders={'email': sender},
        channel_enabled=lambda _channel: False,
    )
    try:
        await center.report(
            'account_unavailable',
            'account:1',
            healthy=True,
            title='投稿账号正常',
            detail='',
        )
        await center.report(
            'account_unavailable',
            'account:1',
            healthy=False,
            title='投稿账号失效',
            detail='',
        )
        assert sender.calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_health_scanner_reports_account_failure_and_recovery(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    sender = FakeSender()
    settings = configured_settings()
    center = OperationalNotificationCenter(
        database,
        settings_provider=lambda: settings,
        senders={'email': sender},
        channel_enabled=lambda _channel: True,
        clock=lambda: 1_000,
    )
    scanner = OperationalHealthScanner(database, center)
    try:
        await database.execute(
            "INSERT INTO bili_accounts("
            "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
            "state,created_at,updated_at) "
            "VALUES(1,42,'投稿账号',X'00',1,'key','active',1,1)"
        )
        await scanner.scan()
        await database.execute(
            "UPDATE bili_accounts SET state='paused',pause_reason='Cookie 已失效' "
            'WHERE id=1'
        )
        await scanner.scan()
        await database.execute(
            "UPDATE bili_accounts SET state='active',pause_reason=NULL WHERE id=1"
        )
        await scanner.scan()

        assert [call[0] for call in sender.calls] == [
            '投稿账号不可用',
            '投稿账号已恢复',
        ]
        assert 'Cookie 已失效' in sender.calls[0][1]
    finally:
        await database.close()
