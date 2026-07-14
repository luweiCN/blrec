from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from loguru import logger

from blrec.setting.models import BiliUploadSettings

from .accounts import AccountManager, AccountWriteGate
from .categories import UploadCategoryCatalog
from .comments import CommentPlanner, CommentPublisher
from .credentials import CredentialStore
from .covers import CoverLibrary, CoverResolver
from .crypto import CredentialCipher
from .danmaku_import import DanmakuImporter
from .danmaku_publish import DanmakuPublisher
from .database import BiliUploadDatabase
from .journal import RecordingJournalBridge
from .models import FeatureUnavailable, validate_feature_gate
from .policies import RoomUploadPolicyManager
from .protocol import AiohttpProtocolTransport, BiliProtocolClient
from .recording_content import RecordingContentReader
from .review import ReviewWatcher
from .signing import WbiSigner, WebSessionBuilder
from .upload import UploadCoordinator
from .upos import UposUploader

__all__ = ('BiliAccountRuntime',)

_COMMENT_ACTION_INTERVAL_SECONDS = 5
_DANMAKU_ACTION_INTERVAL_SECONDS = 25


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
        upload_interval_seconds: float = 30,
        space_threshold_bytes: int = 1024**3,
        on_primary_credential_changed: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        if refresh_interval_seconds <= 0:
            raise ValueError('refresh interval must be positive')
        if upload_interval_seconds <= 0:
            raise ValueError('upload interval must be positive')
        if space_threshold_bytes < 0:
            raise ValueError('space threshold must not be negative')
        self._settings = settings
        self._api_key = api_key
        self._credential_key = credential_key
        self._old_credential_keys = dict(old_credential_keys or {})
        self._provided_protocol = protocol
        self._clock = clock
        self._refresh_interval_seconds = refresh_interval_seconds
        self._upload_interval_seconds = upload_interval_seconds
        self._space_threshold_bytes = space_threshold_bytes
        self._on_primary_credential_changed = on_primary_credential_changed
        self._database: Optional[BiliUploadDatabase] = None
        self._transport: Optional[AiohttpProtocolTransport] = None
        self._manager: Optional[AccountManager] = None
        self._journal: Optional[RecordingJournalBridge] = None
        self._content_reader: Optional[RecordingContentReader] = None
        self._coordinator: Optional[UploadCoordinator] = None
        self._policy_manager: Optional[RoomUploadPolicyManager] = None
        self._category_catalog: Optional[UploadCategoryCatalog] = None
        self._cover_library: Optional[CoverLibrary] = None
        self._cover_resolver: Optional[CoverResolver] = None
        self._review_watcher: Optional[ReviewWatcher] = None
        self._comment_planner: Optional[CommentPlanner] = None
        self._comment_publisher: Optional[CommentPublisher] = None
        self._danmaku_importer: Optional[DanmakuImporter] = None
        self._danmaku_publisher: Optional[DanmakuPublisher] = None
        self._refresh_task: Optional[asyncio.Task[Any]] = None
        self._upload_task: Optional[asyncio.Task[Any]] = None
        self._upload_stop_event: Optional[asyncio.Event] = None
        self._unavailable_reason: Optional[str] = (
            'Bilibili account management is not enabled'
        )

    @property
    def manager(self) -> Optional[AccountManager]:
        return self._manager

    @property
    def journal(self) -> Optional[RecordingJournalBridge]:
        return self._journal

    @property
    def content_reader(self) -> Optional[RecordingContentReader]:
        return self._content_reader

    @property
    def coordinator(self) -> Optional[UploadCoordinator]:
        return self._coordinator

    @property
    def policy_manager(self) -> Optional[RoomUploadPolicyManager]:
        return self._policy_manager

    @property
    def category_catalog(self) -> Optional[UploadCategoryCatalog]:
        return self._category_catalog

    @property
    def cover_library(self) -> Optional[CoverLibrary]:
        return self._cover_library

    @property
    def cover_resolver(self) -> Optional[CoverResolver]:
        return self._cover_resolver

    @property
    def review_watcher(self) -> Optional[ReviewWatcher]:
        return self._review_watcher

    @property
    def comment_planner(self) -> Optional[CommentPlanner]:
        return self._comment_planner

    @property
    def comment_publisher(self) -> Optional[CommentPublisher]:
        return self._comment_publisher

    @property
    def danmaku_importer(self) -> Optional[DanmakuImporter]:
        return self._danmaku_importer

    @property
    def danmaku_publisher(self) -> Optional[DanmakuPublisher]:
        return self._danmaku_publisher

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
            journal = RecordingJournalBridge(database, clock=self._clock)
            content_reader = RecordingContentReader(database)
            await journal.reconcile_open_sessions()
            key_id = hashlib.sha256(self._credential_key).hexdigest()
            keys: Dict[str, bytes] = dict(self._old_credential_keys)
            keys[key_id] = self._credential_key
            cipher = CredentialCipher(keys, current_key_id=key_id)
            protocol = self._provided_protocol or self._create_protocol()
            store = CredentialStore(database)
            write_gates = AccountWriteGate(database)
            manager = AccountManager(
                protocol,
                store,
                database=database,
                cipher=cipher,
                clock=self._clock,
                write_gates=write_gates,
                on_primary_credential_changed=self._on_primary_credential_changed,
            )
            await manager.start()
            upload_stop_event = asyncio.Event()

            def upload_enabled() -> bool:
                return bool(
                    self._settings.enabled and self._settings.auto_upload_enabled
                )

            def upload_stop_requested() -> bool:
                return upload_stop_event.is_set() or not upload_enabled()

            uploader = UposUploader(
                database,
                protocol,
                chunk_size=self._settings.upload_chunk_size,
                concurrency=self._settings.upload_chunk_concurrency,
                clock=self._clock,
                stop_requested=upload_stop_requested,
            )

            async def load_bundle(account_id: int) -> Any:
                return await store.get(account_id=account_id, cipher=cipher)

            coordinator = UploadCoordinator(
                database,
                protocol,
                uploader,
                bundle_loader=load_bundle,
                account_gates=write_gates,
                auto_upload_enabled=upload_enabled,
                auto_comment_enabled=lambda: self._settings.auto_comment_enabled,
                danmaku_backfill_enabled=(
                    lambda: self._settings.danmaku_backfill_enabled
                ),
                clock=self._clock,
                stop_requested=upload_stop_requested,
            )
            policy_manager = RoomUploadPolicyManager(database, clock=self._clock)
            category_catalog = UploadCategoryCatalog(
                database, protocol, bundle_loader=load_bundle, clock=self._clock
            )
            cover_library = CoverLibrary(
                database, Path(database.path).parent / 'cover-assets', clock=self._clock
            )
            cover_resolver = CoverResolver(
                database,
                cover_library,
                protocol,
                bundle_loader=load_bundle,
                clock=self._clock,
            )
            comment_planner = CommentPlanner(database, clock=self._clock)
            comment_publisher = CommentPublisher(
                database,
                protocol,
                bundle_loader=load_bundle,
                account_gates=write_gates,
                auto_comment_enabled=(lambda: self._settings.auto_comment_enabled),
                clock=self._clock,
            )
            danmaku_importer = DanmakuImporter(
                database,
                import_high_watermark=self._settings.import_high_watermark,
                space_threshold_bytes=self._space_threshold_bytes,
                enabled=lambda: self._settings.danmaku_backfill_enabled,
                clock=self._clock,
            )
            danmaku_publisher = DanmakuPublisher(
                database,
                protocol,
                bundle_loader=load_bundle,
                account_gates=write_gates,
                auto_danmaku_enabled=(lambda: self._settings.danmaku_backfill_enabled),
                interval_seconds=self._settings.danmaku_interval_seconds,
                auth_refresh=manager.refresh_account,
                clock=self._clock,
            )
            review_watcher = ReviewWatcher(
                database,
                protocol,
                bundle_loader=load_bundle,
                comment_branch=comment_planner,
                danmaku_branch=danmaku_importer,
                clock=self._clock,
            )
        except Exception:
            logger.exception('Bilibili account management failed to start')
            await self._close_partial(database)
            self._unavailable_reason = 'Bilibili account management failed to start'
            return False

        self._database = database
        self._journal = journal
        self._content_reader = content_reader
        self._manager = manager
        self._coordinator = coordinator
        self._policy_manager = policy_manager
        self._category_catalog = category_catalog
        self._cover_library = cover_library
        self._cover_resolver = cover_resolver
        self._review_watcher = review_watcher
        self._comment_planner = comment_planner
        self._comment_publisher = comment_publisher
        self._danmaku_importer = danmaku_importer
        self._danmaku_publisher = danmaku_publisher
        self._upload_stop_event = upload_stop_event
        self._unavailable_reason = None
        self._refresh_task = asyncio.create_task(self._run_refresh_checks(manager))
        self._upload_task = asyncio.create_task(
            self._run_uploads(
                journal,
                coordinator,
                review_watcher,
                comment_publisher,
                danmaku_importer,
                danmaku_publisher,
                upload_stop_event,
            )
        )
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
        await self._stop_upload_worker()
        refresh_task, self._refresh_task = self._refresh_task, None
        if refresh_task is not None and not refresh_task.done():
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)
        manager, self._manager = self._manager, None
        if manager is not None:
            await manager.close()
        self._coordinator = None
        self._policy_manager = None
        self._category_catalog = None
        self._cover_library = None
        self._cover_resolver = None
        self._review_watcher = None
        self._comment_planner = None
        self._comment_publisher = None
        self._danmaku_importer = None
        self._danmaku_publisher = None
        self._content_reader = None
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        self._journal = None
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

    async def _run_uploads(
        self,
        journal: RecordingJournalBridge,
        coordinator: UploadCoordinator,
        review_watcher: ReviewWatcher,
        comment_publisher: CommentPublisher,
        danmaku_importer: DanmakuImporter,
        danmaku_publisher: DanmakuPublisher,
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            upload_processed = None
            comment_processed = None
            danmaku_imported = None
            danmaku_published = None
            try:
                await journal.finalize_cancelled_sessions()
                await review_watcher.run_once()
                await coordinator.create_ready_jobs()
                upload_processed = await coordinator.run_once()
                comment_processed = await comment_publisher.run_once()
                danmaku_imported = await danmaku_importer.run_once()
                danmaku_published = await danmaku_publisher.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Bilibili upload worker iteration failed')
            delay: float
            if comment_processed is not None:
                delay = _COMMENT_ACTION_INTERVAL_SECONDS
            elif danmaku_published is not None:
                delay = _DANMAKU_ACTION_INTERVAL_SECONDS
            elif upload_processed is not None:
                delay = 1
            elif danmaku_imported is not None:
                delay = 1
            else:
                delay = self._upload_interval_seconds
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _stop_upload_worker(self) -> None:
        stop_event, self._upload_stop_event = self._upload_stop_event, None
        if stop_event is not None:
            stop_event.set()
        upload_task, self._upload_task = self._upload_task, None
        if upload_task is not None:
            await upload_task

    async def _close_partial(self, database: BiliUploadDatabase) -> None:
        await self._stop_upload_worker()
        self._coordinator = None
        self._policy_manager = None
        self._review_watcher = None
        self._comment_planner = None
        self._comment_publisher = None
        self._danmaku_importer = None
        self._danmaku_publisher = None
        self._journal = None
        self._content_reader = None
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        await database.close()
