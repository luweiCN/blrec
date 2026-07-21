import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import attr
from brotli_asgi import BrotliMiddleware
from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
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
from blrec.bili_upload.runtime import BiliAccountRuntime
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
    control_operations,
    highlights,
    live_status,
    network,
    realtime,
    recording_retention,
    recording_sessions,
    room_upload_policies,
    settings,
    tasks,
    update,
    upload_covers,
    validation,
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
_application_started = False
_control_operation_journal = ControlOperationJournal(
    Path(_path).with_name('control.sqlite3')
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
)
app = Application(
    _settings,
    managed_cookie_provider=_managed_cookie_provider,
    auth_failure_reporter=_report_primary_auth_failure,
    recording_journal_provider=lambda: _bili_account_runtime.journal,
    recording_retention_provider=(lambda: _bili_account_runtime.retention_manager),
    network_route_manager=_network_route_manager,
    control_operation_journal=_control_operation_journal,
    room_upload_policy_enabler=_enable_collect_upload_policy,
    notification_dispatcher=_notification_dispatcher,
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


_realtime_sampler = RealtimeSampler(
    realtime.broker,
    task_provider=_realtime_task_snapshot,
    network_provider=network.snapshot,
    upload_provider=_realtime_upload_snapshot,
    highlight_provider=_realtime_highlight_snapshot,
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
bili_accounts.unavailable_reason = _bili_account_runtime.unavailable_reason
recording_sessions.journal = None
recording_sessions.content_reader = None
recording_sessions.task_actions = None
recording_sessions.session_action_runner = None
recording_sessions.session_batch_runner = None
recording_sessions.submission_manager = None
recording_sessions.active_media_service = None
recording_sessions.active_recording_metadata_provider = _active_recording_metadata
recording_sessions.unavailable_reason = _bili_account_runtime.unavailable_reason
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
browser_extension.unavailable_reason = _bili_account_runtime.unavailable_reason
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
    global _settings_apply_reconciler, _settings_file_work
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
            _admin_auth_store, bootstrap_api_key=_env_settings.api_key or ''
        )
        auth.configure(
            _admin_auth_store,
            password_work=password_work,
            bootstrap_api_key=_env_settings.api_key or '',
        )
        browser_extension.application = app
        await _notification_dispatcher.start()
        await _bili_account_runtime.start()
        bili_accounts.manager = _bili_account_runtime.manager
        bili_accounts.unavailable_reason = _bili_account_runtime.unavailable_reason
        recording_sessions.journal = _bili_account_runtime.journal
        recording_sessions.content_reader = _bili_account_runtime.content_reader
        recording_sessions.task_actions = _bili_account_runtime.task_actions
        recording_sessions.session_action_runner = (
            _bili_account_runtime.run_recording_session_action
        )
        recording_sessions.session_batch_runner = (
            _bili_account_runtime.run_recording_session_batch
        )
        recording_sessions.submission_manager = (
            _bili_account_runtime.session_submission_manager
        )
        recording_sessions.active_media_service = active_media
        recording_sessions.unavailable_reason = _bili_account_runtime.unavailable_reason
        recording_retention.manager = _bili_account_runtime.retention_manager
        recording_retention.unavailable_reason = (
            _bili_account_runtime.unavailable_reason
        )
        room_upload_policies.manager = _bili_account_runtime.policy_manager
        room_upload_policies.category_catalog = _bili_account_runtime.category_catalog
        room_upload_policies.unavailable_reason = (
            _bili_account_runtime.unavailable_reason
        )
        upload_covers.library = _bili_account_runtime.cover_library
        upload_covers.unavailable_reason = _bili_account_runtime.unavailable_reason
        bili_collections.manager = _bili_account_runtime.collection_manager
        bili_collections.unavailable_reason = _bili_account_runtime.unavailable_reason
        highlights.service = _bili_account_runtime.highlight_service
        highlights.worker = _bili_account_runtime.highlight_worker
        highlights.upload_task_creator = (
            _bili_account_runtime.create_highlight_upload_task
        )
        highlights.clip_deleter = _bili_account_runtime.delete_highlight_clip
        highlights.unavailable_reason = _bili_account_runtime.unavailable_reason
        browser_extension.highlight_service = _bili_account_runtime.highlight_service
        browser_extension.policy_manager = _bili_account_runtime.policy_manager
        browser_extension.category_catalog = _bili_account_runtime.category_catalog
        browser_extension.unavailable_reason = _bili_account_runtime.unavailable_reason
        application_launch_entered = True
        await app.launch()
        await settings_apply.recover()
        settings_apply.start()
        settings_apply_started = True
        _application_started = True
        await app.refresh_managed_cookie()
        _realtime_sampler.start()
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
            recording_sessions.journal = None
            recording_sessions.content_reader = None
            recording_sessions.task_actions = None
            recording_sessions.session_action_runner = None
            recording_sessions.session_batch_runner = None
            recording_sessions.submission_manager = None
            recording_sessions.active_media_service = None
            recording_retention.manager = None
            room_upload_policies.manager = None
            room_upload_policies.category_catalog = None
            upload_covers.library = None
            bili_collections.manager = None
            highlights.service = None
            highlights.worker = None
            highlights.upload_task_creator = None
            highlights.clip_deleter = None
            browser_extension.reset()
            await _realtime_sampler.stop()
            if settings_apply_started:
                await settings_apply.shutdown()
            await settings_file_work.shutdown()
            if application_launch_entered:
                await app.exit()
        finally:
            try:
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
    global _settings_apply_reconciler, _settings_file_work
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
        recording_sessions.journal = None
        recording_sessions.content_reader = None
        recording_sessions.task_actions = None
        recording_sessions.session_action_runner = None
        recording_sessions.session_batch_runner = None
        recording_sessions.submission_manager = None
        recording_sessions.active_media_service = None
        recording_retention.manager = None
        room_upload_policies.manager = None
        room_upload_policies.category_catalog = None
        upload_covers.library = None
        bili_collections.manager = None
        highlights.service = None
        highlights.worker = None
        highlights.upload_task_creator = None
        highlights.clip_deleter = None
        browser_extension.reset()
        try:
            if settings_apply is not None:
                await settings_apply.shutdown()
            if settings_file_work is not None:
                await settings_file_work.shutdown()
            await app.exit()
        finally:
            try:
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
api.include_router(realtime.router, prefix='/api/v1')
api.include_router(bili_accounts.router, prefix='/api/v1')
api.include_router(recording_sessions.router, prefix='/api/v1')
api.include_router(recording_retention.router, prefix='/api/v1')
api.include_router(room_upload_policies.router, prefix='/api/v1')
api.include_router(upload_covers.router, prefix='/api/v1')
api.include_router(bili_collections.router, prefix='/api/v1')
api.include_router(highlights.router, prefix='/api/v1')
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
