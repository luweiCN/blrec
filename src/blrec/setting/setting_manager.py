from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import TYPE_CHECKING, Iterable, Optional, Sequence, Set, Tuple, cast

from ..exception import ForbiddenError, NotFoundError
from ..logging import configure_logger
from ..logging.audit import audit
from ..notification.notifiers import Notifier
from ..notification.providers import (
    Bark,
    EmailService,
    Pushdeer,
    Pushplus,
    Serverchan,
    Telegram,
)
from ..webhook import WebHook
from .file_work import SettingsFileWorkCoordinator
from .helpers import shadow_settings, update_settings
from .models import (
    DanmakuOptions,
    HeaderOptions,
    MessageTemplateSettings,
    NotificationSettings,
    NotifierSettings,
    OutputOptions,
    PostprocessingOptions,
    RecorderOptions,
    Settings,
    SettingsIn,
    SettingsOut,
    TaskOptions,
    TaskSettings,
)
from .typing import KeySetOfSettings

if TYPE_CHECKING:
    from ..application import Application
    from .file_work import SettingsApplyReconciler


class SettingsManager:
    def __init__(
        self,
        app: Application,
        settings: Settings,
        *,
        file_work: Optional[SettingsFileWorkCoordinator] = None,
    ) -> None:
        self._app = app
        self._settings = settings
        self._file_work = file_work
        self._apply_reconciler: Optional[SettingsApplyReconciler] = None
        self._mutation_lock = asyncio.Lock()
        self._task_desired_state_lock = self._mutation_lock
        self._accepting_mutations = True
        self._dump_override: ContextVar[Optional[Tuple[Settings, Tuple[str, ...]]]] = (
            ContextVar('settings_dump_override', default=None)
        )

    def set_apply_reconciler(self, reconciler: SettingsApplyReconciler) -> None:
        self._apply_reconciler = reconciler

    def set_file_work(self, file_work: SettingsFileWorkCoordinator) -> None:
        self._file_work = file_work
        self._accepting_mutations = True

    def stop_mutation_admission(self) -> None:
        self._accepting_mutations = False

    async def close_mutation_admission(self) -> None:
        self.stop_mutation_admission()
        async with self._mutation_lock:
            pass
        if self._file_work is not None:
            self._file_work.close_admission()

    @property
    def file_work(self) -> Optional[SettingsFileWorkCoordinator]:
        return self._file_work

    def get_settings(
        self,
        include: Optional[KeySetOfSettings] = None,
        exclude: Optional[KeySetOfSettings] = None,
    ) -> SettingsOut:
        return SettingsOut(**self._settings.dict(include=include, exclude=exclude))

    async def change_settings(self, settings: SettingsIn) -> SettingsOut:
        result, _operations = await self.change_settings_with_operations(settings)
        return result

    async def change_settings_with_operations(
        self, settings: SettingsIn
    ) -> Tuple[SettingsOut, Sequence[str]]:
        changed_sections = []
        operation_ids = []
        mode_changed = False
        async with self._mutation_lock:
            if not self._accepting_mutations:
                raise RuntimeError('settings mutation admission is closed')
            live_monitor = settings.live_monitor
            mode_changed = (
                'live_monitor' in settings.__fields_set__
                and live_monitor is not None
                and 'mode' in live_monitor.__fields_set__
                and live_monitor.mode != self._settings.live_monitor.mode
            )
            if mode_changed and self._app.has_recording_task():
                raise ForbiddenError(
                    'Cannot change live monitor mode while a task is recording'
                )
            candidate = self._settings.copy(deep=True)
            self._copy_settings_path(candidate)
            for name in sorted(settings.__fields_set__):
                src_sub_settings = getattr(settings, name)
                dst_sub_settings = getattr(candidate, name)
                if isinstance(src_sub_settings, list):
                    assert isinstance(dst_sub_settings, list)
                    setattr(candidate, name, src_sub_settings)
                else:
                    update_settings(src_sub_settings, dst_sub_settings)
                if getattr(candidate, name) != getattr(self._settings, name):
                    changed_sections.append(name)
            if changed_sections:
                validate_paths = tuple(
                    path
                    for name, path in (
                        ('output', candidate.output.out_dir),
                        ('logging', candidate.logging.log_dir),
                    )
                    if name in changed_sections
                )
                await self._dump_candidate(candidate, validate_paths=validate_paths)
                for name in changed_sections:
                    setattr(self._settings, name, getattr(candidate, name))
                if self._apply_reconciler is not None:
                    for name in changed_sections:
                        operation = await self._apply_reconciler.submit(
                            'settings:{}'.format(name),
                            (
                                'restart'
                                if name == 'live_monitor' and mode_changed
                                else 'apply'
                            ),
                        )
                        operation_ids.append(operation.id)

        if changed_sections:
            audit('application_settings_updated', sections=sorted(changed_sections))
            if self._apply_reconciler is None:
                for name in changed_sections:
                    func = getattr(self, f'apply_{name}_settings')
                    if asyncio.iscoroutinefunction(func):
                        await func()
                    else:
                        func()
                if mode_changed:
                    await self._app.restart()
        return (
            self.get_settings(cast(KeySetOfSettings, settings.__fields_set__)),
            tuple(operation_ids),
        )

    async def apply_live_monitor_settings(self) -> None:
        coordinator = getattr(self._app, '_live_status_coordinator', None)
        if coordinator is None:
            return
        settings = self._settings.live_monitor
        await coordinator.reconfigure(
            interval_seconds=settings.interval_seconds,
            batch_size=settings.batch_size,
            fallback_cooldown_seconds=settings.fallback_cooldown_seconds,
        )

    def apply_network_settings(self) -> None:
        # Network clients resolve the selected interface for each new request.
        pass

    def get_task_options(self, room_id: int) -> TaskOptions:
        if settings := self.find_task_settings(room_id):
            return TaskOptions.from_settings(settings)
        raise NotFoundError(f'task settings of room {room_id} not found')

    async def change_task_options(
        self, room_id: int, options: TaskOptions
    ) -> TaskOptions:
        result, _operations = await self.change_task_options_with_operations(
            room_id, options
        )
        return result

    async def change_task_options_with_operations(
        self, room_id: int, options: TaskOptions
    ) -> Tuple[TaskOptions, Sequence[str]]:
        changed_sections = []
        operation_ids = []
        async with self._mutation_lock:
            if not self._accepting_mutations:
                raise RuntimeError('settings mutation admission is closed')
            candidate = self._settings.copy(deep=True)
            self._copy_settings_path(candidate)
            candidate_settings = self._find_task_settings(candidate, room_id)
            if candidate_settings is None:
                raise NotFoundError(f'task settings of room {room_id} not found')
            for name in sorted(options.__fields_set__):
                src_opts = getattr(options, name)
                dst_opts = getattr(candidate_settings, name)
                update_settings(src_opts, dst_opts)
                live_settings = self.find_task_settings(room_id)
                assert live_settings is not None
                if getattr(candidate_settings, name) != getattr(live_settings, name):
                    changed_sections.append(name)
            if changed_sections:
                await self._dump_candidate(candidate)
                live_settings = self.find_task_settings(room_id)
                assert live_settings is not None
                for name in changed_sections:
                    setattr(live_settings, name, getattr(candidate_settings, name))
                if self._apply_reconciler is not None:
                    for name in changed_sections:
                        operation = await self._apply_reconciler.submit(
                            'task-settings:{}:{}'.format(room_id, name), 'apply'
                        )
                        operation_ids.append(operation.id)

        live_settings = self.find_task_settings(room_id)
        assert live_settings is not None
        if self._apply_reconciler is None:
            for name in changed_sections:
                func = getattr(self, f'apply_task_{name}_settings')
                value = getattr(live_settings, name)
                if asyncio.iscoroutinefunction(func):
                    await func(room_id, value)
                else:
                    func(room_id, value)
        return TaskOptions.from_settings(live_settings), tuple(operation_ids)

    async def dump_settings(self) -> None:
        override = self._dump_override.get()
        settings, validate_paths = (
            (self._settings, ()) if override is None else override
        )
        if self._file_work is not None:
            await self._file_work.atomic_dump(settings, validate_paths=validate_paths)
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, settings.dump)

    async def _dump_candidate(
        self, settings: Settings, *, validate_paths: Tuple[str, ...] = ()
    ) -> None:
        token = self._dump_override.set((settings, validate_paths))
        try:
            await self.dump_settings()
        finally:
            self._dump_override.reset(token)

    def _copy_settings_path(self, candidate: Settings) -> None:
        try:
            candidate._path = self._settings._path
        except AttributeError:
            pass

    def has_task_settings(self, room_id: int) -> bool:
        return self.find_task_settings(room_id) is not None

    def find_task_settings(self, room_id: int) -> Optional[TaskSettings]:
        return self._find_task_settings(self._settings, room_id)

    @staticmethod
    def _find_task_settings(root: Settings, room_id: int) -> Optional[TaskSettings]:
        for settings in root.tasks:
            if settings.room_id == room_id:
                return settings
        return None

    async def add_task_settings(self, room_id: int) -> TaskSettings:
        return await self.ensure_task_settings(room_id)

    async def ensure_task_settings(self, room_id: int) -> TaskSettings:
        """Create one task setting durably, or return the existing setting."""

        async with self._task_desired_state_lock:
            existing = self.find_task_settings(room_id)
            if existing is not None:
                return existing.copy(deep=True)
            settings = TaskSettings(room_id=room_id)
            self._settings.tasks = [*self._settings.tasks, settings]
            try:
                await self.dump_settings()
            except BaseException:
                self._settings.tasks = [
                    item for item in self._settings.tasks if item is not settings
                ]
                raise
            return settings.copy(deep=True)

    async def remove_task_settings(self, room_id: int) -> None:
        removed = await self.remove_task_settings_batch((room_id,))
        if room_id not in removed:
            raise NotFoundError(f"The room {room_id} is not existed")

    async def remove_all_task_settings(self) -> None:
        await self.remove_task_settings_batch(
            settings.room_id for settings in tuple(self._settings.tasks)
        )

    async def remove_task_settings_batch(self, room_ids: Iterable[int]) -> Set[int]:
        """Remove a membership batch with at most one settings-file write."""

        normalized = set(room_ids)
        async with self._task_desired_state_lock:
            previous = self._settings.tasks
            removed = {
                settings.room_id
                for settings in previous
                if settings.room_id in normalized
            }
            if not removed:
                return set()
            self._settings.tasks = [
                settings for settings in previous if settings.room_id not in removed
            ]
            try:
                await self.dump_settings()
            except BaseException:
                self._settings.tasks = previous
                raise
            return removed

    def get_task_desired_state(self, room_id: int) -> Tuple[bool, bool]:
        settings = self.find_task_settings(room_id)
        if settings is None:
            raise NotFoundError(f'task settings of room {room_id} not found')
        return settings.enable_monitor, settings.enable_recorder

    async def change_task_desired_states(
        self,
        room_ids: Iterable[int],
        *,
        enable_monitor: Optional[bool] = None,
        enable_recorder: Optional[bool] = None,
    ) -> Set[int]:
        """Persist a batch of desired task states with at most one file write."""

        normalized_room_ids = tuple(dict.fromkeys(room_ids))
        async with self._task_desired_state_lock:
            task_settings = []
            for room_id in normalized_room_ids:
                settings = self.find_task_settings(room_id)
                if settings is None:
                    raise NotFoundError(f'task settings of room {room_id} not found')
                task_settings.append(settings)

            previous_states = {
                settings.room_id: (settings.enable_monitor, settings.enable_recorder)
                for settings in task_settings
            }
            changed: Set[int] = set()
            for settings in task_settings:
                if (
                    enable_monitor is not None
                    and settings.enable_monitor != enable_monitor
                ):
                    settings.enable_monitor = enable_monitor
                    changed.add(settings.room_id)
                if (
                    enable_recorder is not None
                    and settings.enable_recorder != enable_recorder
                ):
                    settings.enable_recorder = enable_recorder
                    changed.add(settings.room_id)

            if changed:
                try:
                    await self.dump_settings()
                except BaseException:
                    for settings in task_settings:
                        previous_monitor, previous_recorder = previous_states[
                            settings.room_id
                        ]
                        settings.enable_monitor = previous_monitor
                        settings.enable_recorder = previous_recorder
                    raise
            return changed

    async def mark_task_enabled(self, room_id: int) -> None:
        await self.change_task_desired_states(
            [room_id], enable_monitor=True, enable_recorder=True
        )

    async def mark_task_disabled(self, room_id: int) -> None:
        await self.change_task_desired_states(
            [room_id], enable_monitor=False, enable_recorder=False
        )

    async def mark_all_tasks_enabled(self) -> None:
        await self.change_task_desired_states(
            (settings.room_id for settings in self._settings.tasks),
            enable_monitor=True,
            enable_recorder=True,
        )

    async def mark_all_tasks_disabled(self) -> None:
        await self.change_task_desired_states(
            (settings.room_id for settings in self._settings.tasks),
            enable_monitor=False,
            enable_recorder=False,
        )

    async def mark_task_monitor_enabled(self, room_id: int) -> None:
        await self.change_task_desired_states([room_id], enable_monitor=True)

    async def mark_task_monitor_disabled(self, room_id: int) -> None:
        await self.change_task_desired_states([room_id], enable_monitor=False)

    async def mark_all_task_monitors_enabled(self) -> None:
        await self.change_task_desired_states(
            (settings.room_id for settings in self._settings.tasks), enable_monitor=True
        )

    async def mark_all_task_monitors_disabled(self) -> None:
        await self.change_task_desired_states(
            (settings.room_id for settings in self._settings.tasks),
            enable_monitor=False,
        )

    async def mark_task_recorder_enabled(self, room_id: int) -> None:
        await self.change_task_desired_states([room_id], enable_recorder=True)

    async def mark_task_recorder_disabled(self, room_id: int) -> None:
        await self.change_task_desired_states([room_id], enable_recorder=False)

    async def mark_all_task_recorders_enabled(self) -> None:
        await self.change_task_desired_states(
            (settings.room_id for settings in self._settings.tasks),
            enable_recorder=True,
        )

    async def mark_all_task_recorders_disabled(self) -> None:
        await self.change_task_desired_states(
            (settings.room_id for settings in self._settings.tasks),
            enable_recorder=False,
        )

    async def apply_task_header_settings(
        self,
        room_id: int,
        options: HeaderOptions,
        *,
        restart_danmaku_client: bool = True,
    ) -> None:
        final_settings = self._settings.header.copy()
        shadow_settings(options, final_settings)
        await self._app._task_manager.apply_task_header_settings(
            room_id, final_settings, restart_danmaku_client=restart_danmaku_client
        )

    def apply_task_danmaku_settings(
        self, room_id: int, options: DanmakuOptions
    ) -> None:
        final_settings = self._settings.danmaku.copy()
        shadow_settings(options, final_settings)
        self._app._task_manager.apply_task_danmaku_settings(room_id, final_settings)

    def apply_task_recorder_settings(
        self, room_id: int, options: RecorderOptions
    ) -> None:
        final_settings = self._settings.recorder.copy()
        shadow_settings(options, final_settings)
        self._app._task_manager.apply_task_recorder_settings(room_id, final_settings)

    def apply_task_output_settings(self, room_id: int, options: OutputOptions) -> None:
        final_settings = self._settings.output.copy()
        shadow_settings(options, final_settings)
        self._app._task_manager.apply_task_output_settings(room_id, final_settings)

    def apply_task_postprocessing_settings(
        self, room_id: int, options: PostprocessingOptions
    ) -> None:
        final_settings = self._settings.postprocessing.copy()
        shadow_settings(options, final_settings)
        self._app._task_manager.apply_task_postprocessing_settings(
            room_id, final_settings
        )

    async def apply_output_settings(self) -> None:
        for settings in self._settings.tasks:
            self.apply_task_output_settings(settings.room_id, settings.output)

        out_dir = self._settings.output.out_dir
        self._app._out_dir = out_dir

    def apply_logging_settings(self) -> None:
        configure_logger(
            log_dir=self._settings.logging.log_dir,
            console_log_level=self._settings.logging.console_log_level,
            backup_count=self._settings.logging.backup_count,
        )

    def apply_bili_api_settings(self) -> None:
        for task_settings in self._settings.tasks:
            self._app._task_manager.apply_task_bili_api_settings(
                task_settings.room_id, self._settings.bili_api
            )

    def apply_bili_upload_settings(self) -> None:
        # Database and chunk-shape changes take effect after process restart.
        pass

    async def apply_header_settings(self) -> None:
        for settings in self._settings.tasks:
            await self.apply_task_header_settings(settings.room_id, settings.header)

    def apply_danmaku_settings(self) -> None:
        for settings in self._settings.tasks:
            self.apply_task_danmaku_settings(settings.room_id, settings.danmaku)

    def apply_recorder_settings(self) -> None:
        for settings in self._settings.tasks:
            self.apply_task_recorder_settings(settings.room_id, settings.recorder)

    def apply_postprocessing_settings(self) -> None:
        for settings in self._settings.tasks:
            self.apply_task_postprocessing_settings(
                settings.room_id, settings.postprocessing
            )

    def apply_space_settings(self) -> None:
        # Legacy physical-disk monitoring fields remain loadable for old
        # settings files, but recording cleanup is driven only by the managed
        # capacity limit and per-room retention policies.
        pass

    def apply_email_notification_settings(self) -> None:
        notifier = self._app._email_notifier
        settings = self._settings.email_notification
        self._apply_email_settings(notifier.provider)
        self._apply_notifier_settings(notifier, settings)
        self._apply_notification_settings(notifier, settings)
        self._apply_message_template_settings(notifier, settings)

    def apply_serverchan_notification_settings(self) -> None:
        notifier = self._app._serverchan_notifier
        settings = self._settings.serverchan_notification
        self._apply_serverchan_settings(notifier.provider)
        self._apply_notifier_settings(notifier, settings)
        self._apply_notification_settings(notifier, settings)
        self._apply_message_template_settings(notifier, settings)

    def apply_pushdeer_notification_settings(self) -> None:
        notifier = self._app._pushdeer_notifier
        settings = self._settings.pushdeer_notification
        self._apply_pushdeer_settings(notifier.provider)
        self._apply_notifier_settings(notifier, settings)
        self._apply_notification_settings(notifier, settings)
        self._apply_message_template_settings(notifier, settings)

    def apply_pushplus_notification_settings(self) -> None:
        notifier = self._app._pushplus_notifier
        settings = self._settings.pushplus_notification
        self._apply_pushplus_settings(notifier.provider)
        self._apply_notifier_settings(notifier, settings)
        self._apply_notification_settings(notifier, settings)
        self._apply_message_template_settings(notifier, settings)

    def apply_telegram_notification_settings(self) -> None:
        notifier = self._app._telegram_notifier
        settings = self._settings.telegram_notification
        self._apply_telegram_settings(notifier.provider)
        self._apply_notifier_settings(notifier, settings)
        self._apply_notification_settings(notifier, settings)
        self._apply_message_template_settings(notifier, settings)

    def apply_bark_notification_settings(self) -> None:
        notifier = self._app._bark_notifier
        settings = self._settings.bark_notification
        self._apply_bark_settings(notifier.provider)
        self._apply_notifier_settings(notifier, settings)
        self._apply_notification_settings(notifier, settings)
        self._apply_message_template_settings(notifier, settings)

    def apply_operational_notifications_settings(self) -> None:
        # The operational notification center reads this shared settings object
        # for every state transition, so no worker restart is required.
        pass

    def apply_webhooks_settings(self) -> None:
        webhooks = [WebHook.from_settings(s) for s in self._settings.webhooks]
        self._app._webhook_emitter.webhooks = webhooks

    def _apply_email_settings(self, email_service: EmailService) -> None:
        email_service.src_addr = self._settings.email_notification.src_addr
        email_service.dst_addr = self._settings.email_notification.dst_addr
        email_service.auth_code = self._settings.email_notification.auth_code
        email_service.smtp_host = self._settings.email_notification.smtp_host
        email_service.smtp_port = self._settings.email_notification.smtp_port

    def _apply_serverchan_settings(self, serverchan: Serverchan) -> None:
        serverchan.sendkey = self._settings.serverchan_notification.sendkey

    def _apply_pushdeer_settings(self, pushdeer: Pushdeer) -> None:
        pushdeer.server = self._settings.pushdeer_notification.server
        pushdeer.pushkey = self._settings.pushdeer_notification.pushkey

    def _apply_pushplus_settings(self, pushplus: Pushplus) -> None:
        pushplus.token = self._settings.pushplus_notification.token
        pushplus.topic = self._settings.pushplus_notification.topic

    def _apply_telegram_settings(self, telegram: Telegram) -> None:
        telegram.token = self._settings.telegram_notification.token
        telegram.chatid = self._settings.telegram_notification.chatid
        telegram.server = self._settings.telegram_notification.server

    def _apply_bark_settings(self, bark: Bark) -> None:
        bark.server = self._settings.bark_notification.server
        bark.pushkey = self._settings.bark_notification.pushkey

    def _apply_notifier_settings(
        self, notifier: Notifier, settings: NotifierSettings
    ) -> None:
        if settings.enabled:
            notifier.enable()
        else:
            notifier.disable()

    def _apply_notification_settings(
        self, notifier: Notifier, settings: NotificationSettings
    ) -> None:
        notifier.notify_began = settings.notify_began
        notifier.notify_ended = settings.notify_ended
        notifier.notify_error = settings.notify_error
        notifier.notify_space = settings.notify_space

    def _apply_message_template_settings(
        self, notifier: Notifier, settings: MessageTemplateSettings
    ) -> None:
        notifier.began_message_type = settings.began_message_type
        notifier.began_message_title = settings.began_message_title
        notifier.began_message_content = settings.began_message_content
        notifier.ended_message_type = settings.ended_message_type
        notifier.ended_message_title = settings.ended_message_title
        notifier.ended_message_content = settings.ended_message_content
        notifier.space_message_type = settings.space_message_type
        notifier.space_message_title = settings.space_message_title
        notifier.space_message_content = settings.space_message_content
        notifier.error_message_type = settings.error_message_type
        notifier.error_message_title = settings.error_message_title
        notifier.error_message_content = settings.error_message_content
