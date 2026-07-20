from __future__ import annotations

import asyncio
from typing import Any, List, Mapping, Optional

import pytest

from blrec.bili_upload.archive_reads import ArchiveReadService


class MutableClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeProtocol:
    def __init__(self) -> None:
        self.list_calls: List[Mapping[str, Any]] = []
        self.detail_calls: List[str] = []
        self.list_gate: Optional[asyncio.Event] = None
        self.detail_gate: Optional[asyncio.Event] = None
        self.list_started = asyncio.Event()
        self.detail_started = asyncio.Event()
        self.list_errors: List[BaseException] = []
        self.list_cancelled = asyncio.Event()

    async def list_archives(
        self, _bundle: Any, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.list_calls.append(dict(params))
        self.list_started.set()
        try:
            if self.list_gate is not None:
                await self.list_gate.wait()
        except asyncio.CancelledError:
            self.list_cancelled.set()
            raise
        if self.list_errors:
            raise self.list_errors.pop(0)
        return {
            'code': 0,
            'data': {
                'arc_audits': [
                    {'Archive': {'aid': 1, 'bvid': 'BV1', 'title': 'fixture'}}
                ]
            },
        }

    async def archive_view(
        self, _bundle: Any, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        bvid = str(params['bvid'])
        self.detail_calls.append(bvid)
        self.detail_started.set()
        if self.detail_gate is not None:
            await self.detail_gate.wait()
        return {'code': 0, 'data': {'archive': {'aid': 1, 'bvid': bvid}}}


async def list_page(
    reader: ArchiveReadService,
    *,
    account_id: int = 7,
    credential_version: int = 3,
    status: str = 'is_pubing,pubed,not_pubed',
    page_number: int = 1,
    page_size: int = 50,
) -> Any:
    return await reader.list_page(
        object(),
        account_id=account_id,
        credential_version=credential_version,
        status=status,
        page_number=page_number,
        page_size=page_size,
    )


@pytest.mark.asyncio
async def test_review_and_reconciliation_share_one_page_snapshot() -> None:
    protocol = FakeProtocol()
    protocol.list_gate = asyncio.Event()
    reader = ArchiveReadService(protocol)

    async def review_consumer() -> Any:
        return await list_page(reader)

    async def reconciliation_consumer() -> Any:
        return await list_page(reader)

    try:
        consumers = [
            asyncio.create_task(
                review_consumer() if index % 2 == 0 else reconciliation_consumer()
            )
            for index in range(20)
        ]
        await protocol.list_started.wait()
        protocol.list_gate.set()

        pages = await asyncio.gather(*consumers)

        assert len(protocol.list_calls) == 1
        assert all(page == pages[0] for page in pages)
        assert isinstance(pages[0], tuple)
        assert await list_page(reader) is pages[0]
        assert len(protocol.list_calls) == 1
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_page_cache_key_includes_every_query_scope() -> None:
    protocol = FakeProtocol()
    reader = ArchiveReadService(protocol)
    try:
        await list_page(reader)
        await list_page(reader, account_id=8)
        await list_page(reader, credential_version=4)
        await list_page(reader, status='pubed')
        await list_page(reader, page_number=2)
        await list_page(reader, page_size=20)

        assert len(protocol.list_calls) == 6
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_detail_reads_singleflight_by_account_version_and_bvid() -> None:
    protocol = FakeProtocol()
    protocol.detail_gate = asyncio.Event()
    reader = ArchiveReadService(protocol)
    try:
        first = asyncio.create_task(
            reader.detail(
                object(), account_id=7, credential_version=3, bvid='BVfixture'
            )
        )
        second = asyncio.create_task(
            reader.detail(
                object(), account_id=7, credential_version=3, bvid='BVfixture'
            )
        )
        await protocol.detail_started.wait()
        protocol.detail_gate.set()

        assert await first == await second
        assert protocol.detail_calls == ['BVfixture']
        await reader.detail(
            object(), account_id=7, credential_version=4, bvid='BVfixture'
        )
        await reader.detail(
            object(), account_id=8, credential_version=3, bvid='BVfixture'
        )
        await reader.detail(object(), account_id=7, credential_version=3, bvid='BV2')
        assert protocol.detail_calls == ['BVfixture', 'BVfixture', 'BVfixture', 'BV2']
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_cancelling_one_waiter_does_not_cancel_shared_read() -> None:
    protocol = FakeProtocol()
    protocol.list_gate = asyncio.Event()
    reader = ArchiveReadService(protocol)
    try:
        cancelled = asyncio.create_task(list_page(reader))
        survivor = asyncio.create_task(list_page(reader))
        await protocol.list_started.wait()

        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert not protocol.list_cancelled.is_set()

        protocol.list_gate.set()
        assert await survivor
        assert len(protocol.list_calls) == 1
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_failed_read_is_evicted_and_retried() -> None:
    protocol = FakeProtocol()
    protocol.list_errors.append(RuntimeError('temporary failure'))
    reader = ArchiveReadService(protocol)
    try:
        with pytest.raises(RuntimeError, match='temporary failure'):
            await list_page(reader)

        assert await list_page(reader)
        assert len(protocol.list_calls) == 2
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_completed_snapshot_expires_after_thirty_seconds() -> None:
    protocol = FakeProtocol()
    clock = MutableClock()
    reader = ArchiveReadService(protocol, clock=clock)
    try:
        first = await list_page(reader)
        clock.now = 29.9
        assert await list_page(reader) is first
        clock.now = 30.0
        assert await list_page(reader) is not first
        assert len(protocol.list_calls) == 2
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_close_cancels_only_service_owned_reads() -> None:
    protocol = FakeProtocol()
    protocol.list_gate = asyncio.Event()
    reader = ArchiveReadService(protocol)
    unrelated_gate = asyncio.Event()
    unrelated = asyncio.create_task(unrelated_gate.wait())
    waiter = asyncio.create_task(list_page(reader))
    await protocol.list_started.wait()

    await reader.close()

    assert protocol.list_cancelled.is_set()
    assert not unrelated.done()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    unrelated.cancel()
    await asyncio.gather(unrelated, return_exceptions=True)
