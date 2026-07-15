from __future__ import annotations

import pytest
from pydantic import ValidationError

from blrec.setting.models import (
    OperationalNotificationRoute,
    OperationalNotificationSettings,
    OperationalNotificationTarget,
    Settings,
)


def test_operational_notification_routes_have_all_supported_events() -> None:
    settings = OperationalNotificationSettings()

    assert {route.event for route in settings.routes} == {
        'account_unavailable',
        'network_unavailable',
        'network_failover',
        'recording_failed',
        'upload_failed',
        'review_rejected',
        'collection_failed',
        'comment_failed',
        'danmaku_failed',
        'transcode_repair_failed',
        'capacity_warning',
    }
    assert all(route.targets == [] for route in settings.routes)


def test_operational_notification_target_validates_channel_message_type() -> None:
    assert (
        OperationalNotificationTarget(channel='email', message_type='html').message_type
        == 'html'
    )
    assert (
        OperationalNotificationTarget(
            channel='serverchan', message_type='markdown'
        ).message_type
        == 'markdown'
    )

    with pytest.raises(ValidationError, match='does not support'):
        OperationalNotificationTarget(channel='serverchan', message_type='text')


def test_operational_notification_routes_reject_duplicates_and_unknown_events() -> None:
    target = OperationalNotificationTarget(channel='bark', message_type='text')
    with pytest.raises(ValidationError, match='duplicate channel'):
        OperationalNotificationRoute(event='upload_failed', targets=[target, target])
    with pytest.raises(ValidationError):
        OperationalNotificationRoute(event='unknown', targets=[])


def test_settings_round_trip_operational_notification_routes(tmp_path) -> None:
    path = tmp_path / 'settings.toml'
    settings = Settings()
    settings._path = str(path)
    settings.operational_notifications.routes[4].targets = [
        OperationalNotificationTarget(channel='pushdeer', message_type='markdown')
    ]

    settings.dump()
    loaded = Settings.load(str(path))

    route = loaded.operational_notifications.route_for('upload_failed')
    assert [target.dict() for target in route.targets] == [
        {'channel': 'pushdeer', 'message_type': 'markdown'}
    ]
