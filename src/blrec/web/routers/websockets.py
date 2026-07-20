import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional, Tuple

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger
from websockets.exceptions import ConnectionClosed

from blrec.logging.audit import audit
from blrec.web import security

from ...application import Application
from ...event import EventCenter
from ...exception import ExceptionCenter, format_exception

logging.getLogger('websockets').setLevel(logging.WARNING)

app: Application = None  # type: ignore  # bypass flake8 F821

router = APIRouter(tags=['websockets'])


async def _run_connection_pump(
    websocket: WebSocket,
    *,
    route: str,
    subscribe: Callable[[Callable[[Any], None]], Any],
    serialize: Callable[[Any], str],
    handshake_started_at: float,
) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=128)
    finished: asyncio.Future[Tuple[str, int]] = loop.create_future()
    subscription: Optional[Any] = None
    first_event_at: Optional[float] = None
    peak_backlog = 0
    event_count = 0
    byte_count = 0
    accepted_at = time.monotonic()

    def finish(reason: str, code: int) -> None:
        if not finished.done():
            finished.set_result((reason, code))

    async def send_items() -> None:
        nonlocal byte_count, event_count
        while True:
            item = await queue.get()
            try:
                text = serialize(item)
                await websocket.send_text(text)
            except (WebSocketDisconnect, ConnectionClosed) as error:
                raw_code = getattr(error, 'code', 1001)
                finish(
                    'client_disconnect',
                    int(raw_code) if isinstance(raw_code, int) else 1001,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(
                    'Websocket send failed on {}: {}', route, type(error).__name__
                )
                finish('send_error', 1011)
                return
            else:
                event_count += 1
                byte_count += len(text.encode('utf8'))
            finally:
                queue.task_done()

    sender = asyncio.create_task(send_items())

    def enqueue(item: Any) -> None:
        nonlocal first_event_at, peak_backlog
        if finished.done():
            return
        if first_event_at is None:
            first_event_at = time.monotonic()
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            peak_backlog = queue.maxsize
            finish('overflow', 1013)
            sender.cancel()
        else:
            peak_backlog = max(peak_backlog, queue.qsize())

    def on_item(item: Any) -> None:
        try:
            loop.call_soon_threadsafe(enqueue, item)
        except RuntimeError:
            # The event loop can close before a producer thread observes disposal.
            return

    cancelled = False
    reason = 'server_shutdown'
    close_code = 1001
    try:
        subscription = subscribe(on_item)
        reason, close_code = await finished
    except asyncio.CancelledError:
        cancelled = True
    except Exception as error:
        logger.error(
            'Websocket subscription failed on {}: {}', route, type(error).__name__
        )
        reason = 'subscription_error'
        close_code = 1011
    finally:
        if subscription is not None:
            subscription.dispose()
        if not sender.done():
            sender.cancel()
        try:
            await sender
        except asyncio.CancelledError:
            pass
        except Exception as error:
            logger.error(
                'Websocket sender stopped on {}: {}', route, type(error).__name__
            )
        if reason in {
            'overflow',
            'send_error',
            'subscription_error',
            'server_shutdown',
        }:
            try:
                await websocket.close(code=close_code)
            except Exception:
                pass
        ended_at = time.monotonic()
        audit(
            'websocket_connection',
            route=route,
            handshake_ms=round(max(0.0, accepted_at - handshake_started_at) * 1000, 3),
            first_event_ms=(
                None
                if first_event_at is None
                else round(max(0.0, first_event_at - handshake_started_at) * 1000, 3)
            ),
            duration_ms=round(max(0.0, ended_at - accepted_at) * 1000, 3),
            events=event_count,
            bytes=byte_count,
            peak_backlog=peak_backlog,
            disconnect_reason=reason,
            disconnect_code=close_code,
        )
    if cancelled:
        raise asyncio.CancelledError


async def authenticate_websocket(websocket: WebSocket) -> bool:
    store = security.auth_store
    if store is None:
        await websocket.close(code=4401)
        return False
    origin = websocket.headers.get('origin', '')
    if not security.valid_origin(websocket, origin):  # type: ignore[arg-type]
        await websocket.close(code=4403)
        return False
    token = websocket.cookies.get(security.SESSION_COOKIE_NAME, '')
    if store.authenticate_session(token) is None:
        await websocket.close(code=4401)
        return False
    return True


@router.websocket('/ws/v1/events')
async def receive_events(websocket: WebSocket) -> None:
    handshake_started_at = time.monotonic()
    if not await authenticate_websocket(websocket):
        return
    await websocket.accept()
    logger.debug('Events websocket accepted')
    await _run_connection_pump(
        websocket,
        route='events',
        subscribe=EventCenter.get_instance().events.subscribe,
        serialize=lambda event: json.dumps(event.asdict(), ensure_ascii=False),
        handshake_started_at=handshake_started_at,
    )


@router.websocket('/ws/v1/exceptions')
async def receive_exception(websocket: WebSocket) -> None:
    handshake_started_at = time.monotonic()
    if not await authenticate_websocket(websocket):
        return
    await websocket.accept()
    logger.debug('Exceptions websocket accepted')
    await _run_connection_pump(
        websocket,
        route='exceptions',
        subscribe=ExceptionCenter.get_instance().exceptions.subscribe,
        serialize=format_exception,
        handshake_started_at=handshake_started_at,
    )
