from pathlib import Path

from blrec.setting.models import Settings, TaskSettings
from scripts.migrate_legacy_settings import migrate_legacy_settings


def _write_settings(path: Path, settings: Settings) -> None:
    settings._path = str(path)
    settings.dump()


def test_migration_keeps_safe_recording_settings_but_not_secrets(
    tmp_path: Path,
) -> None:
    old_out = tmp_path / 'old-out'
    new_out = tmp_path / 'new-out'
    old_log = tmp_path / 'old-log'
    new_log = tmp_path / 'new-log'
    for directory in (old_out, new_out, old_log, new_log):
        directory.mkdir()

    old_path = tmp_path / 'old.toml'
    new_path = tmp_path / 'new.toml'
    database_path = tmp_path / 'blrec.sqlite3'
    database_path.write_bytes(b'database-before-migration')

    old = Settings(
        output={
            'out_dir': str(old_out),
            'path_template': '{roomid}/old_{year}',
            'filesize_limit': 1024,
            'duration_limit': 600,
        },
        logging={
            'log_dir': str(old_log),
            'console_log_level': 'DEBUG',
            'backup_count': 3,
        },
        header={'user_agent': 'legacy-agent', 'cookie': 'SESSDATA=legacy'},
        danmaku={'save_raw_danmaku': True},
        recorder={'quality_number': 10000, 'save_cover': True},
        postprocessing={'remux_to_mp4': True, 'delete_source': 'never'},
        space={
            'check_interval': 10,
            'space_threshold': 3 * 1024**3,
            'recycle_records': True,
            'recording_capacity': 99 * 1024**3,
        },
        pushplus_notification={
            'enabled': True,
            'token': 'a' * 32,
            'topic': 'legacy-secret',
        },
        webhooks=[{'url': 'https://legacy.example/webhook'}],
        tasks=[
            TaskSettings(
                room_id=100,
                enable_monitor=False,
                enable_recorder=False,
                output={'path_template': '{roomid}/task_{year}', 'duration_limit': 300},
                header={'user_agent': 'legacy-task-agent', 'cookie': 'task-secret'},
                recorder={'quality_number': 10000},
                postprocessing={'delete_source': 'safe'},
            ),
            TaskSettings(room_id=200),
        ],
    )
    new = Settings(
        output={'out_dir': str(new_out)},
        logging={
            'log_dir': str(new_log),
            'console_log_level': 'WARNING',
            'backup_count': 7,
        },
        bili_upload={'database_path': str(database_path)},
        network={'interfaces': {'ovs_eth0': {'enabled': False}}},
        space={
            'recording_capacity': 500 * 1024**3,
            'capacity_warning_threshold': 20 * 1024**3,
        },
        pushplus_notification={
            'enabled': True,
            'token': 'b' * 32,
            'topic': 'new-secret',
        },
        webhooks=[{'url': 'https://new.example/webhook'}],
        tasks=[TaskSettings(room_id=100, recorder={'quality_number': 400})],
    )
    _write_settings(old_path, old)
    _write_settings(new_path, new)
    new_before = new_path.read_text(encoding='utf8')

    report = migrate_legacy_settings(old_path, new_path)
    migrated = report.settings

    assert migrated.output.out_dir == str(new_out)
    assert migrated.output.path_template == '{roomid}/old_{year}'
    assert migrated.output.filesize_limit == 1024
    assert migrated.output.duration_limit == 600
    assert migrated.header.user_agent == 'legacy-agent'
    assert migrated.header.cookie == ''
    assert migrated.danmaku.save_raw_danmaku is True
    assert migrated.recorder.quality_number == 10000
    assert migrated.recorder.save_cover is True
    assert migrated.postprocessing.remux_to_mp4 is True
    assert migrated.logging.log_dir == str(new_log)
    assert migrated.logging.console_log_level == 'WARNING'
    assert migrated.logging.backup_count == 60
    assert migrated.space == new.space
    assert migrated.network == new.network
    assert migrated.pushplus_notification == new.pushplus_notification
    assert migrated.webhooks == new.webhooks

    tasks = {task.room_id: task for task in migrated.tasks}
    assert set(tasks) == {100, 200}
    assert tasks[100].enable_monitor is False
    assert tasks[100].enable_recorder is False
    assert tasks[100].output.path_template == '{roomid}/task_{year}'
    assert tasks[100].header.user_agent is None
    assert tasks[100].header.cookie is None
    assert tasks[100].postprocessing.delete_source is None
    assert tasks[100].recorder.quality_number == 10000

    assert report.settings_backup.read_text(encoding='utf8') == new_before
    assert report.database_backup.read_bytes() == b'database-before-migration'


def test_migration_is_idempotent_and_does_not_duplicate_tasks(tmp_path: Path) -> None:
    out_dir = tmp_path / 'out'
    log_dir = tmp_path / 'log'
    out_dir.mkdir()
    log_dir.mkdir()
    old_path = tmp_path / 'old.toml'
    new_path = tmp_path / 'new.toml'

    old = Settings(
        output={'out_dir': str(out_dir)},
        logging={'log_dir': str(log_dir)},
        tasks=[TaskSettings(room_id=100), TaskSettings(room_id=200)],
    )
    new = Settings(
        output={'out_dir': str(out_dir)},
        logging={'log_dir': str(log_dir)},
        bili_upload={'database_path': str(tmp_path / 'missing.sqlite3')},
        tasks=[TaskSettings(room_id=100)],
    )
    _write_settings(old_path, old)
    _write_settings(new_path, new)

    first = migrate_legacy_settings(old_path, new_path)
    first_contents = new_path.read_text(encoding='utf8')
    second = migrate_legacy_settings(old_path, new_path)

    assert new_path.read_text(encoding='utf8') == first_contents
    assert [task.room_id for task in second.settings.tasks] == [100, 200]
    assert first.database_backup is None
    assert second.database_backup is None


def test_migration_can_limit_legacy_tasks_to_pilot_rooms(tmp_path: Path) -> None:
    out_dir = tmp_path / 'out'
    log_dir = tmp_path / 'log'
    out_dir.mkdir()
    log_dir.mkdir()
    old_path = tmp_path / 'old.toml'
    new_path = tmp_path / 'new.toml'
    common = {
        'output': {'out_dir': str(out_dir)},
        'logging': {'log_dir': str(log_dir)},
        'bili_upload': {'database_path': str(tmp_path / 'missing.sqlite3')},
    }
    _write_settings(
        old_path,
        Settings(
            **common, tasks=[TaskSettings(room_id=100), TaskSettings(room_id=200)]
        ),
    )
    _write_settings(new_path, Settings(**common, tasks=[TaskSettings(room_id=300)]))

    report = migrate_legacy_settings(old_path, new_path, room_ids={200})

    assert [task.room_id for task in report.settings.tasks] == [300, 200]
    assert report.added_room_ids == (200,)
