from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional, Tuple

from liquid import Environment

from .database import BiliUploadDatabase

__all__ = (
    'default_room_upload_policy',
    'InvalidRoomUploadPolicy',
    'RoomUploadPolicyCommand',
    'RoomUploadPolicyManager',
    'RoomUploadPolicyNotFound',
    'RoomUploadPolicyView',
    'room_upload_policy_command',
)


class InvalidRoomUploadPolicy(RuntimeError):
    pass


class RoomUploadPolicyNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class RoomUploadPolicyCommand:
    account_mode: str
    account_id: Optional[int]
    enabled: bool
    title_template: str
    description_template: str
    part_title_template: str
    dynamic_template: str
    tid: int
    tags: str
    creation_statement_id: int
    original_authorization: bool
    source: str
    is_only_self: bool
    publish_dynamic: bool
    up_selection_reply: bool
    up_close_reply: bool
    up_close_danmu: bool
    auto_comment: bool
    danmaku_backfill: bool
    filters: Mapping[str, Any]
    collection_season_id: Optional[int] = None
    collection_section_id: Optional[int] = None
    cover_mode: str = 'live'
    cover_asset_id: Optional[int] = None
    publish_delay_seconds: int = 0
    retention_mode: str = 'submitted'
    retention_days: int = 5


@dataclass(frozen=True)
class RoomUploadPolicyView:
    room_id: int
    account_mode: str
    account_id: Optional[int]
    resolved_account_id: Optional[int]
    resolved_account_name: Optional[str]
    enabled: bool
    title_template: str
    description_template: str
    part_title_template: str
    dynamic_template: str
    tid: int
    tags: str
    creation_statement_id: int
    original_authorization: bool
    copyright: int
    source: str
    is_only_self: bool
    publish_dynamic: bool
    no_reprint: bool
    up_selection_reply: bool
    up_close_reply: bool
    up_close_danmu: bool
    auto_comment: bool
    danmaku_backfill: bool
    filters: Mapping[str, Any]
    blocked_reason: Optional[str]
    created_at: int
    updated_at: int
    collection_season_id: Optional[int] = None
    collection_section_id: Optional[int] = None
    cover_mode: str = 'live'
    cover_asset_id: Optional[int] = None
    publish_delay_seconds: int = 0
    retention_mode: str = 'submitted'
    retention_days: int = 5


def default_room_upload_policy() -> RoomUploadPolicyCommand:
    return RoomUploadPolicyCommand(
        account_mode='primary',
        account_id=None,
        enabled=True,
        title_template=(
            '【直播回放】【{{ anchor_name }}】{{ title }} '
            '{{ live_start_time | date: "%Y年%m月%d日%H点%M分" }}'
        ),
        description_template=(
            '直播录像\n{{ anchor_name }}直播间：'
            'https://live.bilibili.com/{{ room_id }}'
        ),
        part_title_template=(
            'P{{ part_index }}-{{ area_name }}-'
            '{{ live_start_time | date: "%m月%d日%H点%M分" }}'
        ),
        dynamic_template=(
            '直播录像\n{{ anchor_name }}直播间：'
            'https://live.bilibili.com/{{ room_id }}'
        ),
        tid=21,
        tags='直播回放,{{ anchor_name }},{{ area_name }}',
        creation_statement_id=-2,
        original_authorization=False,
        source='https://live.bilibili.com/{{ room_id }}',
        is_only_self=False,
        publish_dynamic=True,
        up_selection_reply=False,
        up_close_reply=False,
        up_close_danmu=False,
        auto_comment=True,
        danmaku_backfill=True,
        filters={},
        retention_mode='submitted',
        retention_days=5,
    )


def room_upload_policy_command(policy: RoomUploadPolicyView) -> RoomUploadPolicyCommand:
    return RoomUploadPolicyCommand(
        account_mode=policy.account_mode,
        account_id=policy.account_id,
        enabled=policy.enabled,
        title_template=policy.title_template,
        description_template=policy.description_template,
        part_title_template=policy.part_title_template,
        dynamic_template=policy.dynamic_template,
        tid=policy.tid,
        tags=policy.tags,
        creation_statement_id=policy.creation_statement_id,
        original_authorization=policy.original_authorization,
        source=policy.source,
        is_only_self=policy.is_only_self,
        publish_dynamic=policy.publish_dynamic,
        up_selection_reply=policy.up_selection_reply,
        up_close_reply=policy.up_close_reply,
        up_close_danmu=policy.up_close_danmu,
        auto_comment=policy.auto_comment,
        danmaku_backfill=policy.danmaku_backfill,
        filters=policy.filters,
        collection_season_id=policy.collection_season_id,
        collection_section_id=policy.collection_section_id,
        cover_mode=policy.cover_mode,
        cover_asset_id=policy.cover_asset_id,
        publish_delay_seconds=policy.publish_delay_seconds,
        retention_mode=policy.retention_mode,
        retention_days=policy.retention_days,
    )


class RoomUploadPolicyManager:
    def __init__(
        self, database: BiliUploadDatabase, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._database = database
        self._clock = clock
        self._liquid = Environment()

    async def list(self) -> List[RoomUploadPolicyView]:
        rows = await self._database.fetchall(
            'SELECT room_id,account_mode,account_id,enabled,title_template,'
            'description_template,part_title_template,dynamic_template,tid,tags,'
            'creation_statement_id,original_authorization,copyright,source,'
            'is_only_self,publish_dynamic,no_reprint,'
            'up_selection_reply,up_close_reply,up_close_danmu,auto_comment,'
            'danmaku_backfill,filter_json,created_at,updated_at,'
            'collection_season_id,collection_section_id,cover_mode,'
            'cover_asset_id,publish_delay_seconds,retention_mode,retention_days '
            'FROM room_upload_policies ORDER BY room_id'
        )
        return [await self._view(row) for row in rows]

    async def get(self, room_id: int) -> RoomUploadPolicyView:
        row = await self._database.fetchone(
            'SELECT room_id,account_mode,account_id,enabled,title_template,'
            'description_template,part_title_template,dynamic_template,tid,tags,'
            'creation_statement_id,original_authorization,copyright,source,'
            'is_only_self,publish_dynamic,no_reprint,'
            'up_selection_reply,up_close_reply,up_close_danmu,auto_comment,'
            'danmaku_backfill,filter_json,created_at,updated_at,'
            'collection_season_id,collection_section_id,cover_mode,'
            'cover_asset_id,publish_delay_seconds,retention_mode,retention_days '
            'FROM room_upload_policies WHERE room_id=?',
            (room_id,),
        )
        if row is None:
            raise RoomUploadPolicyNotFound('room upload policy not found')
        return await self._view(row)

    async def upsert(
        self, room_id: int, command: RoomUploadPolicyCommand
    ) -> RoomUploadPolicyView:
        await self.validate(room_id, command)
        now = int(self._clock())
        filter_json = json.dumps(
            dict(command.filters),
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )
        copyright_value, no_reprint = self._submission_flags(command)
        await self._database.execute(
            'INSERT INTO room_upload_policies('
            'room_id,account_mode,account_id,enabled,title_template,'
            'description_template,part_title_template,dynamic_template,tid,tags,'
            'creation_statement_id,original_authorization,copyright,source,'
            'is_only_self,publish_dynamic,no_reprint,'
            'up_selection_reply,up_close_reply,up_close_danmu,auto_comment,'
            'danmaku_backfill,filter_json,created_at,updated_at,'
            'collection_season_id,collection_section_id,cover_mode,'
            'cover_asset_id,publish_delay_seconds,retention_mode,retention_days) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(room_id) DO UPDATE SET '
            'account_mode=excluded.account_mode,account_id=excluded.account_id,'
            'enabled=excluded.enabled,title_template=excluded.title_template,'
            'description_template=excluded.description_template,'
            'part_title_template=excluded.part_title_template,'
            'dynamic_template=excluded.dynamic_template,tid=excluded.tid,'
            'tags=excluded.tags,'
            'creation_statement_id=excluded.creation_statement_id,'
            'original_authorization=excluded.original_authorization,'
            'copyright=excluded.copyright,'
            'source=excluded.source,is_only_self=excluded.is_only_self,'
            'publish_dynamic=excluded.publish_dynamic,'
            'no_reprint=excluded.no_reprint,'
            'up_selection_reply=excluded.up_selection_reply,'
            'up_close_reply=excluded.up_close_reply,'
            'up_close_danmu=excluded.up_close_danmu,'
            'auto_comment=excluded.auto_comment,'
            'danmaku_backfill=excluded.danmaku_backfill,'
            'filter_json=excluded.filter_json,'
            'collection_season_id=excluded.collection_season_id,'
            'collection_section_id=excluded.collection_section_id,'
            'cover_mode=excluded.cover_mode,'
            'cover_asset_id=excluded.cover_asset_id,'
            'publish_delay_seconds=excluded.publish_delay_seconds,'
            'retention_mode=excluded.retention_mode,'
            'retention_days=excluded.retention_days,'
            'updated_at=excluded.updated_at',
            (
                room_id,
                command.account_mode,
                command.account_id,
                int(command.enabled),
                command.title_template.strip(),
                command.description_template.strip(),
                command.part_title_template.strip(),
                command.dynamic_template.strip(),
                command.tid,
                command.tags.strip(),
                command.creation_statement_id,
                int(command.original_authorization),
                copyright_value,
                command.source.strip(),
                int(command.is_only_self),
                int(command.publish_dynamic),
                int(no_reprint),
                int(command.up_selection_reply),
                int(command.up_close_reply),
                int(command.up_close_danmu),
                int(command.auto_comment),
                int(command.danmaku_backfill),
                filter_json,
                now,
                now,
                command.collection_season_id,
                command.collection_section_id,
                command.cover_mode,
                command.cover_asset_id,
                command.publish_delay_seconds,
                command.retention_mode,
                command.retention_days,
            ),
        )
        return await self.get(room_id)

    async def validate(self, room_id: int, command: RoomUploadPolicyCommand) -> None:
        self._validate(room_id, command)
        await self._require_account(command)
        await self._require_cover_asset(command)

    async def delete(self, room_id: int) -> None:
        deleted = await self._database.execute(
            'DELETE FROM room_upload_policies WHERE room_id=?', (room_id,)
        )
        if deleted != 1:
            raise RoomUploadPolicyNotFound('room upload policy not found')

    def _validate(self, room_id: int, command: RoomUploadPolicyCommand) -> None:
        if room_id <= 0:
            raise InvalidRoomUploadPolicy('roomId must be positive')
        if command.account_mode not in ('primary', 'fixed'):
            raise InvalidRoomUploadPolicy('accountMode must be primary or fixed')
        if command.account_mode == 'primary' and command.account_id is not None:
            raise InvalidRoomUploadPolicy(
                'accountId must be empty when following the primary account'
            )
        if command.account_mode == 'fixed' and (
            command.account_id is None or command.account_id <= 0
        ):
            raise InvalidRoomUploadPolicy(
                'accountId is required for a fixed account policy'
            )
        title_template = command.title_template.strip()
        description_template = command.description_template.strip()
        part_title_template = command.part_title_template.strip()
        dynamic_template = command.dynamic_template.strip()
        if not title_template or len(title_template) > 500:
            raise InvalidRoomUploadPolicy('title template is invalid')
        if len(description_template) > 5000:
            raise InvalidRoomUploadPolicy('description template is too long')
        if not part_title_template or len(part_title_template) > 500:
            raise InvalidRoomUploadPolicy('part title template is invalid')
        if len(dynamic_template) > 5000:
            raise InvalidRoomUploadPolicy('dynamic template is too long')
        try:
            self._liquid.from_string(title_template)
            self._liquid.from_string(description_template)
            self._liquid.from_string(part_title_template)
            self._liquid.from_string(dynamic_template)
            self._liquid.from_string(command.source)
        except Exception as error:
            raise InvalidRoomUploadPolicy('template syntax is invalid') from error
        if command.tid <= 0:
            raise InvalidRoomUploadPolicy('tid must be positive')
        if not command.tags.strip():
            raise InvalidRoomUploadPolicy('tags must not be empty')
        if type(command.creation_statement_id) is not int:
            raise InvalidRoomUploadPolicy('creation statement is invalid')
        if command.creation_statement_id == -2 and not command.source.strip():
            raise InvalidRoomUploadPolicy('source is required for reposted archives')
        if command.creation_statement_id == -2 and command.original_authorization:
            raise InvalidRoomUploadPolicy(
                'repost and original authorization are mutually exclusive'
            )
        if command.auto_comment and command.up_close_reply:
            raise InvalidRoomUploadPolicy(
                'comments must remain open for automatic comments'
            )
        if command.danmaku_backfill and command.up_close_danmu:
            raise InvalidRoomUploadPolicy('danmaku must remain open for backfill')
        if command.up_selection_reply and command.up_close_reply:
            raise InvalidRoomUploadPolicy('selected comments require open comments')
        if (command.collection_season_id is None) != (
            command.collection_section_id is None
        ):
            raise InvalidRoomUploadPolicy(
                'collection season and section must be selected together'
            )
        if command.collection_season_id is not None and (
            command.collection_season_id <= 0
            or command.collection_section_id is None
            or command.collection_section_id <= 0
        ):
            raise InvalidRoomUploadPolicy('collection selection is invalid')
        if command.cover_mode not in ('live', 'custom'):
            raise InvalidRoomUploadPolicy('cover mode is invalid')
        if command.cover_mode == 'live' and command.cover_asset_id is not None:
            raise InvalidRoomUploadPolicy('live cover cannot select a cover asset')
        if command.cover_mode == 'custom' and (
            command.cover_asset_id is None or command.cover_asset_id <= 0
        ):
            raise InvalidRoomUploadPolicy('custom cover requires a cover asset')
        if command.publish_delay_seconds != 0 and not (
            7200 <= command.publish_delay_seconds <= 15 * 24 * 60 * 60
        ):
            raise InvalidRoomUploadPolicy(
                'publish delay must be zero or between 2 hours and 15 days'
            )
        if command.retention_mode not in (
            'never',
            'upload_completed',
            'submitted',
            'approved',
            'capacity',
        ):
            raise InvalidRoomUploadPolicy('retention mode is invalid')
        if not 0 <= command.retention_days <= 3650:
            raise InvalidRoomUploadPolicy('retention days must be between 0 and 3650')
        if not isinstance(command.filters, Mapping):
            raise InvalidRoomUploadPolicy('filters must be an object')

    @staticmethod
    def _submission_flags(command: RoomUploadPolicyCommand) -> Tuple[int, bool]:
        if command.creation_statement_id == -2:
            return 2, False
        if command.original_authorization:
            return 1, True
        return 3, False

    async def _require_account(self, command: RoomUploadPolicyCommand) -> None:
        if command.account_mode == 'primary':
            row = await self._database.fetchone(
                'SELECT account.id,account.state FROM bili_account_selection selection '
                'JOIN bili_accounts account '
                'ON account.id=selection.primary_account_id WHERE selection.id=1'
            )
        else:
            row = await self._database.fetchone(
                'SELECT id,state FROM bili_accounts WHERE id=?', (command.account_id,)
            )
        if row is None or str(row['state']) != 'active':
            raise InvalidRoomUploadPolicy('an active upload account is required')

    async def _require_cover_asset(self, command: RoomUploadPolicyCommand) -> None:
        if command.cover_asset_id is None:
            return
        exists = await self._database.scalar(
            'SELECT 1 FROM cover_assets WHERE id=?', (command.cover_asset_id,)
        )
        if exists != 1:
            raise InvalidRoomUploadPolicy('custom cover asset was not found')

    async def _view(self, row: Any) -> RoomUploadPolicyView:
        account_mode = str(row['account_mode'])
        account_id = None if row['account_id'] is None else int(row['account_id'])
        if account_mode == 'primary':
            account = await self._database.fetchone(
                'SELECT account.id,account.display_name,account.state '
                'FROM bili_account_selection selection '
                'JOIN bili_accounts account '
                'ON account.id=selection.primary_account_id WHERE selection.id=1'
            )
        else:
            account = await self._database.fetchone(
                'SELECT id,display_name,state FROM bili_accounts WHERE id=?',
                (account_id,),
            )
        blocked_reason: Optional[str]
        if account is None:
            resolved_account_id = None
            resolved_account_name = None
            blocked_reason = '未找到可用的投稿账号'
        else:
            resolved_account_id = int(account['id'])
            resolved_account_name = str(account['display_name'])
            blocked_reason = (
                None if str(account['state']) == 'active' else '投稿账号当前不可用'
            )
        try:
            filters = json.loads(str(row['filter_json']))
        except json.JSONDecodeError:
            filters = {}
            blocked_reason = '过滤设置损坏'
        if not isinstance(filters, dict):
            filters = {}
            blocked_reason = '过滤设置损坏'
        return RoomUploadPolicyView(
            room_id=int(row['room_id']),
            account_mode=account_mode,
            account_id=account_id,
            resolved_account_id=resolved_account_id,
            resolved_account_name=resolved_account_name,
            enabled=bool(row['enabled']),
            title_template=str(row['title_template']),
            description_template=str(row['description_template']),
            part_title_template=str(row['part_title_template']),
            dynamic_template=str(row['dynamic_template']),
            tid=int(row['tid']),
            tags=str(row['tags']),
            creation_statement_id=int(row['creation_statement_id']),
            original_authorization=bool(row['original_authorization']),
            copyright=int(row['copyright']),
            source=str(row['source']),
            is_only_self=bool(row['is_only_self']),
            publish_dynamic=bool(row['publish_dynamic']),
            no_reprint=bool(row['no_reprint']),
            up_selection_reply=bool(row['up_selection_reply']),
            up_close_reply=bool(row['up_close_reply']),
            up_close_danmu=bool(row['up_close_danmu']),
            auto_comment=bool(row['auto_comment']),
            danmaku_backfill=bool(row['danmaku_backfill']),
            filters=filters,
            blocked_reason=blocked_reason,
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
            collection_season_id=(
                None
                if row['collection_season_id'] is None
                else int(row['collection_season_id'])
            ),
            collection_section_id=(
                None
                if row['collection_section_id'] is None
                else int(row['collection_section_id'])
            ),
            cover_mode=str(row['cover_mode']),
            cover_asset_id=(
                None if row['cover_asset_id'] is None else int(row['cover_asset_id'])
            ),
            publish_delay_seconds=int(row['publish_delay_seconds']),
            retention_mode=str(row['retention_mode']),
            retention_days=int(row['retention_days']),
        )
