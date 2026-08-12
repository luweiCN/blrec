from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

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


@dataclass(frozen=True)
class _OperationalObservation:
    event: OperationalEventCode
    object_key: str
    healthy: bool
    title: str
    detail: str


class _MessageSender(Protocol):
    def enqueue(
        self,
        title: str,
        content: str,
        message_type: str,
        *,
        coalesce_key: Tuple[str, ...],
    ) -> bool:
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
        return bool(
            await self.report_many(
                (
                    _OperationalObservation(
                        event=event,
                        object_key=object_key,
                        healthy=healthy,
                        title=title,
                        detail=detail,
                    ),
                )
            )
        )

    async def report_many(
        self,
        observations: Sequence[_OperationalObservation],
        *,
        retirements: Sequence[Tuple[OperationalEventCode, str]] = (),
    ) -> int:
        normalized: Dict[Tuple[OperationalEventCode, str], _OperationalObservation] = {}
        for observation in observations:
            if not observation.object_key:
                raise ValueError('notification object key must not be empty')
            normalized[(observation.event, observation.object_key)] = (
                _OperationalObservation(
                    event=observation.event,
                    object_key=observation.object_key,
                    healthy=observation.healthy,
                    title=observation.title[:200],
                    detail=observation.detail[:2000],
                )
            )
        normalized_retirements = tuple(dict.fromkeys(retirements))
        if not normalized and not normalized_retirements:
            return 0
        now = max(1, int(self._clock()))

        def transition(connection: sqlite3.Connection) -> List[_OperationalObservation]:
            for event, object_key in normalized_retirements:
                connection.execute(
                    'DELETE FROM operational_notification_states '
                    'WHERE event_code=? AND object_key=?',
                    (event, object_key),
                )
            existing = {
                (str(row['event_code']), str(row['object_key'])): (
                    int(row['healthy']),
                    str(row['title']),
                    str(row['detail']),
                )
                for row in connection.execute(
                    'SELECT event_code,object_key,healthy,title,detail '
                    'FROM operational_notification_states'
                ).fetchall()
            }
            inserted = []
            updated = []
            changed = []
            for observation in normalized.values():
                key = (str(observation.event), observation.object_key)
                normalized_healthy = 1 if observation.healthy else 0
                previous = existing.get(key)
                values = (
                    normalized_healthy,
                    observation.title,
                    observation.detail,
                    now,
                    observation.event,
                    observation.object_key,
                )
                if previous is None:
                    inserted.append(
                        (
                            observation.event,
                            observation.object_key,
                            normalized_healthy,
                            observation.title,
                            observation.detail,
                            now,
                        )
                    )
                    continue
                current = (normalized_healthy, observation.title, observation.detail)
                if previous == current:
                    continue
                updated.append(values)
                if previous[0] != normalized_healthy:
                    changed.append(observation)
            connection.executemany(
                'INSERT INTO operational_notification_states('
                'event_code,object_key,healthy,title,detail,observed_at) '
                'VALUES(?,?,?,?,?,?)',
                inserted,
            )
            connection.executemany(
                'UPDATE operational_notification_states SET healthy=?,title=?,'
                'detail=?,observed_at=? WHERE event_code=? AND object_key=?',
                updated,
            )
            return changed

        changed = await self._database.write(transition)
        settings = self._settings_provider()
        for observation in changed:
            route = settings.route_for(observation.event)
            if observation.healthy and not route.notify_recovery:
                continue
            self._dispatch(
                observation.event,
                observation.object_key,
                route.targets,
                observation.title,
                observation.detail,
            )
        return len(changed)

    async def retire_state(self, event: OperationalEventCode, object_key: str) -> bool:
        def delete(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                'DELETE FROM operational_notification_states '
                'WHERE event_code=? AND object_key=?',
                (event, object_key),
            )
            return cursor.rowcount == 1

        return await self._database.write(delete)

    def _dispatch(
        self,
        event: OperationalEventCode,
        object_key: str,
        targets: Sequence[OperationalNotificationTarget],
        title: str,
        detail: str,
    ) -> None:
        for target in targets:
            channel = str(target.channel)
            sender = self._senders.get(channel)
            if sender is None or not self._channel_enabled(channel):
                continue
            sender.enqueue(
                title,
                detail,
                str(target.message_type),
                coalesce_key=(str(event), object_key, channel),
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
        observations = []
        observations.extend(await self._account_observations())
        observations.extend(await self._recording_observations())
        observations.extend(await self._upload_job_observations())
        observations.extend(await self._capacity_observations())
        network_observations, retirements = self._network_observations()
        observations.extend(network_observations)
        await self._center.report_many(observations, retirements=retirements)

    async def _account_observations(self) -> List[_OperationalObservation]:
        rows = await self._database.fetchall(
            'SELECT id,display_name,state,pause_reason FROM bili_accounts '
            "WHERE state!='archived' ORDER BY id"
        )
        observations = []
        for row in rows:
            healthy = str(row['state']) == 'active'
            name = str(row['display_name'])
            reason = '' if row['pause_reason'] is None else str(row['pause_reason'])
            observations.append(
                _OperationalObservation(
                    event='account_unavailable',
                    object_key='account:{}'.format(int(row['id'])),
                    healthy=healthy,
                    title='投稿账号已恢复' if healthy else '投稿账号不可用',
                    detail=(
                        '{} 已恢复可用'.format(name)
                        if healthy
                        else '{}：{}'.format(name, reason or str(row['state']))
                    ),
                )
            )
        return observations

    async def _recording_observations(self) -> List[_OperationalObservation]:
        rows = await self._database.fetchall(
            'SELECT session.id,session.room_id,session.state,'
            'MAX(CASE WHEN part.artifact_state IN '
            "('failed','missing','manual_review') THEN 1 ELSE 0 END) AS failed_part "
            'FROM recording_sessions session LEFT JOIN recording_parts part '
            'ON part.session_id=session.id GROUP BY session.id ORDER BY session.id'
        )
        observations = []
        for row in rows:
            session_state = str(row['state'])
            healthy = session_state not in ('cancelled', 'manual_review') and not bool(
                row['failed_part']
            )
            room_id = int(row['room_id'])
            observations.append(
                _OperationalObservation(
                    event='recording_failed',
                    object_key='recording-session:{}'.format(int(row['id'])),
                    healthy=healthy,
                    title='录制任务已恢复' if healthy else '录制任务异常',
                    detail='房间 {}：{}'.format(
                        room_id, '录像文件已恢复可用' if healthy else session_state
                    ),
                )
            )
        return observations

    async def _upload_job_observations(self) -> List[_OperationalObservation]:
        rows = await self._database.fetchall(
            'SELECT id,state,operator_paused,review_reason,repair_state,'
            'repair_error,comment_branch_state,danmaku_branch_state,'
            'collection_branch_state,collection_error FROM upload_jobs ORDER BY id'
        )
        observations = []
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
            verification_required = any(
                marker in reason.lower()
                for marker in ('验证码', '人工验证', '安全验证', 'captcha', 'geetest')
            )
            observations.append(
                self._job_observation(
                    'upload_failed',
                    job_id,
                    not upload_failed,
                    '上传任务已恢复',
                    '投稿需要人工验证' if verification_required else '上传任务失败',
                    reason or state,
                )
            )
            observations.append(
                self._job_observation(
                    'review_rejected',
                    job_id,
                    state != 'rejected',
                    '稿件状态已恢复',
                    '稿件审核未通过',
                    reason or state,
                )
            )
            repair_failed = repair_state in ('failed', 'unknown_outcome')
            repair_error = (
                '' if row['repair_error'] is None else str(row['repair_error'])
            )
            observations.append(
                self._job_observation(
                    'transcode_repair_failed',
                    job_id,
                    not repair_failed,
                    '转码修复已恢复',
                    '自动转码修复失败',
                    repair_error or repair_state,
                )
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
                observations.append(
                    self._job_observation(
                        event,
                        job_id,
                        branch_state != 'failed',
                        '{}已恢复'.format(label),
                        '{}失败'.format(label),
                        error or branch_state,
                    )
                )
        return observations

    @staticmethod
    def _job_observation(
        event: OperationalEventCode,
        job_id: int,
        healthy: bool,
        recovery_title: str,
        failure_title: str,
        detail: str,
    ) -> _OperationalObservation:
        return _OperationalObservation(
            event=event,
            object_key='upload-job:{}'.format(job_id),
            healthy=healthy,
            title=recovery_title if healthy else failure_title,
            detail='任务 {}：{}'.format(job_id, detail),
        )

    async def _capacity_observations(self) -> List[_OperationalObservation]:
        if self._retention_status_provider is None:
            return []
        status = await self._retention_status_provider()
        if status.capacity_bytes <= 0:
            return []
        healthy = not status.warning
        return [
            _OperationalObservation(
                event='capacity_warning',
                object_key='recording-capacity',
                healthy=healthy,
                title='录像容量已恢复' if healthy else '录像容量不足',
                detail='已使用 {:.2f} GB / {:.2f} GB，剩余 {:.2f} GB'.format(
                    status.managed_video_bytes / 1024**3,
                    status.capacity_bytes / 1024**3,
                    status.remaining_bytes / 1024**3,
                ),
            )
        ]

    def _network_observations(
        self,
    ) -> Tuple[
        List[_OperationalObservation], Tuple[Tuple[OperationalEventCode, str], ...]
    ]:
        if self._network_route_manager is None:
            return [], ()
        observations = [
            _OperationalObservation(
                event=state.event,
                object_key=state.object_key,
                healthy=state.healthy,
                title=state.title,
                detail=state.detail,
            )
            for state in self._network_route_manager.notification_states()
        ]
        return observations, (('network_failover', 'network-route:upload:failover'),)

    async def _scan_network(self) -> None:
        observations, retirements = self._network_observations()
        await self._center.report_many(observations, retirements=retirements)
