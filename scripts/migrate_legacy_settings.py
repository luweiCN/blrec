#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Collection, Optional, Sequence

import toml

from blrec.setting.models import Settings, TaskSettings


@dataclass(frozen=True)
class MigrationReport:
    settings: Settings
    settings_backup: Path
    database_backup: Optional[Path]
    added_room_ids: tuple[int, ...]
    updated_room_ids: tuple[int, ...]


def _backup_path(path: Path, migration_id: str) -> Path:
    return path.with_name(f'{path.name}.before-legacy-migration-{migration_id}')


def _resolve_database_path(settings_path: Path, database_path: str) -> Path:
    path = Path(database_path).expanduser()
    if not path.is_absolute():
        path = settings_path.parent / path
    return path


def _copy_safe_task_fields(source: TaskSettings, target: TaskSettings) -> None:
    target.enable_monitor = source.enable_monitor
    target.enable_recorder = source.enable_recorder
    target.output = source.output.copy(deep=True)
    target.danmaku = source.danmaku.copy(deep=True)
    target.recorder = source.recorder.copy(deep=True)
    target.postprocessing = source.postprocessing.copy(deep=True)

    header = target.header.copy(deep=True)
    header.user_agent = source.header.user_agent
    target.header = header


def _write_settings_atomically(settings: Settings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent)
    )
    try:
        with os.fdopen(file_descriptor, 'wt', encoding='utf8') as file:
            toml.dump(settings.dict(exclude_none=True), file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def migrate_legacy_settings(
    old_path: str | Path,
    new_path: str | Path,
    *,
    room_ids: Optional[Collection[int]] = None,
) -> MigrationReport:
    legacy_path = Path(old_path).expanduser()
    destination_path = Path(new_path).expanduser()
    legacy = Settings.load(str(legacy_path))
    destination = Settings.load(str(destination_path))

    migration_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    settings_backup = _backup_path(destination_path, migration_id)
    shutil.copy2(destination_path, settings_backup)

    database_path = _resolve_database_path(
        destination_path, destination.bili_upload.database_path
    )
    database_backup: Optional[Path] = None
    if database_path.is_file():
        database_backup = _backup_path(database_path, migration_id)
        shutil.copy2(database_path, database_backup)

    destination.output.path_template = legacy.output.path_template
    destination.output.filesize_limit = legacy.output.filesize_limit
    destination.output.duration_limit = legacy.output.duration_limit
    destination.header.user_agent = legacy.header.user_agent
    destination.danmaku = legacy.danmaku.copy(deep=True)
    destination.recorder = legacy.recorder.copy(deep=True)
    destination.postprocessing = legacy.postprocessing.copy(deep=True)
    destination.logging.backup_count = 60

    tasks_by_room_id = {task.room_id: task for task in destination.tasks}
    added_room_ids = []
    updated_room_ids = []
    for legacy_task in legacy.tasks:
        if room_ids is not None and legacy_task.room_id not in room_ids:
            continue
        target = tasks_by_room_id.get(legacy_task.room_id)
        if target is None:
            target = TaskSettings(room_id=legacy_task.room_id)
            destination.tasks.append(target)
            tasks_by_room_id[target.room_id] = target
            added_room_ids.append(target.room_id)
        else:
            updated_room_ids.append(target.room_id)
        _copy_safe_task_fields(legacy_task, target)

    migrated = Settings.parse_obj(destination.dict(exclude_none=True))
    migrated._path = str(destination_path)
    _write_settings_atomically(migrated, destination_path)

    return MigrationReport(
        settings=migrated,
        settings_backup=settings_backup,
        database_backup=database_backup,
        added_room_ids=tuple(added_room_ids),
        updated_room_ids=tuple(updated_room_ids),
    )


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Safely migrate compatible recording settings from legacy BLREC.'
    )
    parser.add_argument('old_path', type=Path, help='legacy settings.toml path')
    parser.add_argument('new_path', type=Path, help='BLREC Next settings.toml path')
    parser.add_argument(
        '--rooms', type=int, nargs='+', help='only migrate the listed legacy room IDs'
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    report = migrate_legacy_settings(args.old_path, args.new_path, room_ids=args.rooms)
    print(f'Migrated settings to {args.new_path}')
    print(f'Settings backup: {report.settings_backup}')
    if report.database_backup is not None:
        print(f'Database backup: {report.database_backup}')
    print(f'Added rooms: {len(report.added_room_ids)}')
    print(f'Updated rooms: {len(report.updated_room_ids)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
