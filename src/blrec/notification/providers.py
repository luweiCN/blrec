from __future__ import annotations

import asyncio
import smtplib
import ssl
import time
from abc import ABC, abstractmethod
from email.message import EmailMessage
from http.client import HTTPException
from typing import TYPE_CHECKING, Any, Callable, Dict, Final, Optional, TypedDict, cast
from urllib.parse import urljoin

import aiohttp
from loguru import logger

from ..utils.patterns import Singleton

if TYPE_CHECKING:
    from ..setting.typing import (
        BarkMessageType,
        EmailMessageType,
        MessageType,
        PushdeerMessageType,
        PushplusMessageType,
        ServerchanMessageType,
        TelegramMessageType,
    )
else:
    BarkMessageType = str
    EmailMessageType = str
    MessageType = str
    PushdeerMessageType = str
    PushplusMessageType = str
    ServerchanMessageType = str
    TelegramMessageType = str

__all__ = (
    'MessagingProvider',
    'EmailService',
    'Serverchan',
    'Pushdeer',
    'Pushplus',
    'Telegram',
    'Bark',
)


class MessagingProvider(Singleton, ABC):
    def __init__(self) -> None:
        super().__init__()
        self._session: Optional[Any] = None
        self._attempt_timeout_seconds = 10.0
        self._monotonic: Callable[[], float] = time.monotonic

    def bind_session(
        self,
        session: Optional[Any],
        *,
        attempt_timeout_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._attempt_timeout_seconds = attempt_timeout_seconds
        self._monotonic = monotonic

    def _require_session(self) -> Any:
        if self._session is None:
            raise RuntimeError('notification transport is not started')
        return self._session

    def _request_timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=self._attempt_timeout_seconds)

    @abstractmethod
    async def send_message(
        self, title: str, content: str, msg_type: MessageType
    ) -> None:
        pass


class EmailService(MessagingProvider):
    def __init__(
        self,
        src_addr: str = '',
        dst_addr: str = '',
        auth_code: str = '',
        smtp_host: str = 'smtp.163.com',
        smtp_port: int = 465,
    ) -> None:
        super().__init__()
        self.src_addr = src_addr
        self.dst_addr = dst_addr
        self.auth_code = auth_code
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port

    async def send_message(
        self, subject: str, content: str, msg_type: MessageType
    ) -> None:
        self._check_parameters()
        deadline_at = self._monotonic() + self._attempt_timeout_seconds
        await asyncio.get_running_loop().run_in_executor(
            None, self.send_with_deadline, subject, content, msg_type, deadline_at
        )

    def send_with_deadline(
        self, subject: str, content: str, msg_type: EmailMessageType, deadline_at: float
    ) -> None:
        self._check_parameters()
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = self.src_addr
        msg['To'] = self.dst_addr
        subtype = 'html' if msg_type == 'html' else 'plain'
        msg.set_content(content, subtype=subtype, charset='utf-8')

        try:
            smtp_ssl = smtplib.SMTP_SSL(
                self.smtp_host,
                self.smtp_port,
                timeout=self._remaining_timeout(deadline_at),
            )
        except ssl.SSLCertVerificationError:
            raise
        except ssl.SSLError:
            smtp = smtplib.SMTP(
                self.smtp_host,
                self.smtp_port,
                timeout=self._remaining_timeout(deadline_at),
            )
            try:
                self._set_phase_timeout(smtp, deadline_at)
                context = ssl.create_default_context()
                smtp.starttls(context=context)
                self._set_phase_timeout(smtp, deadline_at)
                smtp.login(self.src_addr, self.auth_code)
                self._set_phase_timeout(smtp, deadline_at)
                smtp.send_message(msg, self.src_addr, self.dst_addr)
            finally:
                self._close_smtp(smtp)
        else:
            try:
                smtp = smtp_ssl
                self._set_phase_timeout(smtp, deadline_at)
                smtp.login(self.src_addr, self.auth_code)
                self._set_phase_timeout(smtp, deadline_at)
                smtp.send_message(msg, self.src_addr, self.dst_addr)
            finally:
                self._close_smtp(smtp_ssl)

    @staticmethod
    def _close_smtp(smtp: Any) -> None:
        try:
            smtp.close()
        except OSError as error:
            logger.warning('SMTP transport close failed error={}', type(error).__name__)

    def _remaining_timeout(self, deadline_at: float) -> float:
        remaining = deadline_at - self._monotonic()
        if remaining <= 0:
            raise TimeoutError('notification delivery deadline exceeded')
        return min(self._attempt_timeout_seconds, remaining)

    def _set_phase_timeout(self, smtp: Any, deadline_at: float) -> None:
        remaining = self._remaining_timeout(deadline_at)
        sock = getattr(smtp, 'sock', None)
        if sock is not None:
            sock.settimeout(remaining)

    def _check_parameters(self) -> None:
        if not self.src_addr:
            raise ValueError('No source email address supplied')
        if not self.dst_addr:
            raise ValueError('No destination email address supplied')
        if not self.auth_code:
            raise ValueError('No auth code supplied')


class Serverchan(MessagingProvider):
    def __init__(self, sendkey: str = '') -> None:
        super().__init__()
        self.sendkey = sendkey

    async def send_message(
        self, title: str, content: str, msg_type: MessageType
    ) -> None:
        self._check_parameters()
        await self._post_message(title, content, cast(ServerchanMessageType, msg_type))

    def _check_parameters(self) -> None:
        if not self.sendkey:
            raise ValueError('No sendkey supplied')

    async def _post_message(
        self, title: str, content: str, msg_type: ServerchanMessageType
    ) -> None:
        url = f'https://sctapi.ftqq.com/{self.sendkey}.send'
        payload = {'text': title, 'desp': content}

        session = self._require_session()
        async with session.post(url, data=payload, timeout=self._request_timeout()):
            pass


class PushdeerResponse(TypedDict):
    code: int
    content: str
    error: str


class Pushdeer(MessagingProvider):
    _server: Final = 'https://api2.pushdeer.com'
    _endpoint: Final = '/message/push'

    def __init__(self, server: str = '', pushkey: str = '') -> None:
        super().__init__()
        self.server = server
        self.pushkey = pushkey

    async def send_message(
        self, title: str, content: str, msg_type: MessageType
    ) -> None:
        self._check_parameters()
        await self._post_message(title, content, cast(PushdeerMessageType, msg_type))

    def _check_parameters(self) -> None:
        if not self.pushkey:
            raise ValueError('No pushkey supplied')

    async def _post_message(
        self, title: str, content: str, msg_type: PushdeerMessageType
    ) -> None:
        url = urljoin(self.server or self._server, self._endpoint)
        payload = {
            'pushkey': self.pushkey,
            'text': title,
            'desp': content,
            'type': msg_type,
        }
        session = self._require_session()
        async with session.post(
            url, json=payload, timeout=self._request_timeout()
        ) as res:
            response = cast(PushdeerResponse, await res.json())
            if response['code'] != 0:
                raise HTTPException(response['code'], response['error'])


class PushplusResponse(TypedDict):
    code: int
    msg: str
    data: str


class Pushplus(MessagingProvider):
    url = 'https://www.pushplus.plus/send'

    def __init__(self, token: str = '', topic: str = '') -> None:
        super().__init__()
        self.token = token
        self.topic = topic

    async def send_message(
        self, title: str, content: str, msg_type: MessageType
    ) -> None:
        self._check_parameters()
        await self._post_message(title, content, msg_type)

    def _check_parameters(self) -> None:
        if not self.token:
            raise ValueError('No token supplied')

    async def _post_message(
        self, title: str, content: str, msg_type: PushplusMessageType
    ) -> None:
        payload = {
            'title': title,
            'content': content,
            'token': self.token,
            'topic': self.topic,
            'template': msg_type,
        }

        session = self._require_session()
        async with session.post(
            self.url, json=payload, timeout=self._request_timeout()
        ) as res:
            response = cast(PushplusResponse, await res.json())
            if response['code'] != 200:
                raise HTTPException(response['code'], response['msg'])


class TelegramResponse(TypedDict):
    ok: bool
    result: Dict[str, Any]


class Telegram(MessagingProvider):
    _server: Final = 'https://api.telegram.org'

    def __init__(self, token: str = '', chatid: str = '', server: str = '') -> None:
        super().__init__()
        self.token = token
        self.chatid = chatid
        self.server = server

    async def send_message(
        self, title: str, content: str, msg_type: MessageType
    ) -> None:
        self._check_parameters()
        await self._post_message(title, content, cast(TelegramMessageType, msg_type))

    def _check_parameters(self) -> None:
        if not self.token:
            raise ValueError('No token supplied')
        if not self.chatid:
            raise ValueError('No chatid supplied')

    async def _post_message(
        self, title: str, content: str, msg_type: TelegramMessageType
    ) -> None:
        url = urljoin(self.server or self._server, f'/bot{self.token}/sendMessage')
        payload = {
            'chat_id': self.chatid,
            'text': title + '\n\n' + content,
            'parse_mode': 'MarkdownV2' if msg_type == 'markdown' else 'HTML',
            'disable_web_page_preview': True,
        }

        session = self._require_session()
        async with session.post(
            url, json=payload, timeout=self._request_timeout()
        ) as res:
            response = cast(TelegramResponse, await res.json())
            if not response['ok']:
                raise HTTPException(
                    response['result']['error_code'], response['result']['description']
                )


class BarkResponse(TypedDict):
    code: int
    message: str
    timestamp: int


class Bark(MessagingProvider):
    _server: Final = 'https://api.day.app'
    _endpoint: Final = 'push'

    def __init__(self, server: str = '', pushkey: str = '') -> None:
        super().__init__()
        self.server = server if len(server) > 0 else self._server
        self.pushkey = pushkey

    async def send_message(
        self, title: str, content: str, msg_type: MessageType
    ) -> None:
        self._check_parameters()
        await self._post_message(title, content, cast(BarkMessageType, msg_type))

    def _check_parameters(self) -> None:
        if not self.pushkey:
            raise ValueError('No pushkey supplied')

    async def _post_message(
        self, title: str, content: str, msg_type: BarkMessageType
    ) -> None:
        url = urljoin(self.server, self._endpoint)
        # content size is limited to a maximum size of 4 KB (4096 bytes)
        if len(content.encode()) >= 4096:
            content = content.encode()[:4090].decode(errors='ignore') + ' ...'
        payload = {
            "title": title,
            "body": content,
            "device_key": self.pushkey,
            "badge": 1,
            "icon": "https://raw.githubusercontent.com/acgnhiki/blrec/master/webapp/src/assets/icons/icon-72x72.png",  # noqa
            "group": "blrec",
        }
        session = self._require_session()
        async with session.post(
            url, json=payload, timeout=self._request_timeout()
        ) as res:
            response = cast(BarkResponse, await res.json())
            if response['code'] != 200:
                raise HTTPException(response['message'])
