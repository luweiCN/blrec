from __future__ import annotations

import base64
import time
from typing import Any, Dict, List, Literal, Optional, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, status
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from loguru import logger
from pydantic import BaseModel, Field

from blrec.utils.string import camel_case
from blrec.vainglory.analysis_protocol import (
    decode_hero,
    decode_match,
    decode_matches,
    decode_recorded_player,
    decode_training_candidates,
)
from blrec.vainglory.analyzer import AnalysisStatus
from blrec.vainglory.archive_backfill import (
    ArchiveBackfillItem,
    ArchiveBackfillNotFound,
    ArchiveBackfillService,
    ArchiveBackfillUnavailable,
    ArchiveContentReview,
    ArchiveSync,
)
from blrec.vainglory.catalog import hero_chinese_name
from blrec.vainglory.publication import VaingloryPublicationService
from blrec.vainglory.repository import (
    AnchorStatsRecord,
    GameModeStatsRecord,
    HeroRecord,
    HeroStatsRecord,
    MatchPlayerRecord,
    MatchRecord,
    MatchSessionRecord,
    PlayerRecord,
    PlayerRoomRecord,
    PlayerStatsRecord,
    ScanJob,
    VaingloryConflict,
    VaingloryNotFound,
    ZeroMatchSessionRecord,
)
from blrec.vainglory.service import VaingloryIndexService

from .. import security
from .bili_accounts import authenticated_manager_subject

service: Optional[VaingloryIndexService] = None
publication: Optional[VaingloryPublicationService] = None
archive_backfill: Optional[ArchiveBackfillService] = None
unavailable_reason: Optional[str] = 'Vainglory match index is not ready'


class ApiModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class ScanJobResponse(ApiModel):
    session_id: int
    state: str
    progress: float
    algorithm_version: int
    match_count: int
    error: Optional[str]
    requested_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    updated_at: int


class AnalysisWorkerHeartbeatRequest(ApiModel):
    kind: Literal['part', 'match_rerun', 'hero_rematch', 'recorded_player_backfill']
    item_id: int = Field(..., ge=1)
    progress: float = Field(0, ge=0, le=0.99)
    runtime_status: Optional[Dict[str, Any]] = None


class AnalysisWorkerCompleteRequest(ApiModel):
    kind: Literal['part', 'match_rerun', 'hero_rematch', 'recorded_player_backfill']
    item_id: int = Field(..., ge=1)
    candidate_count: int = Field(0, ge=0)
    matches: List[Dict[str, Any]] = Field(default_factory=list)
    heroes: List[Dict[str, Any]] = Field(default_factory=list)
    recorded_player: Optional[Dict[str, Any]] = None
    training_candidates: List[Dict[str, Any]] = Field(default_factory=list)


class AnalysisWorkerFailureRequest(ApiModel):
    kind: Literal['part', 'match_rerun', 'hero_rematch', 'recorded_player_backfill']
    item_id: int = Field(..., ge=1)
    error: str = Field(..., min_length=1, max_length=500)


class MatchPlayerResponse(ApiModel):
    side: str
    slot: int
    name: str
    hero_id: Optional[int]
    hero_label: str
    hero_source: Literal['automatic', 'manual']
    kills: Optional[int]
    deaths: Optional[int]
    assists: Optional[int]
    economy: Optional[int]
    last_hits: Optional[int]
    confidence: float
    is_recorded_player: bool


class MatchResponse(ApiModel):
    id: int
    session_id: int
    session_title: str
    session_started_at: int
    part_id: int
    part_index: int
    title: str
    source_title: str
    upload_title: str
    game_mode: str
    team_size: Optional[int]
    match_kind: Literal['pvp', 'bot', 'practice', 'unknown']
    view_context: Literal['played', 'observed', 'unknown']
    stats_eligible: bool
    stats_exclusion_reason: Optional[str]
    started_at_ms: int
    result_at_ms: int
    duration_seconds: Optional[int]
    result_text: str
    end_reason: str
    left_color: str
    right_color: str
    winner_side: str
    winner_color: str
    left_kills: Optional[int]
    right_kills: Optional[int]
    left_economy: Optional[int]
    right_economy: Optional[int]
    confidence: float
    account_id: Optional[int]
    bvid: Optional[str]
    archive_page: Optional[int]
    result_frame_url: Optional[str]
    recorded_player_confidence: Optional[float]
    recorded_player_source: Literal['automatic', 'manual']
    recorded_player_state: Literal[
        'pending', 'uncertain', 'automatic', 'manual', 'unsupported'
    ]
    rerun_state: Optional[Literal['pending', 'running', 'failed']]
    rerun_error: Optional[str]
    players: List[MatchPlayerResponse]


class MatchListResponse(ApiModel):
    total: int
    items: List[MatchResponse]


class MatchSessionResponse(ApiModel):
    session_id: int
    title: str
    source_title: str
    anchor_name: str
    started_at: int
    live_started_at: int
    part_count: int
    recording_duration_seconds: int
    match_count: int
    teal_win_count: int
    orange_win_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    surrender_count: int
    duration_seconds: int
    game_modes: List[str]
    stats_included: bool
    bvid: Optional[str]
    publication_state: Optional[str]
    description_state: Optional[str]
    pin_state: Optional[str]
    chapter_state: Optional[str]
    publication_priority: bool
    publication_updated_at: Optional[int]


class MatchSessionListResponse(ApiModel):
    total: int
    items: List[MatchSessionResponse]


class ZeroMatchSessionResponse(ApiModel):
    session_id: int
    title: str
    source_title: str
    anchor_name: str
    started_at: int
    completed_at: int
    recording_duration_seconds: int
    part_count: int
    bvid: Optional[str]


class ZeroMatchSessionListResponse(ApiModel):
    total: int
    items: List[ZeroMatchSessionResponse]


class AnchorStatsResponse(ApiModel):
    anchor_uid: Optional[int]
    anchor_name: str
    room_id: int
    session_count: int
    match_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    win_rate: float


class PlayerRoomResponse(ApiModel):
    room_id: int
    anchor_uid: Optional[int]
    anchor_name: str


class PlayerResponse(ApiModel):
    id: int
    name: str
    origin: Literal['automatic', 'manual']
    rooms: List[PlayerRoomResponse]
    created_at: int
    updated_at: int


class GameModeStatsResponse(ApiModel):
    game_mode: str
    match_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    win_rate: float


class HeroStatsResponse(ApiModel):
    hero_id: int
    hero_label: str
    player_count: int
    match_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    win_rate: float


class PlayerStatsResponse(ApiModel):
    player_id: int
    player_name: str
    rooms: List[PlayerRoomResponse]
    session_count: int
    match_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    win_rate: float
    modes: List[GameModeStatsResponse]
    heroes: List[HeroStatsResponse]


class HeroResponse(ApiModel):
    id: int
    label: str
    fingerprint: str
    thumbnail_url: str


class LabelHeroRequest(ApiModel):
    label: str = Field('', max_length=80)


class MatchTitleRequest(ApiModel):
    title: str = Field('', max_length=200)


class MatchPlayerUpdateRequest(ApiModel):
    side: Literal['left', 'right']
    slot: int = Field(..., ge=1, le=5)
    name: Optional[str] = Field(None, max_length=80)
    hero_id: Optional[int] = Field(None, ge=1)
    kills: Optional[int] = Field(None, ge=0)
    deaths: Optional[int] = Field(None, ge=0)
    assists: Optional[int] = Field(None, ge=0)
    economy: Optional[int] = Field(None, ge=0)
    last_hits: Optional[int] = Field(None, ge=0)


class MatchUpdateRequest(ApiModel):
    title: Optional[str] = Field(None, max_length=200)
    game_mode: Optional[Literal['3v3', '5v5', 'aram', 'other', 'unknown']] = None
    duration_seconds: Optional[int] = Field(None, gt=0)
    result_text: Optional[str] = Field(None, max_length=32)
    end_reason: Optional[Literal['normal', 'surrender', 'unknown']] = None
    winner_color: Optional[Literal['teal', 'orange', 'unknown']] = None
    match_kind: Optional[Literal['pvp', 'bot', 'practice', 'unknown']] = None
    view_context: Optional[Literal['played', 'observed', 'unknown']] = None
    stats_eligible: Optional[bool] = None
    left_kills: Optional[int] = Field(None, ge=0)
    right_kills: Optional[int] = Field(None, ge=0)
    left_economy: Optional[int] = Field(None, ge=0)
    right_economy: Optional[int] = Field(None, ge=0)
    players: Optional[List[MatchPlayerUpdateRequest]] = Field(None, max_items=10)


class PlayerNameRequest(ApiModel):
    name: str = Field(..., max_length=80)


class PlayerRoomSeedRequest(ApiModel):
    room_id: int = Field(..., gt=0)
    name: str = Field(..., min_length=1, max_length=80)


class PlayerRoomSyncRequest(ApiModel):
    rooms: List[PlayerRoomSeedRequest] = Field(..., max_items=500)


class RecordedPlayerRequest(ApiModel):
    side: Literal['left', 'right']
    slot: int = Field(..., ge=1, le=5)


class PlayerHeroRequest(ApiModel):
    hero_id: int = Field(..., ge=1)


class ManualMatchMarkerRequest(ApiModel):
    part_index: int = Field(..., ge=1, le=10_000)
    at_ms: int = Field(..., ge=0, le=604_800_000)


class ManualMatchMarkerResponse(ApiModel):
    id: int
    session_id: int
    part_id: int
    part_index: int
    at_ms: int


class SessionAnchorRequest(ApiModel):
    anchor_name: str = Field('', max_length=200)


class SessionBulkUpdateRequest(ApiModel):
    session_ids: List[int] = Field(..., min_items=1, max_items=100)
    anchor_name: Optional[str] = Field(None, max_length=200)
    stats_included: Optional[bool] = None


class SessionBulkUpdateResponse(ApiModel):
    updated_count: int


class ArchiveSyncResponse(ApiModel):
    account_id: int
    state: str
    progress: float
    discovered_count: int
    completed_count: int
    error: Optional[str]
    requested_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    updated_at: int
    operator_paused: bool
    daily_limit: int
    daily_used: int
    quota_day: Optional[str]
    next_page: int
    discovery_complete: bool
    season_started_at: Optional[int]
    season_ended_at: Optional[int]


class ArchiveSyncControlRequest(ApiModel):
    paused: Optional[bool] = None
    daily_limit: Optional[int] = Field(None, ge=1, le=500)


class ArchiveBackfillItemResponse(ApiModel):
    id: int
    account_id: int
    aid: int
    bvid: str
    title: str
    published_at: Optional[int]
    state: str
    stage: str
    progress: float
    page_count: int
    completed_page_count: int
    current_page: Optional[int]
    current_part_title: Optional[str]
    download_progress: float
    downloaded_bytes: int
    total_bytes: Optional[int]
    analysis_state: Optional[str]
    analysis_progress: float
    match_count: int
    publication_state: Optional[str]
    description_state: Optional[str]
    comment_count: int
    confirmed_comment_count: int
    pin_state: Optional[str]
    publication_progress: float
    error: Optional[str]
    updated_at: int


class ArchiveContentReviewResponse(ApiModel):
    id: int
    account_id: int
    account_name: str
    aid: int
    bvid: str
    title: str
    published_at: Optional[int]
    reason: str


class ArchiveContentReviewListResponse(ApiModel):
    total: int
    items: List[ArchiveContentReviewResponse]


def get_service() -> VaingloryIndexService:
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Vainglory match index is unavailable',
        )
    return service


def get_archive_backfill() -> ArchiveBackfillService:
    if archive_backfill is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Vainglory archive backfill is unavailable',
        )
    return archive_backfill


def get_publication() -> VaingloryPublicationService:
    if publication is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_reason or 'Vainglory publication is unavailable',
        )
    return publication


def _scan_job(value: ScanJob) -> ScanJobResponse:
    return ScanJobResponse(**value.__dict__)


def _player(value: MatchPlayerRecord) -> MatchPlayerResponse:
    return MatchPlayerResponse(
        side=value.side,
        slot=value.slot,
        name=value.name,
        hero_id=value.hero_id,
        hero_label=hero_chinese_name(value.hero_label),
        hero_source=value.hero_source,
        kills=value.kills,
        deaths=value.deaths,
        assists=value.assists,
        economy=value.economy,
        last_hits=value.last_hits,
        confidence=value.confidence,
        is_recorded_player=value.is_recorded_player,
    )


def _match(value: MatchRecord) -> MatchResponse:
    return MatchResponse(
        id=value.id,
        session_id=value.session_id,
        session_title=value.session_title,
        session_started_at=value.session_started_at,
        part_id=value.part_id,
        part_index=value.part_index,
        title=value.title,
        source_title=value.source_title,
        upload_title=value.upload_title,
        game_mode=value.game_mode,
        team_size=value.team_size,
        match_kind=value.match_kind,
        view_context=value.view_context,
        stats_eligible=value.stats_eligible,
        stats_exclusion_reason=value.stats_exclusion_reason,
        started_at_ms=value.started_at_ms,
        result_at_ms=value.result_at_ms,
        duration_seconds=value.duration_seconds,
        result_text=value.result_text,
        end_reason=value.end_reason,
        left_color=value.left_color,
        right_color=value.right_color,
        winner_side=value.winner_side,
        winner_color=value.winner_color,
        left_kills=value.left_kills,
        right_kills=value.right_kills,
        left_economy=value.left_economy,
        right_economy=value.right_economy,
        confidence=value.confidence,
        account_id=value.account_id,
        bvid=value.bvid,
        archive_page=value.archive_page,
        result_frame_url=(
            '/api/v1/vainglory/matches/{}/result-frame?v={}-{}-{}'.format(
                value.id, value.session_id, value.part_id, value.result_at_ms
            )
            if value.has_result_frame
            else None
        ),
        recorded_player_confidence=value.recorded_player_confidence,
        recorded_player_source=cast(
            Literal['automatic', 'manual'], value.recorded_player_source
        ),
        recorded_player_state=cast(
            Literal['pending', 'uncertain', 'automatic', 'manual', 'unsupported'],
            value.recorded_player_state,
        ),
        rerun_state=cast(
            Optional[Literal['pending', 'running', 'failed']], value.rerun_state
        ),
        rerun_error=value.rerun_error,
        players=[_player(player) for player in value.players],
    )


def _match_session(value: MatchSessionRecord) -> MatchSessionResponse:
    return MatchSessionResponse(
        session_id=value.session_id,
        title=value.title,
        source_title=value.source_title,
        anchor_name=value.anchor_name,
        started_at=value.started_at,
        live_started_at=value.live_started_at,
        part_count=value.part_count,
        recording_duration_seconds=value.recording_duration_seconds,
        match_count=value.match_count,
        teal_win_count=value.teal_win_count,
        orange_win_count=value.orange_win_count,
        win_count=value.win_count,
        loss_count=value.loss_count,
        unknown_count=value.unknown_count,
        surrender_count=value.surrender_count,
        duration_seconds=value.duration_seconds,
        game_modes=list(value.game_modes),
        stats_included=value.stats_included,
        bvid=value.bvid,
        publication_state=value.publication_state,
        description_state=value.description_state,
        pin_state=value.pin_state,
        chapter_state=value.chapter_state,
        publication_priority=value.publication_priority,
        publication_updated_at=value.publication_updated_at,
    )


def _zero_match_session(value: ZeroMatchSessionRecord) -> ZeroMatchSessionResponse:
    return ZeroMatchSessionResponse(
        session_id=value.session_id,
        title=value.title,
        source_title=value.source_title,
        anchor_name=value.anchor_name,
        started_at=value.started_at,
        completed_at=value.completed_at,
        recording_duration_seconds=value.recording_duration_seconds,
        part_count=value.part_count,
        bvid=value.bvid,
    )


def _hero(value: HeroRecord) -> HeroResponse:
    return HeroResponse(
        id=value.id,
        label=hero_chinese_name(value.label),
        fingerprint=value.fingerprint,
        thumbnail_url='/api/v1/vainglory/heroes/{}/thumbnail'.format(value.id),
    )


def _anchor_stats(value: AnchorStatsRecord) -> AnchorStatsResponse:
    return AnchorStatsResponse(**value.__dict__)


def _player_room(value: PlayerRoomRecord) -> PlayerRoomResponse:
    return PlayerRoomResponse(**value.__dict__)


def _stored_player(value: PlayerRecord) -> PlayerResponse:
    return PlayerResponse(
        id=value.id,
        name=value.name,
        origin=value.origin,
        rooms=[_player_room(room) for room in value.rooms],
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _game_mode_stats(value: GameModeStatsRecord) -> GameModeStatsResponse:
    return GameModeStatsResponse(**value.__dict__)


def _hero_stats(value: HeroStatsRecord) -> HeroStatsResponse:
    return HeroStatsResponse(
        hero_id=value.hero_id,
        hero_label=hero_chinese_name(value.hero_label),
        player_count=value.player_count,
        match_count=value.match_count,
        win_count=value.win_count,
        loss_count=value.loss_count,
        unknown_count=value.unknown_count,
        win_rate=value.win_rate,
    )


def _player_stats(value: PlayerStatsRecord) -> PlayerStatsResponse:
    return PlayerStatsResponse(
        player_id=value.player_id,
        player_name=value.player_name,
        rooms=[_player_room(room) for room in value.rooms],
        session_count=value.session_count,
        match_count=value.match_count,
        win_count=value.win_count,
        loss_count=value.loss_count,
        unknown_count=value.unknown_count,
        win_rate=value.win_rate,
        modes=[_game_mode_stats(mode) for mode in value.modes],
        heroes=[_hero_stats(hero) for hero in value.heroes],
    )


def _archive_sync(value: ArchiveSync) -> ArchiveSyncResponse:
    return ArchiveSyncResponse(**value.__dict__)


def _archive_backfill_item(value: ArchiveBackfillItem) -> ArchiveBackfillItemResponse:
    return ArchiveBackfillItemResponse(**value.__dict__)


def _archive_content_review(
    value: ArchiveContentReview,
) -> ArchiveContentReviewResponse:
    return ArchiveContentReviewResponse(**value.__dict__)


def _raise_repository_error(error: ValueError) -> None:
    if isinstance(error, VaingloryNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


router = APIRouter(prefix='/vainglory', tags=['vainglory'])


def _remote_media_path(part_id: int) -> str:
    expires_at = int(time.time()) + 12 * 60 * 60
    query = urlencode(
        {
            'media_token': security.media_access_token(part_id, expires_at),
            'media_expires': expires_at,
        }
    )
    return '/api/v1/recording-sessions/parts/{}/media?{}'.format(part_id, query)


@router.post('/worker/claim', response_model=None)
async def claim_analysis_work(
    _worker: str = Depends(security.authenticated_analysis_worker),
    index: VaingloryIndexService = Depends(get_service),
) -> Response:
    try:
        claim = await index.claim_remote_work()
    except VaingloryConflict as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')
    if claim is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    payload: Dict[str, Any] = {
        'kind': claim.kind,
        'itemId': claim.item_id,
        'sessionId': claim.session_id,
        'resultAtMs': claim.result_at_ms,
        'viewContext': claim.view_context,
        'partDurationSeconds': claim.part_duration_seconds,
        'recordingDurationSeconds': claim.recording_duration_seconds,
        'anchorName': claim.anchor_name,
    }
    if claim.part is not None:
        payload['part'] = {
            'id': claim.part.id,
            'index': claim.part.index,
            'title': claim.part.title,
            'manualCandidateTimesMs': list(claim.part.manual_candidate_times_ms),
            'mediaPath': _remote_media_path(claim.part.id),
        }
    if claim.frame_png:
        payload['framePng'] = base64.b64encode(claim.frame_png).decode('ascii')
    return JSONResponse(payload)


@router.post('/worker/heartbeat', status_code=status.HTTP_204_NO_CONTENT)
async def heartbeat_analysis_work(
    payload: AnalysisWorkerHeartbeatRequest,
    _worker: str = Depends(security.authenticated_analysis_worker),
    index: VaingloryIndexService = Depends(get_service),
) -> Response:
    runtime_status = None
    if payload.runtime_status is not None:
        try:
            runtime_status = AnalysisStatus(**payload.runtime_status)
        except (TypeError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Worker 运行状态无效：{}'.format(error),
            ) from None
    await index.heartbeat_remote_work(
        payload.kind, payload.item_id, payload.progress, runtime_status
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post('/worker/complete', status_code=status.HTTP_204_NO_CONTENT)
async def complete_analysis_work(
    payload: AnalysisWorkerCompleteRequest,
    _worker: str = Depends(security.authenticated_analysis_worker),
    index: VaingloryIndexService = Depends(get_service),
) -> Response:
    try:
        if payload.kind == 'part':
            try:
                training_candidates = decode_training_candidates(
                    payload.training_candidates[:60]
                )
            except (KeyError, TypeError, ValueError) as error:
                logger.warning(
                    'Ignored invalid worker training candidates: part_id={} '
                    'error={!r}',
                    payload.item_id,
                    error,
                )
                training_candidates = ()
            await index.complete_remote_part(
                payload.item_id,
                decode_matches(payload.matches),
                candidate_count=payload.candidate_count,
                training_candidates=training_candidates,
            )
        elif payload.kind == 'match_rerun':
            if len(payload.matches) != 1:
                raise VaingloryConflict('单局重新识别必须返回一场对局')
            await index.complete_remote_match_rerun(
                payload.item_id, decode_match(payload.matches[0])
            )
        elif payload.kind == 'hero_rematch':
            await index.complete_remote_hero_rematch(
                payload.item_id, tuple(decode_hero(hero) for hero in payload.heroes)
            )
        else:
            await index.complete_remote_recorded_player_backfill(
                payload.item_id, decode_recorded_player(payload.recorded_player)
            )
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Worker 返回结果无效：{}'.format(error),
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post('/worker/fail', status_code=status.HTTP_204_NO_CONTENT)
async def fail_analysis_work(
    payload: AnalysisWorkerFailureRequest,
    _worker: str = Depends(security.authenticated_analysis_worker),
    index: VaingloryIndexService = Depends(get_service),
) -> Response:
    await index.fail_remote_work(payload.kind, payload.item_id, payload.error)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    '/archive-syncs/{account_id}',
    response_model=ArchiveSyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_archive_sync(
    account_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    backfill: ArchiveBackfillService = Depends(get_archive_backfill),
) -> ArchiveSyncResponse:
    try:
        return _archive_sync(await backfill.request(account_id))
    except ArchiveBackfillNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from None
    except ArchiveBackfillUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None


@router.get('/archive-syncs/{account_id}', response_model=ArchiveSyncResponse)
async def get_archive_sync(
    account_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    backfill: ArchiveBackfillService = Depends(get_archive_backfill),
) -> ArchiveSyncResponse:
    try:
        return _archive_sync(await backfill.status(account_id))
    except ArchiveBackfillNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from None


@router.patch('/archive-syncs/{account_id}', response_model=ArchiveSyncResponse)
async def update_archive_sync(
    account_id: int,
    payload: ArchiveSyncControlRequest,
    _subject: str = Depends(authenticated_manager_subject),
    backfill: ArchiveBackfillService = Depends(get_archive_backfill),
) -> ArchiveSyncResponse:
    try:
        return _archive_sync(
            await backfill.update_control(
                account_id, paused=payload.paused, daily_limit=payload.daily_limit
            )
        )
    except ArchiveBackfillNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from None


@router.get(
    '/archive-syncs/{account_id}/items',
    response_model=List[ArchiveBackfillItemResponse],
)
async def list_archive_sync_items(
    account_id: int,
    limit: int = Query(30, ge=1, le=100),
    _subject: str = Depends(authenticated_manager_subject),
    backfill: ArchiveBackfillService = Depends(get_archive_backfill),
) -> List[ArchiveBackfillItemResponse]:
    try:
        return [
            _archive_backfill_item(item)
            for item in await backfill.list_items(account_id, limit=limit)
        ]
    except ArchiveBackfillNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from None


@router.get('/archive-content-reviews', response_model=ArchiveContentReviewListResponse)
async def list_archive_content_reviews(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _subject: str = Depends(authenticated_manager_subject),
    backfill: ArchiveBackfillService = Depends(get_archive_backfill),
) -> ArchiveContentReviewListResponse:
    page = await backfill.list_suspected_non_vainglory(limit=limit, offset=offset)
    return ArchiveContentReviewListResponse(
        total=page.total, items=[_archive_content_review(item) for item in page.items]
    )


@router.post(
    '/sessions/{session_id}/scan',
    response_model=ScanJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_scan(
    session_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> ScanJobResponse:
    try:
        return _scan_job(await index.request_scan(session_id))
    except (VaingloryConflict, VaingloryNotFound) as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')


@router.get('/sessions/{session_id}/scan', response_model=ScanJobResponse)
async def get_scan(
    session_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> ScanJobResponse:
    value = await index.get_job(session_id)
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='分析任务不存在'
        )
    return _scan_job(value)


@router.post(
    '/sessions/{session_id}/publication/{step}/retry',
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_publication_step(
    session_id: int,
    step: Literal['description', 'comments', 'pin', 'chapter'],
    _subject: str = Depends(authenticated_manager_subject),
    publication_service: VaingloryPublicationService = Depends(get_publication),
) -> Response:
    try:
        await publication_service.retry_failed_step(session_id, step)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    '/sessions/{session_id}/match-markers',
    response_model=ManualMatchMarkerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def mark_session_match(
    session_id: int,
    payload: ManualMatchMarkerRequest,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> ManualMatchMarkerResponse:
    try:
        marker = await index.mark_session_match(
            session_id, part_index=payload.part_index, at_ms=payload.at_ms
        )
        return ManualMatchMarkerResponse(**marker.__dict__)
    except (VaingloryConflict, VaingloryNotFound) as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error


@router.get('/matches', response_model=MatchListResponse)
async def list_matches(
    player_name: str = Query('', max_length=80, alias='playerName'),
    hero_id: List[int] = Query([], alias='heroId'),
    winner_color: Optional[Literal['teal', 'orange']] = Query(
        None, alias='winnerColor'
    ),
    end_reason: Optional[Literal['normal', 'surrender', 'unknown']] = Query(
        None, alias='endReason'
    ),
    game_mode: Optional[Literal['3v3', '5v5', 'aram', 'other', 'unknown']] = Query(
        None, alias='gameMode'
    ),
    session_id: Optional[int] = Query(None, ge=1, alias='sessionId'),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> MatchListResponse:
    if len(hero_id) > 6 or any(value < 1 for value in hero_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='英雄筛选最多 6 个，且编号必须为正数',
        )
    page = await index.list_matches(
        player_name=player_name,
        hero_ids=hero_id,
        winner_color=winner_color,
        end_reason=end_reason,
        game_mode=game_mode,
        session_id=session_id,
        limit=limit,
        offset=offset,
    )
    return MatchListResponse(
        total=page.total, items=[_match(item) for item in page.items]
    )


@router.get('/sessions', response_model=MatchSessionListResponse)
async def list_match_sessions(
    player_name: str = Query('', max_length=80, alias='playerName'),
    hero_id: List[int] = Query([], alias='heroId'),
    winner_color: Optional[Literal['teal', 'orange']] = Query(
        None, alias='winnerColor'
    ),
    end_reason: Optional[Literal['normal', 'surrender', 'unknown']] = Query(
        None, alias='endReason'
    ),
    game_mode: Optional[Literal['3v3', '5v5', 'aram', 'other', 'unknown']] = Query(
        None, alias='gameMode'
    ),
    session_id: Optional[int] = Query(None, ge=1, alias='sessionId'),
    source_title: str = Query('', max_length=200, alias='sourceTitle'),
    anchor_name: Optional[str] = Query(None, max_length=200, alias='anchorName'),
    stats_included: Optional[bool] = Query(None, alias='statsIncluded'),
    sort: Literal['analyzed', 'started'] = Query('analyzed'),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> MatchSessionListResponse:
    if len(hero_id) > 6 or any(value < 1 for value in hero_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='英雄筛选最多 6 个，且编号必须为正数',
        )
    page = await index.list_match_sessions(
        player_name=player_name,
        hero_ids=hero_id,
        winner_color=winner_color,
        end_reason=end_reason,
        game_mode=game_mode,
        session_id=session_id,
        source_title=source_title,
        anchor_name=anchor_name,
        stats_included=stats_included,
        sort_by=sort,
        limit=limit,
        offset=offset,
    )
    return MatchSessionListResponse(
        total=page.total, items=[_match_session(item) for item in page.items]
    )


@router.get('/zero-match-sessions', response_model=ZeroMatchSessionListResponse)
async def list_zero_match_sessions(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    suppressed: bool = Query(False),
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> ZeroMatchSessionListResponse:
    page = await index.list_zero_match_sessions(
        limit=limit, offset=offset, suppressed=suppressed
    )
    return ZeroMatchSessionListResponse(
        total=page.total, items=[_zero_match_session(item) for item in page.items]
    )


@router.put(
    '/sessions/{session_id}/scan-suppression', status_code=status.HTTP_204_NO_CONTENT
)
async def suppress_zero_match_session(
    session_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> Response:
    try:
        await index.suppress_zero_match_session(session_id)
    except (VaingloryConflict, VaingloryNotFound) as error:
        _raise_repository_error(error)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    '/sessions/{session_id}/scan-suppression', status_code=status.HTTP_204_NO_CONTENT
)
async def restore_zero_match_session(
    session_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> Response:
    try:
        await index.restore_zero_match_session(session_id)
    except (VaingloryConflict, VaingloryNotFound) as error:
        _raise_repository_error(error)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get('/stats/anchors', response_model=List[AnchorStatsResponse])
async def list_anchor_stats(
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> List[AnchorStatsResponse]:
    return [_anchor_stats(item) for item in await index.list_anchor_stats()]


@router.get('/players', response_model=List[PlayerResponse])
async def list_players(
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> List[PlayerResponse]:
    return [_stored_player(item) for item in await index.list_players()]


@router.post(
    '/players', response_model=PlayerResponse, status_code=status.HTTP_201_CREATED
)
async def create_player(
    payload: PlayerNameRequest,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> PlayerResponse:
    try:
        return _stored_player(await index.create_player(payload.name))
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error


@router.post('/players/sync-rooms', response_model=List[PlayerResponse])
async def sync_player_rooms(
    payload: PlayerRoomSyncRequest,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> List[PlayerResponse]:
    try:
        players = await index.ensure_players_for_rooms(
            tuple((room.room_id, room.name) for room in payload.rooms)
        )
        return [_stored_player(player) for player in players]
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error


@router.patch('/players/{player_id}', response_model=PlayerResponse)
async def rename_player(
    player_id: int,
    payload: PlayerNameRequest,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> PlayerResponse:
    try:
        return _stored_player(await index.rename_player(player_id, payload.name))
    except VaingloryNotFound as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error


@router.delete('/players/{player_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_player(
    player_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> Response:
    try:
        await index.delete_player(player_id)
    except VaingloryNotFound as error:
        _raise_repository_error(error)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put('/players/{player_id}/rooms/{room_id}', response_model=PlayerResponse)
async def bind_player_room(
    player_id: int,
    room_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> PlayerResponse:
    try:
        return _stored_player(await index.bind_player_room(player_id, room_id))
    except VaingloryNotFound as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error


@router.delete('/players/{player_id}/rooms/{room_id}', response_model=PlayerResponse)
async def unbind_player_room(
    player_id: int,
    room_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> PlayerResponse:
    try:
        return _stored_player(await index.unbind_player_room(player_id, room_id))
    except VaingloryNotFound as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error


@router.get('/stats/players', response_model=List[PlayerStatsResponse])
async def list_player_stats(
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> List[PlayerStatsResponse]:
    return [_player_stats(item) for item in await index.list_player_stats()]


@router.get('/stats/heroes', response_model=List[HeroStatsResponse])
async def list_hero_stats(
    game_mode: Literal['', '3v3', '5v5', 'aram', 'other', 'unknown'] = Query(
        '', alias='gameMode'
    ),
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> List[HeroStatsResponse]:
    return [
        _hero_stats(item) for item in await index.list_hero_stats(game_mode=game_mode)
    ]


@router.get('/recorded-player-reviews', response_model=MatchListResponse)
async def list_recorded_player_reviews(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> MatchListResponse:
    page = await index.list_recorded_player_reviews(limit=limit, offset=offset)
    return MatchListResponse(
        total=page.total, items=[_match(item) for item in page.items]
    )


@router.get('/hero-reviews', response_model=MatchListResponse)
async def list_hero_reviews(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> MatchListResponse:
    page = await index.list_hero_reviews(limit=limit, offset=offset)
    return MatchListResponse(
        total=page.total, items=[_match(item) for item in page.items]
    )


@router.patch('/matches/{match_id}/recorded-player', response_model=MatchResponse)
async def set_recorded_player(
    match_id: int,
    payload: RecordedPlayerRequest,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> MatchResponse:
    try:
        return _match(
            await index.set_recorded_player(
                match_id, side=payload.side, slot=payload.slot
            )
        )
    except (VaingloryConflict, VaingloryNotFound) as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error


@router.patch(
    '/matches/{match_id}/players/{side}/{slot}/hero', response_model=MatchResponse
)
async def set_player_hero(
    match_id: int,
    side: Literal['left', 'right'],
    slot: int,
    payload: PlayerHeroRequest,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> MatchResponse:
    try:
        return _match(
            await index.set_player_hero(
                match_id, side=side, slot=slot, hero_id=payload.hero_id
            )
        )
    except (VaingloryConflict, VaingloryNotFound) as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error


@router.patch('/matches/{match_id}', response_model=MatchResponse)
async def update_match(
    match_id: int,
    payload: MatchUpdateRequest,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> MatchResponse:
    try:
        changes = payload.dict(exclude_unset=True)
        if set(changes) == {'title'}:
            return _match(
                await index.update_match_title(match_id, str(changes['title'] or ''))
            )
        return _match(await index.update_match_fields(match_id, changes))
    except (VaingloryConflict, VaingloryNotFound) as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error


@router.post('/matches/{match_id}/reanalyze', status_code=status.HTTP_202_ACCEPTED)
async def reanalyze_match(
    match_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> Response:
    try:
        await index.request_match_rerun(match_id)
    except (VaingloryConflict, VaingloryNotFound) as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.put(
    '/matches/{match_id}/review-suppressions/{review_type}',
    status_code=status.HTTP_204_NO_CONTENT,
)
async def suppress_match_review(
    match_id: int,
    review_type: Literal['hero', 'recorded_player'],
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> Response:
    try:
        await index.suppress_match_review(match_id, review_type)
    except VaingloryNotFound as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete('/matches/{match_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_match(
    match_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> Response:
    try:
        await index.delete_match(match_id)
    except VaingloryNotFound as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch('/sessions/bulk-update', response_model=SessionBulkUpdateResponse)
async def bulk_update_sessions(
    payload: SessionBulkUpdateRequest,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> SessionBulkUpdateResponse:
    if payload.anchor_name is None and payload.stats_included is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='请至少选择一种批量修改操作',
        )
    try:
        updated = await index.bulk_update_sessions(
            payload.session_ids,
            anchor_name=payload.anchor_name,
            stats_included=payload.stats_included,
        )
    except VaingloryNotFound as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    return SessionBulkUpdateResponse(updated_count=updated)


@router.patch('/sessions/{session_id}', response_model=MatchSessionResponse)
async def update_session_title(
    session_id: int,
    payload: MatchTitleRequest,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> MatchSessionResponse:
    try:
        return _match_session(
            await index.update_session_title(session_id, payload.title)
        )
    except (VaingloryConflict, VaingloryNotFound) as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')


@router.patch('/sessions/{session_id}/anchor', response_model=MatchSessionResponse)
async def update_session_anchor(
    session_id: int,
    payload: SessionAnchorRequest,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> MatchSessionResponse:
    try:
        return _match_session(
            await index.update_session_anchor(session_id, payload.anchor_name)
        )
    except (VaingloryConflict, VaingloryNotFound) as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')


@router.get('/matches/{match_id}/result-frame')
async def match_result_frame(
    match_id: int,
    download: bool = Query(False),
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> FileResponse:
    path = await index.repository.result_frame_path(match_id)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail='该对局暂无结算画面'
        )
    disposition = 'attachment' if download else 'inline'
    return FileResponse(
        path=str(path),
        media_type='image/png',
        headers={
            'Cache-Control': 'private, no-store',
            'Content-Disposition': '{}; filename="vainglory-match-{}.png"'.format(
                disposition, match_id
            ),
        },
    )


@router.get('/heroes', response_model=List[HeroResponse])
async def list_heroes(
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> List[HeroResponse]:
    return [_hero(value) for value in await index.repository.list_heroes()]


@router.patch('/heroes/{hero_id}', response_model=HeroResponse)
async def label_hero(
    hero_id: int,
    payload: LabelHeroRequest,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> HeroResponse:
    try:
        return _hero(await index.repository.label_hero(hero_id, payload.label))
    except VaingloryNotFound as error:
        _raise_repository_error(error)
        raise AssertionError('unreachable')


@router.get('/heroes/{hero_id}/thumbnail')
async def hero_thumbnail(
    hero_id: int,
    _subject: str = Depends(authenticated_manager_subject),
    index: VaingloryIndexService = Depends(get_service),
) -> Response:
    value = await index.repository.hero_thumbnail(hero_id)
    if value is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='英雄不存在')
    return Response(
        content=value,
        media_type=('image/jpeg' if value.startswith(b'\xff\xd8') else 'image/png'),
        headers={'Cache-Control': 'private, max-age=86400'},
    )
