from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel

from blrec.bili_upload.journal import (
    RecordingJournalBridge,
    RecordingPart,
    RecordingSession,
)
from blrec.utils.string import camel_case

from .bili_accounts import authenticated_manager_subject

journal: Optional[RecordingJournalBridge] = None
unavailable_reason: Optional[str] = 'Recording journal is not enabled'


class ApiModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class RecordingPartResponse(ApiModel):
    id: int
    run_id: str
    part_index: int
    source_path: str
    final_path: Optional[str]
    xml_path: Optional[str]
    record_start_time: int
    artifact_state: str
    xml_completed: bool
    source_exists: bool
    final_exists: bool
    error_message: Optional[str]


class RecordingSessionResponse(ApiModel):
    id: int
    room_id: int
    broadcast_session_key: str
    live_start_time: Optional[int]
    state: str
    started_at: int
    ended_at: Optional[int]
    parts: List[RecordingPartResponse]


class RecordingSessionsResponse(ApiModel):
    degraded_reason: Optional[str]
    sessions: List[RecordingSessionResponse]


def get_recording_journal() -> RecordingJournalBridge:
    if journal is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Recording journal is unavailable',
        )
    return journal


def _part_response(part: RecordingPart) -> RecordingPartResponse:
    return RecordingPartResponse(
        id=part.id,
        run_id=part.run_id,
        part_index=part.part_index,
        source_path=part.source_path,
        final_path=part.final_path,
        xml_path=part.xml_path,
        record_start_time=part.record_start_time,
        artifact_state=part.artifact_state,
        xml_completed=part.xml_completed,
        source_exists=part.source_exists,
        final_exists=part.final_exists,
        error_message=part.error_message,
    )


def _session_response(session: RecordingSession) -> RecordingSessionResponse:
    return RecordingSessionResponse(
        id=session.id,
        room_id=session.room_id,
        broadcast_session_key=session.broadcast_session_key,
        live_start_time=session.live_start_time,
        state=session.state,
        started_at=session.started_at,
        ended_at=session.ended_at,
        parts=[_part_response(part) for part in session.parts],
    )


router = APIRouter(prefix='/recording-sessions', tags=['recording-sessions'])


@router.get('', response_model=RecordingSessionsResponse)
async def list_recording_sessions(
    limit: int = Query(50, ge=1, le=200),
    _subject: str = Depends(authenticated_manager_subject),
    recording_journal: RecordingJournalBridge = Depends(get_recording_journal),
) -> RecordingSessionsResponse:
    sessions = await recording_journal.list_sessions(limit=limit)
    return RecordingSessionsResponse(
        degraded_reason=recording_journal.degraded_reason,
        sessions=[_session_response(session) for session in sessions],
    )
