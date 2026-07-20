from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from .covers import CoverAssetNotFound, CoverResolver, StoredCoverUnavailable
from .database import BiliUploadDatabase
from .errors import RemoteOutcomeUnknown

__all__ = (
    'CollectionCatalogView',
    'CollectionCreationView',
    'CollectionManager',
    'CollectionSectionView',
    'CollectionUnavailable',
    'CollectionView',
    'InvalidCollectionRequest',
)


class InvalidCollectionRequest(RuntimeError):
    pass


class CollectionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CollectionSectionView:
    id: int
    title: str


@dataclass(frozen=True)
class CollectionView:
    id: int
    title: str
    description: str
    cover_url: str
    state: int
    reject_reason: str
    selectable: bool
    sections: Tuple[CollectionSectionView, ...]


@dataclass(frozen=True)
class CollectionCatalogView:
    account_id: int
    collections: Tuple[CollectionView, ...]


@dataclass(frozen=True)
class CollectionCreationView:
    account_id: int
    collection: CollectionView


@dataclass(frozen=True)
class _ResolvedAccount:
    id: int
    credential_version: int


@dataclass(frozen=True)
class _CatalogEntry:
    catalog: CollectionCatalogView
    fresh_until: float
    stale_until: float


@dataclass
class _RefreshGeneration:
    task: asyncio.Task[CollectionCatalogView]
    completed_at: Optional[float] = None


class CollectionManager:
    def __init__(
        self,
        database: BiliUploadDatabase,
        protocol: Any,
        cover_resolver: CoverResolver,
        *,
        bundle_loader: Callable[[int], Awaitable[Any]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._database = database
        self._protocol = protocol
        self._cover_resolver = cover_resolver
        self._bundle_loader = bundle_loader
        self._clock = clock
        self._catalogs: Dict[Tuple[int, int], _CatalogEntry] = {}
        self._refresh_tasks: Dict[Tuple[int, int], _RefreshGeneration] = {}
        self._completed_refreshes: Dict[Tuple[int, int], _RefreshGeneration] = {}
        self._cache_epochs: Dict[Tuple[int, int], int] = {}

    async def list(
        self,
        account_mode: str,
        account_id: Optional[int],
        *,
        force_refresh: bool = False,
    ) -> CollectionCatalogView:
        requested_at = time.monotonic()
        account = await self._resolve_account(account_mode, account_id)
        key = (account.id, account.credential_version)
        task = self._refresh_for_request(key, requested_at)
        if task is not None:
            return await asyncio.shield(task)
        current = self._catalogs.get(key)
        if (
            not force_refresh
            and current is not None
            and self._clock() < current.fresh_until
        ):
            return current.catalog
        task = self._start_refresh(key, account, stale=current)
        return await asyncio.shield(task)

    def _refresh_for_request(
        self, key: Tuple[int, int], requested_at: float
    ) -> Optional[asyncio.Task[CollectionCatalogView]]:
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
        *,
        stale: Optional[_CatalogEntry],
    ) -> asyncio.Task[CollectionCatalogView]:
        epoch = self._cache_epochs.get(key, 0)
        task = asyncio.create_task(
            self._load_catalog(key, account, stale=stale, cache_epoch=epoch)
        )
        generation = _RefreshGeneration(task=task)
        self._refresh_tasks[key] = generation

        def finish_refresh(completed: asyncio.Future[CollectionCatalogView]) -> None:
            generation.completed_at = time.monotonic()
            if self._refresh_tasks.get(key) is generation:
                self._refresh_tasks.pop(key, None)
                self._completed_refreshes[key] = generation
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finish_refresh)
        return task

    async def _load_catalog(
        self,
        key: Tuple[int, int],
        account: _ResolvedAccount,
        *,
        stale: Optional[_CatalogEntry],
        cache_epoch: int,
    ) -> CollectionCatalogView:
        try:
            bundle = await self._bundle_loader(account.id)
            response = await self._protocol.list_collections(bundle)
            collections = self._normalize(response)
        except Exception:
            if stale is not None and self._clock() < stale.stale_until:
                return stale.catalog
            raise CollectionUnavailable('collections are unavailable') from None
        catalog = CollectionCatalogView(account_id=account.id, collections=collections)
        now = self._clock()
        if self._cache_epochs.get(key, 0) == cache_epoch:
            self._catalogs[key] = _CatalogEntry(
                catalog=catalog, fresh_until=now + 60, stale_until=now + 15 * 60
            )
        return catalog

    async def create(
        self,
        account_mode: str,
        account_id: Optional[int],
        *,
        title: str,
        description: str,
        cover_asset_id: int,
    ) -> CollectionCreationView:
        title = title.strip()
        description = description.strip()
        if not title or len(title) > 100:
            raise InvalidCollectionRequest('collection title is invalid')
        if len(description) > 2000:
            raise InvalidCollectionRequest('collection description is too long')
        if type(cover_asset_id) is not int or cover_asset_id <= 0:
            raise InvalidCollectionRequest('collection cover is required')
        account = await self._resolve_account(account_mode, account_id)
        key = (account.id, account.credential_version)
        try:
            cover_url = await self._cover_resolver.remote_url(
                cover_asset_id, account.id
            )
        except (CoverAssetNotFound, StoredCoverUnavailable) as error:
            raise InvalidCollectionRequest(str(error)) from None
        except Exception:
            raise CollectionUnavailable('collection cover upload failed') from None

        try:
            bundle = await self._bundle_loader(account.id)
            result = await self._protocol.create_collection(
                bundle, title=title, description=description, cover_url=cover_url
            )
        except RemoteOutcomeUnknown:
            self._catalogs.pop(key, None)
            self._cache_epochs[key] = self._cache_epochs.get(key, 0) + 1
            self._refresh_tasks.pop(key, None)
            self._completed_refreshes.pop(key, None)
            raise CollectionUnavailable(
                'collection creation result is unknown; refresh before trying again'
            ) from None
        except Exception:
            raise CollectionUnavailable('collection creation failed') from None
        collection_id = result.get('data')
        if type(collection_id) is not int or collection_id <= 0:
            raise CollectionUnavailable('collection creation response is incomplete')

        previous = self._refresh_tasks.get(key)
        if previous is not None:
            try:
                await asyncio.shield(previous.task)
            except asyncio.CancelledError:
                if not previous.task.cancelled():
                    raise
            except Exception:
                pass
        stale = self._catalogs.pop(key, None)
        try:
            task = self._start_refresh(key, account, stale=stale)
            catalog = await asyncio.shield(task)
            collections = catalog.collections
        except CollectionUnavailable:
            collections = ()
        created = next(
            (
                collection
                for collection in collections
                if collection.id == collection_id
            ),
            None,
        )
        if created is None:
            created = CollectionView(
                id=collection_id,
                title=title,
                description=description,
                cover_url=cover_url,
                state=-6,
                reject_reason='',
                selectable=False,
                sections=(),
            )
        return CollectionCreationView(account_id=account.id, collection=created)

    async def _resolve_account(
        self, account_mode: str, account_id: Optional[int]
    ) -> _ResolvedAccount:
        if account_mode == 'primary':
            if account_id is not None:
                raise InvalidCollectionRequest(
                    'accountId must be empty when following the primary account'
                )
            row = await self._database.fetchone(
                'SELECT account.id,account.state,account.credential_version '
                'FROM bili_account_selection selection '
                'JOIN bili_accounts account '
                'ON account.id=selection.primary_account_id WHERE selection.id=1'
            )
        elif account_mode == 'fixed':
            if account_id is None or account_id <= 0:
                raise InvalidCollectionRequest(
                    'accountId is required for a fixed account policy'
                )
            row = await self._database.fetchone(
                'SELECT id,state,credential_version FROM bili_accounts WHERE id=?',
                (account_id,),
            )
        else:
            raise InvalidCollectionRequest('accountMode must be primary or fixed')
        if row is None or str(row['state']) != 'active':
            raise InvalidCollectionRequest('an active upload account is required')
        return _ResolvedAccount(
            id=int(row['id']), credential_version=int(row['credential_version'])
        )

    @classmethod
    def _normalize(cls, response: Mapping[str, Any]) -> Tuple[CollectionView, ...]:
        data = response.get('data')
        if not isinstance(data, Mapping):
            raise CollectionUnavailable('collections are unavailable')
        values = data.get('seasons')
        if not isinstance(values, list):
            raise CollectionUnavailable('collections are unavailable')
        collections = []
        for value in values:
            collection = cls._normalize_collection(value)
            if collection is not None:
                collections.append(collection)
        return tuple(collections)

    @staticmethod
    def _normalize_collection(value: Any) -> Optional[CollectionView]:
        if not isinstance(value, Mapping):
            return None
        season = value.get('season')
        if not isinstance(season, Mapping):
            return None
        collection_id = season.get('id')
        title = season.get('title')
        if (
            type(collection_id) is not int
            or collection_id <= 0
            or not isinstance(title, str)
            or not title.strip()
        ):
            return None
        sections_container = value.get('sections')
        raw_sections = (
            sections_container.get('sections', [])
            if isinstance(sections_container, Mapping)
            else []
        )
        if not isinstance(raw_sections, list):
            raw_sections = []
        sections = []
        for raw_section in raw_sections:
            if not isinstance(raw_section, Mapping):
                continue
            section_id = raw_section.get('id')
            section_title = raw_section.get('title')
            if (
                type(section_id) is int
                and section_id > 0
                and isinstance(section_title, str)
                and section_title.strip()
            ):
                sections.append(
                    CollectionSectionView(id=section_id, title=section_title.strip())
                )
        state = season.get('state', 0)
        if type(state) is not int:
            state = 0
        description = season.get('desc', '')
        cover_url = season.get('cover', '')
        reject_reason = season.get('rejectReason', '')
        return CollectionView(
            id=collection_id,
            title=title.strip(),
            description=description if isinstance(description, str) else '',
            cover_url=cover_url if isinstance(cover_url, str) else '',
            state=state,
            reject_reason=(reject_reason if isinstance(reject_reason, str) else ''),
            selectable=state == 0 and bool(sections),
            sections=tuple(sections),
        )
