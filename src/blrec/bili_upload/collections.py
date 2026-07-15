from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Tuple

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


class CollectionManager:
    def __init__(
        self,
        database: BiliUploadDatabase,
        protocol: Any,
        cover_resolver: CoverResolver,
        *,
        bundle_loader: Callable[[int], Awaitable[Any]],
    ) -> None:
        self._database = database
        self._protocol = protocol
        self._cover_resolver = cover_resolver
        self._bundle_loader = bundle_loader

    async def list(
        self, account_mode: str, account_id: Optional[int]
    ) -> CollectionCatalogView:
        account = await self._resolve_account(account_mode, account_id)
        try:
            bundle = await self._bundle_loader(account.id)
            response = await self._protocol.list_collections(bundle)
            collections = self._normalize(response)
        except Exception:
            raise CollectionUnavailable('collections are unavailable') from None
        return CollectionCatalogView(account_id=account.id, collections=collections)

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
            raise CollectionUnavailable(
                'collection creation result is unknown; refresh before trying again'
            ) from None
        except Exception:
            raise CollectionUnavailable('collection creation failed') from None
        collection_id = result.get('data')
        if type(collection_id) is not int or collection_id <= 0:
            raise CollectionUnavailable('collection creation response is incomplete')

        try:
            collections = self._normalize(await self._protocol.list_collections(bundle))
        except Exception:
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
                'SELECT account.id,account.state FROM bili_account_selection selection '
                'JOIN bili_accounts account '
                'ON account.id=selection.primary_account_id WHERE selection.id=1'
            )
        elif account_mode == 'fixed':
            if account_id is None or account_id <= 0:
                raise InvalidCollectionRequest(
                    'accountId is required for a fixed account policy'
                )
            row = await self._database.fetchone(
                'SELECT id,state FROM bili_accounts WHERE id=?', (account_id,)
            )
        else:
            raise InvalidCollectionRequest('accountMode must be primary or fixed')
        if row is None or str(row['state']) != 'active':
            raise InvalidCollectionRequest('an active upload account is required')
        return _ResolvedAccount(id=int(row['id']))

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
