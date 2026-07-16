import asyncio
import json
from typing import Any, AsyncIterator, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..realtime import RealtimeBroker

router = APIRouter(prefix='/realtime', tags=['realtime'])
broker = RealtimeBroker()


def _encode(event_type: str, data: Mapping[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str)
    return 'event: {}\ndata: {}\n\n'.format(event_type, payload).encode('utf8')


async def _events(request: Request) -> AsyncIterator[bytes]:
    subscription = broker.subscribe()
    try:
        yield _encode('resync', {})
        while not await request.is_disconnected():
            try:
                event = await asyncio.wait_for(subscription.get(), timeout=15)
            except asyncio.TimeoutError:
                yield _encode('heartbeat', {})
            else:
                yield _encode(event.type, event.data)
    finally:
        broker.unsubscribe(subscription)


@router.get('')
async def get_realtime(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _events(request),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
            'Content-Encoding': 'identity',
        },
    )
