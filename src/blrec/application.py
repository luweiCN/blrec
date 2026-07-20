from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
)

import aiohttp
import attr
import psutil
from loguru import logger
from pydantic import BaseModel as PydanticBaseModel

from . import __prog__, __version__
from .bili.live_status import BreakerState
from .exception import ExceptionHandler, ExistsError, exception_callback
from .setting import Settings, SettingsIn, SettingsManager, SettingsOut, TaskOptions
from .setting.typing import KeySetOfSettings
from .utils.string import camel_case

if TYPE_CHECKING:
    from .bili.live_status_coordinator import LiveStatusCoordinator
    from .bili_upload.journal import RecordingJournalBridge
    from .bili_upload.retention import RetentionManager
    from .control.operations import ControlOperationJournal, ControlOperationSnapshot
    from .core.typing import MetaData
    from .flv.operators import StreamProfile
    from .networking.aiohttp_session import AiohttpSessionPool
    from .networking.manager import NetworkRouteManager
    from .notification.dispatcher import NotificationDispatcher
    from .setting.file_work import SettingsApplyReconciler, SettingsFileWorkCoordinator
    from .task import DanmakuFileDetail, TaskData, TaskParam, VideoFileDetail


@attr.s(auto_attribs=True, slots=True, frozen=True)
class AppInfo:
    name: str
    version: str
    pid: int
    ppid: int
    create_time: float
    cwd: str
    exe: str
    cmdline: List[str]


@attr.s(auto_attribs=True, slots=True, frozen=True)
class AppStatus:
    cpu_percent: float
    memory_percent: float
    num_threads: int


class CoordinatorMetrics(PydanticBaseModel):
    mode: str
    interval_seconds: int
    batch_size: int
    registered_rooms: int
    active_websockets: int
    last_success_at: Optional[float]
    snapshot_max_age_seconds: Optional[float]
    missing_results: int
    fallback_requests: int
    breaker_state: BreakerState
    breaker_reason: Optional[str]

    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True
        frozen = True


def _raise_teardown_errors(errors: List[BaseException]) -> None:
    if not errors:
        return
    primary = next(
        (error for error in errors if isinstance(error, asyncio.CancelledError)),
        errors[0],
    )
    for error in errors:
        if error is not primary:
            logger.error('Additional teardown error: {!r}', error)
    raise primary


async def _collect_teardown_error(
    operation: Awaitable[None], errors: List[BaseException]
) -> None:
    try:
        await operation
    except BaseException as error:
        errors.append(error)


class Application:
    def __init__(
        self,
        settings: Settings,
        *,
        managed_cookie_provider: Optional[
            Callable[[str], Awaitable[Optional[str]]]
        ] = None,
        auth_failure_reporter: Optional[Callable[[str], Awaitable[None]]] = None,
        recording_journal_provider: Optional[
            Callable[[], Optional[RecordingJournalBridge]]
        ] = None,
        recording_retention_provider: Optional[
            Callable[[], Optional[RetentionManager]]
        ] = None,
        network_route_manager: Optional[NetworkRouteManager] = None,
        control_operation_journal: Optional[ControlOperationJournal] = None,
        room_upload_policy_enabler: Optional[Callable[[int], Awaitable[None]]] = None,
        validation_timeout_seconds: float = 10,
        notification_dispatcher: Optional[NotificationDispatcher] = None,
        settings_file_work: Optional[SettingsFileWorkCoordinator] = None,
    ) -> None:
        if not 0 < validation_timeout_seconds <= 10:
            raise ValueError('validation_timeout_seconds must be in (0, 10]')
        self._settings = settings
        self._out_dir = settings.output.out_dir
        self._settings_manager = SettingsManager(
            self, settings, file_work=settings_file_work
        )
        self._live_status_session: Optional[Any] = None
        self._live_status_coordinator: Optional[LiveStatusCoordinator] = None
        self._network_route_manager = network_route_manager
        self._network_session_pool: Optional[AiohttpSessionPool] = None
        self._bili_validation_session: Optional[Any] = None
        self._validation_timeout_seconds = validation_timeout_seconds
        self._notification_dispatcher = notification_dispatcher
        self._managed_cookie_provider = managed_cookie_provider
        self._auth_failure_reporter = auth_failure_reporter
        self._recording_journal_provider = recording_journal_provider
        self._recording_retention_provider = recording_retention_provider
        self._control_operation_journal = control_operation_journal
        self._room_upload_policy_enabler = room_upload_policy_enabler
        self._task_control_reconciler: Optional[Any] = None
        self._room_membership_reconciler: Optional[Any] = None
        self._settings_apply_reconciler: Optional[SettingsApplyReconciler] = None

    def set_settings_apply_reconciler(
        self, reconciler: SettingsApplyReconciler
    ) -> None:
        self._settings_apply_reconciler = reconciler
        self._settings_manager.set_apply_reconciler(reconciler)

    def set_settings_file_work(self, coordinator: SettingsFileWorkCoordinator) -> None:
        self._settings_manager.set_file_work(coordinator)

    async def close_settings_mutation_admission(self) -> None:
        errors: List[BaseException] = []
        self._settings_manager.stop_mutation_admission()
        membership = getattr(self, '_room_membership_reconciler', None)
        reconciler = getattr(self, '_task_control_reconciler', None)
        if membership is not None:
            membership.close_admission()
        self._room_membership_reconciler = None
        if membership is not None:
            await _collect_teardown_error(membership.shutdown(), errors)
        if reconciler is not None:
            reconciler.close_admission()
        self._task_control_reconciler = None
        if reconciler is not None:
            await _collect_teardown_error(reconciler.shutdown(), errors)
        await self._settings_manager.close_mutation_admission()
        _raise_teardown_errors(errors)

    @property
    def info(self) -> AppInfo:
        p = psutil.Process(os.getpid())
        with p.oneshot():
            return AppInfo(
                name=__prog__,
                version=__version__,
                pid=p.pid,
                ppid=p.ppid(),
                create_time=p.create_time(),
                cwd=p.cwd(),
                exe=p.exe(),
                cmdline=p.cmdline(),
            )

    @property
    def status(self) -> AppStatus:
        p = psutil.Process(os.getpid())
        with p.oneshot():
            return AppStatus(
                cpu_percent=p.cpu_percent(),
                memory_percent=p.memory_percent(),
                num_threads=p.num_threads(),
            )

    def run(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()

        await self.launch()
        try:
            self._interrupt_event = asyncio.Event()
            await self._interrupt_event.wait()
        finally:
            await self.exit()

    async def launch(self) -> None:
        self._setup_logger()
        logger.info('Launching Application...')
        await self._setup_live_status_monitor()
        try:
            self._setup()
            await self._start_webhooks()
            await self._start_update_metadata()
            if self._control_operation_journal is not None:
                from .task.control_reconciler import TaskControlReconciler
                from .task.membership_reconciler import RoomMembershipReconciler

                await self._control_operation_journal.open()
                self._task_control_reconciler = TaskControlReconciler(
                    self._control_operation_journal,
                    self._settings_manager,
                    self._task_manager,
                )
                self._room_membership_reconciler = RoomMembershipReconciler(
                    self._control_operation_journal,
                    self._settings_manager,
                    self._task_manager,
                    self._task_control_reconciler,
                    room_id_resolver=self._resolve_room_id,
                    upload_policy_enabler=self._room_upload_policy_enabler,
                )
                self._task_control_reconciler.set_desired_absent_provider(
                    self._room_membership_reconciler.desires_absent
                )
            self._loading_task = asyncio.create_task(self._load_tasks_and_controls())

            def callback(future: asyncio.Future) -> None:  # type: ignore
                del self._loading_task

            self._loading_task.add_done_callback(exception_callback)
            self._loading_task.add_done_callback(callback)
        except BaseException as error:
            try:
                await self._teardown_update_metadata()
            except asyncio.CancelledError as cleanup_error:
                raise cleanup_error from error
            except BaseException as cleanup_error:
                logger.error(
                    'Update metadata teardown after launch failure: {}',
                    type(cleanup_error).__name__,
                )
            try:
                await self._teardown_webhooks()
            except asyncio.CancelledError as cleanup_error:
                raise cleanup_error from error
            except BaseException as cleanup_error:
                logger.error(
                    'Webhook teardown after launch failure: {}',
                    type(cleanup_error).__name__,
                )
            try:
                self._destroy()
            except asyncio.CancelledError as cleanup_error:
                raise cleanup_error from error
            except BaseException as cleanup_error:
                logger.error(
                    'Application teardown after launch failure: {}',
                    type(cleanup_error).__name__,
                )
            await self._teardown_live_status_monitor_after_failure(error)
            raise
        logger.debug(f'Default umask {os.umask(0o000)}')
        logger.info(f'Launched Application v{__version__}')

    async def exit(self) -> None:
        logger.info('Exiting Application...')
        await self._exit()
        logger.info('Exited Application')

    async def abort(self) -> None:
        logger.info('Aborting Application...')
        await self._exit(force=True)
        logger.info('Aborted Application')

    async def _exit(self, force: bool = False) -> None:
        errors: List[BaseException] = []
        if hasattr(self, '_loading_task'):
            self._loading_task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await self._loading_task
            except BaseException as error:
                errors.append(error)
        await _collect_teardown_error(self._teardown_update_metadata(), errors)
        await _collect_teardown_error(self._teardown_webhooks(), errors)
        membership = getattr(self, '_room_membership_reconciler', None)
        self._room_membership_reconciler = None
        if membership is not None:
            await _collect_teardown_error(membership.shutdown(), errors)
        reconciler = getattr(self, '_task_control_reconciler', None)
        self._task_control_reconciler = None
        if reconciler is not None:
            await _collect_teardown_error(reconciler.shutdown(), errors)
        task_manager = getattr(self, '_task_manager', None)
        if task_manager is not None:
            await _collect_teardown_error(
                task_manager.stop_all_tasks(force=force), errors
            )
            await _collect_teardown_error(task_manager.destroy_all_tasks(), errors)
        await _collect_teardown_error(self._teardown_live_status_monitor(), errors)
        await _collect_teardown_error(self._teardown_bili_validation_session(), errors)
        await _collect_teardown_error(self._teardown_network_sessions(), errors)
        try:
            self._destroy()
        except BaseException as error:
            errors.append(error)
        _raise_teardown_errors(errors)

    async def restart(self) -> None:
        logger.info('Restarting Application...')
        await self.exit()
        await self.launch()
        logger.info('Restarted Application')

    async def submit_restart(self) -> ControlOperationSnapshot:
        reconciler = self._settings_apply_reconciler
        if reconciler is None:
            raise RuntimeError('settings apply service is not ready')
        return await reconciler.submit('application:restart', 'restart')

    def has_task(self, room_id: int) -> bool:
        return self._task_manager.has_task(room_id)

    def get_all_task_room_ids(self) -> Iterator[int]:
        yield from self._task_manager.get_all_task_room_ids()

    async def submit_task_control(
        self, action: str, room_ids: List[int], force: bool = False
    ) -> ControlOperationSnapshot:
        reconciler = self._task_control_reconciler
        if reconciler is None:
            raise RuntimeError('task control service is not ready')
        valid = []
        rejected = {}
        for room_id in room_ids:
            if self.has_task(room_id):
                valid.append(room_id)
            else:
                rejected[room_id] = 'TASK_NOT_FOUND'
        return await reconciler.submit(action, valid, rejected=rejected, force=force)

    async def submit_room_add(self, room_id: int) -> ControlOperationSnapshot:
        reconciler = self._room_membership_reconciler
        if reconciler is None:
            raise RuntimeError('room membership service is not ready')
        return await reconciler.submit_add(room_id)

    async def submit_room_remove(
        self, room_ids: List[int], *, remove_all: bool = False
    ) -> ControlOperationSnapshot:
        reconciler = self._room_membership_reconciler
        if reconciler is None:
            raise RuntimeError('room membership service is not ready')
        return await reconciler.submit_remove(room_ids, remove_all=remove_all)

    async def submit_room_collect(
        self, room_id: int, *, upload: bool
    ) -> ControlOperationSnapshot:
        reconciler = self._room_membership_reconciler
        if reconciler is None:
            raise RuntimeError('room membership service is not ready')
        return await reconciler.submit_collect(room_id, upload=upload)

    def has_recording_task(self) -> bool:
        from .task import RunningStatus

        return any(
            data.task_status.running_status is RunningStatus.RECORDING
            for data in self._task_manager.get_all_task_data()
        )

    def get_live_status_metrics(self) -> CoordinatorMetrics:
        if self._live_status_coordinator is not None:
            metrics = self._live_status_coordinator.metrics(time.monotonic())
            return CoordinatorMetrics(**vars(metrics))

        settings = self._settings.live_monitor
        registered_rooms = 0
        active_websockets = 0
        for task_data in self._task_manager.get_all_task_data():
            registered_rooms += 1
            active_websockets += task_data.task_status.monitor_enabled
        return CoordinatorMetrics(
            mode='legacy',
            interval_seconds=settings.interval_seconds,
            batch_size=settings.batch_size,
            registered_rooms=registered_rooms,
            active_websockets=active_websockets,
            last_success_at=None,
            snapshot_max_age_seconds=None,
            missing_results=0,
            fallback_requests=0,
            breaker_state=BreakerState.CLOSED,
            breaker_reason=None,
        )

    def resume_live_status_coordinator(self) -> None:
        if self._live_status_coordinator is not None:
            self._live_status_coordinator.resume()

    async def add_task(self, room_id: int) -> int:
        room_id = await self._resolve_room_id(room_id)

        if self._task_manager.has_task(room_id):
            raise ExistsError(f'a task for the room {room_id} is already existed')

        settings = self._settings_manager.find_task_settings(room_id)
        if not settings:
            settings = await self._settings_manager.add_task_settings(room_id)

        await self._task_manager.add_task(settings)

        return room_id

    async def remove_task(self, room_id: int) -> None:
        logger.info(f'Removing task {room_id}...')
        await self._task_manager.remove_task(room_id)
        await self._settings_manager.remove_task_settings(room_id)
        logger.info(f'Successfully removed task {room_id}')

    async def remove_all_tasks(self) -> None:
        logger.info('Removing all tasks...')
        await self._task_manager.remove_all_tasks()
        await self._settings_manager.remove_all_task_settings()
        logger.info('Successfully removed all tasks')

    async def start_task(self, room_id: int) -> None:
        logger.info(f'Starting task {room_id}...')
        await self._task_manager.start_task(room_id)
        await self._settings_manager.mark_task_enabled(room_id)
        logger.info(f'Successfully started task {room_id}')

    async def stop_task(self, room_id: int, force: bool = False) -> None:
        logger.info(f'Stopping task {room_id}...')
        await self._task_manager.stop_task(room_id, force)
        await self._settings_manager.mark_task_disabled(room_id)
        logger.info(f'Successfully stopped task {room_id}')

    async def suppress_current_live(self, room_id: int) -> None:
        await self._task_manager.suppress_current_live(room_id)

    async def start_all_tasks(self) -> None:
        logger.info('Starting all tasks...')
        await self._task_manager.start_all_tasks()
        await self._settings_manager.mark_all_tasks_enabled()
        logger.info('Successfully started all tasks')

    async def stop_all_tasks(self, force: bool = False) -> None:
        logger.info('Stopping all tasks...')
        await self._task_manager.stop_all_tasks(force)
        await self._settings_manager.mark_all_tasks_disabled()
        logger.info('Successfully stopped all tasks')

    async def enable_task_monitor(self, room_id: int) -> None:
        logger.info(f'Enabling monitor for task {room_id}...')
        await self._task_manager.enable_task_monitor(room_id)
        await self._settings_manager.mark_task_monitor_enabled(room_id)
        logger.info(f'Successfully enabled monitor for task {room_id}')

    async def disable_task_monitor(self, room_id: int) -> None:
        logger.info(f'Disabling monitor for task {room_id}...')
        await self._task_manager.disable_task_monitor(room_id)
        await self._settings_manager.mark_task_monitor_disabled(room_id)
        logger.info(f'Successfully disabled monitor for task {room_id}')

    async def enable_all_task_monitors(self) -> None:
        logger.info('Enabling monitors for all tasks...')
        await self._task_manager.enable_all_task_monitors()
        await self._settings_manager.mark_all_task_monitors_enabled()
        logger.info('Successfully enabled monitors for all tasks')

    async def disable_all_task_monitors(self) -> None:
        logger.info('Disabling monitors for all tasks...')
        await self._task_manager.disable_all_task_monitors()
        await self._settings_manager.mark_all_task_monitors_disabled()
        logger.info('Successfully disabled monitors for all tasks')

    async def enable_task_recorder(self, room_id: int) -> None:
        logger.info(f'Enabling recorder for task {room_id}...')
        await self._task_manager.enable_task_recorder(room_id)
        await self._settings_manager.mark_task_recorder_enabled(room_id)
        logger.info(f'Successfully enabled recorder for task {room_id}')

    async def disable_task_recorder(self, room_id: int, force: bool = False) -> None:
        logger.info(f'Disabling recorder for task {room_id}...')
        await self._task_manager.disable_task_recorder(room_id, force)
        await self._settings_manager.mark_task_recorder_disabled(room_id)
        logger.info(f'Successfully disabled recorder for task {room_id}')

    async def enable_all_task_recorders(self) -> None:
        logger.info('Enabling recorders for all tasks...')
        await self._task_manager.enable_all_task_recorders()
        await self._settings_manager.mark_all_task_recorders_enabled()
        logger.info('Successfully enabled recorders for all tasks')

    async def disable_all_task_recorders(self, force: bool = False) -> None:
        logger.info('Disabling recorders for all tasks...')
        await self._task_manager.disable_all_task_recorders(force)
        await self._settings_manager.mark_all_task_recorders_disabled()
        logger.info('Successfully disabled recorders for all tasks')

    def get_task_data(self, room_id: int) -> TaskData:
        return self._task_manager.get_task_data(room_id)

    def get_all_task_data(self) -> Iterator[TaskData]:
        yield from self._task_manager.get_all_task_data()

    def get_task_param(self, room_id: int) -> TaskParam:
        return self._task_manager.get_task_param(room_id)

    def get_task_metadata(self, room_id: int) -> Optional[MetaData]:
        return self._task_manager.get_task_metadata(room_id)

    def get_task_stream_profile(self, room_id: int) -> StreamProfile:
        return self._task_manager.get_task_stream_profile(room_id)

    def get_task_video_file_details(self, room_id: int) -> Iterator[VideoFileDetail]:
        yield from self._task_manager.get_task_video_file_details(room_id)

    def get_task_danmaku_file_details(
        self, room_id: int
    ) -> Iterator[DanmakuFileDetail]:
        yield from self._task_manager.get_task_danmaku_file_details(room_id)

    def can_cut_stream(self, room_id: int) -> bool:
        return self._task_manager.can_cut_stream(room_id)

    def cut_stream(self, room_id: int) -> bool:
        return self._task_manager.cut_stream(room_id)

    async def update_task_info(self, room_id: int) -> None:
        logger.info(f'Updating info for task {room_id}...')
        await self._task_manager.update_task_info(room_id)
        logger.info(f'Successfully updated info for task {room_id}')

    async def update_all_task_infos(self) -> None:
        logger.info('Updating info for all tasks...')
        await self._task_manager.update_all_task_infos()
        logger.info('Successfully updated info for all tasks')

    def get_settings(
        self,
        include: Optional[KeySetOfSettings] = None,
        exclude: Optional[KeySetOfSettings] = None,
    ) -> SettingsOut:
        return self._settings_manager.get_settings(include, exclude)

    async def change_settings(self, settings: SettingsIn) -> SettingsOut:
        return await self._settings_manager.change_settings(settings)

    async def change_settings_with_operations(
        self, settings: SettingsIn
    ) -> Tuple[SettingsOut, Sequence[str]]:
        return await self._settings_manager.change_settings_with_operations(settings)

    def get_task_options(self, room_id: int) -> TaskOptions:
        return self._settings_manager.get_task_options(room_id)

    async def change_task_options(
        self, room_id: int, options: TaskOptions
    ) -> TaskOptions:
        return await self._settings_manager.change_task_options(room_id, options)

    async def change_task_options_with_operations(
        self, room_id: int, options: TaskOptions
    ) -> Tuple[TaskOptions, Sequence[str]]:
        return await self._settings_manager.change_task_options_with_operations(
            room_id, options
        )

    async def apply_settings_target(self, target_key: str, action: str) -> None:
        if action == 'restart':
            await self.restart()
            return
        loading = getattr(self, '_loading_task', None)
        if loading is not None and loading is not asyncio.current_task():
            await asyncio.shield(loading)
        if target_key.startswith('settings:'):
            section = target_key.split(':', 1)[1]
            function = getattr(self._settings_manager, f'apply_{section}_settings')
            if asyncio.iscoroutinefunction(function):
                await function()
            else:
                function()
            return
        prefix, room_id, section = target_key.split(':', 2)
        if prefix != 'task-settings':
            raise ValueError('unsupported settings apply target')
        task_settings = self._settings_manager.find_task_settings(int(room_id))
        if task_settings is None:
            raise RuntimeError('task settings no longer exist')
        function = getattr(self._settings_manager, f'apply_task_{section}_settings')
        value = getattr(task_settings, section)
        if asyncio.iscoroutinefunction(function):
            await function(int(room_id), value)
        else:
            function(int(room_id), value)

    async def refresh_managed_cookie(self) -> None:
        await self._task_manager.refresh_managed_cookie()

    async def get_latest_version_string(self, project_name: str) -> Optional[str]:
        client = getattr(self, '_update_metadata_client', None)
        if client is None:
            raise RuntimeError('update metadata client is not ready')
        return await client.get_latest_version_string(project_name)

    async def validate_bili_cookie(self, cookie: str) -> Any:
        from .bili.helpers import get_nav

        return await asyncio.wait_for(
            get_nav(cookie, self._get_bili_validation_session()),
            timeout=self._validation_timeout_seconds,
        )

    async def validate_directory(self, path: str) -> Tuple[int, str]:
        file_work = self._settings_manager.file_work
        if file_work is None:
            raise RuntimeError('settings file work is not ready')
        return await file_work.validate_directory(path)

    async def _load_tasks_and_controls(self) -> None:
        membership = self._room_membership_reconciler
        desired_absent = (
            set() if membership is None else await membership.pending_removal_room_ids()
        )
        await self._task_manager.load_all_tasks(desired_absent)
        reconciler = self._task_control_reconciler
        if reconciler is not None:
            await reconciler.recover()
            reconciler.start()
        if membership is not None:
            membership.start()

    async def _resolve_room_id(self, room_id: int) -> int:
        from .bili.helpers import ensure_room_id

        network_pool = self._ensure_network_session_pool()
        return await ensure_room_id(
            room_id, None if network_pool is None else network_pool.client('bili_api')
        )

    async def _setup_live_status_monitor(self) -> None:
        from .bili.anonymous_room_client import AnonymousRoomClient
        from .bili.batch_status_client import BatchStatusClient
        from .bili.live_status_coordinator import LiveStatusCoordinator
        from .bili.net import connector, timeout
        from .task import RecordTaskManager

        network_session_pool = self._ensure_network_session_pool()

        settings = self._settings.live_monitor
        recording_journal = (
            None
            if self._recording_journal_provider is None
            else self._recording_journal_provider()
        )
        self._live_status_session = None
        self._live_status_coordinator = None
        if settings.mode == 'legacy':
            self._task_manager = RecordTaskManager(
                self._settings_manager,
                managed_cookie_provider=self._managed_cookie_provider,
                auth_failure_reporter=self._auth_failure_reporter,
                recording_journal=recording_journal,
                network_session_pool=network_session_pool,
                network_route_manager=self._network_route_manager,
            )
            return

        session: Any
        if network_session_pool is None:
            session = aiohttp.ClientSession(
                connector=connector,
                connector_owner=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=timeout,
                trust_env=False,
            )
        else:
            session = network_session_pool.client('room_status', anonymous=True)
        self._live_status_session = session
        try:
            coordinator = LiveStatusCoordinator(
                BatchStatusClient(session),
                interval_seconds=settings.interval_seconds,
                batch_size=settings.batch_size,
                fallback_cooldown_seconds=settings.fallback_cooldown_seconds,
            )
            self._live_status_coordinator = coordinator
            self._task_manager = RecordTaskManager(
                self._settings_manager,
                coordinator,
                AnonymousRoomClient(session),
                managed_cookie_provider=self._managed_cookie_provider,
                auth_failure_reporter=self._auth_failure_reporter,
                recording_journal=recording_journal,
                network_session_pool=network_session_pool,
                network_route_manager=self._network_route_manager,
            )
            await coordinator.start()
        except BaseException as error:
            await self._teardown_live_status_monitor_after_failure(error)
            raise

    async def _teardown_live_status_monitor_after_failure(
        self, original_error: BaseException
    ) -> None:
        try:
            await self._teardown_live_status_monitor()
            await self._teardown_network_sessions()
        except asyncio.CancelledError as cleanup_error:
            raise cleanup_error from original_error
        except BaseException as cleanup_error:
            raise original_error from cleanup_error

    async def _teardown_live_status_monitor(self) -> None:
        coordinator = getattr(self, '_live_status_coordinator', None)
        session = getattr(self, '_live_status_session', None)
        self._live_status_coordinator = None
        self._live_status_session = None
        errors: List[BaseException] = []
        if coordinator is not None:
            await _collect_teardown_error(coordinator.stop(), errors)
        if session is not None:
            await _collect_teardown_error(session.close(), errors)
        _raise_teardown_errors(errors)

    def _ensure_network_session_pool(self) -> Optional[AiohttpSessionPool]:
        if self._network_route_manager is None:
            return None
        if self._network_session_pool is None or self._network_session_pool.closed:
            from .networking.aiohttp_session import AiohttpSessionPool

            self._network_session_pool = AiohttpSessionPool(self._network_route_manager)
        return self._network_session_pool

    def _get_bili_validation_session(self) -> Any:
        network_pool = self._ensure_network_session_pool()
        if network_pool is not None:
            return network_pool.client('bili_api', anonymous=True)

        session = self._bili_validation_session
        if session is None or getattr(session, 'closed', False):
            from .bili.net import connector, timeout

            session = aiohttp.ClientSession(
                connector=connector,
                connector_owner=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=timeout,
                trust_env=False,
            )
            self._bili_validation_session = session
        return session

    async def _teardown_bili_validation_session(self) -> None:
        session = getattr(self, '_bili_validation_session', None)
        self._bili_validation_session = None
        if session is not None:
            await session.close()

    async def _teardown_network_sessions(self) -> None:
        pool = getattr(self, '_network_session_pool', None)
        self._network_session_pool = None
        if pool is not None:
            await pool.close()

    def _setup(self) -> None:
        self._setup_exception_handler()
        self._setup_notifiers()
        self._setup_webhooks()
        self._setup_update_metadata()

    def _setup_logger(self) -> None:
        self._settings_manager.apply_logging_settings()

    def _setup_exception_handler(self) -> None:
        self._exception_handler = ExceptionHandler()
        self._exception_handler.enable()

    def _setup_notifiers(self) -> None:
        from .notification import (
            BarkNotifier,
            EmailNotifier,
            PushdeerNotifier,
            PushplusNotifier,
            ServerchanNotifier,
            TelegramNotifier,
        )

        dispatcher = self._notification_dispatcher
        self._email_notifier = EmailNotifier(dispatcher=dispatcher)
        self._serverchan_notifier = ServerchanNotifier(dispatcher=dispatcher)
        self._pushdeer_notifier = PushdeerNotifier(dispatcher=dispatcher)
        self._pushplus_notifier = PushplusNotifier(dispatcher=dispatcher)
        self._telegram_notifier = TelegramNotifier(dispatcher=dispatcher)
        self._bark_notifier = BarkNotifier(dispatcher=dispatcher)
        self._settings_manager.apply_email_notification_settings()
        self._settings_manager.apply_serverchan_notification_settings()
        self._settings_manager.apply_pushdeer_notification_settings()
        self._settings_manager.apply_pushplus_notification_settings()
        self._settings_manager.apply_telegram_notification_settings()
        self._settings_manager.apply_bark_notification_settings()

    def _setup_webhooks(self) -> None:
        from .webhook import WebHookEmitter

        self._webhook_emitter = WebHookEmitter()
        self._settings_manager.apply_webhooks_settings()

    async def _start_webhooks(self) -> None:
        emitter = getattr(self, '_webhook_emitter', None)
        if emitter is None:
            return
        await emitter.start()
        emitter.enable()

    async def _teardown_webhooks(self) -> None:
        emitter = getattr(self, '_webhook_emitter', None)
        if emitter is None:
            return
        errors: List[BaseException] = []
        try:
            emitter.disable()
        except BaseException as error:
            errors.append(error)
        try:
            await emitter.close(drain_timeout_seconds=5)
        except BaseException as error:
            errors.append(error)
        else:
            if getattr(self, '_webhook_emitter', None) is emitter:
                del self._webhook_emitter
        _raise_teardown_errors(errors)

    def _setup_update_metadata(self) -> None:
        from .update.helpers import UpdateMetadataClient

        self._update_metadata_client = UpdateMetadataClient()

    async def _start_update_metadata(self) -> None:
        client = getattr(self, '_update_metadata_client', None)
        if client is not None:
            await client.start()

    async def _teardown_update_metadata(self) -> None:
        client = getattr(self, '_update_metadata_client', None)
        if client is None:
            return
        await client.close()
        if getattr(self, '_update_metadata_client', None) is client:
            del self._update_metadata_client

    def _destroy(self) -> None:
        errors: List[BaseException] = []
        for operation in (self._destroy_notifiers, self._destroy_exception_handler):
            try:
                operation()
            except BaseException as error:
                errors.append(error)
        _raise_teardown_errors(errors)

    def _destroy_notifiers(self) -> None:
        errors: List[BaseException] = []
        for name in (
            '_email_notifier',
            '_serverchan_notifier',
            '_pushdeer_notifier',
            '_pushplus_notifier',
            '_telegram_notifier',
            '_bark_notifier',
        ):
            notifier = getattr(self, name, None)
            if notifier is None:
                continue
            try:
                notifier.disable()
            except BaseException as error:
                errors.append(error)
            else:
                if getattr(self, name, None) is notifier:
                    delattr(self, name)
        _raise_teardown_errors(errors)

    def _destroy_webhooks(self) -> None:
        self._webhook_emitter.disable()
        del self._webhook_emitter

    def _destroy_exception_handler(self) -> None:
        handler = getattr(self, '_exception_handler', None)
        if handler is None:
            return
        handler.disable()
        if getattr(self, '_exception_handler', None) is handler:
            del self._exception_handler
