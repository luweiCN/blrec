import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import pytest

import blrec.bili_upload.collections as collections_module
from blrec.bili_upload.accounts import AccountWriteGate
from blrec.bili_upload.collections import (
    CollectionManager,
    CollectionUnavailable,
    InvalidCollectionRequest,
)
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.errors import AccountWriteBusy, RemoteOutcomeUnknown


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
        self.create_error: Optional[Exception] = None

    async def list_collections(self, bundle: Any) -> Mapping[str, Any]:
        self.list_calls.append(bundle)
        if self.fail_list:
            raise RuntimeError('list failed')
        return response(include_created=self.include_created)

    async def create_collection(self, bundle: Any, **values: Any) -> Mapping[str, Any]:
        self.create_calls.append((bundle, values))
        if self.create_error is not None:
            raise self.create_error
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
    database: BiliUploadDatabase,
    protocol: FakeProtocol,
    resolver: FakeCoverResolver,
    *,
    clock: Callable[[], float] = lambda: 1000,
    account_gates: Optional[AccountWriteGate] = None,
) -> CollectionManager:
    async def load_bundle(account_id: int) -> str:
        return 'bundle-{}'.format(account_id)

    return CollectionManager(
        database,
        protocol,
        resolver,
        bundle_loader=load_bundle,
        account_gates=account_gates or AccountWriteGate(database),
        clock=clock,
    )


class BlockingListProtocol(FakeProtocol):
    def __init__(self) -> None:
        super().__init__()
        self.list_started = asyncio.Event()
        self.list_release = asyncio.Event()
        self.in_flight = 0
        self.max_in_flight = 0

    async def list_collections(self, bundle: Any) -> Mapping[str, Any]:
        self.list_calls.append(bundle)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.list_started.set()
            await self.list_release.wait()
            if self.fail_list:
                raise RuntimeError('list failed')
            return response(include_created=self.include_created)
        finally:
            self.in_flight -= 1


class VersionedListProtocol(FakeProtocol):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = asyncio.Event()
        self.first_release = asyncio.Event()
        self.second_started = asyncio.Event()

    async def list_collections(self, bundle: Any) -> Mapping[str, Any]:
        self.list_calls.append(bundle)
        if len(self.list_calls) == 1:
            self.first_started.set()
            await self.first_release.wait()
        else:
            self.second_started.set()
        return response(include_created=self.include_created)


class PreCreateListProtocol(FakeProtocol):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = asyncio.Event()
        self.first_release = asyncio.Event()
        self.create_finished = asyncio.Event()

    async def list_collections(self, bundle: Any) -> Mapping[str, Any]:
        self.list_calls.append(bundle)
        snapshot = response(include_created=self.include_created)
        if len(self.list_calls) == 1:
            self.first_started.set()
            await self.first_release.wait()
        return snapshot

    async def create_collection(self, bundle: Any, **values: Any) -> Mapping[str, Any]:
        result = await super().create_collection(bundle, **values)
        self.create_finished.set()
        return result


class BlockingWriteProtocol(FakeProtocol):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = asyncio.Event()
        self.other_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.entered_bundles = []
        self.active_by_bundle = {}
        self.max_active_by_bundle = {}
        self.active_total = 0
        self.max_active_total = 0

    async def create_collection(self, bundle: Any, **values: Any) -> Mapping[str, Any]:
        self.entered_bundles.append(bundle)
        self.active_by_bundle[bundle] = self.active_by_bundle.get(bundle, 0) + 1
        self.max_active_by_bundle[bundle] = max(
            self.max_active_by_bundle.get(bundle, 0), self.active_by_bundle[bundle]
        )
        self.active_total += 1
        self.max_active_total = max(self.max_active_total, self.active_total)
        try:
            if bundle == 'bundle-1' and self.entered_bundles.count(bundle) == 1:
                self.first_started.set()
                await self.release_first.wait()
            if bundle == 'bundle-2':
                self.other_started.set()
            return await super().create_collection(bundle, **values)
        finally:
            self.active_by_bundle[bundle] -= 1
            self.active_total -= 1


class PostCreateRaceProtocol(FakeProtocol):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = asyncio.Event()
        self.first_release = asyncio.Event()
        self.third_started = asyncio.Event()
        self.release_stale = asyncio.Event()
        self.create_finished = asyncio.Event()
        self.in_flight = 0
        self.max_in_flight = 0

    async def list_collections(self, bundle: Any) -> Mapping[str, Any]:
        self.list_calls.append(bundle)
        call = len(self.list_calls)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if call == 1:
                self.first_started.set()
                await self.first_release.wait()
                return response(include_created=False)
            if call == 2:
                for _index in range(100):
                    if self.third_started.is_set():
                        await self.release_stale.wait()
                        return response(include_created=False)
                    await asyncio.sleep(0)
                return response(include_created=True)
            if call == 3:
                self.third_started.set()
                return response(include_created=True)
            raise AssertionError('unexpected collection list call')
        finally:
            self.in_flight -= 1

    async def create_collection(self, bundle: Any, **values: Any) -> Mapping[str, Any]:
        result = await super().create_collection(bundle, **values)
        self.create_finished.set()
        return result


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
async def test_collection_list_coalesces_concurrent_normal_and_forced_refreshes(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = BlockingListProtocol()
        catalog = manager(database, protocol, FakeCoverResolver())

        normal = asyncio.gather(*(catalog.list('fixed', 1) for _ in range(20)))
        await asyncio.wait_for(protocol.list_started.wait(), timeout=1)
        assert protocol.list_calls == ['bundle-1']
        protocol.list_release.set()
        normal_results = await normal

        assert {result.account_id for result in normal_results} == {1}
        protocol.list_started = asyncio.Event()
        protocol.list_release = asyncio.Event()
        forced = asyncio.gather(
            *(catalog.list('fixed', 1, force_refresh=True) for _ in range(20))
        )
        await asyncio.wait_for(protocol.list_started.wait(), timeout=1)
        assert protocol.list_calls == ['bundle-1', 'bundle-1']
        protocol.list_release.set()
        forced_results = await forced

        assert {result.account_id for result in forced_results} == {1}
        assert len(protocol.list_calls) == 2
        assert protocol.max_in_flight == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_cache_expires_and_is_scoped_to_credential_version(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    now = [1000.0]
    try:
        await seed_accounts(database)
        protocol = FakeProtocol()
        catalog = manager(database, protocol, FakeCoverResolver(), clock=lambda: now[0])

        first = await catalog.list('fixed', 1)
        cached = await catalog.list('fixed', 1)
        now[0] += 61
        expired = await catalog.list('fixed', 1)
        await database.execute(
            'UPDATE bili_accounts SET credential_version=2 WHERE id=1'
        )
        credential_changed = await catalog.list('fixed', 1)

        assert first == cached
        assert expired.account_id == credential_changed.account_id == 1
        assert protocol.list_calls == ['bundle-1', 'bundle-1', 'bundle-1']
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('switch_primary', (False, True))
async def test_collection_refresh_uses_resolved_account_generation(
    tmp_path: Path, switch_primary: bool
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = VersionedListProtocol()
        catalog = manager(database, protocol, FakeCoverResolver())
        mode = 'primary' if switch_primary else 'fixed'
        account_id = None if switch_primary else 1
        old = asyncio.create_task(catalog.list(mode, account_id, force_refresh=True))
        await asyncio.wait_for(protocol.first_started.wait(), timeout=1)
        if switch_primary:
            await database.execute(
                'UPDATE bili_account_selection SET primary_account_id=2 WHERE id=1'
            )
        else:
            await database.execute(
                'UPDATE bili_accounts SET credential_version=2 WHERE id=1'
            )

        new = asyncio.create_task(catalog.list(mode, account_id, force_refresh=True))
        try:
            await asyncio.wait_for(protocol.second_started.wait(), timeout=1)
            new_result = await new
        finally:
            protocol.first_release.set()
            await asyncio.gather(old, new, return_exceptions=True)

        assert new_result.account_id == (2 if switch_primary else 1)
        assert len(protocol.list_calls) == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_cache_is_stale_for_at_most_fifteen_minutes(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    now = [1000.0]
    try:
        await seed_accounts(database)
        protocol = FakeProtocol()
        catalog = manager(database, protocol, FakeCoverResolver(), clock=lambda: now[0])
        fresh = await catalog.list('fixed', 1)
        protocol.fail_list = True
        now[0] = 1061

        stale = await catalog.list('fixed', 1)

        assert stale == fresh
        now[0] = 1901
        with pytest.raises(CollectionUnavailable, match='unavailable'):
            await catalog.list('fixed', 1)
        assert len(protocol.list_calls) == 3
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_failed_refresh_is_evicted_for_a_later_retry(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = BlockingListProtocol()
        protocol.fail_list = True
        catalog = manager(database, protocol, FakeCoverResolver())

        failed = asyncio.gather(
            *(catalog.list('fixed', 1) for _ in range(20)), return_exceptions=True
        )
        await asyncio.wait_for(protocol.list_started.wait(), timeout=1)
        assert len(protocol.list_calls) == 1
        protocol.list_release.set()
        errors = await failed
        assert all(isinstance(error, CollectionUnavailable) for error in errors)

        protocol.fail_list = False
        result = await catalog.list('fixed', 1)

        assert result.account_id == 1
        assert len(protocol.list_calls) == 2
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

        cached = await catalog.list('fixed', 2)

        assert any(item.id == created.collection.id for item in cached.collections)
        assert protocol.list_calls == ['bundle-2']
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_create_uses_timed_account_gate_admission(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        gates = AccountWriteGate(database)
        protocol = FakeProtocol()
        resolver = FakeCoverResolver()
        catalog = manager(database, protocol, resolver, account_gates=gates)

        async with gates.for_account(1).hold(1):
            with pytest.raises(AccountWriteBusy, match='busy'):
                await catalog.create(
                    'fixed',
                    1,
                    title='新合集',
                    description='',
                    cover_asset_id=7,
                    admission_timeout_seconds=0.01,
                    operation_timeout_seconds=60,
                )

        assert resolver.calls == []
        assert protocol.create_calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_create_serializes_one_account_but_not_another(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        gates = AccountWriteGate(database)
        protocol = BlockingWriteProtocol()
        catalog = manager(database, protocol, FakeCoverResolver(), account_gates=gates)
        first = asyncio.create_task(
            catalog.create(
                'fixed', 1, title='账号一 A', description='', cover_asset_id=7
            )
        )
        await asyncio.wait_for(protocol.first_started.wait(), timeout=1)
        second = asyncio.create_task(
            catalog.create(
                'fixed', 1, title='账号一 B', description='', cover_asset_id=7
            )
        )
        other = asyncio.create_task(
            catalog.create('fixed', 2, title='账号二', description='', cover_asset_id=7)
        )

        await asyncio.wait_for(protocol.other_started.wait(), timeout=1)
        await other
        assert protocol.entered_bundles.count('bundle-1') == 1
        assert protocol.max_active_total == 2
        protocol.release_first.set()
        await asyncio.gather(first, second)

        assert protocol.entered_bundles.count('bundle-1') == 2
        assert protocol.max_active_by_bundle['bundle-1'] == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_create_installs_one_operation_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    deadlines = []

    @contextmanager
    def capture_deadline(seconds: float):
        deadlines.append(seconds)
        yield

    monkeypatch.setattr(
        collections_module, 'protocol_request_deadline', capture_deadline
    )
    try:
        await seed_accounts(database)
        await manager(database, FakeProtocol(), FakeCoverResolver()).create(
            'fixed',
            1,
            title='新合集',
            description='',
            cover_asset_id=7,
            operation_timeout_seconds=0.01,
        )

        assert deadlines == [0.01]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_create_starts_a_new_refresh_after_an_older_list(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = PreCreateListProtocol()
        catalog = manager(database, protocol, FakeCoverResolver())
        old_list = asyncio.create_task(catalog.list('fixed', 1, force_refresh=True))
        await asyncio.wait_for(protocol.first_started.wait(), timeout=1)

        creation = asyncio.create_task(
            catalog.create(
                'fixed', 1, title='新合集', description='新简介', cover_asset_id=7
            )
        )
        await asyncio.wait_for(protocol.create_finished.wait(), timeout=1)
        protocol.first_release.set()
        await old_list
        created = await creation

        assert len(protocol.create_calls) == 1
        assert len(protocol.list_calls) == 2
        assert created.collection.id == 20
        assert created.collection.sections[0].id == 21
        cached = await catalog.list('fixed', 1)
        assert [item.id for item in cached.collections] == [10, 20]
        assert len(protocol.list_calls) == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_post_create_generation_cannot_be_overwritten_by_an_older_force(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = PostCreateRaceProtocol()
        catalog = manager(database, protocol, FakeCoverResolver())
        old_list = asyncio.create_task(catalog.list('fixed', 1, force_refresh=True))
        await asyncio.wait_for(protocol.first_started.wait(), timeout=1)
        first_refresh = catalog._refresh_tasks[(1, 1)].task
        original_resolve = catalog._resolve_account
        resolved_account = await original_resolve('fixed', 1)

        async def immediate_resolve(mode: str, account_id: int) -> Any:
            return resolved_account

        catalog._resolve_account = immediate_resolve
        forced_tasks = []

        def start_forced_list(_completed: Any) -> None:
            forced_tasks.append(
                asyncio.create_task(catalog.list('fixed', 1, force_refresh=True))
            )

        first_refresh.add_done_callback(start_forced_list)
        creation = asyncio.create_task(
            catalog.create('fixed', 1, title='新合集', description='', cover_asset_id=7)
        )
        await asyncio.wait_for(protocol.create_finished.wait(), timeout=1)
        await asyncio.sleep(0)
        protocol.first_release.set()
        created = await asyncio.wait_for(creation, timeout=1)
        protocol.release_stale.set()
        await asyncio.wait_for(old_list, timeout=1)
        await asyncio.wait_for(forced_tasks[0], timeout=1)

        cached = await catalog.list('fixed', 1)
        assert created.collection.sections[0].id == 21
        assert [item.id for item in cached.collections] == [10, 20]
        assert len(protocol.list_calls) == 2
        assert protocol.max_in_flight == 1
    finally:
        protocol.first_release.set()
        protocol.release_stale.set()
        await database.close()


@pytest.mark.asyncio
async def test_collection_unknown_create_invalidates_without_reconciliation(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = FakeProtocol()
        resolver = FakeCoverResolver()
        catalog = manager(database, protocol, resolver)
        await catalog.list('fixed', 1)
        protocol.create_error = RemoteOutcomeUnknown('create_collection')

        with pytest.raises(CollectionUnavailable, match='unknown'):
            await catalog.create(
                'fixed', 1, title='结果未知', description='', cover_asset_id=7
            )

        assert len(protocol.create_calls) == 1
        assert len(protocol.list_calls) == 1
        protocol.create_error = None
        await catalog.list('fixed', 1)
        assert len(protocol.list_calls) == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_unknown_create_prevents_an_older_list_reinstalling_cache(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        protocol = PreCreateListProtocol()
        protocol.create_error = RemoteOutcomeUnknown('create_collection')
        catalog = manager(database, protocol, FakeCoverResolver())
        old_list = asyncio.create_task(catalog.list('fixed', 1, force_refresh=True))
        await asyncio.wait_for(protocol.first_started.wait(), timeout=1)

        with pytest.raises(CollectionUnavailable, match='unknown'):
            await catalog.create(
                'fixed', 1, title='结果未知', description='', cover_asset_id=7
            )
        protocol.first_release.set()
        await old_list

        protocol.create_error = None
        await catalog.list('fixed', 1)
        assert len(protocol.create_calls) == 1
        assert len(protocol.list_calls) == 2
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
