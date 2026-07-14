from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from .database import BiliUploadDatabase

__all__ = (
    'InvalidUploadCategoryRequest',
    'UploadCategoryCatalog',
    'UploadCategoryCatalogView',
    'UploadCategoryNode',
    'UploadCategoryUnavailable',
)


class InvalidUploadCategoryRequest(RuntimeError):
    pass


class UploadCategoryUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class UploadCategoryNode:
    id: int
    name: str
    description: str
    children: Tuple['UploadCategoryNode', ...]


@dataclass(frozen=True)
class UploadCategoryCatalogView:
    account_id: int
    credential_version: int
    fetched_at: int
    stale: bool
    categories: Tuple[UploadCategoryNode, ...]


@dataclass(frozen=True)
class _ResolvedAccount:
    id: int
    credential_version: int


class UploadCategoryCatalog:
    def __init__(
        self,
        database: BiliUploadDatabase,
        protocol: Any,
        *,
        bundle_loader: Callable[[int], Awaitable[Any]],
        clock: Callable[[], float] = time.time,
        ttl_seconds: int = 24 * 60 * 60,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError('category cache TTL must be positive')
        self._database = database
        self._protocol = protocol
        self._bundle_loader = bundle_loader
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._locks: Dict[int, asyncio.Lock] = {}

    async def list(
        self,
        account_mode: str,
        account_id: Optional[int],
        *,
        force_refresh: bool = False,
    ) -> UploadCategoryCatalogView:
        account = await self._resolve_account(account_mode, account_id)
        cached = await self._cached(account.id)
        now = int(self._clock())
        if not force_refresh and self._is_fresh(cached, account, now):
            assert cached is not None
            return cached

        lock = self._locks.setdefault(account.id, asyncio.Lock())
        async with lock:
            account = await self._resolve_account(account_mode, account_id)
            cached = await self._cached(account.id)
            now = int(self._clock())
            if not force_refresh and self._is_fresh(cached, account, now):
                assert cached is not None
                return cached
            try:
                bundle = await self._bundle_loader(account.id)
                response = await self._protocol.archive_pre(bundle)
                categories = self._normalize(response)
            except Exception:
                if cached is not None:
                    return UploadCategoryCatalogView(
                        account_id=cached.account_id,
                        credential_version=cached.credential_version,
                        fetched_at=cached.fetched_at,
                        stale=True,
                        categories=cached.categories,
                    )
                raise UploadCategoryUnavailable(
                    'upload categories are unavailable'
                ) from None

            payload_json = json.dumps(
                {'categories': [asdict(category) for category in categories]},
                ensure_ascii=False,
                separators=(',', ':'),
                sort_keys=True,
            )
            await self._database.execute(
                'INSERT INTO upload_category_cache('
                'account_id,credential_version,payload_json,fetched_at) '
                'VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET '
                'credential_version=excluded.credential_version,'
                'payload_json=excluded.payload_json,fetched_at=excluded.fetched_at',
                (account.id, account.credential_version, payload_json, now),
            )
            return UploadCategoryCatalogView(
                account_id=account.id,
                credential_version=account.credential_version,
                fetched_at=now,
                stale=False,
                categories=categories,
            )

    async def _resolve_account(
        self, account_mode: str, account_id: Optional[int]
    ) -> _ResolvedAccount:
        if account_mode == 'primary':
            if account_id is not None:
                raise InvalidUploadCategoryRequest(
                    'accountId must be empty when following the primary account'
                )
            row = await self._database.fetchone(
                'SELECT account.id,account.state,account.credential_version '
                'FROM bili_account_selection selection JOIN bili_accounts account '
                'ON account.id=selection.primary_account_id WHERE selection.id=1'
            )
        elif account_mode == 'fixed':
            if account_id is None or account_id <= 0:
                raise InvalidUploadCategoryRequest(
                    'accountId is required for a fixed account policy'
                )
            row = await self._database.fetchone(
                'SELECT id,state,credential_version FROM bili_accounts WHERE id=?',
                (account_id,),
            )
        else:
            raise InvalidUploadCategoryRequest('accountMode must be primary or fixed')
        if row is None or str(row['state']) != 'active':
            raise InvalidUploadCategoryRequest('an active upload account is required')
        return _ResolvedAccount(
            id=int(row['id']), credential_version=int(row['credential_version'])
        )

    async def _cached(self, account_id: int) -> Optional[UploadCategoryCatalogView]:
        row = await self._database.fetchone(
            'SELECT credential_version,payload_json,fetched_at '
            'FROM upload_category_cache WHERE account_id=?',
            (account_id,),
        )
        if row is None:
            return None
        try:
            document = json.loads(str(row['payload_json']))
            raw_categories = document['categories']
            categories = tuple(self._decode_node(value) for value in raw_categories)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not categories:
            return None
        return UploadCategoryCatalogView(
            account_id=account_id,
            credential_version=int(row['credential_version']),
            fetched_at=int(row['fetched_at']),
            stale=False,
            categories=categories,
        )

    def _is_fresh(
        self,
        cached: Optional[UploadCategoryCatalogView],
        account: _ResolvedAccount,
        now: int,
    ) -> bool:
        return bool(
            cached is not None
            and cached.credential_version == account.credential_version
            and 0 <= now - cached.fetched_at < self._ttl_seconds
        )

    @classmethod
    def _normalize(cls, response: Mapping[str, Any]) -> Tuple[UploadCategoryNode, ...]:
        data = response.get('data')
        if not isinstance(data, Mapping):
            raise UploadCategoryUnavailable('upload categories are unavailable')
        values = data.get('typelist')
        if not isinstance(values, list):
            raise UploadCategoryUnavailable('upload categories are unavailable')
        categories = []
        for value in values:
            parent = cls._normalize_node(value, require_children=True)
            if parent is not None:
                categories.append(parent)
        if not categories:
            raise UploadCategoryUnavailable('upload categories are unavailable')
        return tuple(categories)

    @classmethod
    def _normalize_node(
        cls, value: Any, *, require_children: bool
    ) -> Optional[UploadCategoryNode]:
        if not isinstance(value, Mapping) or value.get('show') is False:
            return None
        category_id = value.get('id')
        name = value.get('name')
        if type(category_id) is not int or category_id <= 0:
            return None
        if not isinstance(name, str) or not name.strip():
            return None
        raw_children = value.get('children', [])
        if not isinstance(raw_children, list):
            return None
        children = tuple(
            child
            for child in (
                cls._normalize_node(item, require_children=False)
                for item in raw_children
            )
            if child is not None
        )
        if require_children and not children:
            return None
        description = value.get('desc', value.get('description', ''))
        if not isinstance(description, str):
            description = ''
        return UploadCategoryNode(
            id=category_id,
            name=name.strip(),
            description=description.strip(),
            children=children,
        )

    @classmethod
    def _decode_node(cls, value: Any) -> UploadCategoryNode:
        if not isinstance(value, Mapping) or set(value) != {
            'id',
            'name',
            'description',
            'children',
        }:
            raise ValueError('invalid cached category')
        category_id = value['id']
        name = value['name']
        description = value['description']
        children = value['children']
        if (
            type(category_id) is not int
            or category_id <= 0
            or not isinstance(name, str)
            or not name
            or not isinstance(description, str)
            or not isinstance(children, list)
        ):
            raise ValueError('invalid cached category')
        return UploadCategoryNode(
            id=category_id,
            name=name,
            description=description,
            children=tuple(cls._decode_node(child) for child in children),
        )
