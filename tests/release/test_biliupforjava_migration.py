import json
from pathlib import Path

import pytest
import toml

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.policies import RoomUploadPolicyManager
from blrec.setting.models import Settings, TaskSettings
from scripts.migrate_biliupforjava_rooms import (
    convert_java_template,
    migrate_biliupforjava_rooms,
)


async def seed_primary_account(database_path: Path) -> None:
    database = BiliUploadDatabase(str(database_path))
    await database.open()
    try:
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            "state,created_at,updated_at) VALUES(1,42,'账号',X'00',1,'k','active',1,1)"
        )
        await database.execute(
            'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
        )
    finally:
        await database.close()


def write_settings(path: Path) -> None:
    settings = Settings(tasks=[TaskSettings(room_id=100), TaskSettings(room_id=200)])
    path.write_text(toml.dumps(settings.dict(exclude_none=True)), encoding='utf8')


def test_convert_java_template_preserves_text_and_maps_known_values() -> None:
    assert convert_java_template(
        '${uname}-${title}-${roomId}-${areaName}-${index}-' '${yyyy年MM月dd日HH点mm分}'
    ) == (
        '{{ anchor_name }}-{{ title }}-{{ room_id }}-{{ area_name }}-'
        '{{ part_index }}-'
        '{{ live_start_time | date: "%Y年%m月%d日%H点%M分" }}'
    )
    assert convert_java_template('${unknown}') == '${unknown}'


@pytest.mark.asyncio
async def test_migration_preserves_private_and_submission_settings(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / 'settings.toml'
    database_path = tmp_path / 'blrec.sqlite3'
    export_path = tmp_path / 'rooms.json'
    write_settings(settings_path)
    await seed_primary_account(database_path)
    export_path.write_text(
        json.dumps(
            [
                {
                    'roomId': '100',
                    'upload': True,
                    'titleTemplate': '${uname} ${title}',
                    'partTitleTemplate': 'P${index}-${MM月dd日HH点mm分}',
                    'descTemplate': '直播间 ${roomId}',
                    'dynamicTemplate': '${title}',
                    'tags': '直播,${uname}',
                    'tid': 171,
                    'copyright': 2,
                    'isOnlySelf': 1,
                    'noDisturbance': 1,
                    'sendDm': True,
                    'deleteType': 3,
                    'deleteDay': 7,
                    'seasonId': 88,
                    'coverUrl': 'https://example.invalid/cover.jpg',
                    'filters': {'blackList': ['广告'], 'ulLevel': 10, 'fanLevel': 3},
                },
                {
                    'roomId': '200',
                    'upload': False,
                    'titleTemplate': '${title}',
                    'partTitleTemplate': 'P${index}',
                    'descTemplate': '',
                    'dynamicTemplate': '',
                    'tags': '录播',
                    'tid': 17,
                    'copyright': 1,
                    'isOnlySelf': 0,
                    'noDisturbance': 0,
                    'sendDm': False,
                    'deleteType': 2,
                },
                {'roomId': '300', 'upload': True},
            ],
            ensure_ascii=False,
        ),
        encoding='utf8',
    )

    report = await migrate_biliupforjava_rooms(
        export_path, settings_path, database_path, deferred_room_ids=(100,)
    )

    database = BiliUploadDatabase(str(database_path))
    await database.open()
    try:
        manager = RoomUploadPolicyManager(database)
        private = await manager.get(100)
        public = await manager.get(200)
    finally:
        await database.close()

    assert private.enabled is False
    assert private.title_template == '{{ anchor_name }} {{ title }}'
    assert private.part_title_template == (
        'P{{ part_index }}-' '{{ live_start_time | date: "%m月%d日%H点%M分" }}'
    )
    assert private.tid == 171
    assert private.creation_statement_id == -2
    assert private.source == 'https://live.bilibili.com/{{ room_id }}'
    assert private.is_only_self is True
    assert private.publish_dynamic is False
    assert private.danmaku_backfill is True
    assert private.filters == {
        'blockedWords': ['广告'],
        'minimumFanMedalLevel': 3,
        'minimumUserLevel': 10,
    }
    assert private.retention_mode == 'submitted'
    assert private.retention_days == 7
    assert private.collection_season_id is None
    assert private.cover_mode == 'live'
    assert public.enabled is False
    assert public.creation_statement_id == -1
    assert public.is_only_self is False
    assert public.publish_dynamic is True
    assert public.danmaku_backfill is False
    assert public.retention_mode == 'approved'
    assert report.migrated_room_ids == (100, 200)
    assert report.private_room_ids == (100,)
    assert report.deferred_room_ids == (100,)
    assert report.skipped_room_ids == (300,)
    assert report.skipped_collection_room_ids == (100,)
    assert report.skipped_custom_cover_room_ids == (100,)
    assert report.database_backup is not None
    assert report.database_backup.is_file()
