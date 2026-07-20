import asyncio
from pathlib import Path
from typing import Any, Mapping

import pytest

from blrec.bili_upload.categories import (
    InvalidUploadCategoryRequest,
    UploadCategoryCatalog,
    UploadCategoryUnavailable,
)
from blrec.bili_upload.database import BiliUploadDatabase


class FakeProtocol:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.error: Exception = RuntimeError('category request failed')
        self.fail = False
        self.calls = []

    async def archive_pre(self, bundle: Any) -> Mapping[str, Any]:
        self.calls.append(bundle)
        if self.fail:
            raise self.error
        return self.response


class BlockingProtocol(FakeProtocol):
    def __init__(self, response: Mapping[str, Any]) -> None:
        super().__init__(response)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.in_flight = 0
        self.max_in_flight = 0

    async def archive_pre(self, bundle: Any) -> Mapping[str, Any]:
        self.calls.append(bundle)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.started.set()
        try:
            await self.release.wait()
            if self.fail:
                raise self.error
            return self.response
        finally:
            self.in_flight -= 1


def category_response(*, child_id: int = 17) -> Mapping[str, Any]:
    return {
        'code': 0,
        'data': {
            'neutral_mark': {
                'tips': '请按内容选择创作声明',
                'mark_list': [
                    {'id': -1, 'content': '内容无需标注'},
                    {'id': 1, 'content': '含 AI 生成内容'},
                    {'id': -2, 'content': '内容为转载'},
                ],
            },
            'typelist': [
                {
                    'id': 4,
                    'name': '游戏',
                    'children': [
                        {
                            'id': child_id,
                            'name': '单机游戏',
                            'desc': '以单机或主机游戏为主要内容',
                            'show': True,
                        },
                        {'id': 18, 'name': '隐藏分区', 'show': False},
                        {'id': 0, 'name': '无效分区', 'show': True},
                    ],
                }
            ],
        },
    }


async def seed_accounts(database: BiliUploadDatabase) -> None:
    for account_id, state in ((1, 'active'), (2, 'paused')):
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) VALUES(?,?,?,X\'00\',3,\'k\',?,?,?)',
            (account_id, 40 + account_id, '账号{}'.format(account_id), state, 1, 1),
        )
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )


@pytest.mark.asyncio
async def test_catalog_fetches_normalizes_and_reuses_fresh_cache(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = FakeProtocol(category_response())
        loaded = []

        async def load_bundle(account_id: int) -> Any:
            loaded.append(account_id)
            return 'bundle-{}'.format(account_id)

        catalog = UploadCategoryCatalog(
            database, protocol, bundle_loader=load_bundle, clock=lambda: 1000
        )

        first = await catalog.list('primary', None)
        second = await catalog.list('primary', None)

        assert first == second
        assert first.account_id == 1
        assert first.credential_version == 3
        assert first.fetched_at == 1000
        assert first.stale is False
        assert first.categories[0].id == 4
        assert first.categories[0].children[0].id == 17
        assert len(first.categories[0].children) == 1
        assert [statement.id for statement in first.creation_statements] == [-1, 1, -2]
        assert first.creation_statements[2].content == '内容为转载'
        assert first.creation_statement_tip == '请按内容选择创作声明'
        assert loaded == [1]
        assert protocol.calls == ['bundle-1']
        cached = await database.fetchone(
            'SELECT credential_version,payload_json,fetched_at '
            'FROM upload_category_cache WHERE account_id=1'
        )
        assert cached is not None
        assert int(cached['credential_version']) == 3
        assert 'typelist' not in str(cached['payload_json'])
        assert '"format_version":2' in str(cached['payload_json'])
        assert int(cached['fetched_at']) == 1000
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_catalog_coalesces_concurrent_normal_and_forced_generations(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = BlockingProtocol(category_response())

        async def load_bundle(account_id: int) -> Any:
            return 'bundle-{}'.format(account_id)

        catalog = UploadCategoryCatalog(
            database, protocol, bundle_loader=load_bundle, clock=lambda: 1000
        )

        normal = asyncio.gather(*(catalog.list('primary', None) for _ in range(20)))
        await asyncio.wait_for(protocol.started.wait(), timeout=1)
        assert protocol.calls == ['bundle-1']
        protocol.release.set()
        normal_results = await normal

        assert all(result.stale is False for result in normal_results)
        protocol.started = asyncio.Event()
        protocol.release = asyncio.Event()
        forced = asyncio.gather(
            *(catalog.list('primary', None, force_refresh=True) for _ in range(20))
        )
        await asyncio.wait_for(protocol.started.wait(), timeout=1)
        assert len(protocol.calls) == 2
        protocol.release.set()
        forced_results = await forced

        assert all(result.stale is False for result in forced_results)
        assert len(protocol.calls) == 2
        assert protocol.max_in_flight == 1

        await catalog.list('primary', None, force_refresh=True)
        assert len(protocol.calls) == 3
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_catalog_failed_generation_is_evicted_for_a_later_retry(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = FakeProtocol(category_response())
        protocol.fail = True

        async def load_bundle(account_id: int) -> Any:
            return 'bundle-{}'.format(account_id)

        catalog = UploadCategoryCatalog(
            database, protocol, bundle_loader=load_bundle, clock=lambda: 1000
        )
        with pytest.raises(UploadCategoryUnavailable):
            await catalog.list('primary', None)

        protocol.fail = False
        result = await catalog.list('primary', None)

        assert result.account_id == 1
        assert protocol.calls == ['bundle-1', 'bundle-1']
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_catalog_refreshes_after_credential_change(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = FakeProtocol(category_response())

        async def load_bundle(account_id: int) -> Any:
            return 'bundle-{}'.format(account_id)

        catalog = UploadCategoryCatalog(
            database, protocol, bundle_loader=load_bundle, clock=lambda: 1000
        )
        await catalog.list('primary', None)
        await database.execute(
            'UPDATE bili_accounts SET credential_version=4 WHERE id=1'
        )
        protocol.response = category_response(child_id=171)

        refreshed = await catalog.list('primary', None)

        assert refreshed.credential_version == 4
        assert refreshed.categories[0].children[0].id == 171
        assert len(protocol.calls) == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_catalog_returns_stale_cache_when_forced_refresh_fails(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = FakeProtocol(category_response())

        async def load_bundle(account_id: int) -> Any:
            return 'bundle-{}'.format(account_id)

        catalog = UploadCategoryCatalog(
            database, protocol, bundle_loader=load_bundle, clock=lambda: 1000
        )
        await catalog.list('primary', None)
        protocol.fail = True

        fallback = await catalog.list('primary', None, force_refresh=True)

        assert fallback.stale is True
        assert fallback.categories[0].children[0].id == 17
        assert fallback.creation_statements[0].id == -1
        assert len(protocol.calls) == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_catalog_ignores_legacy_cache_without_creation_statements(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        await database.execute(
            'INSERT INTO upload_category_cache('
            'account_id,credential_version,payload_json,fetched_at) '
            "VALUES(1,3,'{\"categories\":[]}',1000)"
        )
        protocol = FakeProtocol(category_response())

        async def load_bundle(account_id: int) -> Any:
            return 'bundle-{}'.format(account_id)

        catalog = UploadCategoryCatalog(
            database, protocol, bundle_loader=load_bundle, clock=lambda: 1000
        )

        result = await catalog.list('primary', None)

        assert result.creation_statements[0].id == -1
        assert protocol.calls == ['bundle-1']
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_catalog_rejects_invalid_account_and_missing_upstream_data(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = FakeProtocol({'code': 0, 'data': {'typelist': 'invalid'}})

        async def load_bundle(account_id: int) -> Any:
            return 'bundle-{}'.format(account_id)

        catalog = UploadCategoryCatalog(
            database, protocol, bundle_loader=load_bundle, clock=lambda: 1000
        )

        with pytest.raises(InvalidUploadCategoryRequest, match='active'):
            await catalog.list('fixed', 2)
        with pytest.raises(InvalidUploadCategoryRequest, match='accountId'):
            await catalog.list('fixed', None)
        with pytest.raises(UploadCategoryUnavailable):
            await catalog.list('primary', None)
    finally:
        await database.close()
