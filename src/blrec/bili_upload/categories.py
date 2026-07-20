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
    'UploadCreationStatement',
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
class UploadCreationStatement:
    id: int
    content: str


@dataclass(frozen=True)
class UploadCategoryCatalogView:
    account_id: int
    credential_version: int
    fetched_at: int
    stale: bool
    categories: Tuple[UploadCategoryNode, ...]
    creation_statements: Tuple[UploadCreationStatement, ...]
    creation_statement_tip: str


@dataclass(frozen=True)
class _ResolvedAccount:
    id: int
    credential_version: int


@dataclass
class _RefreshGeneration:
    task: asyncio.Task[UploadCategoryCatalogView]
    completed_at: Optional[float] = None


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
        self._refresh_tasks: Dict[Tuple[int, int], _RefreshGeneration] = {}
        self._completed_refreshes: Dict[Tuple[int, int], _RefreshGeneration] = {}

    async def list(
        self,
        account_mode: str,
        account_id: Optional[int],
        *,
        force_refresh: bool = False,
    ) -> UploadCategoryCatalogView:
        requested_at = time.monotonic()
        account = await self._resolve_account(account_mode, account_id)
        cached = await self._cached(account.id)
        now = int(self._clock())
        key = (account.id, account.credential_version)
        task = self._refresh_for_request(key, requested_at)
        if task is not None:
            return await asyncio.shield(task)
        if not force_refresh and self._is_fresh(cached, account, now):
            assert cached is not None
            return cached

        lock = self._locks.setdefault(account.id, asyncio.Lock())
        async with lock:
            account = await self._resolve_account(account_mode, account_id)
            cached = await self._cached(account.id)
            now = int(self._clock())
            key = (account.id, account.credential_version)
            task = self._refresh_for_request(key, requested_at)
            if (
                task is None
                and not force_refresh
                and self._is_fresh(cached, account, now)
            ):
                assert cached is not None
                return cached
            if task is None:
                task = self._start_refresh(key, account, cached, now)
            return await asyncio.shield(task)

    def _refresh_for_request(
        self, key: Tuple[int, int], requested_at: float
    ) -> Optional[asyncio.Task[UploadCategoryCatalogView]]:
        completed = self._completed_refreshes.get(key)
        if (
            completed is not None
            and completed.completed_at is not None
            and requested_at <= completed.completed_at
        ):
            return completed.task
        active = self._refresh_tasks.get(key)
        return None if active is None else active.task

    def _start_refresh(
        self,
        key: Tuple[int, int],
        account: _ResolvedAccount,
        cached: Optional[UploadCategoryCatalogView],
        now: int,
    ) -> asyncio.Task[UploadCategoryCatalogView]:
        task = asyncio.create_task(self._refresh(account, cached, now))
        generation = _RefreshGeneration(task=task)
        self._refresh_tasks[key] = generation

        def finish_refresh(
            completed: asyncio.Future[UploadCategoryCatalogView],
        ) -> None:
            generation.completed_at = time.monotonic()
            if self._refresh_tasks.get(key) is generation:
                self._refresh_tasks.pop(key, None)
                self._completed_refreshes[key] = generation
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finish_refresh)
        return task

    async def _refresh(
        self,
        account: _ResolvedAccount,
        cached: Optional[UploadCategoryCatalogView],
        now: int,
    ) -> UploadCategoryCatalogView:
        try:
            bundle = await self._bundle_loader(account.id)
            response = await self._protocol.archive_pre(bundle)
            categories, creation_statements, creation_statement_tip = self._normalize(
                response
            )
        except Exception:
            if cached is not None:
                return UploadCategoryCatalogView(
                    account_id=cached.account_id,
                    credential_version=cached.credential_version,
                    fetched_at=cached.fetched_at,
                    stale=True,
                    categories=cached.categories,
                    creation_statements=cached.creation_statements,
                    creation_statement_tip=cached.creation_statement_tip,
                )
            raise UploadCategoryUnavailable(
                'upload categories are unavailable'
            ) from None

        payload_json = json.dumps(
            {
                'format_version': 2,
                'categories': [asdict(category) for category in categories],
                'creation_statements': [
                    asdict(statement) for statement in creation_statements
                ],
                'creation_statement_tip': creation_statement_tip,
            },
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )
        await self._database.execute(
            'INSERT INTO upload_category_cache('
            'account_id,credential_version,payload_json,fetched_at) '
            'SELECT ?,?,?,? WHERE EXISTS('
            'SELECT 1 FROM bili_accounts WHERE id=? AND credential_version=?'
            ') ON CONFLICT(account_id) DO UPDATE SET '
            'credential_version=excluded.credential_version,'
            'payload_json=excluded.payload_json,fetched_at=excluded.fetched_at',
            (
                account.id,
                account.credential_version,
                payload_json,
                now,
                account.id,
                account.credential_version,
            ),
        )
        return UploadCategoryCatalogView(
            account_id=account.id,
            credential_version=account.credential_version,
            fetched_at=now,
            stale=False,
            categories=categories,
            creation_statements=creation_statements,
            creation_statement_tip=creation_statement_tip,
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
            if document.get('format_version') != 2:
                return None
            raw_categories = document['categories']
            categories = tuple(self._decode_node(value) for value in raw_categories)
            creation_statements = tuple(
                self._decode_statement(value)
                for value in document['creation_statements']
            )
            creation_statement_tip = document['creation_statement_tip']
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            not categories
            or not creation_statements
            or not isinstance(creation_statement_tip, str)
        ):
            return None
        return UploadCategoryCatalogView(
            account_id=account_id,
            credential_version=int(row['credential_version']),
            fetched_at=int(row['fetched_at']),
            stale=False,
            categories=categories,
            creation_statements=creation_statements,
            creation_statement_tip=creation_statement_tip,
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
    def _normalize(
        cls, response: Mapping[str, Any]
    ) -> Tuple[
        Tuple[UploadCategoryNode, ...], Tuple[UploadCreationStatement, ...], str
    ]:
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
        neutral_mark = data.get('neutral_mark')
        if not isinstance(neutral_mark, Mapping):
            raise UploadCategoryUnavailable('creation statements are unavailable')
        raw_statements = neutral_mark.get('mark_list')
        if not isinstance(raw_statements, list):
            raise UploadCategoryUnavailable('creation statements are unavailable')
        creation_statements = []
        seen_ids = set()
        for value in raw_statements:
            if not isinstance(value, Mapping):
                continue
            statement_id = value.get('id')
            content = value.get('content')
            if (
                type(statement_id) is not int
                or statement_id in seen_ids
                or not isinstance(content, str)
                or not content.strip()
            ):
                continue
            seen_ids.add(statement_id)
            creation_statements.append(
                UploadCreationStatement(id=statement_id, content=content.strip())
            )
        if not creation_statements:
            raise UploadCategoryUnavailable('creation statements are unavailable')
        tip = neutral_mark.get('tips', '')
        if not isinstance(tip, str):
            tip = ''
        return tuple(categories), tuple(creation_statements), tip.strip()

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

    @staticmethod
    def _decode_statement(value: Any) -> UploadCreationStatement:
        if not isinstance(value, Mapping) or set(value) != {'id', 'content'}:
            raise ValueError('invalid cached creation statement')
        statement_id = value['id']
        content = value['content']
        if type(statement_id) is not int or not isinstance(content, str) or not content:
            raise ValueError('invalid cached creation statement')
        return UploadCreationStatement(id=statement_id, content=content)
