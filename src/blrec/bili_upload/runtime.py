from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from loguru import logger

from blrec.networking.manager import NetworkRouteManager
from blrec.notification.operational import (
    OperationalHealthScanner,
    OperationalNotificationCenter,
)
from blrec.setting.models import BiliUploadSettings, OperationalNotificationSettings

from .accounts import AccountManager, AccountWriteGate
from .categories import (
    InvalidUploadCategoryRequest,
    UploadCategoryCatalog,
    UploadCategoryUnavailable,
)
from .collection_publish import CollectionPublisher
from .collections import CollectionManager
from .comments import CommentPlanner, CommentPublisher
from .covers import CoverLibrary, CoverResolver
from .credentials import CredentialStore
from .crypto import CredentialCipher
from .danmaku_import import DanmakuImporter
from .danmaku_publish import DanmakuPublisher
from .database import BiliUploadDatabase
from .deletion_worker import LocalDeletionRejected, LocalDeletionWorker
from .highlight_cut import LosslessClipper
from .highlight_danmaku import HighlightDanmakuClipper
from .highlight_worker import HighlightWorker
from .highlights import HighlightService
from .journal import RecordingJournalBridge
from .media_index import MediaIndexWorker
from .models import FeatureUnavailable, validate_feature_gate
from .policies import RoomUploadPolicyCommand, RoomUploadPolicyManager
from .protocol import AiohttpProtocolTransport, BiliProtocolClient
from .recording_content import RecordingContentReader
from .retention import RetentionManager
from .review import ReviewWatcher
from .session_submission import SessionSubmissionManager
from .signing import WebSessionBuilder
from .task_actions import UploadTaskActionManager, UploadTaskActionRejected
from .upload import UploadCoordinator
from .upos import UposUploader

__all__ = ('BiliAccountRuntime',)

_COMMENT_ACTION_INTERVAL_SECONDS = 5
_DANMAKU_ACTION_INTERVAL_SECONDS = 25


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
        recording_root: Optional[str] = None,
        recording_capacity_bytes: Callable[[], int] = lambda: 0,
        capacity_warning_threshold_bytes: Callable[[], int] = lambda: 0,
        on_primary_credential_changed: Optional[Callable[[], Awaitable[None]]] = None,
        active_session_canceller: Optional[Callable[[int], Awaitable[None]]] = None,
        network_route_manager: Optional[NetworkRouteManager] = None,
        operational_settings_provider: Optional[
            Callable[[], OperationalNotificationSettings]
        ] = None,
        notification_senders: Optional[Mapping[str, Any]] = None,
        notification_channel_enabled: Callable[[str], bool] = lambda _channel: False,
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
        self._recording_root = recording_root
        self._recording_capacity_bytes = recording_capacity_bytes
        self._capacity_warning_threshold_bytes = capacity_warning_threshold_bytes
        self._on_primary_credential_changed = on_primary_credential_changed
        self._active_session_canceller = active_session_canceller
        self._network_route_manager = network_route_manager
        self._operational_settings_provider = operational_settings_provider
        self._notification_senders = dict(notification_senders or {})
        self._notification_channel_enabled = notification_channel_enabled
        self._database: Optional[BiliUploadDatabase] = None
        self._transport: Optional[AiohttpProtocolTransport] = None
        self._manager: Optional[AccountManager] = None
        self._journal: Optional[RecordingJournalBridge] = None
        self._content_reader: Optional[RecordingContentReader] = None
        self._coordinator: Optional[UploadCoordinator] = None
        self._policy_manager: Optional[RoomUploadPolicyManager] = None
        self._session_submission_manager: Optional[SessionSubmissionManager] = None
        self._category_catalog: Optional[UploadCategoryCatalog] = None
        self._cover_library: Optional[CoverLibrary] = None
        self._cover_resolver: Optional[CoverResolver] = None
        self._collection_manager: Optional[CollectionManager] = None
        self._collection_publisher: Optional[CollectionPublisher] = None
        self._review_watcher: Optional[ReviewWatcher] = None
        self._comment_planner: Optional[CommentPlanner] = None
        self._comment_publisher: Optional[CommentPublisher] = None
        self._danmaku_importer: Optional[DanmakuImporter] = None
        self._danmaku_publisher: Optional[DanmakuPublisher] = None
        self._retention_manager: Optional[RetentionManager] = None
        self._task_actions: Optional[UploadTaskActionManager] = None
        self._highlight_service: Optional[HighlightService] = None
        self._highlight_worker: Optional[HighlightWorker] = None
        self._media_index_worker: Optional[MediaIndexWorker] = None
        self._deletion_worker: Optional[LocalDeletionWorker] = None
        self._notification_scanner: Optional[OperationalHealthScanner] = None
        self._refresh_task: Optional[asyncio.Task[Any]] = None
        self._upload_task: Optional[asyncio.Task[Any]] = None
        self._upload_stop_event: Optional[asyncio.Event] = None
        self._highlight_task: Optional[asyncio.Task[Any]] = None
        self._highlight_stop_event: Optional[asyncio.Event] = None
        self._media_index_task: Optional[asyncio.Task[Any]] = None
        self._media_index_stop_event: Optional[asyncio.Event] = None
        self._deletion_task: Optional[asyncio.Task[Any]] = None
        self._deletion_stop_event: Optional[asyncio.Event] = None
        self._session_action_lock = asyncio.Lock()
        self._unavailable_reason: Optional[str] = (
            'Bilibili account management is not ready'
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
    def session_submission_manager(self) -> Optional[SessionSubmissionManager]:
        return self._session_submission_manager

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
    def collection_manager(self) -> Optional[CollectionManager]:
        return self._collection_manager

    @property
    def collection_publisher(self) -> Optional[CollectionPublisher]:
        return self._collection_publisher

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
    def retention_manager(self) -> Optional[RetentionManager]:
        return self._retention_manager

    @property
    def task_actions(self) -> Optional[UploadTaskActionManager]:
        return self._task_actions

    @property
    def highlight_service(self) -> Optional[HighlightService]:
        return self._highlight_service

    @property
    def highlight_worker(self) -> Optional[HighlightWorker]:
        return self._highlight_worker

    @property
    def media_index_worker(self) -> Optional[MediaIndexWorker]:
        return self._media_index_worker

    @property
    def deletion_worker(self) -> Optional[LocalDeletionWorker]:
        return self._deletion_worker

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
        assert self._credential_key is not None

        database = BiliUploadDatabase(self._settings.database_path)
        try:
            await database.open()
            journal = RecordingJournalBridge(database, clock=self._clock)
            content_reader = RecordingContentReader(database)
            await journal.reconcile_open_sessions()
            lossless_clipper = LosslessClipper()
            highlight_danmaku_clipper = HighlightDanmakuClipper()
            highlight_service = HighlightService(
                database,
                clip_root=(
                    None
                    if self._recording_root is None
                    else Path(self._recording_root).resolve().parent / 'clips'
                ),
                clipper=lossless_clipper,
                clock=self._clock,
            )
            if self._recording_root is not None:
                await highlight_service.migrate_legacy_outputs(
                    Path(self._recording_root)
                )
            highlight_worker = HighlightWorker(
                database, lossless_clipper, highlight_danmaku_clipper, clock=self._clock
            )
            await highlight_worker.recover_interrupted()
            await highlight_worker.backfill_file_sizes(limit=100)
            media_index_worker = MediaIndexWorker(database, clock=self._clock)
            await media_index_worker.recover_interrupted()
            recording_root = (
                Path(self._recording_root)
                if self._recording_root is not None
                else Path(database.path).parent / 'rec'
            )
            deletion_worker = LocalDeletionWorker(
                database,
                recording_root=recording_root,
                clip_root=recording_root.resolve().parent / 'clips',
                active_session_canceller=self._active_session_canceller,
                clock=self._clock,
            )
            await deletion_worker.recover_interrupted()
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

            def upload_stop_requested() -> bool:
                stop_event = self._upload_stop_event
                return stop_event is not None and stop_event.is_set()

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
            coordinator = UploadCoordinator(
                database,
                protocol,
                uploader,
                bundle_loader=load_bundle,
                account_gates=write_gates,
                cover_resolver=cover_resolver,
                clock=self._clock,
                stop_requested=upload_stop_requested,
            )
            task_actions = UploadTaskActionManager(
                database,
                protocol,
                uploader,
                bundle_loader=load_bundle,
                account_gates=write_gates,
                edit_payload_builder=coordinator.build_edit_payload,
                recording_root=(
                    None if self._recording_root is None else Path(self._recording_root)
                ),
                deletion_worker=deletion_worker,
                clock=self._clock,
            )
            await task_actions.recover_interrupted()
            policy_manager = RoomUploadPolicyManager(database, clock=self._clock)
            session_submission_manager = SessionSubmissionManager(
                database, policy_manager=policy_manager, clock=self._clock
            )
            category_catalog = UploadCategoryCatalog(
                database, protocol, bundle_loader=load_bundle, clock=self._clock
            )
            collection_manager = CollectionManager(
                database, protocol, cover_resolver, bundle_loader=load_bundle
            )
            collection_publisher = CollectionPublisher(
                database, protocol, bundle_loader=load_bundle, clock=self._clock
            )
            await collection_publisher.recover_interrupted()
            comment_planner = CommentPlanner(database, clock=self._clock)
            comment_publisher = CommentPublisher(
                database,
                protocol,
                bundle_loader=load_bundle,
                account_gates=write_gates,
                clock=self._clock,
            )
            danmaku_importer = DanmakuImporter(
                database,
                import_high_watermark=self._settings.import_high_watermark,
                space_threshold_bytes=self._space_threshold_bytes,
                clock=self._clock,
            )
            danmaku_publisher = DanmakuPublisher(
                database,
                protocol,
                bundle_loader=load_bundle,
                account_gates=write_gates,
                interval_seconds=self._settings.danmaku_interval_seconds,
                auth_refresh=manager.refresh_account,
                clock=self._clock,
            )
            await danmaku_publisher.recover_interrupted()
            review_watcher = ReviewWatcher(
                database,
                protocol,
                bundle_loader=load_bundle,
                comment_branch=comment_planner,
                danmaku_branch=danmaku_importer,
                collection_branch=collection_publisher,
                clock=self._clock,
            )
            await review_watcher.recover_legacy_page_order_pauses()
            retention_manager = (
                None
                if self._recording_root is None
                else RetentionManager(
                    database,
                    Path(self._recording_root),
                    capacity_bytes=self._recording_capacity_bytes,
                    warning_threshold_bytes=(self._capacity_warning_threshold_bytes),
                    clock=self._clock,
                )
            )
            notification_scanner = None
            if self._operational_settings_provider is not None:
                notification_center = OperationalNotificationCenter(
                    database,
                    settings_provider=self._operational_settings_provider,
                    senders=self._notification_senders,
                    channel_enabled=self._notification_channel_enabled,
                    clock=self._clock,
                )
                notification_scanner = OperationalHealthScanner(
                    database,
                    notification_center,
                    retention_status_provider=(
                        None if retention_manager is None else retention_manager.status
                    ),
                    network_route_manager=self._network_route_manager,
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
        self._session_submission_manager = session_submission_manager
        self._category_catalog = category_catalog
        self._cover_library = cover_library
        self._cover_resolver = cover_resolver
        self._collection_manager = collection_manager
        self._collection_publisher = collection_publisher
        self._review_watcher = review_watcher
        self._comment_planner = comment_planner
        self._comment_publisher = comment_publisher
        self._danmaku_importer = danmaku_importer
        self._danmaku_publisher = danmaku_publisher
        self._retention_manager = retention_manager
        self._task_actions = task_actions
        self._highlight_service = highlight_service
        self._highlight_worker = highlight_worker
        self._media_index_worker = media_index_worker
        self._deletion_worker = deletion_worker
        self._notification_scanner = notification_scanner
        self._unavailable_reason = None
        self._refresh_task = asyncio.create_task(self._run_refresh_checks(manager))
        await self._start_upload_worker()
        await self._start_highlight_worker()
        await self._start_media_index_worker()
        await self._start_deletion_worker()
        return True

    async def primary_cookie_header(self, url: str) -> Optional[str]:
        if self._manager is None:
            return None
        return await self._manager.primary_cookie_header(url)

    async def recording_cookie_header(self, url: str) -> Optional[str]:
        if self._manager is None:
            return None
        return await self._manager.recording_cookie_header(url)

    async def report_primary_auth_failure(self, credential_fingerprint: str) -> None:
        if self._manager is not None:
            await self._manager.report_primary_auth_failure(credential_fingerprint)

    async def create_highlight_upload_task(
        self, clip_id: int, *, settings: RoomUploadPolicyCommand, manager_subject: str
    ) -> int:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        title = settings.title_template.strip()
        if not title or len(title) > 80:
            raise UploadTaskActionRejected('片段稿件标题需填写 1～80 个字符')
        settings = replace(
            settings,
            enabled=True,
            title_template=title,
            part_title_template=title,
            retention_mode='never',
            retention_days=0,
        )
        service = self._highlight_service
        coordinator = self._coordinator
        submissions = self._session_submission_manager
        policy_manager = self._policy_manager
        category_catalog = self._category_catalog
        if (
            service is None
            or coordinator is None
            or submissions is None
            or policy_manager is None
            or category_catalog is None
        ):
            raise UploadTaskActionRejected('高光投稿当前不可用')
        clip = await service.get_clip(clip_id)
        await policy_manager.validate(clip.room_id, settings)
        try:
            catalog = await category_catalog.list(
                settings.account_mode, settings.account_id
            )
        except (InvalidUploadCategoryRequest, UploadCategoryUnavailable) as error:
            raise UploadTaskActionRejected(str(error)) from error
        if not any(
            child.id == settings.tid
            for parent in catalog.categories
            for child in parent.children
        ):
            raise UploadTaskActionRejected('请选择有效的二级投稿分区')
        if not any(
            statement.id == settings.creation_statement_id
            for statement in catalog.creation_statements
        ):
            raise UploadTaskActionRejected('请选择当前账号支持的创作声明')
        async with self._session_action_lock:
            session_id = await service.ensure_upload_session(clip_id)
            await submissions.save_override(
                session_id, settings, manager_subject=manager_subject
            )
            return await coordinator.create_highlight_job(session_id)

    async def delete_highlight_clip(self, clip_id: int) -> str:
        deletion_worker = self._deletion_worker
        if deletion_worker is None:
            raise UploadTaskActionRejected('高光片段管理当前不可用')
        try:
            await deletion_worker.request_clip(clip_id)
        except LocalDeletionRejected as error:
            raise UploadTaskActionRejected(str(error)) from None
        return 'queued'

    async def run_recording_session_action(
        self, action: str, session_id: int, *, manager_subject: str
    ) -> str:
        database = self._database
        actions = self._task_actions
        submissions = self._session_submission_manager
        if database is None or actions is None or submissions is None:
            raise UploadTaskActionRejected('上传任务管理当前不可用')
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        row = await database.fetchone(
            'SELECT session.room_id,session.state,job.id AS job_id '
            'FROM recording_sessions session LEFT JOIN upload_jobs job '
            'ON job.session_id=session.id WHERE session.id=?',
            (session_id,),
        )
        if row is None:
            raise UploadTaskActionRejected('录制场次不存在')

        if action == 'delete_local':
            deletion_worker = self._deletion_worker
            if deletion_worker is None:
                raise UploadTaskActionRejected('本地删除服务当前不可用')
            try:
                await deletion_worker.request_session(
                    session_id, manager_subject=manager_subject
                )
            except LocalDeletionRejected as error:
                raise UploadTaskActionRejected(str(error)) from None
            return '已排队删除本地场次及文件'

        if action in ('set_upload', 'set_skip', 'pause_upload', 'resume_upload'):
            async with self._session_action_lock:
                job_id = row['job_id']
                if action in ('pause_upload', 'resume_upload'):
                    if job_id is None:
                        raise UploadTaskActionRejected('本场录像尚未创建上传任务')
                    if action == 'pause_upload':
                        return await actions.pause_upload(
                            int(job_id), manager_subject=manager_subject
                        )
                    return await actions.resume_upload(
                        int(job_id), manager_subject=manager_subject
                    )
                if row['job_id'] is not None:
                    if action == 'set_skip':
                        return await actions.skip_upload(
                            int(row['job_id']), manager_subject=manager_subject
                        )
                    return '本场录像已经创建上传任务'
                await submissions.set_decision(
                    session_id,
                    'upload' if action == 'set_upload' else 'skip',
                    manager_subject=manager_subject,
                )
                return (
                    '本场录像将在录制结束后创建上传任务'
                    if action == 'set_upload'
                    else '本场录像已设为不投稿'
                )

        job_id = row['job_id']
        if job_id is None:
            raise UploadTaskActionRejected('本场录像尚未创建上传任务')
        numeric_job_id = int(job_id)
        if action == 'retry_failed':
            return await actions.retry_failed(
                numeric_job_id, manager_subject=manager_subject
            )
        if action == 'repair_transcode':
            return await actions.request_transcode_repair(
                numeric_job_id, manager_subject=manager_subject
            )
        if action == 'backfill_danmaku':
            return await actions.request_danmaku_backfill(
                numeric_job_id, manager_subject=manager_subject
            )
        if action == 'repost_as_new':
            return await actions.repost_as_new(
                numeric_job_id, manager_subject=manager_subject
            )
        raise UploadTaskActionRejected('不支持的场次操作')

    async def close(self) -> None:
        cover_library, self._cover_library = self._cover_library, None
        if cover_library is not None:
            cover_library.close_admission()
        if self._deletion_worker is not None:
            self._deletion_worker.stop_admission()
        await self._stop_deletion_worker()
        await self._stop_media_index_worker()
        await self._stop_highlight_worker()
        await self._stop_upload_worker()
        if cover_library is not None:
            await cover_library.shutdown()
        refresh_task, self._refresh_task = self._refresh_task, None
        if refresh_task is not None and not refresh_task.done():
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)
        manager, self._manager = self._manager, None
        if manager is not None:
            await manager.close()
        self._coordinator = None
        self._policy_manager = None
        self._session_submission_manager = None
        self._category_catalog = None
        self._cover_resolver = None
        self._collection_manager = None
        self._collection_publisher = None
        self._review_watcher = None
        self._comment_planner = None
        self._comment_publisher = None
        self._danmaku_importer = None
        self._danmaku_publisher = None
        self._retention_manager = None
        self._task_actions = None
        self._highlight_service = None
        self._highlight_worker = None
        self._media_index_worker = None
        self._deletion_worker = None
        self._notification_scanner = None
        content_reader, self._content_reader = self._content_reader, None
        if content_reader is not None:
            content_reader.close()
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        self._journal = None
        database, self._database = self._database, None
        if database is not None:
            await database.close()

    def _create_protocol(self) -> BiliProtocolClient:
        transport = AiohttpProtocolTransport(route_manager=self._network_route_manager)
        self._transport = transport
        return BiliProtocolClient(
            transport=transport,
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

    async def _run_highlights(
        self, worker: HighlightWorker, stop_event: asyncio.Event
    ) -> None:
        while not stop_event.is_set():
            processed = None
            try:
                processed = await worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Highlight worker iteration failed')
            delay = 0.0 if processed is not None else 2.0
            if delay <= 0:
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _run_media_indexes(
        self, worker: MediaIndexWorker, stop_event: asyncio.Event
    ) -> None:
        while not stop_event.is_set():
            processed = None
            try:
                processed = await worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Media index worker iteration failed')
            delay = 0.0 if processed is not None else 2.0
            if delay <= 0:
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _run_uploads(
        self,
        journal: RecordingJournalBridge,
        coordinator: UploadCoordinator,
        review_watcher: ReviewWatcher,
        comment_publisher: CommentPublisher,
        danmaku_importer: DanmakuImporter,
        danmaku_publisher: DanmakuPublisher,
        stop_event: asyncio.Event,
        retention_manager: Optional[RetentionManager] = None,
        task_actions: Optional[UploadTaskActionManager] = None,
        notification_scanner: Optional[OperationalHealthScanner] = None,
    ) -> None:
        while not stop_event.is_set():
            upload_processed = None
            comment_processed = None
            danmaku_imported = None
            danmaku_published = None
            repair_processed = None
            try:
                await journal.finalize_cancelled_sessions()
                await review_watcher.run_once()
                if task_actions is not None:
                    repair_processed = await task_actions.run_once()
                await coordinator.sync_live_sessions()
                await coordinator.prepare_waiting_jobs()
                upload_processed = await coordinator.run_once()
                comment_processed = await comment_publisher.run_once()
                danmaku_imported = await danmaku_importer.run_once()
                danmaku_published = await danmaku_publisher.run_once()
                if retention_manager is not None:
                    await retention_manager.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Bilibili upload worker iteration failed')
            if notification_scanner is not None:
                try:
                    await notification_scanner.scan()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception('Operational notification scan failed')
            delay: float
            if comment_processed is not None:
                delay = _COMMENT_ACTION_INTERVAL_SECONDS
            elif danmaku_published is not None:
                delay = _DANMAKU_ACTION_INTERVAL_SECONDS
            elif upload_processed is not None or repair_processed is not None:
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
        stop_event = self._upload_stop_event
        if stop_event is not None:
            stop_event.set()
        upload_task = self._upload_task
        try:
            if upload_task is not None:
                await upload_task
        finally:
            if self._upload_task is upload_task:
                self._upload_task = None
            if self._upload_stop_event is stop_event:
                self._upload_stop_event = None

    async def _start_upload_worker(self) -> None:
        if self._upload_task is not None and not self._upload_task.done():
            return
        journal = self._journal
        coordinator = self._coordinator
        review_watcher = self._review_watcher
        comment_publisher = self._comment_publisher
        danmaku_importer = self._danmaku_importer
        danmaku_publisher = self._danmaku_publisher
        task_actions = self._task_actions
        if any(
            component is None
            for component in (
                journal,
                coordinator,
                review_watcher,
                comment_publisher,
                danmaku_importer,
                danmaku_publisher,
                task_actions,
            )
        ):
            return
        assert journal is not None
        assert coordinator is not None
        assert review_watcher is not None
        assert comment_publisher is not None
        assert danmaku_importer is not None
        assert danmaku_publisher is not None
        assert task_actions is not None
        stop_event = asyncio.Event()
        self._upload_stop_event = stop_event
        self._upload_task = asyncio.create_task(
            self._run_uploads(
                journal,
                coordinator,
                review_watcher,
                comment_publisher,
                danmaku_importer,
                danmaku_publisher,
                stop_event,
                self._retention_manager,
                task_actions,
                self._notification_scanner,
            )
        )

    async def _stop_highlight_worker(self) -> None:
        stop_event, self._highlight_stop_event = self._highlight_stop_event, None
        if stop_event is not None:
            stop_event.set()
        task, self._highlight_task = self._highlight_task, None
        if task is not None:
            await task

    async def _start_highlight_worker(self) -> None:
        if self._highlight_task is not None and not self._highlight_task.done():
            return
        worker = self._highlight_worker
        if worker is None:
            return
        stop_event = asyncio.Event()
        self._highlight_stop_event = stop_event
        self._highlight_task = asyncio.create_task(
            self._run_highlights(worker, stop_event)
        )

    async def _stop_media_index_worker(self) -> None:
        stop_event, self._media_index_stop_event = (self._media_index_stop_event, None)
        if stop_event is not None:
            stop_event.set()
        task, self._media_index_task = self._media_index_task, None
        if task is not None:
            await task

    async def _stop_deletion_worker(self) -> None:
        stop_event, self._deletion_stop_event = self._deletion_stop_event, None
        if stop_event is not None:
            stop_event.set()
        task, self._deletion_task = self._deletion_task, None
        if task is not None:
            await task

    async def _start_deletion_worker(self) -> None:
        if self._deletion_task is not None and not self._deletion_task.done():
            return
        worker = self._deletion_worker
        if worker is None:
            return
        stop_event = asyncio.Event()
        self._deletion_stop_event = stop_event
        self._deletion_task = asyncio.create_task(worker.run(stop_event))

    async def _start_media_index_worker(self) -> None:
        if self._media_index_task is not None and not self._media_index_task.done():
            return
        worker = self._media_index_worker
        if worker is None:
            return
        stop_event = asyncio.Event()
        self._media_index_stop_event = stop_event
        self._media_index_task = asyncio.create_task(
            self._run_media_indexes(worker, stop_event)
        )

    async def _close_partial(self, database: BiliUploadDatabase) -> None:
        if self._deletion_worker is not None:
            self._deletion_worker.stop_admission()
        await self._stop_deletion_worker()
        await self._stop_media_index_worker()
        await self._stop_highlight_worker()
        await self._stop_upload_worker()
        self._coordinator = None
        self._policy_manager = None
        self._session_submission_manager = None
        self._category_catalog = None
        self._cover_library = None
        self._cover_resolver = None
        self._collection_manager = None
        self._collection_publisher = None
        self._review_watcher = None
        self._comment_planner = None
        self._comment_publisher = None
        self._danmaku_importer = None
        self._danmaku_publisher = None
        self._retention_manager = None
        self._task_actions = None
        self._highlight_service = None
        self._highlight_worker = None
        self._media_index_worker = None
        self._deletion_worker = None
        self._notification_scanner = None
        self._journal = None
        content_reader, self._content_reader = self._content_reader, None
        if content_reader is not None:
            content_reader.close()
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        await database.close()
