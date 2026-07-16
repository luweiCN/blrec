from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Dict, Literal, Mapping, Optional, cast

from .database import BiliUploadDatabase
from .policies import (
    RoomUploadPolicyCommand,
    RoomUploadPolicyManager,
    RoomUploadPolicyNotFound,
    default_room_upload_policy,
    room_upload_policy_command,
)

__all__ = (
    'InvalidSessionSubmission',
    'RecordingSessionNotFound',
    'SessionSubmissionLocked',
    'SessionSubmissionManager',
    'SessionSubmissionView',
    'SubmissionDecision',
    'decode_submission_settings',
    'encode_submission_settings',
)


SubmissionDecision = Literal['follow_room', 'upload', 'skip']
_DECISIONS = frozenset(('follow_room', 'upload', 'skip'))


class InvalidSessionSubmission(RuntimeError):
    pass


class RecordingSessionNotFound(RuntimeError):
    pass


class SessionSubmissionLocked(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionSubmissionView:
    session_id: int
    room_id: int
    decision: SubmissionDecision
    inherited: bool
    settings_source: str
    settings: RoomUploadPolicyCommand
    resolution_state: str
    resolution_error: Optional[str]


def encode_submission_settings(command: RoomUploadPolicyCommand) -> str:
    return json.dumps(
        asdict(command), ensure_ascii=False, separators=(',', ':'), sort_keys=True
    )


def decode_submission_settings(value: str) -> RoomUploadPolicyCommand:
    try:
        raw = json.loads(value)
        if not isinstance(raw, Mapping):
            raise TypeError('submission settings must be an object')
        return RoomUploadPolicyCommand(**cast(Dict[str, Any], dict(raw)))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidSessionSubmission(
            'stored submission settings are invalid'
        ) from error


class SessionSubmissionManager:
    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        policy_manager: RoomUploadPolicyManager,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._policy_manager = policy_manager
        self._clock = clock

    async def get(self, session_id: int) -> SessionSubmissionView:
        row = await self._database.fetchone(
            'SELECT id,room_id,upload_decision,upload_override_json,'
            'upload_resolution_state,upload_resolution_error '
            'FROM recording_sessions WHERE id=?',
            (session_id,),
        )
        if row is None:
            raise RecordingSessionNotFound('recording session not found')
        override_json = row['upload_override_json']
        if override_json is not None:
            settings = decode_submission_settings(str(override_json))
            inherited = False
            settings_source = 'session'
        else:
            inherited = True
            try:
                policy = await self._policy_manager.get(int(row['room_id']))
            except RoomUploadPolicyNotFound:
                settings = default_room_upload_policy()
                settings_source = 'default'
            else:
                settings = room_upload_policy_command(policy)
                settings_source = 'room'
        return SessionSubmissionView(
            session_id=int(row['id']),
            room_id=int(row['room_id']),
            decision=cast(SubmissionDecision, str(row['upload_decision'])),
            inherited=inherited,
            settings_source=settings_source,
            settings=settings,
            resolution_state=str(row['upload_resolution_state']),
            resolution_error=(
                None
                if row['upload_resolution_error'] is None
                else str(row['upload_resolution_error'])
            ),
        )

    async def set_decision(
        self, session_id: int, decision: str, *, manager_subject: str
    ) -> SessionSubmissionView:
        if decision not in _DECISIONS:
            raise InvalidSessionSubmission('submission decision is invalid')
        self._require_subject(manager_subject)
        now = int(self._clock())

        def update(connection: sqlite3.Connection) -> None:
            row = self._mutable_session(connection, session_id)
            old_decision = str(row['upload_decision'])
            connection.execute(
                "UPDATE recording_sessions SET upload_decision=?,"
                "upload_resolution_state='pending',upload_resolution_error=NULL,"
                'upload_resolved_at=NULL WHERE id=?',
                (decision, session_id),
            )
            self._audit(
                connection,
                manager_subject=manager_subject,
                action='set_session_submission_decision',
                session_id=session_id,
                old_state=old_decision,
                new_state=decision,
                now=now,
            )

        await self._database.write(update)
        return await self.get(session_id)

    async def save_override(
        self, session_id: int, command: RoomUploadPolicyCommand, *, manager_subject: str
    ) -> SessionSubmissionView:
        self._require_subject(manager_subject)
        session = await self._database.fetchone(
            'SELECT room_id FROM recording_sessions WHERE id=?', (session_id,)
        )
        if session is None:
            raise RecordingSessionNotFound('recording session not found')
        normalized = replace(command, enabled=True)
        await self._policy_manager.validate(int(session['room_id']), normalized)
        encoded = encode_submission_settings(normalized)
        now = int(self._clock())

        def update(connection: sqlite3.Connection) -> None:
            row = self._mutable_session(connection, session_id)
            old_state = (
                'inherited' if row['upload_override_json'] is None else 'override'
            )
            connection.execute(
                "UPDATE recording_sessions SET upload_override_json=?,"
                "upload_resolution_state='pending',upload_resolution_error=NULL,"
                'upload_resolved_at=NULL WHERE id=?',
                (encoded, session_id),
            )
            self._audit(
                connection,
                manager_subject=manager_subject,
                action='save_session_submission_override',
                session_id=session_id,
                old_state=old_state,
                new_state='override',
                now=now,
            )

        await self._database.write(update)
        return await self.get(session_id)

    async def clear_override(
        self, session_id: int, *, manager_subject: str
    ) -> SessionSubmissionView:
        self._require_subject(manager_subject)
        now = int(self._clock())

        def update(connection: sqlite3.Connection) -> None:
            row = self._mutable_session(connection, session_id)
            old_state = (
                'inherited' if row['upload_override_json'] is None else 'override'
            )
            connection.execute(
                "UPDATE recording_sessions SET upload_override_json=NULL,"
                "upload_resolution_state='pending',upload_resolution_error=NULL,"
                'upload_resolved_at=NULL WHERE id=?',
                (session_id,),
            )
            self._audit(
                connection,
                manager_subject=manager_subject,
                action='clear_session_submission_override',
                session_id=session_id,
                old_state=old_state,
                new_state='inherited',
                now=now,
            )

        await self._database.write(update)
        return await self.get(session_id)

    @staticmethod
    def _mutable_session(
        connection: sqlite3.Connection, session_id: int
    ) -> sqlite3.Row:
        row = connection.execute(
            'SELECT id,upload_decision,upload_override_json,'
            'upload_resolution_state FROM recording_sessions WHERE id=?',
            (session_id,),
        ).fetchone()
        if row is None:
            raise RecordingSessionNotFound('recording session not found')
        job = connection.execute(
            'SELECT 1 FROM upload_jobs WHERE session_id=?', (session_id,)
        ).fetchone()
        if job is not None or str(row['upload_resolution_state']) == 'job_created':
            raise SessionSubmissionLocked(
                'submission settings are immutable after upload job creation'
            )
        return row

    @staticmethod
    def _require_subject(manager_subject: str) -> None:
        if not manager_subject.strip():
            raise InvalidSessionSubmission('manager subject must not be empty')

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        manager_subject: str,
        action: str,
        session_id: int,
        old_state: str,
        new_state: str,
        now: int,
    ) -> None:
        connection.execute(
            'INSERT INTO management_audit('
            'manager_subject,action,target_type,target_id,old_state,new_state,'
            'reason,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (
                manager_subject,
                action,
                'recording_session',
                str(session_id),
                old_state,
                new_state,
                'recording session submission settings changed',
                now,
            ),
        )
