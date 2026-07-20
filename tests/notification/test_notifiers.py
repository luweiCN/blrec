import asyncio
from typing import List, Optional, Tuple

from blrec.notification.notifiers import EmailNotifier


class FakeDispatcher:
    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.calls: List[Tuple[str, str, str, str, Optional[object]]] = []

    def enqueue(
        self,
        channel: str,
        title: str,
        content: str,
        message_type: str,
        *,
        coalesce_key: Optional[object] = None,
    ) -> bool:
        self.calls.append((channel, title, content, message_type, coalesce_key))
        return self.accepted


def test_message_notifier_only_enqueues_without_detached_task(monkeypatch) -> None:
    dispatcher = FakeDispatcher()

    def fail_create_task(*_args, **_kwargs):
        raise AssertionError('detached notification task created')

    monkeypatch.setattr(asyncio, 'create_task', fail_create_task)
    notifier = EmailNotifier(dispatcher=dispatcher)

    notifier._send_message('title', 'body', 'html')

    assert dispatcher.calls == [('email', 'title', 'body', 'html', None)]
