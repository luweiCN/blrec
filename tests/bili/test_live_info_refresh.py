from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import pytest

from blrec.bili.live import Live


def info_response(room_id: int = 123, uid: int = 456) -> Dict[str, Any]:
    return {
        'room_info': {
            'uid': uid,
            'room_id': room_id,
            'short_id': 0,
            'area_id': 1,
            'area_name': '测试分区',
            'parent_area_id': 2,
            'parent_area_name': '测试父分区',
            'live_status': 0,
            'live_start_time': 0,
            'online': 0,
            'title': '测试直播',
            'cover': '',
            'tags': '',
            'description': '',
        },
        'anchor_info': {'base_info': {'uname': '测试主播', 'gender': '', 'face': ''}},
    }


class InfoApi:
    def __init__(
        self,
        *,
        response: Optional[Dict[str, Any]] = None,
        error: Optional[BaseException] = None,
        entered: Optional[asyncio.Event] = None,
        release: Optional[asyncio.Event] = None,
    ) -> None:
        self.response = response or info_response()
        self.error = error
        self.entered = entered
        self.release = release
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def get_info_by_room(self, _room_id: int) -> Dict[str, Any]:
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.entered is not None:
                self.entered.set()
            if self.release is not None:
                await self.release.wait()
            if self.error is not None:
                raise self.error
            return self.response
        finally:
            self.in_flight -= 1


def live_with_apis(
    web: InfoApi, app: InfoApi, *, info_timeout_seconds: float = 10
) -> Live:
    live = Live(123, session=object(), info_timeout_seconds=info_timeout_seconds)
    live._webapi = web
    live._appapi = app
    return live


@pytest.mark.asyncio
async def test_concurrent_updates_share_one_composite_room_request() -> None:
    web = InfoApi()
    live = live_with_apis(web, InfoApi(error=RuntimeError('unused fallback')))

    results = await asyncio.gather(*(live.update_info(True) for _ in range(10)))

    assert results == [True] * 10
    assert web.calls == 1
    assert web.max_in_flight == 1
    assert live.room_info.uid == live.user_info.uid == 456
    assert live.info_revision == 1


@pytest.mark.asyncio
async def test_composite_refresh_uses_each_fallback_at_most_once() -> None:
    web = InfoApi(error=RuntimeError('web unavailable'))
    app = InfoApi()
    live = live_with_apis(web, app)

    assert await live.update_info(True) is True

    assert web.calls == 1
    assert app.calls == 1
    assert live.info_revision == 1


@pytest.mark.asyncio
async def test_invalid_web_projection_falls_back_without_partial_application() -> None:
    invalid = info_response()
    invalid['anchor_info'] = {}
    web = InfoApi(response=invalid)
    app = InfoApi(response=info_response(room_id=321, uid=654))
    live = live_with_apis(web, app)

    assert await live.update_info(True) is True

    assert (web.calls, app.calls) == (1, 1)
    assert live.room_info.room_id == 321
    assert live.room_info.uid == live.user_info.uid == 654
    assert live.info_revision == 1


@pytest.mark.asyncio
async def test_composite_refresh_uses_html_only_after_both_apis_fail() -> None:
    web = InfoApi(error=RuntimeError('web unavailable'))
    app = InfoApi(error=RuntimeError('app unavailable'))
    live = live_with_apis(web, app)
    html_calls = 0

    async def html() -> Dict[str, Any]:
        nonlocal html_calls
        html_calls += 1
        return info_response()

    live._get_room_info_res_via_html_page = html  # type: ignore[method-assign]

    assert await live.update_info(True) is True

    assert (web.calls, app.calls, html_calls) == (1, 1, 1)


@pytest.mark.asyncio
async def test_composite_refresh_has_one_absolute_timeout() -> None:
    never = asyncio.Event()
    web = InfoApi(release=never)
    live = live_with_apis(
        web, InfoApi(error=RuntimeError('unused fallback')), info_timeout_seconds=0.01
    )

    started = asyncio.get_running_loop().time()
    assert await live.update_info() is False
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.2
    assert web.calls == 1
    assert live.info_revision == 0


@pytest.mark.asyncio
async def test_failed_refresh_is_evicted_and_can_be_retried() -> None:
    web = InfoApi(error=RuntimeError('web unavailable'))
    app = InfoApi(error=RuntimeError('app unavailable'))
    live = live_with_apis(web, app)

    async def failing_html() -> Dict[str, Any]:
        raise RuntimeError('html unavailable')

    live._get_room_info_res_via_html_page = failing_html  # type: ignore[method-assign]
    assert await live.update_info() is False

    web.error = None
    assert await live.update_info(True) is True

    assert web.calls == 2
    assert live.info_revision == 1


@pytest.mark.asyncio
async def test_failed_refresh_preserves_the_previous_complete_snapshot() -> None:
    web = InfoApi(response=info_response(room_id=321, uid=654))
    app = InfoApi(error=RuntimeError('app unavailable'))
    live = live_with_apis(web, app)

    assert await live.update_info(True) is True
    previous_room = live.room_info
    previous_user = live.user_info

    invalid = info_response(room_id=999, uid=888)
    invalid['anchor_info'] = {}
    web.response = invalid

    async def failing_html() -> Dict[str, Any]:
        raise RuntimeError('html unavailable')

    live._get_room_info_res_via_html_page = failing_html  # type: ignore[method-assign]

    assert await live.update_info() is False
    assert live.room_info is previous_room
    assert live.user_info is previous_user
    assert live.info_revision == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_refresh() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    web = InfoApi(entered=entered, release=release)
    live = live_with_apis(web, InfoApi(error=RuntimeError('unused fallback')))

    cancelled = asyncio.create_task(live.update_info(True))
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    surviving = asyncio.create_task(live.update_info(True))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()

    assert await asyncio.wait_for(surviving, timeout=0.5) is True
    assert web.calls == 1
    assert live.info_revision == 1


@pytest.mark.asyncio
async def test_completed_refresh_is_not_retained_as_a_ttl_cache() -> None:
    web = InfoApi()
    live = live_with_apis(web, InfoApi(error=RuntimeError('unused fallback')))

    assert await live.update_info(True) is True
    assert await live.update_info(True) is True

    assert web.calls == 2
    assert live.info_revision == 2


@pytest.mark.asyncio
async def test_room_and_user_update_methods_share_the_composite_response() -> None:
    web = InfoApi()
    live = live_with_apis(web, InfoApi(error=RuntimeError('unused fallback')))

    room_updated, user_updated = await asyncio.gather(
        live.update_room_info(True), live.update_user_info(True)
    )

    assert (room_updated, user_updated) == (True, True)
    assert web.calls == 1
    assert live.room_info.uid == live.user_info.uid
    assert live.info_revision == 1


@pytest.mark.asyncio
async def test_init_applies_one_composite_snapshot() -> None:
    web = InfoApi()
    live = live_with_apis(web, InfoApi(error=RuntimeError('unused fallback')))

    await live.init()

    assert web.calls == 1
    assert live.info_revision == 1


@pytest.mark.asyncio
async def test_deinit_cancels_owned_composite_refresh_before_closing_session() -> None:
    entered = asyncio.Event()
    never = asyncio.Event()
    web = InfoApi(entered=entered, release=never)
    live = live_with_apis(web, InfoApi(error=RuntimeError('unused fallback')))
    closed = False

    async def close() -> None:
        nonlocal closed
        closed = True

    live._owns_session = True
    live._session = type('ClosableSession', (), {'close': staticmethod(close)})()
    updating = asyncio.create_task(live.update_info(True))
    await asyncio.wait_for(entered.wait(), timeout=0.5)

    await asyncio.wait_for(live.deinit(), timeout=0.5)

    with pytest.raises(asyncio.CancelledError):
        await updating
    assert closed is True


@pytest.mark.asyncio
async def test_deinit_closes_refresh_admission_before_closing_session() -> None:
    entered = asyncio.Event()
    never = asyncio.Event()
    web = InfoApi(entered=entered, release=never)
    live = live_with_apis(web, InfoApi(error=RuntimeError('unused fallback')))
    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    close_calls = 0

    async def close() -> None:
        nonlocal close_calls
        close_calls += 1
        close_entered.set()
        await close_release.wait()

    live._owns_session = True
    live._session = type('ClosableSession', (), {'close': staticmethod(close)})()
    updating = asyncio.create_task(live.update_info(True))
    closing = None
    quiet_update = None
    loud_update = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        closing = asyncio.create_task(live.deinit())
        await asyncio.wait_for(close_entered.wait(), timeout=0.5)

        quiet_update = asyncio.create_task(live.update_info())
        loud_update = asyncio.create_task(live.update_info(True))

        assert await asyncio.wait_for(quiet_update, timeout=0.1) is False
        with pytest.raises(RuntimeError, match='refresh is closed'):
            await asyncio.wait_for(loud_update, timeout=0.1)
        assert web.calls == 1

        close_release.set()
        await asyncio.wait_for(closing, timeout=0.5)
        with pytest.raises(asyncio.CancelledError):
            await updating
        assert live._info_refresh_task is None

        await live.deinit()
        assert close_calls == 1
    finally:
        close_release.set()
        never.set()
        for task in (updating, closing, quiet_update, loud_update):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (updating, closing, quiet_update, loud_update) if task),
            return_exceptions=True,
        )
