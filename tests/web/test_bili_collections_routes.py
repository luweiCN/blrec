from dataclasses import dataclass
from typing import Iterator, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.bili_upload.collections import (
    CollectionCatalogView,
    CollectionCreationView,
    CollectionSectionView,
    CollectionUnavailable,
    CollectionView,
    InvalidCollectionRequest,
)
from blrec.web import security
from blrec.web.routers import bili_collections


def collection() -> CollectionView:
    return CollectionView(
        id=10,
        title='合集',
        description='简介',
        cover_url='https://archive.biliimg.com/cover.jpg',
        state=0,
        reject_reason='',
        selectable=True,
        sections=(CollectionSectionView(id=11, title='正片'),),
    )


@dataclass
class FakeManager:
    request: Optional[tuple] = None
    invalid: bool = False
    unavailable: bool = False

    async def list(
        self, account_mode: str, account_id: Optional[int]
    ) -> CollectionCatalogView:
        if self.invalid:
            raise InvalidCollectionRequest('invalid request')
        if self.unavailable:
            raise CollectionUnavailable('collections unavailable')
        self.request = ('list', account_mode, account_id)
        return CollectionCatalogView(account_id=7, collections=(collection(),))

    async def create(self, account_mode: str, account_id: Optional[int], **values):
        if self.invalid:
            raise InvalidCollectionRequest('invalid request')
        if self.unavailable:
            raise CollectionUnavailable('collections unavailable')
        self.request = ('create', account_mode, account_id, values)
        return CollectionCreationView(account_id=7, collection=collection())


@pytest.fixture
def manager() -> FakeManager:
    value = FakeManager()
    old_manager = bili_collections.manager
    old_reason = bili_collections.unavailable_reason
    bili_collections.manager = value  # type: ignore[assignment]
    bili_collections.unavailable_reason = None
    yield value
    bili_collections.manager = old_manager
    bili_collections.unavailable_reason = old_reason


@pytest.fixture
def client(manager: FakeManager) -> Iterator[TestClient]:
    old_key = security.api_key
    security.api_key = 'test-api-key'
    api = FastAPI()
    api.include_router(bili_collections.router, prefix='/api/v1')
    try:
        with TestClient(api) as value:
            yield value
    finally:
        security.api_key = old_key


def headers() -> dict:
    return {'x-api-key': 'test-api-key'}


def test_list_collections_uses_selected_account(
    client: TestClient, manager: FakeManager
) -> None:
    response = client.get(
        '/api/v1/bili-collections',
        params={'accountMode': 'fixed', 'accountId': 7},
        headers=headers(),
    )

    assert response.status_code == 200
    assert manager.request == ('list', 'fixed', 7)
    assert response.json()['collections'][0]['sections'] == [
        {'id': 11, 'title': '正片'}
    ]


def test_create_collection_forwards_cover_asset(
    client: TestClient, manager: FakeManager
) -> None:
    response = client.post(
        '/api/v1/bili-collections',
        headers=headers(),
        json={
            'accountMode': 'primary',
            'accountId': None,
            'title': '新合集',
            'description': '简介',
            'coverAssetId': 8,
        },
    )

    assert response.status_code == 201
    assert manager.request == (
        'create',
        'primary',
        None,
        {'title': '新合集', 'description': '简介', 'cover_asset_id': 8},
    )


@pytest.mark.parametrize(
    ('failure', 'status_code'), (('invalid', 409), ('unavailable', 503))
)
def test_collection_routes_map_domain_errors(
    client: TestClient, manager: FakeManager, failure: str, status_code: int
) -> None:
    setattr(manager, failure, True)

    response = client.get(
        '/api/v1/bili-collections', params={'accountMode': 'primary'}, headers=headers()
    )

    assert response.status_code == status_code
