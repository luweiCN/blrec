from __future__ import annotations

import ssl
from typing import Any, Dict, List, Mapping, Tuple, Type

import aiohttp
import pytest

from blrec.notification.providers import (
    Bark,
    EmailService,
    Pushdeer,
    Pushplus,
    Serverchan,
    Telegram,
)


class FakeResponse:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload

    async def __aenter__(self) -> 'FakeResponse':
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def json(self) -> Mapping[str, Any]:
        return self._payload


class FakeSession:
    def __init__(self) -> None:
        self.cookie_jar = aiohttp.DummyCookieJar()
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        if 'pushdeer' in url:
            payload = {'code': 0, 'content': '', 'error': ''}
        elif 'pushplus' in url:
            payload = {'code': 200, 'msg': 'ok', 'data': ''}
        elif 'telegram' in url:
            payload = {'ok': True, 'result': {}}
        elif 'day.app' in url:
            payload = {'code': 200, 'message': 'ok', 'timestamp': 1}
        else:
            payload = {}
        return FakeResponse(payload)


def make_provider(provider_type: Type[Any], **kwargs: Any) -> Any:
    provider = object.__new__(provider_type)
    provider_type.__init__(provider, **kwargs)
    return provider


@pytest.mark.asyncio
async def test_http_providers_reuse_one_injected_cookie_less_session() -> None:
    session = FakeSession()
    providers = [
        make_provider(Serverchan, sendkey='send-key'),
        make_provider(Pushdeer, pushkey='push-key'),
        make_provider(Pushplus, token='token'),
        make_provider(Telegram, token='token', chatid='chat'),
        make_provider(Bark, pushkey='push-key'),
    ]
    for provider in providers:
        provider.bind_session(session, attempt_timeout_seconds=10)
        await provider.send_message('title', 'content', 'text')

    assert len(session.calls) == 5
    assert not any(True for _cookie in session.cookie_jar)
    assert all(call[1]['timeout'].total == 10 for call in session.calls)
    assert session.calls[2][0] == 'https://www.pushplus.plus/send'


@pytest.mark.asyncio
async def test_http_provider_rejects_send_without_bound_session() -> None:
    provider = make_provider(Serverchan, sendkey='send-key')

    with pytest.raises(RuntimeError, match='transport is not started'):
        await provider.send_message('title', 'content', 'text')


class FakeSocket:
    def __init__(self) -> None:
        self.timeouts: List[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)


class FakeSmtp:
    def __init__(self, clock: List[float], timeout: float) -> None:
        self.clock = clock
        self.constructor_timeout = timeout
        self.sock = FakeSocket()
        self.starttls_calls = 0
        self.login_calls = 0
        self.send_calls = 0

    def __enter__(self) -> 'FakeSmtp':
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def starttls(self, **_kwargs: Any) -> None:
        self.starttls_calls += 1
        self.clock[0] += 5

    def login(self, *_args: Any) -> None:
        self.login_calls += 1

    def send_message(self, *_args: Any) -> None:
        self.send_calls += 1

    def close(self) -> None:
        return None


class DeadlineExitSmtp(FakeSmtp):
    def __init__(self, clock: List[float], timeout: float) -> None:
        super().__init__(clock, timeout)
        self.closed = False
        self.exit_calls = 0
        self.close_calls = 0

    def __exit__(self, *_args: Any) -> None:
        self.exit_calls += 1
        if not self.closed:
            self.clock[0] += self.sock.timeouts[-1]

    def starttls(self, **_kwargs: Any) -> None:
        self.starttls_calls += 1

    def send_message(self, *_args: Any) -> None:
        self.send_calls += 1
        self.clock[0] += 10

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def test_smtp_fallback_uses_remaining_single_attempt_budget(monkeypatch) -> None:
    clock = [100.0]
    fallback: List[FakeSmtp] = []

    def fail_ssl(_host: str, _port: int, *, timeout: float) -> None:
        assert timeout == 10
        clock[0] += 6
        raise ssl.SSLError('try STARTTLS')

    def make_fallback(_host: str, _port: int, *, timeout: float) -> FakeSmtp:
        smtp = FakeSmtp(clock, timeout)
        fallback.append(smtp)
        return smtp

    monkeypatch.setattr('smtplib.SMTP_SSL', fail_ssl)
    monkeypatch.setattr('smtplib.SMTP', make_fallback)
    provider = make_provider(
        EmailService,
        src_addr='from@example.com',
        dst_addr='to@example.com',
        auth_code='secret',
    )
    provider.bind_session(None, attempt_timeout_seconds=10, monotonic=lambda: clock[0])

    with pytest.raises(TimeoutError):
        provider.send_with_deadline('title', 'content', 'text', 110.0)

    assert fallback[0].constructor_timeout == 4
    assert fallback[0].starttls_calls == 1
    assert fallback[0].login_calls == 0
    assert fallback[0].send_calls == 0
    assert fallback[0].sock.timeouts == [4]


@pytest.mark.parametrize('transport', ('ssl', 'starttls'))
def test_smtp_teardown_stays_inside_single_attempt_budget(
    monkeypatch, transport: str
) -> None:
    clock = [100.0]
    sessions: List[DeadlineExitSmtp] = []

    def make_smtp(_host: str, _port: int, *, timeout: float) -> DeadlineExitSmtp:
        smtp = DeadlineExitSmtp(clock, timeout)
        sessions.append(smtp)
        return smtp

    if transport == 'ssl':
        monkeypatch.setattr('smtplib.SMTP_SSL', make_smtp)
    else:
        monkeypatch.setattr(
            'smtplib.SMTP_SSL',
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ssl.SSLError()),
        )
        monkeypatch.setattr('smtplib.SMTP', make_smtp)
    provider = make_provider(
        EmailService,
        src_addr='from@example.com',
        dst_addr='to@example.com',
        auth_code='secret',
    )
    provider.bind_session(None, attempt_timeout_seconds=10, monotonic=lambda: clock[0])

    provider.send_with_deadline('title', 'content', 'text', 110.0)

    assert clock[0] == 110.0
    assert sessions[0].close_calls == 1


def test_smtp_certificate_verification_failure_does_not_fallback(monkeypatch) -> None:
    def fail_ssl(*_args: Any, **_kwargs: Any) -> None:
        raise ssl.SSLCertVerificationError(1, 'certificate verify failed')

    def fail_fallback(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError('certificate failure downgraded to STARTTLS fallback')

    monkeypatch.setattr('smtplib.SMTP_SSL', fail_ssl)
    monkeypatch.setattr('smtplib.SMTP', fail_fallback)
    provider = make_provider(
        EmailService,
        src_addr='from@example.com',
        dst_addr='to@example.com',
        auth_code='secret',
    )
    provider.bind_session(None, attempt_timeout_seconds=10, monotonic=lambda: 100.0)

    with pytest.raises(ssl.SSLCertVerificationError):
        provider.send_with_deadline('title', 'content', 'text', 110.0)
