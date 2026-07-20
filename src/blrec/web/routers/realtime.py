import asyncio
import json
from typing import Any, AsyncIterator, Collection, FrozenSet, Mapping, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..realtime import REALTIME_TOPICS, RealtimeBroker

router = APIRouter(prefix='/realtime', tags=['realtime'])
broker = RealtimeBroker()


def _encode(event_type: str, data: Mapping[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str)
    return 'event: {}\ndata: {}\n\n'.format(event_type, payload).encode('utf8')


async def _events(
    request: Request, topics: Optional[Collection[str]] = None
) -> AsyncIterator[bytes]:
    subscription = broker.subscribe(topics)
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
async def get_realtime(
    request: Request, topics: Optional[str] = None
) -> StreamingResponse:
    requested_topics: Optional[FrozenSet[str]] = None
    if topics is not None:
        topic_items = topics.split(',')
        if any(not topic for topic in topic_items):
            raise HTTPException(
                status_code=422, detail='realtime topics must not be empty'
            )
        requested_topics = frozenset(topic_items)
        if not requested_topics.issubset(REALTIME_TOPICS):
            raise HTTPException(status_code=422, detail='unknown realtime topic')
    return StreamingResponse(
        _events(request, requested_topics),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
            'Content-Encoding': 'identity',
        },
    )
