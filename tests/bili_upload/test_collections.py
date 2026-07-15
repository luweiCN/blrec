from pathlib import Path
from typing import Any, Mapping

import pytest

from blrec.bili_upload.collections import (
    CollectionManager,
    CollectionUnavailable,
    InvalidCollectionRequest,
)
from blrec.bili_upload.database import BiliUploadDatabase


def response(*, include_created: bool = False) -> Mapping[str, Any]:
    seasons = [
        {
            'season': {
                'id': 10,
                'title': '已有合集',
                'desc': '简介',
                'cover': 'https://archive.biliimg.com/existing.jpg',
                'state': 0,
                'rejectReason': '',
            },
            'sections': {
                'sections': [{'id': 11, 'title': '正片'}, {'id': 0, 'title': '无效'}]
            },
        },
        {'season': {'id': 'bad', 'title': '无效'}, 'sections': {}},
    ]
    if include_created:
        seasons.append(
            {
                'season': {
                    'id': 20,
                    'title': '新合集',
                    'desc': '新简介',
                    'cover': 'https://archive.biliimg.com/new.jpg',
                    'state': -6,
                    'rejectReason': '',
                },
                'sections': {'sections': [{'id': 21, 'title': '正片'}]},
            }
        )
    return {'code': 0, 'data': {'seasons': seasons}}


class FakeProtocol:
    def __init__(self) -> None:
        self.include_created = False
        self.list_calls = []
        self.create_calls = []
        self.fail_list = False

    async def list_collections(self, bundle: Any) -> Mapping[str, Any]:
        self.list_calls.append(bundle)
        if self.fail_list:
            raise RuntimeError('list failed')
        return response(include_created=self.include_created)

    async def create_collection(self, bundle: Any, **values: Any) -> Mapping[str, Any]:
        self.create_calls.append((bundle, values))
        self.include_created = True
        return {'code': 0, 'data': 20}


class FakeCoverResolver:
    def __init__(self) -> None:
        self.calls = []

    async def remote_url(self, asset_id: int, account_id: int) -> str:
        self.calls.append((asset_id, account_id))
        return 'https://archive.biliimg.com/cover-{}-{}.jpg'.format(
            asset_id, account_id
        )


async def seed_accounts(database: BiliUploadDatabase) -> None:
    for account_id, state in ((1, 'active'), (2, 'active'), (3, 'paused')):
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) VALUES(?,?,?,X\'00\',1,\'k\',?,?,?)',
            (account_id, 40 + account_id, '账号{}'.format(account_id), state, 1, 1),
        )
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )


def manager(
    database: BiliUploadDatabase, protocol: FakeProtocol, resolver: FakeCoverResolver
) -> CollectionManager:
    async def load_bundle(account_id: int) -> str:
        return 'bundle-{}'.format(account_id)

    return CollectionManager(database, protocol, resolver, bundle_loader=load_bundle)


@pytest.mark.asyncio
async def test_collection_list_is_scoped_to_the_resolved_account(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = FakeProtocol()
        catalog = manager(database, protocol, FakeCoverResolver())

        primary = await catalog.list('primary', None)
        fixed = await catalog.list('fixed', 2)

        assert primary.account_id == 1
        assert fixed.account_id == 2
        assert primary.collections[0].id == 10
        assert primary.collections[0].sections[0].id == 11
        assert primary.collections[0].selectable is True
        assert protocol.list_calls == ['bundle-1', 'bundle-2']
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_create_uploads_cover_and_refreshes_new_default_section(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = FakeProtocol()
        resolver = FakeCoverResolver()
        catalog = manager(database, protocol, resolver)

        created = await catalog.create(
            'fixed', 2, title=' 新合集 ', description=' 新简介 ', cover_asset_id=7
        )

        assert created.account_id == 2
        assert created.collection.id == 20
        assert created.collection.sections[0].id == 21
        assert created.collection.state == -6
        assert created.collection.selectable is False
        assert resolver.calls == [(7, 2)]
        assert protocol.create_calls == [
            (
                'bundle-2',
                {
                    'title': '新合集',
                    'description': '新简介',
                    'cover_url': 'https://archive.biliimg.com/cover-7-2.jpg',
                },
            )
        ]
        assert protocol.list_calls == ['bundle-2']
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_create_returns_pending_result_when_refresh_cannot_see_it(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = FakeProtocol()

        async def create_without_visibility(
            bundle: Any, **values: Any
        ) -> Mapping[str, Any]:
            return {'code': 0, 'data': 99}

        setattr(protocol, 'create_collection', create_without_visibility)
        catalog = manager(database, protocol, FakeCoverResolver())

        created = await catalog.create(
            'primary', None, title='等待审核', description='', cover_asset_id=7
        )

        assert created.collection.id == 99
        assert created.collection.state == -6
        assert created.collection.sections == ()
        assert created.collection.selectable is False
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('mode', 'account_id', 'title', 'cover_asset_id'),
    (
        ('fixed', 3, '合集', 7),
        ('primary', 1, '合集', 7),
        ('invalid', None, '合集', 7),
        ('primary', None, ' ', 7),
        ('primary', None, '合集', 0),
    ),
)
async def test_collection_requests_reject_invalid_account_or_content(
    tmp_path: Path, mode: str, account_id: Any, title: str, cover_asset_id: int
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        catalog = manager(database, FakeProtocol(), FakeCoverResolver())

        with pytest.raises(InvalidCollectionRequest):
            await catalog.create(
                mode,
                account_id,
                title=title,
                description='',
                cover_asset_id=cover_asset_id,
            )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_list_hides_upstream_failure(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = FakeProtocol()
        protocol.fail_list = True

        with pytest.raises(CollectionUnavailable, match='unavailable'):
            await manager(database, protocol, FakeCoverResolver()).list('primary', None)
    finally:
        await database.close()
