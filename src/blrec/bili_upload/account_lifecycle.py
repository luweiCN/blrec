from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Set, Tuple

from .database import BiliUploadDatabase

__all__ = (
    'AccountLifecycle',
    'AccountRelationships',
    'AccountRemovalBlocked',
    'AccountRemovalCommand',
    'AccountRemovalResult',
    'InvalidAccountReplacement',
    'LifecycleAccountNotFound',
    'RelatedUploadJob',
    'RemovalMode',
)


class RemovalMode(str, Enum):
    FOLLOW_PRIMARY = 'follow_primary'
    FIXED = 'fixed'
    DISABLE = 'disable'


@dataclass(frozen=True)
class RelatedUploadJob:
    id: int
    room_id: int
    state: str


@dataclass(frozen=True)
class AccountRelationships:
    account_id: int
    is_primary: bool
    follow_primary_room_ids: Tuple[int, ...]
    fixed_room_ids: Tuple[int, ...]
    reassignable_jobs: Tuple[RelatedUploadJob, ...]
    blocking_jobs: Tuple[RelatedUploadJob, ...]
    historical_job_count: int


@dataclass(frozen=True)
class AccountRemovalCommand:
    mode: RemovalMode
    replacement_account_id: Optional[int] = None
    new_primary_account_id: Optional[int] = None


@dataclass(frozen=True)
class AccountRemovalResult:
    account_id: int
    state: str = 'archived'


class LifecycleAccountNotFound(RuntimeError):
    pass


class InvalidAccountReplacement(RuntimeError):
    pass


class AccountRemovalBlocked(RuntimeError):
    def __init__(self, jobs: Tuple[RelatedUploadJob, ...]) -> None:
        super().__init__('account has upload jobs with remote side effects')
        self.jobs = jobs


class AccountLifecycle:
    _REASSIGNABLE_JOB_STATES = frozenset(('waiting_artifacts', 'ready', 'paused'))
    _HISTORICAL_JOB_STATES = frozenset(('completed', 'rejected'))
    _DISABLED_JOB_REASON = 'upload account removed; select an account before resuming'

    def __init__(
        self, database: BiliUploadDatabase, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._database = database
        self._clock = clock

    async def relationships(self, account_id: int) -> AccountRelationships:
        return await self._database.read(
            lambda connection: self._relationships(connection, account_id)
        )

    async def remove(
        self, account_id: int, command: AccountRemovalCommand, *, manager_subject: str
    ) -> AccountRemovalResult:
        if not manager_subject:
            raise ValueError('manager subject is required')
        timestamp = int(self._clock())
        return await self._database.write(
            lambda connection: self._remove(
                connection,
                account_id,
                command,
                manager_subject=manager_subject,
                timestamp=timestamp,
            )
        )

    def _relationships(
        self, connection: sqlite3.Connection, account_id: int
    ) -> AccountRelationships:
        account = connection.execute(
            'SELECT state,EXISTS('
            'SELECT 1 FROM bili_account_selection selection '
            'WHERE selection.id=1 AND selection.primary_account_id='
            'bili_accounts.id) AS is_primary '
            'FROM bili_accounts WHERE id=?',
            (account_id,),
        ).fetchone()
        if account is None:
            raise LifecycleAccountNotFound('Bilibili account not found')
        is_primary = bool(account['is_primary'])
        fixed_room_ids = tuple(
            int(row['room_id'])
            for row in connection.execute(
                "SELECT room_id FROM room_upload_policies "
                "WHERE account_mode='fixed' AND account_id=? ORDER BY room_id",
                (account_id,),
            ).fetchall()
        )
        follow_primary_room_ids: Tuple[int, ...] = ()
        if is_primary:
            follow_primary_room_ids = tuple(
                int(row['room_id'])
                for row in connection.execute(
                    "SELECT room_id FROM room_upload_policies "
                    "WHERE account_mode='primary' ORDER BY room_id"
                ).fetchall()
            )

        reassignable: List[RelatedUploadJob] = []
        blocking: List[RelatedUploadJob] = []
        historical_count = 0
        rows = connection.execute(
            'SELECT job.id,session.room_id,job.state,job.submit_state,'
            'job.lease_owner,'
            'EXISTS(SELECT 1 FROM upload_parts part '
            "WHERE part.job_id=job.id AND part.upload_state!='prepared') "
            'AS has_started_part,'
            'EXISTS(SELECT 1 FROM upload_chunks chunk '
            'JOIN upload_parts part ON part.id=chunk.part_id '
            "WHERE part.job_id=job.id AND chunk.state!='prepared') "
            'AS has_started_chunk,'
            'EXISTS(SELECT 1 FROM comment_items comment '
            "WHERE comment.job_id=job.id AND comment.state!='prepared') "
            'AS has_started_comment,'
            'EXISTS(SELECT 1 FROM danmaku_items danmaku '
            'JOIN upload_parts part ON part.id=danmaku.part_id '
            "WHERE part.job_id=job.id AND danmaku.state!='prepared') "
            'AS has_started_danmaku '
            'FROM upload_jobs job '
            'JOIN recording_sessions session ON session.id=job.session_id '
            'WHERE job.account_id=? ORDER BY job.id',
            (account_id,),
        ).fetchall()
        for row in rows:
            job = RelatedUploadJob(
                id=int(row['id']), room_id=int(row['room_id']), state=str(row['state'])
            )
            if job.state in self._HISTORICAL_JOB_STATES:
                historical_count += 1
                continue
            has_remote_side_effect = any(
                bool(row[column])
                for column in (
                    'has_started_part',
                    'has_started_chunk',
                    'has_started_comment',
                    'has_started_danmaku',
                )
            )
            if (
                job.state in self._REASSIGNABLE_JOB_STATES
                and str(row['submit_state']) == 'prepared'
                and row['lease_owner'] is None
                and not has_remote_side_effect
            ):
                reassignable.append(job)
            else:
                blocking.append(job)

        return AccountRelationships(
            account_id=account_id,
            is_primary=is_primary,
            follow_primary_room_ids=follow_primary_room_ids,
            fixed_room_ids=fixed_room_ids,
            reassignable_jobs=tuple(reassignable),
            blocking_jobs=tuple(blocking),
            historical_job_count=historical_count,
        )

    def _remove(
        self,
        connection: sqlite3.Connection,
        account_id: int,
        command: AccountRemovalCommand,
        *,
        manager_subject: str,
        timestamp: int,
    ) -> AccountRemovalResult:
        relationships = self._relationships(connection, account_id)
        account = connection.execute(
            'SELECT state FROM bili_accounts WHERE id=?', (account_id,)
        ).fetchone()
        assert account is not None
        old_state = str(account['state'])
        if old_state == 'archived':
            raise InvalidAccountReplacement('Bilibili account is already archived')
        if relationships.blocking_jobs:
            raise AccountRemovalBlocked(relationships.blocking_jobs)

        active_ids = {
            int(row['id'])
            for row in connection.execute(
                "SELECT id FROM bili_accounts WHERE state='active' AND id!=?",
                (account_id,),
            ).fetchall()
        }
        current_primary_row = connection.execute(
            'SELECT account.id FROM bili_account_selection selection '
            'JOIN bili_accounts account ON account.id=selection.primary_account_id '
            "WHERE selection.id=1 AND account.state='active'"
        ).fetchone()
        current_primary_id = (
            None if current_primary_row is None else int(current_primary_row['id'])
        )
        new_primary_id = current_primary_id
        if relationships.is_primary:
            if active_ids:
                new_primary_id = self._require_active_replacement(
                    command.new_primary_account_id,
                    active_ids,
                    'a new primary account is required',
                )
            else:
                if command.mode is not RemovalMode.DISABLE:
                    raise InvalidAccountReplacement(
                        'disable is required when no other active account exists'
                    )
                new_primary_id = None
        elif command.new_primary_account_id is not None:
            raise InvalidAccountReplacement(
                'new primary account is only valid when removing the primary account'
            )

        replacement_id: Optional[int] = None
        if command.mode is RemovalMode.FIXED:
            replacement_id = self._require_active_replacement(
                command.replacement_account_id,
                active_ids,
                'an active replacement account is required',
            )
        elif command.replacement_account_id is not None:
            raise InvalidAccountReplacement(
                'replacement account is only valid for fixed reassignment'
            )
        elif command.mode is RemovalMode.FOLLOW_PRIMARY and new_primary_id is None:
            raise InvalidAccountReplacement('an active primary account is required')

        if relationships.is_primary:
            if new_primary_id is None:
                connection.execute('DELETE FROM bili_account_selection WHERE id=1')
            else:
                connection.execute(
                    'UPDATE bili_account_selection SET primary_account_id=? '
                    'WHERE id=1',
                    (new_primary_id,),
                )

        self._update_room_policies(
            connection,
            account_id,
            command.mode,
            replacement_id=replacement_id,
            include_follow_primary=relationships.is_primary,
            timestamp=timestamp,
        )
        job_ids = tuple(job.id for job in relationships.reassignable_jobs)
        self._update_jobs(
            connection,
            job_ids,
            command.mode,
            replacement_id=(
                new_primary_id
                if command.mode is RemovalMode.FOLLOW_PRIMARY
                else replacement_id
            ),
            timestamp=timestamp,
        )
        cursor = connection.execute(
            "UPDATE bili_accounts SET state='archived',pause_reason=?,"
            "credential_ciphertext=X'',key_id='archived',"
            'credential_expires_at=0,updated_at=? WHERE id=?',
            ('removed by operator', timestamp, account_id),
        )
        if cursor.rowcount != 1:
            raise LifecycleAccountNotFound('Bilibili account not found')
        connection.execute(
            'INSERT INTO management_audit('
            'manager_subject,action,target_type,target_id,old_state,new_state,'
            'reason,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (
                manager_subject,
                'remove_bili_account',
                'bili_account',
                str(account_id),
                old_state,
                'archived',
                'removal mode: {}'.format(command.mode.value),
                timestamp,
            ),
        )
        return AccountRemovalResult(account_id=account_id)

    @staticmethod
    def _require_active_replacement(
        account_id: Optional[int], active_ids: Set[int], message: str
    ) -> int:
        if account_id is None or account_id not in active_ids:
            raise InvalidAccountReplacement(message)
        return account_id

    @staticmethod
    def _update_room_policies(
        connection: sqlite3.Connection,
        account_id: int,
        mode: RemovalMode,
        *,
        replacement_id: Optional[int],
        include_follow_primary: bool,
        timestamp: int,
    ) -> None:
        affected = (
            "(account_mode='fixed' AND account_id=?) OR "
            "(?=1 AND account_mode='primary')"
        )
        if mode is RemovalMode.FOLLOW_PRIMARY:
            connection.execute(
                "UPDATE room_upload_policies SET account_mode='primary',"
                'account_id=NULL,updated_at=? WHERE ' + affected,
                (timestamp, account_id, int(include_follow_primary)),
            )
        elif mode is RemovalMode.FIXED:
            assert replacement_id is not None
            connection.execute(
                "UPDATE room_upload_policies SET account_mode='fixed',"
                'account_id=?,updated_at=? WHERE ' + affected,
                (replacement_id, timestamp, account_id, int(include_follow_primary)),
            )
        else:
            connection.execute(
                'UPDATE room_upload_policies SET enabled=0,updated_at=? WHERE '
                + affected,
                (timestamp, account_id, int(include_follow_primary)),
            )

    def _update_jobs(
        self,
        connection: sqlite3.Connection,
        job_ids: Tuple[int, ...],
        mode: RemovalMode,
        *,
        replacement_id: Optional[int],
        timestamp: int,
    ) -> None:
        if mode is RemovalMode.DISABLE:
            connection.executemany(
                "UPDATE upload_jobs SET state='paused',review_reason=?,"
                'updated_at=? WHERE id=?',
                ((self._DISABLED_JOB_REASON, timestamp, job_id) for job_id in job_ids),
            )
            return
        assert replacement_id is not None
        connection.executemany(
            'UPDATE upload_jobs SET account_id=?,updated_at=? WHERE id=?',
            ((replacement_id, timestamp, job_id) for job_id in job_ids),
        )
