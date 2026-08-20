import os
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import attr
from brotli_asgi import BrotliMiddleware
from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pkg_resources import resource_filename
from pydantic import ValidationError
from starlette.responses import Response

from blrec.bili_upload.active_media import (
    ActiveMediaBusy,
    ActiveMediaMetadata,
    ActiveMediaService,
)
from blrec.bili_upload.recording_content import (
    MediaResource,
    RecordingContentNotFound,
    RecordingContentUnavailable,
)
from blrec.bili_upload.recording_outbox import (
    LocalRecordingOutbox,
    RecordingOutboxRuntime,
)
from blrec.bili_upload.runtime import BiliAccountRuntime
from blrec.cloud_cost import (
    AliyunCloudCostService,
    AliyunOpenApiClient,
    AliyunOssStatClient,
    CloudCostConfig,
)
from blrec.control.operations import ControlOperationJournal
from blrec.exception import ExistsError, ForbiddenError, NotFoundError
from blrec.networking.manager import NetworkRouteManager
from blrec.notification.dispatcher import NotificationDispatcher
from blrec.notification.providers import (
    Bark,
    EmailService,
    Pushdeer,
    Pushplus,
    Serverchan,
    Telegram,
)
from blrec.path.helpers import create_file, file_exists
from blrec.setting import EnvSettings, Settings, SettingsIn
from blrec.setting.file_work import (
    SettingsApplyReconciler,
    SettingsDirectoryError,
    SettingsFileWorkCoordinator,
    SettingsFileWorkSaturated,
    validate_directory_sync,
)
from blrec.setting.models import DEFAULT_LOG_DIR, DEFAULT_OUT_DIR
from blrec.visitor_analytics import (
    AliyunSlsQueryClient,
    VisitorAnalyticsArchive,
    VisitorAnalyticsConfig,
    VisitorAnalyticsService,
    VisitorAnalyticsSynchronizer,
)
from blrec.web.middlewares.base_herf import BaseHrefMiddleware
from blrec.web.middlewares.request_performance import RequestPerformanceMiddleware
from blrec.web.middlewares.route_redirect import RouteRedirectMiddleware
from blrec.web.middlewares.security_headers import SecurityHeadersMiddleware

from ..application import Application
from . import security
from .auth_store import AdminAuthStore
from .password_work import PasswordWorkCoordinator
from .realtime import RealtimeSampler
from .routers import (
    application,
    auth,
    bili_accounts,
    bili_collections,
    browser_extension,
    cloud_cost,
    control_operations,
    highlights,
    live_status,
    media_library,
    network,
    realtime,
    recording_retention,
    recording_sessions,
    room_upload_policies,
    settings,
    tasks,
    update,
    upload_covers,
    vainglory,
    validation,
    visitor_analytics,
    websockets,
)
from .schemas import ResponseMessage

_env_settings = EnvSettings()
_path = os.path.abspath(os.path.expanduser(_env_settings.settings_file))
if not file_exists(_path):
    create_file(_path)
_env_settings.settings_file = _path

_settings = Settings.load(_env_settings.settings_file)
_settings.update_from_env_settings(_env_settings)
for _directory, _default_directory in (
    (_settings.output.out_dir, os.path.normpath(os.path.expanduser(DEFAULT_OUT_DIR))),
    (_settings.logging.log_dir, os.path.normpath(os.path.expanduser(DEFAULT_LOG_DIR))),
):
    if _directory == _default_directory:
        os.makedirs(_directory, exist_ok=True)
    _directory_code, _directory_message = validate_directory_sync(_directory)
    if _directory_code:
        raise SettingsDirectoryError(_directory, _directory_code, _directory_message)
_auth_database_path = os.environ.get(
    'BLREC_AUTH_DATABASE', os.path.join(os.path.dirname(_path), 'auth.sqlite3')
)
_admin_auth_store = AdminAuthStore(
    _auth_database_path, admin_username=_env_settings.admin_username
)
_admin_auth_store.open()
_password_work_coordinator: Optional[PasswordWorkCoordinator] = None
_active_media_service: Optional[ActiveMediaService] = None
_settings_file_work: Optional[SettingsFileWorkCoordinator] = None
_settings_apply_reconciler: Optional[SettingsApplyReconciler] = None
_visitor_analytics_sync: Optional[VisitorAnalyticsSynchronizer] = None
_application_started = False
_control_operation_journal = ControlOperationJournal(
    Path(_path).with_name('control.sqlite3')
)
_recording_outbox = LocalRecordingOutbox(
    Path(
        os.environ.get(
            'BLREC_RECORDING_JOURNAL_DATABASE',
            str(Path(_path).with_name('recording-journal.sqlite3')),
        )
    )
)
_network_route_manager = NetworkRouteManager(lambda: _settings.network)

_notification_providers = {
    'email': EmailService.get_instance(),
    'serverchan': Serverchan.get_instance(),
    'pushdeer': Pushdeer.get_instance(),
    'pushplus': Pushplus.get_instance(),
    'telegram': Telegram.get_instance(),
    'bark': Bark.get_instance(),
}
_notification_dispatcher = NotificationDispatcher(_notification_providers)


def _notification_channel_enabled(channel: str) -> bool:
    setting_name = '{}_notification'.format(channel)
    channel_settings = getattr(_settings, setting_name, None)
    return bool(channel_settings is not None and channel_settings.enabled)


async def _managed_cookie_provider(url: str) -> Optional[str]:
    return await _bili_account_runtime.recording_cookie_header(url)


async def _report_primary_auth_failure(credential_fingerprint: str) -> None:
    await _bili_account_runtime.report_primary_auth_failure(credential_fingerprint)


async def _apply_primary_credential() -> None:
    if _application_started:
        await app.refresh_managed_cookie()


async def _cancel_active_recording(room_id: int) -> None:
    await app.suppress_current_live(room_id)


async def _enable_collect_upload_policy(room_id: int) -> None:
    policy_manager = _bili_account_runtime.policy_manager
    category_catalog = _bili_account_runtime.category_catalog
    if policy_manager is None or category_catalog is None:
        raise RuntimeError('upload policy service is not ready')
    await browser_extension._enable_upload_policy(
        room_id, policy_manager, category_catalog
    )


async def _reconcile_recording_events(journal: Any) -> None:
    projected = await _recording_outbox_runtime.drain_pending(journal)
    if projected:
        logger.info(
            'Projected {} local recording events before remote workers started',
            projected,
        )


_bili_account_runtime = BiliAccountRuntime(
    _settings.bili_upload,
    api_key=_env_settings.api_key,
    credential_key=_env_settings.load_credential_key(),
    old_credential_keys=_env_settings.load_old_credential_keys(),
    space_threshold_bytes=_settings.space.space_threshold,
    recording_root=_settings.output.out_dir,
    recording_capacity_bytes=lambda: _settings.space.recording_capacity,
    capacity_warning_threshold_bytes=(
        lambda: _settings.space.capacity_warning_threshold
    ),
    on_primary_credential_changed=_apply_primary_credential,
    active_session_canceller=_cancel_active_recording,
    network_route_manager=_network_route_manager,
    operational_settings_provider=lambda: _settings.operational_notifications,
    notification_senders={
        channel: _notification_dispatcher.channel(channel)
        for channel in _notification_providers
    },
    notification_channel_enabled=_notification_channel_enabled,
    control_operation_journal=_control_operation_journal,
    recording_event_reconciler=_reconcile_recording_events,
)
app = Application(
    _settings,
    managed_cookie_provider=_managed_cookie_provider,
    auth_failure_reporter=_report_primary_auth_failure,
    recording_journal_provider=lambda: _recording_outbox,
    recording_retention_provider=(lambda: _bili_account_runtime.retention_manager),
    network_route_manager=_network_route_manager,
    control_operation_journal=_control_operation_journal,
    room_upload_policy_enabler=_enable_collect_upload_policy,
    notification_dispatcher=_notification_dispatcher,
)

_cloud_cost_config = CloudCostConfig.from_env()
_cloud_cost_client = AliyunOpenApiClient(
    _cloud_cost_config.access_key_id or '',
    _cloud_cost_config.access_key_secret or '',
    lambda: app.network_session(
        'cloud_cost', anonymous=False, affinity_key='aliyun-cloud-cost'
    ),
)
_cloud_cost_oss_client = AliyunOssStatClient(
    _cloud_cost_config.access_key_id or '',
    _cloud_cost_config.access_key_secret or '',
    lambda: app.network_session(
        'cloud_cost', anonymous=False, affinity_key='aliyun-cloud-cost'
    ),
)
cloud_cost.service = AliyunCloudCostService(
    _cloud_cost_config, _cloud_cost_client, _cloud_cost_oss_client
)

_visitor_analytics_config = VisitorAnalyticsConfig.from_env()
_visitor_analytics_client = AliyunSlsQueryClient(
    _visitor_analytics_config,
    lambda: app.network_session(
        'visitor_analytics', anonymous=False, affinity_key='aliyun-visitor-analytics'
    ),
)
visitor_analytics.service = VisitorAnalyticsService(
    _visitor_analytics_config, _visitor_analytics_client
)


def _bind_bili_runtime_services() -> None:
    runtime = _bili_account_runtime
    bili_accounts.manager = runtime.manager
    bili_accounts.archive_migration = runtime.archive_migration
    bili_accounts.unavailable_reason = runtime.unavailable_reason
    recording_sessions.journal = runtime.journal
    recording_sessions.content_reader = runtime.content_reader
    recording_sessions.remote_media_cache = runtime.remote_media_cache
    recording_sessions.task_actions = runtime.task_actions
    recording_sessions.session_action_runner = runtime.run_recording_session_action
    recording_sessions.session_batch_runner = runtime.run_recording_session_batch
    recording_sessions.submission_manager = runtime.session_submission_manager
    recording_sessions.active_media_service = _active_media_service
    recording_sessions.unavailable_reason = runtime.unavailable_reason
    media_library.library = runtime.media_library
    media_library.item_deleter = runtime.delete_media_library_item
    media_library.unavailable_reason = runtime.unavailable_reason
    recording_retention.manager = runtime.retention_manager
    recording_retention.unavailable_reason = runtime.unavailable_reason
    room_upload_policies.manager = runtime.policy_manager
    room_upload_policies.category_catalog = runtime.category_catalog
    room_upload_policies.unavailable_reason = runtime.unavailable_reason
    upload_covers.library = runtime.cover_library
    upload_covers.unavailable_reason = runtime.unavailable_reason
    bili_collections.manager = runtime.collection_manager
    bili_collections.unavailable_reason = runtime.unavailable_reason
    highlights.service = runtime.highlight_service
    highlights.worker = runtime.highlight_worker
    highlights.upload_task_creator = runtime.create_highlight_upload_task
    highlights.clip_deleter = runtime.delete_highlight_clip
    highlights.unavailable_reason = runtime.unavailable_reason
    browser_extension.highlight_service = runtime.highlight_service
    browser_extension.policy_manager = runtime.policy_manager
    browser_extension.category_catalog = runtime.category_catalog
    browser_extension.vainglory_service = runtime.vainglory_service
    browser_extension.unavailable_reason = runtime.unavailable_reason
    vainglory.service = runtime.vainglory_service
    vainglory.publication = runtime.vainglory_publication
    vainglory.archive_backfill = runtime.archive_backfill
    vainglory.unavailable_reason = runtime.unavailable_reason


async def _on_bili_runtime_ready() -> None:
    global _visitor_analytics_sync
    analytics_database = _bili_account_runtime.database
    if analytics_database is not None and _visitor_analytics_sync is None:
        analytics_archive = VisitorAnalyticsArchive(analytics_database)
        visitor_analytics.service = VisitorAnalyticsService(
            _visitor_analytics_config,
            _visitor_analytics_client,
            archive=analytics_archive,
        )
        _visitor_analytics_sync = VisitorAnalyticsSynchronizer(
            _visitor_analytics_config, _visitor_analytics_client, analytics_archive
        )
    _bind_bili_runtime_services()
    if _application_started:
        await app.refresh_managed_cookie()
        if _visitor_analytics_sync is not None:
            _visitor_analytics_sync.start()


_recording_outbox_runtime = RecordingOutboxRuntime(
    _recording_outbox,
    lambda: _bili_account_runtime,
    on_remote_ready=_on_bili_runtime_ready,
)
recording_sessions.local_outbox = _recording_outbox
recording_sessions.local_outbox_ready_provider = (
    lambda: _recording_outbox_runtime.local_ready
)


async def _persist_network_settings(value: object) -> None:
    await app.change_settings(SettingsIn(network=value))  # type: ignore[arg-type]


_network_route_manager.set_settings_persister(_persist_network_settings)


def _realtime_task_snapshot() -> List[Dict[str, Any]]:
    if not _application_started:
        return []
    return [attr.asdict(data) for data in app.get_all_task_data()]


async def _realtime_upload_snapshot() -> List[Dict[str, object]]:
    journal = _bili_account_runtime.journal
    if journal is None:
        return []
    return await journal.realtime_upload_progress()


async def _realtime_highlight_snapshot() -> List[Mapping[str, object]]:
    highlight_worker = _bili_account_runtime.highlight_worker
    if highlight_worker is None:
        return []
    return list(await highlight_worker.progress())


async def _realtime_archive_migration_snapshot() -> Mapping[str, object]:
    service = _bili_account_runtime.archive_migration
    if service is None:
        return {'migrations': [], 'items': {}}
    statuses = await service.list_statuses()
    migrations: List[Dict[str, object]] = []
    items: Dict[str, object] = {}
    for status_value in statuses:
        migrations.append(
            {
                'id': status_value.id,
                'sourceUid': status_value.source_uid,
                'sourceName': status_value.source_name,
                'downloadAccountId': status_value.download_account_id,
                'targetAccountId': status_value.target_account_id,
                'state': status_value.state,
                'progress': status_value.progress,
                'discoveredCount': status_value.discovered_count,
                'completedCount': status_value.completed_count,
                'failedCount': status_value.failed_count,
                'error': status_value.error,
                'requestedAt': status_value.requested_at,
                'startedAt': status_value.started_at,
                'completedAt': status_value.completed_at,
                'updatedAt': status_value.updated_at,
                'operatorPaused': status_value.operator_paused,
                'dailyLimit': status_value.daily_limit,
                'dailyUsed': status_value.daily_used,
                'quotaDay': status_value.quota_day,
                'todayAnalyzedCount': status_value.today_analyzed_count,
            }
        )
        item_values = await service.list_items(status_value.id, limit=100)
        items[str(status_value.id)] = [
            {
                'id': item.id,
                'migrationId': item.migration_id,
                'bvid': item.bvid,
                'title': item.title,
                'publishedAt': item.published_at,
                'state': item.state,
                'progress': item.progress,
                'pageCount': item.page_count,
                'downloadedPageCount': item.downloaded_page_count,
                'attemptCount': item.attempt_count,
                'sessionId': item.session_id,
                'uploadJobId': item.upload_job_id,
                'uploadState': item.upload_state,
                'submitState': item.submit_state,
                'commentBranchState': item.comment_branch_state,
                'danmakuBranchState': item.danmaku_branch_state,
                'analysisState': item.analysis_state,
                'targetBvid': item.target_bvid,
                'error': item.error,
                'updatedAt': item.updated_at,
            }
            for item in item_values
        ]
    return {'migrations': migrations, 'items': items}


async def _realtime_vainglory_index_snapshot() -> Mapping[str, object]:
    index_service = _bili_account_runtime.vainglory_service
    analysis_queue: Dict[str, object] = {
        'workerState': 'stopped',
        'worker': {
            'state': 'stopped',
            'remoteEnabled': False,
            'workerId': '',
            'modelPackageId': '',
            'pipelineVersion': '',
            'lastSeenAt': None,
        },
        'workers': [],
        'active': [],
        'queued': [],
        'recentCompletions': [],
        'pendingCount': 0,
        'manualPending': 0,
        'realtimePending': 0,
        'archivePending': 0,
        'migrationPending': 0,
        'backlogPending': 0,
        'liveStreamCount': 0,
        'liveRunningCount': 0,
        'livePendingWindowCount': 0,
        'liveSampleCount': 0,
        'liveProvisionalMatchCount': 0,
        'liveLastObservedAt': None,
        'liveItems': [],
    }
    index_summary: Dict[str, int] = {
        'matchCount': 0,
        'sessionCount': 0,
        'anchorCount': 0,
        'unassignedSessionCount': 0,
        'winCount': 0,
        'lossCount': 0,
        'unknownCount': 0,
        'playerSlotCount': 0,
        'recognizedHeroCount': 0,
    }
    if index_service is not None:
        queue_status = await index_service.analysis_queue_status()
        summary = await index_service.index_summary()
        worker_status = index_service.analysis_worker_status
        worker_nodes = await index_service.list_analysis_workers()
        remote_worker_for = index_service.remote_worker_for

        def match_preview(value: Any) -> Dict[str, object]:
            return {
                'matchId': value.match_id,
                'partId': value.part_id,
                'partIndex': value.part_index,
                'resultAtMs': value.result_at_ms,
                'title': value.title,
                'resultFrameUrl': (
                    '/api/v1/vainglory/matches/{}/result-frame?v={}-{}-{}'.format(
                        value.match_id,
                        value.session_id,
                        value.part_id,
                        value.result_at_ms,
                    )
                ),
            }

        def queue_item(value: Any) -> Dict[str, object]:
            return {
                'partId': value.part_id,
                'workerId': remote_worker_for('part', value.part_id),
                'sessionId': value.session_id,
                'partIndex': value.part_index,
                'title': value.title,
                'anchorName': value.anchor_name,
                'state': value.state,
                'stage': value.stage,
                'category': value.category,
                'progress': value.progress,
                'requestedAt': value.requested_at,
                'startedAt': value.started_at,
                'updatedAt': value.updated_at,
                'liveStartedAt': value.live_started_at,
                'partDurationSeconds': value.part_duration_seconds,
                'recordingDurationSeconds': value.recording_duration_seconds,
                'matchCount': value.match_count,
                'partCount': value.part_count,
                'completedPartCount': value.completed_part_count,
                'originalPartCount': value.original_part_count,
                'ignoredPartCount': value.ignored_part_count,
                'runtimeStage': value.runtime_stage,
                'runtimeDetail': value.runtime_detail,
                'runtimeElapsedSeconds': value.runtime_elapsed_seconds,
                'coarseFrames': value.coarse_frames,
                'gameplayRuns': value.gameplay_runs,
                'resultWindows': value.result_windows,
                'currentWindow': value.current_window,
                'totalWindows': value.total_windows,
                'candidateCount': value.candidate_count,
                'currentCandidate': value.current_candidate,
                'totalCandidates': value.total_candidates,
                'rejectedCandidates': value.rejected_candidates,
                'recognizedMatches': value.recognized_matches,
                'modelPackageId': value.model_package_id,
                'keyframeFrames': value.keyframe_frames,
                'seekFillFrames': value.seek_fill_frames,
                'decodedResultFrames': value.decoded_result_frames,
                'modeConflictCount': value.mode_conflict_count,
                'hudLineupCandidateCount': value.hud_lineup_candidate_count,
                'trainingCandidateCount': value.training_candidate_count,
                'bvid': value.bvid,
                'archivePage': value.archive_page,
                'localVideoAvailable': value.local_video_available,
                'imageCount': value.image_count,
                'matchPreviews': [
                    match_preview(preview) for preview in value.match_previews
                ],
                'events': [
                    {
                        'at': event.at,
                        'stage': event.stage,
                        'detail': event.detail,
                        'elapsedSeconds': event.elapsed_seconds,
                    }
                    for event in value.events
                ],
            }

        analysis_queue = {
            'workerState': worker_status.state,
            'worker': {
                'state': worker_status.state,
                'remoteEnabled': worker_status.remote_enabled,
                'workerId': worker_status.worker_id,
                'modelPackageId': worker_status.model_package_id,
                'pipelineVersion': worker_status.pipeline_version,
                'lastSeenAt': worker_status.last_seen_at,
            },
            'workers': [
                {
                    'state': worker.state,
                    'workerId': worker.worker_id,
                    'displayName': worker.display_name,
                    'enabled': worker.enabled,
                    'modelPackageId': worker.model_package_id,
                    'pipelineVersion': worker.pipeline_version,
                    'lastSeenAt': worker.last_seen_at,
                    'activeTaskCount': worker.active_task_count,
                    'activePartIds': list(worker.active_part_ids),
                    'concurrency': worker.concurrency,
                    'completedTaskCount': worker.completed_task_count,
                    'failedTaskCount': worker.failed_task_count,
                    'totalProcessingSeconds': worker.total_processing_seconds,
                    'profiledTaskCount': worker.profiled_task_count,
                    'profiledVideoSeconds': worker.profiled_video_seconds,
                    'totalDecodeAnalysisSeconds': (
                        worker.total_decode_analysis_seconds
                    ),
                    'totalProfiledTaskSeconds': worker.total_profiled_task_seconds,
                    'lastTaskFinishedAt': worker.last_task_finished_at,
                }
                for worker in worker_nodes
            ],
            'active': [queue_item(item) for item in queue_status.active],
            'queued': [queue_item(item) for item in queue_status.queued],
            'recentCompletions': [
                {
                    'completedAt': value.completed_at,
                    'sessionId': value.session_id,
                    'partId': value.part_id,
                    'partIndex': value.part_index,
                    'title': value.title,
                    'partDurationSeconds': value.part_duration_seconds,
                    'recordingDurationSeconds': value.recording_duration_seconds,
                    'partMatchDurationSeconds': value.part_match_duration_seconds,
                    'sessionMatchDurationSeconds': (
                        value.session_match_duration_seconds
                    ),
                    'candidateCount': value.candidate_count,
                    'matchCount': value.match_count,
                    'elapsedSeconds': value.elapsed_seconds,
                    'partCount': value.part_count,
                    'originalPartCount': value.original_part_count,
                    'ignoredPartCount': value.ignored_part_count,
                    'bvid': value.bvid,
                    'archivePage': value.archive_page,
                    'localVideoAvailable': value.local_video_available,
                    'imageCount': value.image_count,
                    'matchPreviews': [
                        match_preview(preview) for preview in value.match_previews
                    ],
                    'analysisSummary': value.analysis_summary,
                }
                for value in queue_status.recent_completions
            ],
            'pendingCount': queue_status.pending_count,
            'manualPending': queue_status.manual_pending,
            'realtimePending': queue_status.realtime_pending,
            'archivePending': queue_status.archive_pending,
            'migrationPending': queue_status.migration_pending,
            'backlogPending': queue_status.backlog_pending,
            'liveStreamCount': queue_status.live_stream_count,
            'liveRunningCount': queue_status.live_running_count,
            'livePendingWindowCount': queue_status.live_pending_window_count,
            'liveSampleCount': queue_status.live_sample_count,
            'liveProvisionalMatchCount': queue_status.live_provisional_match_count,
            'liveLastObservedAt': queue_status.live_last_observed_at,
            'liveItems': [
                {
                    'partId': value.part_id,
                    'sessionId': value.session_id,
                    'partIndex': value.part_index,
                    'title': value.title,
                    'anchorName': value.anchor_name,
                    'roomId': value.room_id,
                    'liveStartedAt': value.live_started_at,
                    'recordingDurationSeconds': value.recording_duration_seconds,
                    'lastObservedAtMs': value.last_observed_at_ms,
                    'sampleCount': value.sample_count,
                    'fineScanCount': value.fine_scan_count,
                    'lastSampleAt': value.last_sample_at,
                    'nextSampleAt': value.next_sample_at,
                    'matchFlowLabel': value.match_flow_label,
                    'matchFlowConfidence': value.match_flow_confidence,
                    'workerId': value.worker_id,
                    'pendingWindowCount': value.pending_window_count,
                    'runningWindowCount': value.running_window_count,
                    'completedWindowCount': value.completed_window_count,
                    'failedWindowCount': value.failed_window_count,
                    'provisionalMatchCount': value.provisional_match_count,
                    'lastError': value.last_error,
                }
                for value in queue_status.live_items
            ],
        }
        index_summary = {
            'matchCount': summary.match_count,
            'sessionCount': summary.session_count,
            'anchorCount': summary.anchor_count,
            'unassignedSessionCount': summary.unassigned_session_count,
            'winCount': summary.win_count,
            'lossCount': summary.loss_count,
            'unknownCount': summary.unknown_count,
            'playerSlotCount': summary.player_slot_count,
            'recognizedHeroCount': summary.recognized_hero_count,
        }
    return {
        'sampledAt': int(time.time()),
        'analysisQueue': analysis_queue,
        'indexSummary': index_summary,
    }


async def _realtime_archive_backfill_snapshot() -> Mapping[str, object]:
    service = _bili_account_runtime.archive_backfill
    remote_media_cache = _bili_account_runtime.remote_media_cache
    download_queue: Optional[Dict[str, object]] = None
    if remote_media_cache is not None:
        queue = await remote_media_cache.queue_status()
        download_queue = {
            'pendingDownloadCount': queue.pending_download_count,
            'pendingDownloadArchiveCount': queue.pending_download_archive_count,
            'activeDownloadCount': queue.active_download_count,
            'activeDownloadArchiveCount': queue.active_download_archive_count,
            'downloadedWaitingAnalysisCount': (queue.downloaded_waiting_analysis_count),
            'downloadedWaitingAnalysisArchiveCount': (
                queue.downloaded_waiting_analysis_archive_count
            ),
            'activeAnalysisCount': queue.active_analysis_count,
            'activeAnalysisArchiveCount': queue.active_analysis_archive_count,
            'failedDownloadCount': queue.failed_download_count,
            'failedDownloadArchiveCount': queue.failed_download_archive_count,
            'downloadsPerInterface': queue.downloads_per_interface,
            'interfaceCount': queue.interface_count,
            'totalConcurrency': queue.total_concurrency,
            'latestActivityAt': queue.latest_activity_at,
        }
    if service is None:
        return {'syncs': [], 'items': {}, 'downloadQueue': download_queue}
    statuses = await service.list_statuses()
    syncs: List[Dict[str, object]] = []
    items: Dict[str, object] = {}
    for status_value in statuses:
        syncs.append(
            {
                'accountId': status_value.account_id,
                'state': status_value.state,
                'progress': status_value.progress,
                'discoveredCount': status_value.discovered_count,
                'completedCount': status_value.completed_count,
                'error': status_value.error,
                'requestedAt': status_value.requested_at,
                'startedAt': status_value.started_at,
                'completedAt': status_value.completed_at,
                'updatedAt': status_value.updated_at,
                'operatorPaused': status_value.operator_paused,
                'dailyLimit': status_value.daily_limit,
                'dailyUsed': status_value.daily_used,
                'quotaDay': status_value.quota_day,
                'nextPage': status_value.next_page,
                'discoveryComplete': status_value.discovery_complete,
                'seasonStartedAt': status_value.season_started_at,
                'seasonEndedAt': status_value.season_ended_at,
                'todayAnalyzedCount': status_value.today_analyzed_count,
            }
        )
        item_values = await service.list_items(status_value.account_id, limit=30)
        items[str(status_value.account_id)] = [
            {
                'id': item.id,
                'accountId': item.account_id,
                'aid': item.aid,
                'bvid': item.bvid,
                'title': item.title,
                'publishedAt': item.published_at,
                'state': item.state,
                'stage': item.stage,
                'progress': item.progress,
                'pageCount': item.page_count,
                'completedPageCount': item.completed_page_count,
                'currentPage': item.current_page,
                'currentPartTitle': item.current_part_title,
                'downloadProgress': item.download_progress,
                'downloadedBytes': item.downloaded_bytes,
                'totalBytes': item.total_bytes,
                'analysisState': item.analysis_state,
                'analysisProgress': item.analysis_progress,
                'matchCount': item.match_count,
                'publicationState': item.publication_state,
                'descriptionState': item.description_state,
                'commentCount': item.comment_count,
                'confirmedCommentCount': item.confirmed_comment_count,
                'pinState': item.pin_state,
                'publicationProgress': item.publication_progress,
                'error': item.error,
                'updatedAt': item.updated_at,
            }
            for item in item_values
        ]
    return {'syncs': syncs, 'items': items, 'downloadQueue': download_queue}


_realtime_sampler = RealtimeSampler(
    realtime.broker,
    task_provider=_realtime_task_snapshot,
    network_provider=network.snapshot,
    upload_provider=_realtime_upload_snapshot,
    highlight_provider=_realtime_highlight_snapshot,
    archive_migration_provider=_realtime_archive_migration_snapshot,
    archive_backfill_provider=_realtime_archive_backfill_snapshot,
    vainglory_index_provider=_realtime_vainglory_index_snapshot,
)


def _active_recording_metadata(resource: MediaResource) -> Optional[object]:
    if not _application_started or resource.path is None:
        return None
    try:
        task = app.get_task_data(resource.room_id)
        metadata = app.get_task_metadata(resource.room_id)
    except (NotFoundError, RuntimeError):
        return None
    recording_path = task.task_status.recording_path
    if recording_path is None or metadata is None:
        return None
    return ActiveMediaMetadata(recording_path=recording_path, value=metadata)


async def _active_highlight_durations(session_id: int) -> Mapping[int, int]:
    journal = _bili_account_runtime.journal
    reader = _bili_account_runtime.content_reader
    service = _active_media_service
    if not _application_started or journal is None or reader is None or service is None:
        return {}
    part = await journal.active_part_for_session(session_id)
    if part is None:
        return {}
    try:
        resource = await reader.media(part.id)
    except (RecordingContentNotFound, RecordingContentUnavailable):
        return {}
    if (
        not resource.recording
        or resource.path is None
        or resource.size is None
        or resource.content_type != 'video/x-flv'
    ):
        return {}
    metadata = _active_recording_metadata(resource)
    if metadata is None:
        return {}
    try:
        snapshot = await service.snapshot(
            part.id, resource.path, resource.size, metadata
        )
    except (
        ActiveMediaBusy,
        OSError,
        EOFError,
        ValueError,
        AssertionError,
        RuntimeError,
    ):
        return {}
    if snapshot.duration_ms is None:
        return {}
    return {part.id: snapshot.duration_ms}


bili_accounts.manager = None
bili_accounts.archive_migration = None
bili_accounts.unavailable_reason = _bili_account_runtime.unavailable_reason
recording_sessions.journal = None
recording_sessions.content_reader = None
recording_sessions.remote_media_cache = None
recording_sessions.task_actions = None
recording_sessions.session_action_runner = None
recording_sessions.session_batch_runner = None
recording_sessions.submission_manager = None
recording_sessions.active_media_service = None
recording_sessions.active_recording_metadata_provider = _active_recording_metadata
recording_sessions.unavailable_reason = _bili_account_runtime.unavailable_reason
media_library.library = None
media_library.item_deleter = None
media_library.unavailable_reason = _bili_account_runtime.unavailable_reason
recording_retention.manager = None
recording_retention.unavailable_reason = _bili_account_runtime.unavailable_reason
room_upload_policies.manager = None
room_upload_policies.category_catalog = None
room_upload_policies.unavailable_reason = _bili_account_runtime.unavailable_reason
upload_covers.library = None
upload_covers.unavailable_reason = _bili_account_runtime.unavailable_reason
bili_collections.manager = None
bili_collections.unavailable_reason = _bili_account_runtime.unavailable_reason
highlights.service = None
highlights.worker = None
highlights.upload_task_creator = None
highlights.clip_deleter = None
highlights.active_durations_provider = _active_highlight_durations
highlights.unavailable_reason = _bili_account_runtime.unavailable_reason
browser_extension.application = app
browser_extension.highlight_service = None
browser_extension.policy_manager = None
browser_extension.category_catalog = None
browser_extension.vainglory_service = None
browser_extension.unavailable_reason = _bili_account_runtime.unavailable_reason
vainglory.service = None
vainglory.publication = None
vainglory.archive_backfill = None
vainglory.unavailable_reason = _bili_account_runtime.unavailable_reason
network.manager = _network_route_manager
control_operations.journal = _control_operation_journal

_dependencies = [Depends(security.authenticate)]

api = FastAPI(
    title='Bilibili live streaming recorder web API',
    description='Web API to communicate with the backend application',
    version='v1',
    dependencies=_dependencies,
)

api.add_middleware(BaseHrefMiddleware)
api.add_middleware(RequestPerformanceMiddleware)
api.add_middleware(BrotliMiddleware)
api.add_middleware(SecurityHeadersMiddleware)
api.add_middleware(
    CORSMiddleware,
    allow_origins=[
        'http://localhost:4200',
        'http://127.0.0.1:4200',
    ],  # angular development
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
    expose_headers=[
        'Accept-Ranges',
        'Content-Length',
        'Content-Range',
        'ETag',
        'Cache-Control',
        'Content-Disposition',
        'X-BLREC-Operation-ID',
    ],
)
api.add_middleware(RouteRedirectMiddleware)


@api.exception_handler(NotFoundError)
async def not_found_error_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=dict(ResponseMessage(code=status.HTTP_404_NOT_FOUND, message=str(exc))),
    )


@api.exception_handler(ForbiddenError)
async def forbidden_error_handler(
    request: Request, exc: ForbiddenError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content=dict(ResponseMessage(code=status.HTTP_403_FORBIDDEN, message=str(exc))),
    )


@api.exception_handler(ExistsError)
async def exists_error_handler(request: Request, exc: ExistsError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=dict(ResponseMessage(code=status.HTTP_409_CONFLICT, message=str(exc))),
    )


@api.exception_handler(ValidationError)
async def validation_error_handler(
    request: Request, exc: ValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_406_NOT_ACCEPTABLE,
        content=dict(
            ResponseMessage(code=status.HTTP_406_NOT_ACCEPTABLE, message=str(exc))
        ),
    )


@api.exception_handler(SettingsFileWorkSaturated)
async def settings_file_work_saturated_handler(
    request: Request, exc: SettingsFileWorkSaturated
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        headers={'Retry-After': str(exc.retry_after)},
        content=dict(
            ResponseMessage(
                code=status.HTTP_503_SERVICE_UNAVAILABLE,
                message='settings file work is saturated',
            )
        ),
    )


@api.exception_handler(SettingsDirectoryError)
async def settings_directory_error_handler(
    request: Request, exc: SettingsDirectoryError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_406_NOT_ACCEPTABLE,
        content=dict(ResponseMessage(code=exc.code, message=exc.message)),
    )


@api.on_event('startup')
async def on_startup() -> None:
    global _active_media_service, _application_started, _password_work_coordinator
    global _settings_apply_reconciler, _settings_file_work, _visitor_analytics_sync
    _admin_auth_store.open()
    password_work = PasswordWorkCoordinator()
    active_media = ActiveMediaService()
    settings_file_work = SettingsFileWorkCoordinator()

    async def apply_settings_target(target_key: str, action: str) -> None:
        await app.apply_settings_target(target_key, action)

    settings_apply = SettingsApplyReconciler(
        _control_operation_journal, apply_settings_target
    )
    _password_work_coordinator = password_work
    _active_media_service = active_media
    _settings_file_work = settings_file_work
    _settings_apply_reconciler = settings_apply
    set_file_work = getattr(app, 'set_settings_file_work', None)
    if set_file_work is not None:
        set_file_work(settings_file_work)
    set_apply = getattr(app, 'set_settings_apply_reconciler', None)
    if set_apply is not None:
        set_apply(settings_apply)
    application_launch_entered = False
    settings_apply_started = False
    try:
        security.configure(
            _admin_auth_store,
            bootstrap_api_key=_env_settings.api_key or '',
            worker_token=os.environ.get('BLREC_ANALYSIS_WORKER_TOKEN', ''),
        )
        auth.configure(
            _admin_auth_store,
            password_work=password_work,
            bootstrap_api_key=_env_settings.api_key or '',
        )
        browser_extension.application = app
        await _notification_dispatcher.start()
        await _recording_outbox_runtime.start()
        _bind_bili_runtime_services()
        application_launch_entered = True
        await app.launch()
        await settings_apply.recover()
        settings_apply.start()
        settings_apply_started = True
        _application_started = True
        await app.refresh_managed_cookie()
        _realtime_sampler.start()
        if _visitor_analytics_sync is not None:
            _visitor_analytics_sync.start()
    except BaseException:
        settings_apply.close_admission()
        close_settings = getattr(app, 'close_settings_mutation_admission', None)
        if close_settings is not None:
            await close_settings()
        else:
            settings_file_work.close_admission()
        password_work.close_admission()
        active_media.close_admission()
        _application_started = False
        try:
            try:
                auth.reset()
            finally:
                security.reset()
            bili_accounts.manager = None
            bili_accounts.archive_migration = None
            recording_sessions.journal = None
            recording_sessions.content_reader = None
            recording_sessions.remote_media_cache = None
            recording_sessions.task_actions = None
            recording_sessions.session_action_runner = None
            recording_sessions.session_batch_runner = None
            recording_sessions.submission_manager = None
            recording_sessions.active_media_service = None
            media_library.library = None
            media_library.item_deleter = None
            recording_retention.manager = None
            room_upload_policies.manager = None
            room_upload_policies.category_catalog = None
            upload_covers.library = None
            bili_collections.manager = None
            highlights.service = None
            highlights.worker = None
            highlights.upload_task_creator = None
            highlights.clip_deleter = None
            vainglory.service = None
            vainglory.publication = None
            vainglory.archive_backfill = None
            browser_extension.reset()
            await _realtime_sampler.stop()
            if settings_apply_started:
                await settings_apply.shutdown()
            await settings_file_work.shutdown()
            if application_launch_entered:
                await app.exit()
        finally:
            try:
                if _visitor_analytics_sync is not None:
                    await _visitor_analytics_sync.close()
                    _visitor_analytics_sync = None
                try:
                    await _recording_outbox_runtime.close()
                finally:
                    await _bili_account_runtime.close()
            finally:
                try:
                    await _notification_dispatcher.close(drain_timeout_seconds=15)
                finally:
                    try:
                        await active_media.shutdown()
                    finally:
                        try:
                            await password_work.shutdown()
                        finally:
                            _active_media_service = None
                            _password_work_coordinator = None
                            _settings_apply_reconciler = None
                            _settings_file_work = None
                            _admin_auth_store.close()
                            await _control_operation_journal.close()
        raise


@api.on_event('shutdown')
async def on_shuntdown() -> None:
    global _active_media_service, _application_started, _password_work_coordinator
    global _settings_apply_reconciler, _settings_file_work, _visitor_analytics_sync
    password_work = _password_work_coordinator
    active_media = _active_media_service
    settings_apply = _settings_apply_reconciler
    settings_file_work = _settings_file_work
    if password_work is not None:
        password_work.close_admission()
    if active_media is not None:
        active_media.close_admission()
    if settings_apply is not None:
        settings_apply.close_admission()
    close_settings = getattr(app, 'close_settings_mutation_admission', None)
    if close_settings is not None:
        await close_settings()
    elif settings_file_work is not None:
        settings_file_work.close_admission()
    _application_started = False
    try:
        try:
            auth.reset()
        finally:
            security.reset()
        await _realtime_sampler.stop()
        bili_accounts.manager = None
        bili_accounts.archive_migration = None
        recording_sessions.journal = None
        recording_sessions.content_reader = None
        recording_sessions.remote_media_cache = None
        recording_sessions.task_actions = None
        recording_sessions.session_action_runner = None
        recording_sessions.session_batch_runner = None
        recording_sessions.submission_manager = None
        recording_sessions.active_media_service = None
        media_library.library = None
        media_library.item_deleter = None
        recording_retention.manager = None
        room_upload_policies.manager = None
        room_upload_policies.category_catalog = None
        upload_covers.library = None
        bili_collections.manager = None
        highlights.service = None
        highlights.worker = None
        highlights.upload_task_creator = None
        highlights.clip_deleter = None
        vainglory.service = None
        vainglory.publication = None
        vainglory.archive_backfill = None
        browser_extension.reset()
        try:
            if settings_apply is not None:
                await settings_apply.shutdown()
            if settings_file_work is not None:
                await settings_file_work.shutdown()
            await app.exit()
        finally:
            try:
                if _visitor_analytics_sync is not None:
                    await _visitor_analytics_sync.close()
                    _visitor_analytics_sync = None
                try:
                    await _recording_outbox_runtime.close()
                finally:
                    await _bili_account_runtime.close()
            finally:
                await _notification_dispatcher.close(drain_timeout_seconds=15)
    finally:
        try:
            if active_media is not None:
                await active_media.shutdown()
        finally:
            try:
                if password_work is not None:
                    await password_work.shutdown()
            finally:
                _active_media_service = None
                _password_work_coordinator = None
                _settings_apply_reconciler = None
                _settings_file_work = None
                _admin_auth_store.close()
                await _control_operation_journal.close()


tasks.app = app
settings.app = app
application.app = app
validation.app = app
websockets.app = app
update.app = app
live_status.app = app
api.include_router(auth.router, prefix='/api/v1')
api.include_router(control_operations.router, prefix='/api/v1')
api.include_router(tasks.router)
api.include_router(settings.router)
api.include_router(application.router)
api.include_router(validation.router)
api.include_router(websockets.router)
api.include_router(update.router)
api.include_router(live_status.router, prefix='/api/v1')
api.include_router(network.router, prefix='/api/v1')
api.include_router(cloud_cost.router, prefix='/api/v1')
api.include_router(visitor_analytics.router, prefix='/api/v1')
api.include_router(realtime.router, prefix='/api/v1')
api.include_router(bili_accounts.router, prefix='/api/v1')
api.include_router(recording_sessions.router, prefix='/api/v1')
api.include_router(media_library.router, prefix='/api/v1')
api.include_router(recording_retention.router, prefix='/api/v1')
api.include_router(room_upload_policies.router, prefix='/api/v1')
api.include_router(upload_covers.router, prefix='/api/v1')
api.include_router(bili_collections.router, prefix='/api/v1')
api.include_router(highlights.router, prefix='/api/v1')
api.include_router(vainglory.router, prefix='/api/v1')
api.include_router(browser_extension.router, prefix='/api/v1')


class WebAppFiles(StaticFiles):
    def lookup_path(self, path: str) -> Tuple[str, Optional[os.stat_result]]:
        if path == '404.html':
            path = 'index.html'
        return super().lookup_path(path)

    def file_response(self, full_path: str, *args, **kwargs) -> Response:  # type: ignore # noqa
        # ignore MIME types from Windows registry
        # workaround for https://github.com/acgnhiki/blrec/issues/12
        response = super().file_response(full_path, *args, **kwargs)
        if full_path.endswith('.js'):
            js_media_type = 'application/javascript'
            if response.media_type != js_media_type:
                response.media_type = js_media_type
                headers = response.headers
                headers['content-type'] = js_media_type
                response.raw_headers = headers.raw
                del response._headers
        return response


directory = resource_filename(__name__, '../data/webapp')
api.mount('/', WebAppFiles(directory=directory, html=True), name='webapp')
