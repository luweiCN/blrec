import json
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

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
    assert broker.has_subscribers('tasks')
    assert broker.has_subscribers('network')
    assert broker.has_subscribers('upload_progress')
    assert broker.has_subscribers('highlight_progress')
    request.disconnected = True
    await stream.aclose()  # type: ignore[union-attr]

    assert response.media_type == 'text/event-stream'
    assert response.headers['cache-control'] == 'no-cache'
    assert response.headers['x-accel-buffering'] == 'no'
    assert response.headers['content-encoding'] == 'identity'
    assert decode_event(first) == {'type': 'resync', 'data': {}}
    assert not broker.has_subscribers('tasks')


@pytest.mark.asyncio
async def test_realtime_response_subscribes_to_requested_topics() -> None:
    broker = RealtimeBroker()
    realtime.broker = broker
    request = FakeRequest()

    response = await realtime.get_realtime(  # type: ignore[arg-type]
        request, topics='tasks,network'
    )
    stream = response.body_iterator
    first = await stream.__anext__()  # type: ignore[union-attr]
    assert broker.has_subscribers('tasks')
    assert broker.has_subscribers('network')
    assert not broker.has_subscribers('upload_progress')

    await broker.publish('upload_progress', {'jobs': []})
    await broker.publish('network', {'interfaces': []})
    second = await stream.__anext__()  # type: ignore[union-attr]
    request.disconnected = True
    await stream.aclose()  # type: ignore[union-attr]

    assert decode_event(first) == {'type': 'resync', 'data': {}}
    assert decode_event(second) == {'type': 'network', 'data': {'interfaces': []}}
    assert not broker.has_subscribers('tasks')
    assert not broker.has_subscribers('network')


@pytest.mark.asyncio
@pytest.mark.parametrize('topics', ['', ' ', 'tasks,', 'unknown', 'tasks,unknown'])
async def test_realtime_rejects_invalid_explicit_topics(topics: str) -> None:
    broker = RealtimeBroker()
    realtime.broker = broker

    with pytest.raises(HTTPException) as error:
        await realtime.get_realtime(  # type: ignore[arg-type]
            FakeRequest(), topics=topics
        )

    assert error.value.status_code == 422
    assert not broker.has_subscribers('tasks')


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
async def test_topic_subscriber_receives_only_requested_events() -> None:
    broker = RealtimeBroker()
    subscription = broker.subscribe({'network'})

    await broker.publish('tasks', {})
    await broker.publish('network', {'interfaces': []})

    event = await subscription.get()
    broker.unsubscribe(subscription)

    assert event.type == 'network'
    assert subscription.empty()


@pytest.mark.asyncio
async def test_subscription_copies_requested_topics() -> None:
    broker = RealtimeBroker()
    topics = {'network'}
    subscription = broker.subscribe(topics)
    topics.clear()

    await broker.publish('network', {'interfaces': []})

    event = await subscription.get()
    broker.unsubscribe(subscription)

    assert event.type == 'network'


@pytest.mark.asyncio
@pytest.mark.parametrize('event_type', ['resync', 'heartbeat'])
async def test_control_events_reach_every_topic_subscriber(event_type: str) -> None:
    broker = RealtimeBroker()
    subscription = broker.subscribe({'tasks'})

    await broker.publish(event_type, {})

    event = await subscription.get()
    broker.unsubscribe(subscription)

    assert event.type == event_type


@pytest.mark.parametrize('topics', [set(), {'unknown'}, {'tasks', 'unknown'}])
def test_broker_rejects_invalid_topic_sets(topics: set) -> None:
    broker = RealtimeBroker()

    with pytest.raises(ValueError):
        broker.subscribe(topics)


def test_broker_reports_only_active_topic_interest() -> None:
    broker = RealtimeBroker()

    assert not broker.has_subscribers('tasks')
    subscription = broker.subscribe({'tasks'})
    assert broker.has_subscribers('tasks')
    assert not broker.has_subscribers('network')

    broker.unsubscribe(subscription)

    assert not broker.has_subscribers('tasks')


@pytest.mark.asyncio
async def test_sampler_does_not_compute_unsubscribed_topics() -> None:
    tasks = Mock(return_value=[])
    network = Mock(return_value={})
    uploads = AsyncMock(return_value=[])
    highlights = AsyncMock(return_value=[])
    broker = RealtimeBroker()
    subscription = broker.subscribe({'tasks'})
    sampler = RealtimeSampler(
        broker,
        task_provider=tasks,
        network_provider=network,
        upload_provider=uploads,
        highlight_provider=highlights,
    )

    await sampler.sample_once()

    tasks.assert_called_once_with()
    network.assert_not_called()
    uploads.assert_not_awaited()
    highlights.assert_not_awaited()
    assert (await subscription.get()).type == 'tasks'
    assert subscription.empty()
    broker.unsubscribe(subscription)


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
async def test_sampler_reuses_same_channel_for_highlight_progress() -> None:
    async def uploads() -> list:
        return []

    async def highlights() -> list:
        return [{'id': 3, 'state': 'processing'}]

    broker = RealtimeBroker()
    subscription = broker.subscribe()
    sampler = RealtimeSampler(
        broker,
        task_provider=lambda: [],
        network_provider=lambda: {},
        upload_provider=uploads,
        highlight_provider=highlights,
    )

    await sampler.sample_once()
    events = [await subscription.get() for _ in range(4)]
    broker.unsubscribe(subscription)

    assert [event.type for event in events] == [
        'tasks',
        'network',
        'upload_progress',
        'highlight_progress',
    ]
    assert events[-1].data == {'clips': [{'id': 3, 'state': 'processing'}]}


@pytest.mark.asyncio
async def test_broker_unsubscribe_releases_subscriber() -> None:
    broker = RealtimeBroker()
    subscription = broker.subscribe()

    broker.unsubscribe(subscription)
    await broker.publish('tasks', {'version': 1})

    assert subscription.empty()
