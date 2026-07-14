from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional

from liquid import Environment

from .database import BiliUploadDatabase

__all__ = (
    'InvalidRoomUploadPolicy',
    'RoomUploadPolicyCommand',
    'RoomUploadPolicyManager',
    'RoomUploadPolicyNotFound',
    'RoomUploadPolicyView',
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
    tid: int
    tags: str
    copyright: int
    source: str
    auto_comment: bool
    danmaku_backfill: bool
    filters: Mapping[str, Any]


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
    tid: int
    tags: str
    copyright: int
    source: str
    auto_comment: bool
    danmaku_backfill: bool
    filters: Mapping[str, Any]
    blocked_reason: Optional[str]
    created_at: int
    updated_at: int


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
            'description_template,tid,tags,copyright,source,auto_comment,'
            'danmaku_backfill,filter_json,created_at,updated_at '
            'FROM room_upload_policies ORDER BY room_id'
        )
        return [await self._view(row) for row in rows]

    async def get(self, room_id: int) -> RoomUploadPolicyView:
        row = await self._database.fetchone(
            'SELECT room_id,account_mode,account_id,enabled,title_template,'
            'description_template,tid,tags,copyright,source,auto_comment,'
            'danmaku_backfill,filter_json,created_at,updated_at '
            'FROM room_upload_policies WHERE room_id=?',
            (room_id,),
        )
        if row is None:
            raise RoomUploadPolicyNotFound('room upload policy not found')
        return await self._view(row)

    async def upsert(
        self, room_id: int, command: RoomUploadPolicyCommand
    ) -> RoomUploadPolicyView:
        self._validate(room_id, command)
        await self._require_account(command)
        now = int(self._clock())
        filter_json = json.dumps(
            dict(command.filters),
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )
        await self._database.execute(
            'INSERT INTO room_upload_policies('
            'room_id,account_mode,account_id,enabled,title_template,'
            'description_template,tid,tags,copyright,source,auto_comment,'
            'danmaku_backfill,filter_json,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(room_id) DO UPDATE SET '
            'account_mode=excluded.account_mode,account_id=excluded.account_id,'
            'enabled=excluded.enabled,title_template=excluded.title_template,'
            'description_template=excluded.description_template,tid=excluded.tid,'
            'tags=excluded.tags,copyright=excluded.copyright,'
            'source=excluded.source,auto_comment=excluded.auto_comment,'
            'danmaku_backfill=excluded.danmaku_backfill,'
            'filter_json=excluded.filter_json,updated_at=excluded.updated_at',
            (
                room_id,
                command.account_mode,
                command.account_id,
                int(command.enabled),
                command.title_template.strip(),
                command.description_template.strip(),
                command.tid,
                command.tags.strip(),
                command.copyright,
                command.source.strip(),
                int(command.auto_comment),
                int(command.danmaku_backfill),
                filter_json,
                now,
                now,
            ),
        )
        return await self.get(room_id)

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
        if not title_template or len(title_template) > 500:
            raise InvalidRoomUploadPolicy('title template is invalid')
        if len(description_template) > 5000:
            raise InvalidRoomUploadPolicy('description template is too long')
        try:
            self._liquid.from_string(title_template)
            self._liquid.from_string(description_template)
            self._liquid.from_string(command.source)
        except Exception as error:
            raise InvalidRoomUploadPolicy('template syntax is invalid') from error
        if command.tid <= 0:
            raise InvalidRoomUploadPolicy('tid must be positive')
        if not command.tags.strip():
            raise InvalidRoomUploadPolicy('tags must not be empty')
        if command.copyright not in (1, 2):
            raise InvalidRoomUploadPolicy('copyright must be 1 or 2')
        if command.copyright == 2 and not command.source.strip():
            raise InvalidRoomUploadPolicy('source is required for reposted archives')
        if not isinstance(command.filters, Mapping):
            raise InvalidRoomUploadPolicy('filters must be an object')

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
            tid=int(row['tid']),
            tags=str(row['tags']),
            copyright=int(row['copyright']),
            source=str(row['source']),
            auto_comment=bool(row['auto_comment']),
            danmaku_backfill=bool(row['danmaku_backfill']),
            filters=filters,
            blocked_reason=blocked_reason,
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
        )
