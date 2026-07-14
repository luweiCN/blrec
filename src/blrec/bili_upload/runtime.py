from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from loguru import logger

from blrec.setting.models import BiliUploadSettings

from .accounts import AccountManager, AccountWriteGate
from .credentials import CredentialStore
from .crypto import CredentialCipher
from .database import BiliUploadDatabase
from .models import FeatureUnavailable, validate_feature_gate
from .protocol import AiohttpProtocolTransport, BiliProtocolClient
from .signing import WbiSigner, WebSessionBuilder

__all__ = ('BiliAccountRuntime',)


async def _unavailable_wbi_keys() -> Tuple[str, str]:
    raise RuntimeError('WBI key provider is not configured')


class BiliAccountRuntime:
    def __init__(
        self,
        settings: BiliUploadSettings,
        *,
        api_key: Optional[str],
        credential_key: Optional[bytes],
        old_credential_keys: Optional[Mapping[str, bytes]] = None,
        protocol: Optional[Any] = None,
        clock: Callable[[], float] = time.time,
        refresh_interval_seconds: float = 3600,
        on_primary_credential_changed: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        if refresh_interval_seconds <= 0:
            raise ValueError('refresh interval must be positive')
        self._settings = settings
        self._api_key = api_key
        self._credential_key = credential_key
        self._old_credential_keys = dict(old_credential_keys or {})
        self._provided_protocol = protocol
        self._clock = clock
        self._refresh_interval_seconds = refresh_interval_seconds
        self._on_primary_credential_changed = on_primary_credential_changed
        self._database: Optional[BiliUploadDatabase] = None
        self._transport: Optional[AiohttpProtocolTransport] = None
        self._manager: Optional[AccountManager] = None
        self._refresh_task: Optional[asyncio.Task[Any]] = None
        self._unavailable_reason: Optional[str] = (
            'Bilibili account management is not enabled'
        )

    @property
    def manager(self) -> Optional[AccountManager]:
        return self._manager

    @property
    def unavailable_reason(self) -> Optional[str]:
        return self._unavailable_reason

    async def start(self) -> bool:
        if self._manager is not None:
            return True
        try:
            validate_feature_gate(
                self._settings,
                api_key=self._api_key,
                credential_key=self._credential_key,
            )
        except FeatureUnavailable as error:
            self._unavailable_reason = str(error)
            return False
        if not self._settings.enabled:
            self._unavailable_reason = 'Bilibili account management is not enabled'
            return False
        assert self._credential_key is not None

        database = BiliUploadDatabase(self._settings.database_path)
        try:
            await database.open()
            key_id = hashlib.sha256(self._credential_key).hexdigest()
            keys: Dict[str, bytes] = dict(self._old_credential_keys)
            keys[key_id] = self._credential_key
            cipher = CredentialCipher(keys, current_key_id=key_id)
            protocol = self._provided_protocol or self._create_protocol()
            manager = AccountManager(
                protocol,
                CredentialStore(database),
                database=database,
                cipher=cipher,
                clock=self._clock,
                write_gates=AccountWriteGate(database),
                on_primary_credential_changed=self._on_primary_credential_changed,
            )
            await manager.start()
        except Exception:
            logger.exception('Bilibili account management failed to start')
            await self._close_partial(database)
            self._unavailable_reason = 'Bilibili account management failed to start'
            return False

        self._database = database
        self._manager = manager
        self._unavailable_reason = None
        self._refresh_task = asyncio.create_task(self._run_refresh_checks(manager))
        return True

    async def primary_cookie_header(self, url: str) -> Optional[str]:
        if self._manager is None:
            return None
        return await self._manager.primary_cookie_header(url)

    async def recording_cookie_header(self, url: str) -> Optional[str]:
        if self._manager is None:
            return None
        return await self._manager.recording_cookie_header(url)

    async def report_primary_auth_failure(self) -> None:
        if self._manager is not None:
            await self._manager.report_primary_auth_failure()

    async def close(self) -> None:
        refresh_task, self._refresh_task = self._refresh_task, None
        if refresh_task is not None and not refresh_task.done():
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)
        manager, self._manager = self._manager, None
        if manager is not None:
            await manager.close()
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        database, self._database = self._database, None
        if database is not None:
            await database.close()

    def _create_protocol(self) -> BiliProtocolClient:
        transport = AiohttpProtocolTransport()
        self._transport = transport
        return BiliProtocolClient(
            transport=transport,
            wbi_signer=WbiSigner(_unavailable_wbi_keys, clock=self._clock),
            web_session_builder=WebSessionBuilder(clock=self._clock),
            clock=self._clock,
        )

    async def _run_refresh_checks(self, manager: AccountManager) -> None:
        while True:
            try:
                await manager.refresh_due_accounts()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Bilibili account health check failed')
            await asyncio.sleep(self._refresh_interval_seconds)

    async def _close_partial(self, database: BiliUploadDatabase) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        await database.close()
