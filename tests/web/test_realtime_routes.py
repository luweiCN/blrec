import json
from typing import Any, Dict

import pytest

from blrec.web.realtime import RealtimeBroker, RealtimeSampler
from blrec.web.routers import realtime


class FakeRequest:
    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


def decode_event(value: bytes) -> Dict[str, Any]:
    lines = value.decode('utf8').strip().splitlines()
    return {
        'type': lines[0].split(': ', 1)[1],
        'data': json.loads(lines[1].split(': ', 1)[1]),
    }


@pytest.mark.asyncio
async def test_realtime_response_starts_with_resync_and_proxy_safe_headers() -> None:
    broker = RealtimeBroker()
    realtime.broker = broker
    request = FakeRequest()

    response = await realtime.get_realtime(request)  # type: ignore[arg-type]
    stream = response.body_iterator
    first = await stream.__anext__()  # type: ignore[union-attr]
    request.disconnected = True
    await stream.aclose()  # type: ignore[union-attr]

    assert response.media_type == 'text/event-stream'
    assert response.headers['cache-control'] == 'no-cache'
    assert response.headers['x-accel-buffering'] == 'no'
    assert response.headers['content-encoding'] == 'identity'
    assert decode_event(first) == {'type': 'resync', 'data': {}}


@pytest.mark.asyncio
async def test_slow_subscriber_receives_resync_after_queue_overflow() -> None:
    broker = RealtimeBroker(queue_size=2)
    subscription = broker.subscribe()

    await broker.publish('tasks', {'version': 1})
    await broker.publish('network', {'version': 2})
    await broker.publish('upload_progress', {'version': 3})

    event = await subscription.get()
    broker.unsubscribe(subscription)

    assert event.type == 'resync'
    assert event.data == {}


@pytest.mark.asyncio
async def test_sampler_publishes_only_changed_snapshots() -> None:
    tasks = [{'roomId': 1, 'state': 'waiting'}]
    network = {'interfaces': []}

    async def uploads() -> list:
        return [{'jobId': 9, 'percent': 25.0}]

    broker = RealtimeBroker()
    subscription = broker.subscribe()
    sampler = RealtimeSampler(
        broker,
        task_provider=lambda: tasks,
        network_provider=lambda: network,
        upload_provider=uploads,
    )

    await sampler.sample_once()
    first = [await subscription.get() for _ in range(3)]
    await sampler.sample_once()
    tasks[0] = {'roomId': 1, 'state': 'recording'}
    await sampler.sample_once()
    changed = await subscription.get()
    broker.unsubscribe(subscription)

    assert [event.type for event in first] == ['tasks', 'network', 'upload_progress']
    assert subscription.empty()
    assert changed.type == 'tasks'
    assert changed.data['tasks'][0]['state'] == 'recording'


@pytest.mark.asyncio
async def test_broker_unsubscribe_releases_subscriber() -> None:
    broker = RealtimeBroker()
    subscription = broker.subscribe()

    broker.unsubscribe(subscription)
    await broker.publish('tasks', {'version': 1})

    assert subscription.empty()
