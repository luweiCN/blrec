from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from loguru import logger

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.setting.models import (
    OperationalEventCode,
    OperationalNotificationSettings,
    OperationalNotificationTarget,
)

if TYPE_CHECKING:
    from blrec.bili_upload.retention import RetentionStatus
    from blrec.networking.manager import NetworkRouteManager

__all__ = ('OperationalHealthScanner', 'OperationalNotificationCenter')


class _MessageSender(Protocol):
    async def send_message(self, title: str, content: str, message_type: str) -> None:
        pass


class OperationalNotificationCenter:
    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        settings_provider: Callable[[], OperationalNotificationSettings],
        senders: Mapping[str, _MessageSender],
        channel_enabled: Callable[[str], bool],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._settings_provider = settings_provider
        self._senders = dict(senders)
        self._channel_enabled = channel_enabled
        self._clock = clock

    async def report(
        self,
        event: OperationalEventCode,
        object_key: str,
        *,
        healthy: bool,
        title: str,
        detail: str,
    ) -> bool:
        if not object_key:
            raise ValueError('notification object key must not be empty')
        now = max(1, int(self._clock()))

        def transition(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                'SELECT healthy FROM operational_notification_states '
                'WHERE event_code=? AND object_key=?',
                (event, object_key),
            ).fetchone()
            normalized_healthy = 1 if healthy else 0
            if row is None:
                connection.execute(
                    'INSERT INTO operational_notification_states('
                    'event_code,object_key,healthy,title,detail,observed_at) '
                    'VALUES(?,?,?,?,?,?)',
                    (
                        event,
                        object_key,
                        normalized_healthy,
                        title[:200],
                        detail[:2000],
                        now,
                    ),
                )
                return False
            changed = int(row['healthy']) != normalized_healthy
            connection.execute(
                'UPDATE operational_notification_states SET healthy=?,title=?,'
                'detail=?,observed_at=? WHERE event_code=? AND object_key=?',
                (
                    normalized_healthy,
                    title[:200],
                    detail[:2000],
                    now,
                    event,
                    object_key,
                ),
            )
            return changed

        changed = await self._database.write(transition)
        if not changed:
            return False
        route = self._settings_provider().route_for(event)
        if healthy and not route.notify_recovery:
            return True
        await self._dispatch(route.targets, title[:200], detail[:2000])
        return True

    async def _dispatch(
        self, targets: Sequence[OperationalNotificationTarget], title: str, detail: str
    ) -> None:
        coroutines = []
        names = []
        for target in targets:
            channel = str(target.channel)
            sender = self._senders.get(channel)
            if sender is None or not self._channel_enabled(channel):
                continue
            names.append(channel)
            coroutines.append(
                sender.send_message(title, detail, str(target.message_type))
            )
        if not coroutines:
            return
        results = await asyncio.gather(*coroutines, return_exceptions=True)
        for channel, result in zip(names, results):
            if isinstance(result, BaseException):
                logger.warning(
                    'Operational notification via {} failed: {}'.format(
                        channel, repr(result)
                    )
                )


class OperationalHealthScanner:
    def __init__(
        self,
        database: BiliUploadDatabase,
        center: OperationalNotificationCenter,
        *,
        retention_status_provider: Optional[
            Callable[[], Awaitable['RetentionStatus']]
        ] = None,
        network_route_manager: Optional['NetworkRouteManager'] = None,
    ) -> None:
        self._database = database
        self._center = center
        self._retention_status_provider = retention_status_provider
        self._network_route_manager = network_route_manager

    async def scan(self) -> None:
        await self._scan_accounts()
        await self._scan_recordings()
        await self._scan_upload_jobs()
        await self._scan_capacity()
        await self._scan_network()

    async def _scan_accounts(self) -> None:
        rows = await self._database.fetchall(
            'SELECT id,display_name,state,pause_reason FROM bili_accounts '
            "WHERE state!='archived' ORDER BY id"
        )
        for row in rows:
            healthy = str(row['state']) == 'active'
            name = str(row['display_name'])
            reason = '' if row['pause_reason'] is None else str(row['pause_reason'])
            await self._center.report(
                'account_unavailable',
                'account:{}'.format(int(row['id'])),
                healthy=healthy,
                title='投稿账号已恢复' if healthy else '投稿账号不可用',
                detail=(
                    '{} 已恢复可用'.format(name)
                    if healthy
                    else '{}：{}'.format(name, reason or str(row['state']))
                ),
            )

    async def _scan_recordings(self) -> None:
        rows = await self._database.fetchall(
            'SELECT session.id,session.room_id,session.state,'
            'MAX(CASE WHEN part.artifact_state IN '
            "('failed','missing','manual_review') THEN 1 ELSE 0 END) AS failed_part "
            'FROM recording_sessions session LEFT JOIN recording_parts part '
            'ON part.session_id=session.id GROUP BY session.id ORDER BY session.id'
        )
        for row in rows:
            session_state = str(row['state'])
            healthy = session_state not in ('cancelled', 'manual_review') and not bool(
                row['failed_part']
            )
            room_id = int(row['room_id'])
            await self._center.report(
                'recording_failed',
                'recording-session:{}'.format(int(row['id'])),
                healthy=healthy,
                title='录制任务已恢复' if healthy else '录制任务异常',
                detail='房间 {}：{}'.format(
                    room_id, '录像文件已恢复可用' if healthy else session_state
                ),
            )

    async def _scan_upload_jobs(self) -> None:
        rows = await self._database.fetchall(
            'SELECT id,state,operator_paused,review_reason,repair_state,'
            'repair_error,comment_branch_state,danmaku_branch_state,'
            'collection_branch_state,collection_error FROM upload_jobs ORDER BY id'
        )
        for row in rows:
            job_id = int(row['id'])
            state = str(row['state'])
            repair_state = str(row['repair_state'])
            reason = '' if row['review_reason'] is None else str(row['review_reason'])
            upload_failed = (
                state == 'paused'
                and not bool(row['operator_paused'])
                and repair_state not in ('failed', 'unknown_outcome')
            )
            await self._report_job_state(
                'upload_failed',
                job_id,
                not upload_failed,
                '上传任务已恢复',
                '上传任务失败',
                reason or state,
            )
            await self._report_job_state(
                'review_rejected',
                job_id,
                state != 'rejected',
                '稿件状态已恢复',
                '稿件审核未通过',
                reason or state,
            )
            repair_failed = repair_state in ('failed', 'unknown_outcome')
            repair_error = (
                '' if row['repair_error'] is None else str(row['repair_error'])
            )
            await self._report_job_state(
                'transcode_repair_failed',
                job_id,
                not repair_failed,
                '转码修复已恢复',
                '自动转码修复失败',
                repair_error or repair_state,
            )
            branch_states: Tuple[Tuple[OperationalEventCode, str, str, str], ...] = (
                (
                    'collection_failed',
                    'collection_branch_state',
                    'collection_error',
                    '合集处理',
                ),
                ('comment_failed', 'comment_branch_state', 'review_reason', '自动评论'),
                ('danmaku_failed', 'danmaku_branch_state', 'review_reason', '弹幕回灌'),
            )
            for event, state_column, error_column, label in branch_states:
                branch_state = str(row[state_column])
                error = '' if row[error_column] is None else str(row[error_column])
                await self._report_job_state(
                    event,
                    job_id,
                    branch_state != 'failed',
                    '{}已恢复'.format(label),
                    '{}失败'.format(label),
                    error or branch_state,
                )

    async def _report_job_state(
        self,
        event: OperationalEventCode,
        job_id: int,
        healthy: bool,
        recovery_title: str,
        failure_title: str,
        detail: str,
    ) -> None:
        await self._center.report(
            event,
            'upload-job:{}'.format(job_id),
            healthy=healthy,
            title=recovery_title if healthy else failure_title,
            detail='任务 {}：{}'.format(job_id, detail),
        )

    async def _scan_capacity(self) -> None:
        if self._retention_status_provider is None:
            return
        status = await self._retention_status_provider()
        if status.capacity_bytes <= 0:
            return
        healthy = not status.warning
        await self._center.report(
            'capacity_warning',
            'recording-capacity',
            healthy=healthy,
            title='录像容量已恢复' if healthy else '录像容量不足',
            detail='已使用 {:.2f} GB / {:.2f} GB，剩余 {:.2f} GB'.format(
                status.managed_video_bytes / 1024**3,
                status.capacity_bytes / 1024**3,
                status.remaining_bytes / 1024**3,
            ),
        )

    async def _scan_network(self) -> None:
        if self._network_route_manager is None:
            return
        for state in self._network_route_manager.notification_states():
            await self._center.report(
                state.event,
                state.object_key,
                healthy=state.healthy,
                title=state.title,
                detail=state.detail,
            )
