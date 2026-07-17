#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.policies import RoomUploadPolicyCommand, RoomUploadPolicyManager
from blrec.setting.models import Settings


@dataclass(frozen=True)
class MigrationReport:
    migrated_room_ids: tuple[int, ...]
    private_room_ids: tuple[int, ...]
    deferred_room_ids: tuple[int, ...]
    skipped_room_ids: tuple[int, ...]
    skipped_collection_room_ids: tuple[int, ...]
    skipped_custom_cover_room_ids: tuple[int, ...]
    database_backup: Optional[Path]


_VARIABLES = {
    'uname': 'anchor_name',
    'title': 'title',
    'roomId': 'room_id',
    'areaName': 'area_name',
    'index': 'part_index',
}
_DATE_PATTERN = re.compile(r'\$\{([yMdHms年月日点分秒:/._ -]+)\}')
_VARIABLE_PATTERN = re.compile(r'\$\{([^{}]+)\}')


def convert_java_template(value: Any) -> str:
    text = '' if value is None else str(value)

    def replace_date(match: re.Match[str]) -> str:
        date_format = match.group(1)
        for source, target in (
            ('yyyy', '%Y'),
            ('MM', '%m'),
            ('dd', '%d'),
            ('HH', '%H'),
            ('mm', '%M'),
            ('ss', '%S'),
        ):
            date_format = date_format.replace(source, target)
        return '{{ live_start_time | date: "' + date_format + '" }}'

    text = _DATE_PATTERN.sub(replace_date, text)

    def replace_variable(match: re.Match[str]) -> str:
        name = _VARIABLES.get(match.group(1))
        return match.group(0) if name is None else '{{ ' + name + ' }}'

    return _VARIABLE_PATTERN.sub(replace_variable, text)


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _retention(room: Mapping[str, Any]) -> tuple[str, int]:
    delete_type = _positive_int(room.get('deleteType'), 0)
    delete_days = min(_positive_int(room.get('deleteDay'), 0), 3650)
    if delete_type == 1:
        return 'upload_completed', 0
    if delete_type == 2:
        return 'approved', 0
    if delete_type == 3:
        return 'submitted', delete_days
    if delete_type == 9:
        return 'submitted', 0
    return 'never', 0


def _filters(room: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = room.get('filters')
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, Any] = {}
    blocked = raw.get('blackList') or raw.get('blockedWords')
    if isinstance(blocked, list):
        result['blockedWords'] = [
            item.strip() for item in blocked if isinstance(item, str) and item.strip()
        ]
    for source, target in (
        ('ulLevel', 'minimumUserLevel'),
        ('fanLevel', 'minimumFanMedalLevel'),
    ):
        value = raw.get(source)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[target] = value
    return result


def room_policy_command(
    room: Mapping[str, Any], *, enabled: bool
) -> RoomUploadPolicyCommand:
    copyright_value = _positive_int(room.get('copyright'), 1)
    is_repost = copyright_value == 2
    retention_mode, retention_days = _retention(room)
    title = convert_java_template(room.get('titleTemplate')).strip()
    description = convert_java_template(room.get('descTemplate')).strip()
    part_title = convert_java_template(room.get('partTitleTemplate')).strip()
    dynamic = convert_java_template(room.get('dynamicTemplate')).strip()
    tags = convert_java_template(room.get('tags')).strip()
    return RoomUploadPolicyCommand(
        account_mode='primary',
        account_id=None,
        enabled=enabled,
        title_template=title or '{{ title }}',
        description_template=description,
        part_title_template=part_title or 'P{{ part_index }}',
        dynamic_template=dynamic,
        tid=_positive_int(room.get('tid'), 21),
        tags=tags or '直播回放',
        creation_statement_id=-2 if is_repost else -1,
        original_authorization=False,
        source=('https://live.bilibili.com/{{ room_id }}' if is_repost else ''),
        is_only_self=room.get('isOnlySelf') == 1,
        publish_dynamic=room.get('noDisturbance') != 1,
        up_selection_reply=False,
        up_close_reply=False,
        up_close_danmu=False,
        auto_comment=True,
        danmaku_backfill=room.get('sendDm') is not False,
        filters=_filters(room),
        collection_season_id=None,
        collection_section_id=None,
        cover_mode='live',
        cover_asset_id=None,
        publish_delay_seconds=0,
        retention_mode=retention_mode,
        retention_days=retention_days,
    )


def _load_rooms(path: Path) -> list[Mapping[str, Any]]:
    raw = json.loads(path.read_text(encoding='utf8'))
    if isinstance(raw, Mapping):
        raw = raw.get('roomList')
    if not isinstance(raw, list):
        raise ValueError('biliupforjava room export must be a JSON array')
    rooms = [room for room in raw if isinstance(room, Mapping)]
    if len(rooms) != len(raw):
        raise ValueError('biliupforjava room export contains an invalid room')
    return rooms


def _database_backup(path: Path) -> Optional[Path]:
    if not path.is_file():
        return None
    migration_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = path.with_name(
        f'{path.name}.before-biliupforjava-migration-{migration_id}'
    )
    shutil.copy2(path, backup)
    return backup


async def migrate_biliupforjava_rooms(
    room_export_path: str | Path,
    settings_path: str | Path,
    database_path: str | Path,
    *,
    deferred_room_ids: Sequence[int] = (),
    create_backup: bool = True,
) -> MigrationReport:
    export_path = Path(room_export_path).expanduser()
    settings = Settings.load(str(Path(settings_path).expanduser()))
    monitored_room_ids = {task.room_id for task in settings.tasks}
    deferred = set(deferred_room_ids)
    rooms = _load_rooms(export_path)
    selected: dict[int, Mapping[str, Any]] = {}
    skipped_room_ids: list[int] = []
    for room in rooms:
        room_id = _positive_int(room.get('roomId'), 0)
        if room_id <= 0:
            raise ValueError('biliupforjava room export contains an invalid roomId')
        if room_id in selected:
            raise ValueError(f'duplicate biliupforjava roomId: {room_id}')
        if room_id not in monitored_room_ids:
            skipped_room_ids.append(room_id)
            continue
        selected[room_id] = room

    database_file = Path(database_path).expanduser()
    backup = _database_backup(database_file) if create_backup else None
    database = BiliUploadDatabase(str(database_file))
    await database.open()
    try:
        manager = RoomUploadPolicyManager(database)
        commands = {
            room_id: room_policy_command(
                room, enabled=bool(room.get('upload')) and room_id not in deferred
            )
            for room_id, room in selected.items()
        }
        for room_id, command in commands.items():
            await manager.validate(room_id, command)
        for room_id, command in commands.items():
            await manager.upsert(room_id, command)
    finally:
        await database.close()

    private = tuple(
        sorted(
            room_id for room_id, room in selected.items() if room.get('isOnlySelf') == 1
        )
    )
    collections = tuple(
        sorted(
            room_id
            for room_id, room in selected.items()
            if _positive_int(room.get('seasonId'), 0) > 0
        )
    )
    custom_covers = tuple(
        sorted(
            room_id
            for room_id, room in selected.items()
            if str(room.get('coverUrl') or '').strip() not in ('', 'live')
        )
    )
    return MigrationReport(
        migrated_room_ids=tuple(sorted(selected)),
        private_room_ids=private,
        deferred_room_ids=tuple(sorted(deferred & selected.keys())),
        skipped_room_ids=tuple(sorted(skipped_room_ids)),
        skipped_collection_room_ids=collections,
        skipped_custom_cover_room_ids=custom_covers,
        database_backup=backup,
    )


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Migrate biliupforjava room publishing rules into BLREC Next.'
    )
    parser.add_argument('room_export_path', type=Path)
    parser.add_argument('settings_path', type=Path)
    parser.add_argument('database_path', type=Path)
    parser.add_argument('--defer-room', type=int, action='append', default=[])
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    report = asyncio.run(
        migrate_biliupforjava_rooms(
            args.room_export_path,
            args.settings_path,
            args.database_path,
            deferred_room_ids=args.defer_room,
        )
    )
    print(f'Migrated room rules: {len(report.migrated_room_ids)}')
    print(f'Private room rules: {len(report.private_room_ids)}')
    print(f'Deferred room rules: {len(report.deferred_room_ids)}')
    print(f'Skipped unmonitored rooms: {len(report.skipped_room_ids)}')
    print(
        'Skipped collections without section IDs: '
        f'{len(report.skipped_collection_room_ids)}'
    )
    print(f'Skipped remote custom covers: {len(report.skipped_custom_cover_room_ids)}')
    if report.database_backup is not None:
        print(f'Database backup: {report.database_backup}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
