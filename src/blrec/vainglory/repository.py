from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    cast,
)

from loguru import logger

from blrec.bili_upload.database import BiliUploadDatabase

from .analyzer import (
    AnalyzedHero,
    AnalyzedMatch,
    ScannedPart,
    TrainingCandidate,
    VideoPart,
)
from .anchor_identity import infer_recorded_anchor
from .catalog import identify_builtin_hero
from .exclusions import EXCLUDED_TITLE_MARKER, is_excluded_title
from .hero_recognition import HeroReference
from .ocr import clean_player_name, normalize_player_name
from .title_time import current_season_started_at
from .vision import RecordedPlayer

# MediaLibrary uses this sentinel for an external import without a source room.
_EXTERNAL_IMPORT_ROOM_ID = 2_147_483_647


class VaingloryNotFound(ValueError):
    pass


class VaingloryConflict(ValueError):
    pass


def _analysis_revision_snapshot(matches: Sequence[AnalyzedMatch]) -> Tuple[str, str]:
    payload = {
        'matches': [
            {
                'part_id': int(match.part_id),
                'part_index': int(match.part_index),
                'result_at_ms': int(match.result_at_ms),
                'layout': {
                    'left_color': match.layout.left_color,
                    'right_color': match.layout.right_color,
                    'winner_color': match.layout.winner_color,
                    'winner_side': match.layout.winner_side,
                    'team_size': match.layout.team_size,
                },
                'header': {
                    'result_text': match.ocr.header.result_text,
                    'end_reason': match.ocr.header.end_reason,
                    'duration_seconds': match.ocr.header.duration_seconds,
                    'left_kills': match.ocr.header.left_kills,
                    'right_kills': match.ocr.header.right_kills,
                    'left_economy': match.ocr.header.left_economy,
                    'right_economy': match.ocr.header.right_economy,
                },
                'players': [
                    {
                        'side': player.side,
                        'slot': int(player.slot),
                        'name': player.name,
                        'normalized_name': player.normalized_name,
                        'raw_name': player.raw_name,
                        'kills': player.stats.kills,
                        'deaths': player.stats.deaths,
                        'assists': player.stats.assists,
                        'economy': player.stats.economy,
                        'last_hits': player.stats.last_hits,
                        'confidence': float(player.confidence),
                    }
                    for player in match.ocr.players
                ],
                'heroes': [
                    {
                        'side': hero.side,
                        'slot': int(hero.slot),
                        'fingerprint': hero.fingerprint,
                        'label': hero.label,
                        'confidence': float(hero.confidence),
                        'thumbnail_sha256': hashlib.sha256(
                            hero.thumbnail_png
                        ).hexdigest(),
                    }
                    for hero in match.heroes
                ],
                'confidence': float(match.confidence),
                'game_mode': match.game_mode,
                'recorded_player': (
                    None
                    if match.recorded_player is None
                    else {
                        'side': match.recorded_player.side,
                        'slot': int(match.recorded_player.slot),
                        'confidence': float(match.recorded_player.confidence),
                    }
                ),
                'match_kind': match.match_kind,
                'view_context': match.view_context,
                'stats_eligible': bool(match.stats_eligible),
                'stats_exclusion_reason': match.stats_exclusion_reason,
                'result_frame_sha256': (
                    None
                    if not match.result_frame_png
                    else hashlib.sha256(match.result_frame_png).hexdigest()
                ),
            }
            for match in sorted(
                matches,
                key=lambda item: (item.part_index, item.result_at_ms, item.part_id),
            )
        ],
        'version': 1,
    }
    snapshot_json = json.dumps(
        payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True
    )
    return snapshot_json, hashlib.sha256(snapshot_json.encode('utf8')).hexdigest()


def _analysis_summary_json(value: Optional[Mapping[str, Any]]) -> Optional[str]:
    if value is None:
        return None
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(',', ':'), sort_keys=True
    )
    if len(encoded.encode('utf8')) > 100_000:
        raise ValueError('analysis summary is too large')
    return encoded


def _decode_analysis_summary(value: object) -> Optional[Dict[str, Any]]:
    if value in (None, ''):
        return None
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _unified_training_suggestions(
    candidates: Sequence[TrainingCandidate],
) -> Dict[str, Dict[str, Any]]:
    """把旧模型的多路输出合成一张图的四项预标。"""
    suggestions: Dict[str, Dict[str, Any]] = {}

    def remember(
        task: str,
        label: str,
        candidate: TrainingCandidate,
        *,
        confidence: Optional[float] = None,
    ) -> None:
        selected_confidence = float(
            candidate.suggestion_confidence if confidence is None else confidence
        )
        previous = suggestions.get(task)
        if (
            previous is not None
            and float(previous['confidence']) >= selected_confidence
        ):
            return
        suggestions[task] = {
            'label': label,
            'confidence': selected_confidence,
            'model_version': candidate.model_version,
            'reason': candidate.selection_reason,
        }

    for candidate in candidates:
        label = candidate.suggested_label
        if candidate.task in {'match_flow', 'hero_select', 'match_mode'}:
            remember(candidate.task, str(label), candidate)
        elif candidate.task == 'screen_state':
            flow = (
                'match_flow'
                if label in ('in_match', 'talent_select', 'post_match')
                else 'unreadable' if label == 'transition' else 'not_match_flow'
            )
            remember('match_flow', flow, candidate)
            if label in ('in_match', 'talent_select') and candidate.mode_class in (
                '3v3',
                'aram',
                '5v5',
            ):
                remember(
                    'match_mode',
                    candidate.mode_class,
                    candidate,
                    confidence=candidate.mode_confidence,
                )
        elif candidate.task == 'bp_review':
            select = {
                'bp_3v3': 'select_3v3',
                'bp_aram': 'select_aram',
                'bp_5v5': 'select_5v5',
                'not_bp': 'not_select',
            }[label]
            remember('hero_select', select, candidate)
            if label != 'not_bp':
                remember('match_flow', 'not_match_flow', candidate)
                remember('result_panel', 'no_result_panel', candidate)
        elif candidate.task == 'key_screen_review':
            if label == 'result_page':
                remember('match_flow', 'match_flow', candidate)
                remember('hero_select', 'not_select', candidate)
                remember('result_panel', 'result_panel', candidate)
            elif label == 'scoreboard':
                remember('match_flow', 'match_flow', candidate)
                remember('match_mode', 'unreadable', candidate)
                remember('hero_select', 'not_select', candidate)
                remember('result_panel', 'no_result_panel', candidate)
            else:
                remember('result_panel', 'no_result_panel', candidate)
        elif candidate.task == 'result_detector':
            remember('result_panel', str(label), candidate)
        elif candidate.task == 'mode_gate' and candidate.mode_class in (
            '3v3',
            'aram',
            '5v5',
        ):
            remember('match_flow', 'match_flow', candidate)
            remember('match_mode', candidate.mode_class, candidate)
            remember('hero_select', 'not_select', candidate)
            remember('result_panel', 'no_result_panel', candidate)
    return suggestions


def _training_candidate_groups(
    candidates: Sequence[TrainingCandidate],
) -> Tuple[Tuple[TrainingCandidate, ...], ...]:
    selected = tuple(candidates[:80])
    parent = list(range(len(selected)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    digests = [hashlib.sha256(item.image_jpeg).digest() for item in selected]
    for left, candidate in enumerate(selected):
        for right in range(left):
            previous = selected[right]
            same_image = digests[left] == digests[right]
            same_result_event = (
                _is_result_training_candidate(candidate)
                and _is_result_training_candidate(previous)
                and (
                    candidate.segment_start_ms == previous.segment_start_ms
                    or abs(candidate.at_ms - previous.at_ms) <= 60_000
                )
            )
            if same_image or same_result_event:
                union(right, left)
    groups: Dict[int, List[TrainingCandidate]] = {}
    for index, candidate in enumerate(selected):
        groups.setdefault(find(index), []).append(candidate)
    return tuple(tuple(group) for group in groups.values())


def _is_result_training_candidate(candidate: TrainingCandidate) -> bool:
    return (
        (
            candidate.task == 'result_detector'
            and candidate.suggested_label == 'result_panel'
        )
        or (
            candidate.task == 'key_screen_review'
            and candidate.suggested_label == 'result_page'
        )
        or (
            candidate.task == 'screen_state'
            and candidate.suggested_label == 'post_match'
        )
    )


@dataclass(frozen=True)
class ScanJob:
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
    part_count: int = 0
    original_part_count: int = 0
    ignored_part_count: int = 0
    ignored_part_reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanClaim:
    session_id: int
    part: VideoPart
    realtime: bool
    part_duration_seconds: Optional[int] = None
    recording_duration_seconds: int = 0
    anchor_name: str = ''

    @property
    def parts(self) -> Tuple[VideoPart, ...]:
        return (self.part,)


@dataclass(frozen=True)
class LiveAnalysisClaim:
    kind: Literal['coarse', 'fine']
    item_id: int
    session_id: int
    part: VideoPart
    lease_owner: str
    lease_generation: int
    window_start_ms: Optional[int] = None
    window_end_ms: Optional[int] = None
    window_focus_ms: Optional[int] = None
    mode: str = 'unknown'


@dataclass(frozen=True)
class LiveFrameObservation:
    observed_at_ms: int
    stage: int
    stage_confidence: float
    match_flow_label: str
    match_flow_confidence: float
    hero_select_label: str
    hero_select_confidence: float
    match_mode_label: str
    match_mode_confidence: float
    result_confidence: float
    hero_lineup: Tuple[str, ...] = ()


@dataclass(frozen=True)
class OcrClaim:
    session_id: int
    part: VideoPart
    scanned: ScannedPart
    analysis_started_at: Optional[int] = None
    part_duration_seconds: Optional[int] = None
    recording_duration_seconds: int = 0


@dataclass(frozen=True)
class AnalysisQueueEvent:
    at: int
    stage: str
    detail: str
    elapsed_seconds: float


@dataclass(frozen=True)
class AnalysisMatchPreview:
    match_id: int
    session_id: int
    part_id: int
    part_index: int
    result_at_ms: int
    title: str


@dataclass(frozen=True)
class AnalysisQueueCompletion:
    completed_at: int
    session_id: int
    part_id: int
    part_index: int
    title: str
    part_duration_seconds: Optional[int]
    recording_duration_seconds: int
    part_match_duration_seconds: int
    session_match_duration_seconds: int
    candidate_count: Optional[int]
    match_count: int
    elapsed_seconds: float
    part_count: int = 0
    original_part_count: int = 0
    ignored_part_count: int = 0
    bvid: Optional[str] = None
    archive_page: Optional[int] = None
    local_video_available: bool = False
    image_count: int = 0
    match_previews: Tuple[AnalysisMatchPreview, ...] = ()
    analysis_summary: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class AnalysisQueueItem:
    part_id: int
    session_id: int
    part_index: int
    title: str
    anchor_name: str
    state: str
    stage: str
    category: str
    progress: float
    requested_at: int
    started_at: Optional[int]
    updated_at: int
    live_started_at: int
    part_duration_seconds: Optional[int]
    recording_duration_seconds: int
    match_count: int
    part_count: int
    completed_part_count: int
    original_part_count: int = 0
    ignored_part_count: int = 0
    runtime_stage: str = ''
    runtime_detail: str = ''
    runtime_elapsed_seconds: float = 0
    coarse_frames: int = 0
    gameplay_runs: int = 0
    result_windows: int = 0
    current_window: int = 0
    total_windows: int = 0
    candidate_count: int = 0
    current_candidate: int = 0
    total_candidates: int = 0
    rejected_candidates: int = 0
    recognized_matches: int = 0
    model_package_id: str = ''
    keyframe_frames: int = 0
    seek_fill_frames: int = 0
    decoded_result_frames: int = 0
    mode_conflict_count: int = 0
    hud_lineup_candidate_count: int = 0
    training_candidate_count: int = 0
    events: Tuple[AnalysisQueueEvent, ...] = ()
    bvid: Optional[str] = None
    archive_page: Optional[int] = None
    local_video_available: bool = False
    image_count: int = 0
    match_previews: Tuple[AnalysisMatchPreview, ...] = ()


@dataclass(frozen=True)
class LiveAnalysisStatusItem:
    part_id: int
    session_id: int
    part_index: int
    title: str
    anchor_name: str
    room_id: int
    live_started_at: int
    recording_duration_seconds: int
    last_observed_at_ms: Optional[int]
    sample_count: int
    fine_scan_count: int
    last_sample_at: Optional[int]
    next_sample_at: int
    match_flow_label: str
    match_flow_confidence: float
    worker_id: str
    pending_window_count: int
    running_window_count: int
    completed_window_count: int
    failed_window_count: int
    provisional_match_count: int
    last_error: str


@dataclass(frozen=True)
class AnalysisQueueStatus:
    active: Tuple[AnalysisQueueItem, ...]
    queued: Tuple[AnalysisQueueItem, ...]
    pending_count: int
    manual_pending: int
    realtime_pending: int
    archive_pending: int
    migration_pending: int
    backlog_pending: int
    recent_completions: Tuple[AnalysisQueueCompletion, ...] = ()
    live_stream_count: int = 0
    live_running_count: int = 0
    live_pending_window_count: int = 0
    live_sample_count: int = 0
    live_provisional_match_count: int = 0
    live_last_observed_at: Optional[int] = None
    live_items: Tuple[LiveAnalysisStatusItem, ...] = ()


@dataclass(frozen=True)
class IndexSummary:
    match_count: int
    session_count: int
    anchor_count: int
    unassigned_session_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    player_slot_count: int
    recognized_hero_count: int


@dataclass(frozen=True)
class AnalysisWorkerRecord:
    worker_id: str
    display_name: str
    enabled: bool
    model_package_id: str
    pipeline_version: str
    concurrency: int
    first_seen_at: Optional[int]
    last_seen_at: Optional[int]
    completed_task_count: int
    failed_task_count: int
    total_processing_seconds: float
    profiled_task_count: int
    profiled_video_seconds: float
    total_decode_analysis_seconds: float
    total_profiled_task_seconds: float
    last_task_finished_at: Optional[int]
    desired_concurrency: Optional[int] = None


@dataclass(frozen=True)
class HeroRematchClaim:
    match_id: int


@dataclass(frozen=True)
class RecordedPlayerBackfillClaim:
    match_id: int


@dataclass(frozen=True)
class MatchRerunClaim:
    match_id: int
    session_id: int
    part: VideoPart
    result_at_ms: int
    view_context: Literal['played', 'observed', 'unknown']


@dataclass(frozen=True)
class ManualMatchMarkerRecord:
    id: int
    session_id: int
    part_id: int
    part_index: int
    at_ms: int


@dataclass(frozen=True)
class MatchPlayerRecord:
    side: str
    slot: int
    name: str
    normalized_name: str
    hero_id: Optional[int]
    hero_label: str
    hero_source: Literal['automatic', 'manual']
    kills: Optional[int]
    deaths: Optional[int]
    assists: Optional[int]
    economy: Optional[int]
    confidence: float
    last_hits: Optional[int] = None
    is_recorded_player: bool = False


@dataclass(frozen=True)
class MatchRecord:
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
    has_result_frame: bool
    recorded_player_confidence: Optional[float]
    recorded_player_source: str
    players: Tuple[MatchPlayerRecord, ...]
    match_kind: Literal['pvp', 'bot', 'practice', 'unknown'] = 'unknown'
    view_context: Literal['played', 'observed', 'unknown'] = 'unknown'
    stats_eligible: bool = True
    stats_exclusion_reason: Optional[str] = None
    recorded_player_state: str = 'pending'
    rerun_state: Optional[str] = None
    rerun_error: Optional[str] = None
    previous_archive_page: Optional[int] = None
    previous_archive_duration_seconds: Optional[int] = None
    previous_archive_segments: Tuple[Tuple[int, int], ...] = ()
    analysis_state: Literal['provisional', 'final'] = 'final'


@dataclass(frozen=True)
class MatchPage:
    total: int
    items: Tuple[MatchRecord, ...]


@dataclass(frozen=True)
class MatchSessionRecord:
    session_id: int
    title: str
    started_at: int
    match_count: int
    teal_win_count: int
    orange_win_count: int
    surrender_count: int
    duration_seconds: int
    game_modes: Tuple[str, ...]
    source_title: str = ''
    anchor_name: str = ''
    live_started_at: int = 0
    part_count: int = 0
    original_part_count: int = 0
    ignored_part_count: int = 0
    recording_duration_seconds: int = 0
    win_count: int = 0
    loss_count: int = 0
    unknown_count: int = 0
    stats_included: bool = True
    bvid: Optional[str] = None
    publication_state: Optional[str] = None
    description_state: Optional[str] = None
    pin_state: Optional[str] = None
    chapter_state: Optional[str] = None
    publication_priority: bool = False
    publication_updated_at: Optional[int] = None


@dataclass(frozen=True)
class MatchSessionPage:
    total: int
    items: Tuple[MatchSessionRecord, ...]


@dataclass(frozen=True)
class ZeroMatchSessionRecord:
    session_id: int
    title: str
    source_title: str
    anchor_name: str
    started_at: int
    completed_at: int
    recording_duration_seconds: int
    part_count: int
    bvid: Optional[str] = None


@dataclass(frozen=True)
class ZeroMatchSessionPage:
    total: int
    items: Tuple[ZeroMatchSessionRecord, ...]


@dataclass(frozen=True)
class HeroRecord:
    id: int
    label: str
    fingerprint: str


@dataclass(frozen=True)
class AnchorStatsRecord:
    anchor_uid: Optional[int]
    anchor_name: str
    room_id: int
    session_count: int
    match_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    win_rate: float


@dataclass(frozen=True)
class PlayerRoomRecord:
    room_id: int
    anchor_uid: Optional[int]
    anchor_name: str


@dataclass(frozen=True)
class PlayerRecord:
    id: int
    name: str
    origin: Literal['automatic', 'manual']
    rooms: Tuple[PlayerRoomRecord, ...]
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class GameModeStatsRecord:
    game_mode: str
    match_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    win_rate: float


@dataclass(frozen=True)
class HeroStatsRecord:
    hero_id: int
    hero_label: str
    player_count: int
    match_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    win_rate: float


@dataclass(frozen=True)
class PlayerStatsRecord:
    player_id: int
    player_name: str
    rooms: Tuple[PlayerRoomRecord, ...]
    session_count: int
    match_count: int
    win_count: int
    loss_count: int
    unknown_count: int
    win_rate: float
    modes: Tuple[GameModeStatsRecord, ...]
    heroes: Tuple[HeroStatsRecord, ...]


@dataclass
class _AnchorStatsAccumulator:
    anchor_uid: Optional[int]
    anchor_name: str
    room_id: int
    session_ids: Set[int]
    match_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    unknown_count: int = 0


@dataclass
class _OutcomeAccumulator:
    match_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    unknown_count: int = 0

    def add(self, winner_color: str) -> None:
        self.match_count += 1
        if winner_color == 'teal':
            self.win_count += 1
        elif winner_color == 'orange':
            self.loss_count += 1
        else:
            self.unknown_count += 1

    @property
    def win_rate(self) -> float:
        return 0.0 if self.match_count == 0 else self.win_count / self.match_count


@dataclass
class _PlayerStatsAccumulator:
    player: PlayerRecord
    session_ids: Set[int] = field(default_factory=set)
    outcomes: _OutcomeAccumulator = field(default_factory=_OutcomeAccumulator)
    modes: Dict[str, _OutcomeAccumulator] = field(default_factory=dict)
    heroes: Dict[int, Tuple[str, _OutcomeAccumulator]] = field(default_factory=dict)


def refresh_session_scan_job(
    connection: sqlite3.Connection, session_id: int, now: int
) -> None:
    summary = connection.execute(
        'SELECT COUNT(*) AS part_count,'
        "SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending_count,"
        "SUM(CASE WHEN state='analyzing' THEN 1 ELSE 0 END) AS analyzing_count,"
        "SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed_count,"
        'SUM(CASE WHEN ignored_reason IS NOT NULL THEN 1 ELSE 0 END) '
        'AS ignored_count,'
        'AVG(progress) AS progress,SUM(match_count) AS match_count,'
        'MIN(requested_at) AS requested_at,MIN(started_at) AS started_at,'
        'MAX(algorithm_version) AS algorithm_version '
        'FROM vainglory_part_jobs WHERE session_id=?',
        (session_id,),
    ).fetchone()
    if summary is None or int(summary['part_count']) == 0:
        return
    pending_count = int(summary['pending_count'])
    analyzing_count = int(summary['analyzing_count'])
    failed_count = int(summary['failed_count'])
    part_count = int(summary['part_count'])
    ignored_count = int(summary['ignored_count'] or 0)
    analyzable_count = part_count - ignored_count
    archive = connection.execute(
        'SELECT COALESCE(MAX(imported.page_count),0) AS expected_count,'
        'COUNT(part.id) AS materialized_count,'
        "SUM(CASE WHEN part.state='ready' THEN 1 ELSE 0 END) AS ready_count,"
        "SUM(CASE WHEN part.state='failed' THEN 1 ELSE 0 END) AS failed_count,"
        'COALESCE(SUM(part.progress),0) AS progress_sum '
        'FROM vainglory_archive_imports imported '
        'LEFT JOIN vainglory_archive_parts part ON part.import_id=imported.id '
        'WHERE imported.session_id=?',
        (session_id,),
    ).fetchone()
    expected_count = int(archive['expected_count'])
    materialized_count = int(archive['materialized_count'])
    archive_ready_count = int(archive['ready_count'] or 0)
    archive_failed_count = int(archive['failed_count'] or 0)
    archive_terminal_count = archive_ready_count + archive_failed_count
    archive_incomplete = expected_count > 0 and (
        materialized_count < expected_count
        or archive_terminal_count < expected_count
        or int(summary['part_count']) < expected_count
    )
    archive_failed = (
        expected_count > 0 and not archive_incomplete and archive_failed_count > 0
    )
    error: Optional[str] = None
    completed_at: Optional[int] = None
    if analyzable_count == 0:
        state = 'failed'
        progress = 1.0
        error = '没有可分析的视频分 P（已忽略 {} 个损坏分 P）'.format(ignored_count)
        completed_at = now
    elif archive_incomplete:
        has_progress = (
            archive_terminal_count > 0
            or float(archive['progress_sum'] or 0) > 0
            or analyzing_count > 0
            or summary['started_at'] is not None
        )
        state = 'analyzing' if has_progress else 'pending'
        progress = min(0.99, float(archive['progress_sum'] or 0) / expected_count)
    elif archive_failed:
        state = 'failed'
        progress = 1.0
        error_row = connection.execute(
            'SELECT part.error AS error FROM vainglory_archive_parts part '
            'JOIN vainglory_archive_imports imported ON imported.id=part.import_id '
            "WHERE imported.session_id=? AND part.state='failed' "
            'ORDER BY part.updated_at DESC,part.id DESC LIMIT 1',
            (session_id,),
        ).fetchone()
        error = (
            '部分分 P 分析失败'
            if error_row is None or error_row['error'] is None
            else str(error_row['error'])
        )
        completed_at = now
    elif analyzing_count:
        state = 'analyzing'
        progress = float(summary['progress'] or 0)
    elif pending_count:
        state = 'pending'
        progress = float(summary['progress'] or 0)
    elif failed_count:
        state = 'failed'
        progress = float(summary['progress'] or 0)
        error_row = connection.execute(
            'SELECT error FROM vainglory_part_jobs '
            "WHERE session_id=? AND state='failed' "
            'ORDER BY updated_at DESC,part_id DESC LIMIT 1',
            (session_id,),
        ).fetchone()
        error = (
            '部分分 P 分析失败'
            if error_row is None or error_row['error'] is None
            else str(error_row['error'])
        )
        completed_at = now
    else:
        state = 'ready'
        progress = float(summary['progress'] or 0)
        completed_at = now
    started_at = None if summary['started_at'] is None else int(summary['started_at'])
    if state == 'pending':
        started_at = None
    connection.execute(
        'UPDATE vainglory_scan_jobs SET state=?,progress=?,'
        'algorithm_version=?,match_count=?,error=?,requested_at=?,'
        'started_at=?,completed_at=?,updated_at=? WHERE session_id=?',
        (
            state,
            progress,
            int(summary['algorithm_version']),
            int(summary['match_count'] or 0),
            error,
            int(summary['requested_at']),
            started_at,
            completed_at,
            now,
            session_id,
        ),
    )


def _preferred_part_path(source_path: object, final_path: object) -> str:
    for path in (final_path, source_path):
        if path is not None and os.path.isfile(str(path)):
            return str(path)
    return str(final_path if final_path is not None else source_path)


def _definitely_unusable_part_reason(
    source_path: object, final_path: object, media_index_state: object
) -> Optional[str]:
    path = _preferred_part_path(source_path, final_path)
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size == 0:
        return '视频文件为空（0 B），没有可分析的音视频内容'
    if size < 1_024 and str(media_index_state or '') == 'failed':
        return '视频文件损坏（仅 {} B 且索引失败），没有可分析的音视频内容'.format(size)
    return None


def _analysis_worker_record(row: sqlite3.Row) -> AnalysisWorkerRecord:
    return AnalysisWorkerRecord(
        worker_id=str(row['worker_id']),
        display_name=str(row['display_name'] or ''),
        enabled=bool(row['enabled']),
        model_package_id=str(row['model_package_id'] or ''),
        pipeline_version=str(row['pipeline_version'] or ''),
        concurrency=int(row['concurrency'] or 0),
        first_seen_at=(
            None if row['first_seen_at'] is None else int(row['first_seen_at'])
        ),
        last_seen_at=(
            None if row['last_seen_at'] is None else int(row['last_seen_at'])
        ),
        completed_task_count=int(row['completed_task_count'] or 0),
        failed_task_count=int(row['failed_task_count'] or 0),
        total_processing_seconds=float(row['total_processing_seconds'] or 0),
        profiled_task_count=int(row['profiled_task_count'] or 0),
        profiled_video_seconds=float(row['profiled_video_seconds'] or 0),
        total_decode_analysis_seconds=float(row['total_decode_analysis_seconds'] or 0),
        total_profiled_task_seconds=float(row['total_profiled_task_seconds'] or 0),
        last_task_finished_at=(
            None
            if row['last_task_finished_at'] is None
            else int(row['last_task_finished_at'])
        ),
        desired_concurrency=(
            None
            if row['desired_concurrency'] is None
            else int(row['desired_concurrency'])
        ),
    )


class VaingloryRepository:
    ALGORITHM_VERSION = 18
    HERO_RECOGNITION_VERSION = 5
    RECORDED_PLAYER_DETECTION_VERSION = 3
    _REALTIME_WINDOW_SECONDS = 48 * 60 * 60
    _PUBLICATION_ANALYSIS_DEBT = (
        '(EXISTS(SELECT 1 FROM vainglory_publications publication '
        'WHERE publication.session_id=job.session_id '
        'AND publication.needs_refresh=1) OR EXISTS('
        'SELECT 1 FROM upload_jobs published_job '
        'JOIN bili_accounts published_account '
        'ON published_account.id=published_job.account_id '
        'WHERE published_job.session_id=job.session_id '
        "AND published_account.state='active' "
        "AND published_job.state IN ('waiting_review','approved','completed') "
        "AND published_job.submit_state='confirmed' "
        'AND COALESCE(published_job.aid,0)>0 '
        "AND COALESCE(published_job.bvid,'')<>'' "
        'AND EXISTS(SELECT 1 FROM upload_parts published_part '
        'WHERE published_part.job_id=published_job.id '
        'AND published_part.cid IS NOT NULL) '
        'AND NOT EXISTS(SELECT 1 FROM vainglory_publications existing '
        'WHERE existing.account_id=published_job.account_id '
        'AND existing.bvid=published_job.bvid)))'
    )
    _MATCH_SELECT = (
        'SELECT match.id,match.session_id,match.result_part_id,'
        'match.result_at_ms,match.duration_seconds,match.result_text,'
        'match.end_reason,match.left_color,match.right_color,match.winner_side,'
        'match.left_kills,match.right_kills,match.left_economy,'
        'match.right_economy,match.confidence,match.game_mode,match.team_size,'
        'match.match_kind,match.view_context,match.stats_eligible,'
        'match.stats_exclusion_reason,match.analysis_state,'
        'match.started_at_ms,match.custom_title,'
        'match.result_frame_path,match.recorded_player_confidence,'
        'match.recorded_player_source,match.recorded_player_detection_version,'
        '(SELECT rerun.state FROM vainglory_match_rerun_jobs rerun '
        'WHERE rerun.match_id=match.id) AS rerun_state,'
        '(SELECT rerun.error FROM vainglory_match_rerun_jobs rerun '
        'WHERE rerun.match_id=match.id) AS rerun_error,'
        'session.title AS session_title,'
        'session.started_at AS session_started_at,'
        'part.part_index AS part_index,'
        'COALESCE(job.account_id,CASE WHEN NOT EXISTS('
        'SELECT 1 FROM archive_migration_items source_migration '
        'WHERE source_migration.session_id=session.id) '
        'THEN video_source.account_id END,'
        'archive_import.account_id) AS account_id,'
        'COALESCE(job.bvid,CASE WHEN NOT EXISTS('
        'SELECT 1 FROM archive_migration_items source_migration '
        'WHERE source_migration.session_id=session.id) '
        'THEN video_source.bvid END,archive_import.bvid) AS bvid,'
        'job.policy_snapshot_json AS upload_title_source,'
        'CASE WHEN job.bvid IS NOT NULL AND job.bvid<>\'\' THEN ('
        'SELECT COUNT(*) FROM upload_parts remote_part '
        'WHERE remote_part.job_id=job.id AND remote_part.cid IS NOT NULL '
        'AND remote_part.part_index<=part.part_index) '
        'ELSE COALESCE(CASE WHEN NOT EXISTS('
        'SELECT 1 FROM archive_migration_items source_migration '
        'WHERE source_migration.session_id=session.id) '
        'THEN video_source.page END,archive_part.page,part.part_index) '
        'END AS archive_page,'
        'CASE WHEN job.bvid IS NOT NULL AND job.bvid<>\'\' THEN ('
        'SELECT COUNT(*) FROM upload_parts previous_remote '
        'WHERE previous_remote.job_id=job.id '
        'AND previous_remote.cid IS NOT NULL '
        'AND previous_remote.part_index<part.part_index) '
        'ELSE CASE WHEN archive_part.page>1 '
        'THEN archive_part.page-1 END END AS previous_archive_page,'
        'CASE WHEN job.bvid IS NOT NULL AND job.bvid<>\'\' THEN ('
        'SELECT previous_part.record_duration_seconds '
        'FROM recording_parts previous_part '
        'JOIN upload_parts previous_remote '
        'ON previous_remote.job_id=job.id '
        'AND previous_remote.part_index=previous_part.part_index '
        'WHERE previous_part.session_id=match.session_id '
        'AND previous_remote.cid IS NOT NULL '
        'AND previous_part.part_index<part.part_index '
        'ORDER BY previous_part.part_index DESC LIMIT 1) '
        'ELSE (SELECT previous_archive.duration_seconds '
        'FROM vainglory_archive_parts previous_archive '
        'WHERE previous_archive.import_id=archive_import.id '
        'AND previous_archive.page<archive_part.page '
        'ORDER BY previous_archive.page DESC LIMIT 1) '
        'END AS previous_archive_duration_seconds,'
        'CASE WHEN job.bvid IS NOT NULL AND job.bvid<>\'\' THEN ('
        'SELECT GROUP_CONCAT(('
        'SELECT COUNT(*) FROM upload_parts counted_remote '
        'WHERE counted_remote.job_id=job.id '
        'AND counted_remote.cid IS NOT NULL '
        'AND counted_remote.part_index<=previous_remote.part_index'
        ") || ':' || previous_part.record_duration_seconds, ',') "
        'FROM upload_parts previous_remote '
        'JOIN recording_parts previous_part '
        'ON previous_part.session_id=match.session_id '
        'AND previous_part.part_index=previous_remote.part_index '
        'WHERE previous_remote.job_id=job.id '
        'AND previous_remote.cid IS NOT NULL '
        'AND previous_remote.part_index<part.part_index '
        'AND previous_part.record_duration_seconds>0) '
        'ELSE (SELECT GROUP_CONCAT('
        "previous_archive.page || ':' || previous_archive.duration_seconds, ',') "
        'FROM vainglory_archive_parts previous_archive '
        'WHERE previous_archive.import_id=archive_import.id '
        'AND previous_archive.page<archive_part.page '
        'AND previous_archive.duration_seconds>0) '
        'END AS previous_archive_segments '
        'FROM vainglory_matches match '
        'JOIN recording_sessions session ON session.id=match.session_id '
        'JOIN recording_parts part ON part.id=match.result_part_id '
        'LEFT JOIN upload_jobs job ON job.session_id=match.session_id '
        'LEFT JOIN vainglory_video_sources video_source '
        'ON video_source.part_id=part.id '
        'LEFT JOIN vainglory_archive_parts archive_part '
        'ON archive_part.recording_part_id=part.id '
        'LEFT JOIN vainglory_archive_imports archive_import '
        'ON archive_import.id=archive_part.import_id '
    )

    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        result_frame_root: Optional[Path] = None,
        training_candidate_root: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._result_frame_root = (
            Path(database.path).parent / 'vainglory-result-frames'
            if result_frame_root is None
            else Path(result_frame_root)
        ).resolve()
        self._training_candidate_root = (
            Path(database.path).parent / 'vainglory-training-candidates'
            if training_candidate_root is None
            else Path(training_candidate_root)
        ).resolve()
        self._clock = clock

    async def recover_interrupted(self) -> int:
        now = self._now()

        def recover(connection: sqlite3.Connection) -> int:
            recovered_matches = connection.execute(
                "UPDATE vainglory_match_rerun_jobs SET state='pending',"
                'started_at=NULL,error=NULL,updated_at=? WHERE state=\'running\'',
                (now,),
            ).rowcount
            recovered_ocr = connection.execute(
                "UPDATE vainglory_ocr_jobs SET state='pending',started_at=NULL,"
                'updated_at=? WHERE state=\'running\'',
                (now,),
            ).rowcount
            cursor = connection.execute(
                "UPDATE vainglory_part_jobs SET state='pending',progress=0,"
                "error=NULL,started_at=NULL,completed_at=NULL,updated_at=? "
                "WHERE state='analyzing' AND NOT EXISTS("
                'SELECT 1 FROM vainglory_ocr_jobs ocr '
                'WHERE ocr.part_id=vainglory_part_jobs.part_id)',
                (now,),
            )
            connection.execute(
                'UPDATE vainglory_part_jobs SET progress=MAX(progress,0.7),'
                'error=NULL,updated_at=? WHERE state=\'analyzing\' AND EXISTS('
                'SELECT 1 FROM vainglory_ocr_jobs ocr '
                'WHERE ocr.part_id=vainglory_part_jobs.part_id)',
                (now,),
            )
            rows = connection.execute(
                "SELECT session_id FROM vainglory_scan_jobs WHERE state='analyzing' "
                'UNION SELECT session_id FROM vainglory_ocr_jobs'
            ).fetchall()
            for row in rows:
                self._refresh_session_job(connection, int(row['session_id']), now)
            return cursor.rowcount + recovered_ocr + recovered_matches

        return await self._database.write(recover)

    async def prepare_remote_worker(self) -> int:
        """Move unfinished local scan/OCR work back to the whole-part queue."""
        now = self._now()

        def prepare(connection: sqlite3.Connection) -> int:
            session_rows = connection.execute(
                "SELECT DISTINCT session_id FROM vainglory_part_jobs "
                "WHERE state='analyzing'"
            ).fetchall()
            ocr_count = connection.execute('DELETE FROM vainglory_ocr_jobs').rowcount
            part_count = connection.execute(
                "UPDATE vainglory_part_jobs SET state='pending',progress=0,"
                'error=NULL,started_at=NULL,completed_at=NULL,updated_at=? '
                "WHERE state='analyzing'",
                (now,),
            ).rowcount
            rerun_count = connection.execute(
                "UPDATE vainglory_match_rerun_jobs SET state='pending',"
                'started_at=NULL,error=NULL,updated_at=? '
                "WHERE state='running'",
                (now,),
            ).rowcount
            for row in session_rows:
                self._refresh_session_job(connection, int(row['session_id']), now)
            return ocr_count + part_count + rerun_count

        return await self._database.write(prepare)

    async def recover_stale_remote_work(self, stale_after_seconds: int) -> int:
        if stale_after_seconds < 1:
            raise ValueError('stale timeout must be positive')
        now = self._now()
        cutoff = now - int(stale_after_seconds)

        def recover(connection: sqlite3.Connection) -> int:
            session_rows = connection.execute(
                'SELECT DISTINCT session_id FROM vainglory_part_jobs '
                "WHERE state='analyzing' AND updated_at<?",
                (cutoff,),
            ).fetchall()
            part_count = connection.execute(
                "UPDATE vainglory_part_jobs SET state='pending',progress=0,"
                'error=NULL,started_at=NULL,completed_at=NULL,updated_at=? '
                "WHERE state='analyzing' AND updated_at<?",
                (now, cutoff),
            ).rowcount
            rerun_count = connection.execute(
                "UPDATE vainglory_match_rerun_jobs SET state='pending',"
                'started_at=NULL,error=NULL,updated_at=? '
                "WHERE state='running' AND updated_at<?",
                (now, cutoff),
            ).rowcount
            for row in session_rows:
                self._refresh_session_job(connection, int(row['session_id']), now)
            return part_count + rerun_count

        return await self._database.write(recover)

    async def list_analysis_workers(self) -> Tuple[AnalysisWorkerRecord, ...]:
        rows = await self._database.fetchall(
            'SELECT * FROM vainglory_analysis_workers '
            'ORDER BY last_seen_at DESC,worker_id'
        )
        return tuple(_analysis_worker_record(row) for row in rows)

    async def register_analysis_worker(
        self,
        worker_id: str,
        *,
        model_package_id: str = '',
        pipeline_version: str = '',
        concurrency: int = 0,
    ) -> AnalysisWorkerRecord:
        now = self._now()

        def register(connection: sqlite3.Connection) -> AnalysisWorkerRecord:
            connection.execute(
                'INSERT INTO vainglory_analysis_workers('
                'worker_id,model_package_id,pipeline_version,concurrency,'
                'first_seen_at,last_seen_at,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET '
                'model_package_id=CASE WHEN excluded.model_package_id<>\'\' '
                'THEN excluded.model_package_id '
                'ELSE vainglory_analysis_workers.model_package_id END,'
                'pipeline_version=CASE WHEN excluded.pipeline_version<>\'\' '
                'THEN excluded.pipeline_version '
                'ELSE vainglory_analysis_workers.pipeline_version END,'
                'concurrency=CASE WHEN excluded.concurrency>0 '
                'THEN excluded.concurrency '
                'ELSE vainglory_analysis_workers.concurrency END,'
                'first_seen_at=COALESCE('
                'vainglory_analysis_workers.first_seen_at,excluded.first_seen_at),'
                'last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at',
                (
                    worker_id,
                    model_package_id,
                    pipeline_version,
                    max(0, int(concurrency)),
                    now,
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                'SELECT * FROM vainglory_analysis_workers WHERE worker_id=?',
                (worker_id,),
            ).fetchone()
            assert row is not None
            return _analysis_worker_record(row)

        return await self._database.write(register)

    async def add_analysis_worker(
        self, worker_id: str, display_name: str
    ) -> AnalysisWorkerRecord:
        now = self._now()

        def add(connection: sqlite3.Connection) -> AnalysisWorkerRecord:
            try:
                connection.execute(
                    'INSERT INTO vainglory_analysis_workers('
                    'worker_id,display_name,created_at,updated_at) VALUES(?,?,?,?)',
                    (worker_id, display_name, now, now),
                )
            except sqlite3.IntegrityError as error:
                raise VaingloryConflict('这个 Worker 已经登记') from error
            row = connection.execute(
                'SELECT * FROM vainglory_analysis_workers WHERE worker_id=?',
                (worker_id,),
            ).fetchone()
            assert row is not None
            return _analysis_worker_record(row)

        return await self._database.write(add)

    async def update_analysis_worker(
        self,
        worker_id: str,
        *,
        display_name: Optional[str] = None,
        enabled: Optional[bool] = None,
        desired_concurrency: Optional[int] = None,
    ) -> AnalysisWorkerRecord:
        now = self._now()

        def update(connection: sqlite3.Connection) -> AnalysisWorkerRecord:
            cursor = connection.execute(
                'UPDATE vainglory_analysis_workers SET '
                'display_name=COALESCE(?,display_name),'
                'enabled=COALESCE(?,enabled),'
                'desired_concurrency=COALESCE(?,desired_concurrency),'
                'updated_at=? WHERE worker_id=?',
                (
                    display_name,
                    None if enabled is None else int(enabled),
                    desired_concurrency,
                    now,
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise VaingloryNotFound('Worker 不存在')
            row = connection.execute(
                'SELECT * FROM vainglory_analysis_workers WHERE worker_id=?',
                (worker_id,),
            ).fetchone()
            assert row is not None
            return _analysis_worker_record(row)

        return await self._database.write(update)

    async def record_analysis_worker_task(
        self,
        worker_id: str,
        *,
        succeeded: bool,
        processing_seconds: float,
        video_duration_seconds: Optional[float] = None,
        decode_analysis_seconds: Optional[float] = None,
    ) -> None:
        now = self._now()
        profiled = (
            succeeded
            and video_duration_seconds is not None
            and video_duration_seconds > 0
            and decode_analysis_seconds is not None
            and decode_analysis_seconds >= 0
        )
        await self._database.execute(
            'UPDATE vainglory_analysis_workers SET '
            'completed_task_count=completed_task_count+?,'
            'failed_task_count=failed_task_count+?,'
            'total_processing_seconds=total_processing_seconds+?,'
            'profiled_task_count=profiled_task_count+?,'
            'profiled_video_seconds=profiled_video_seconds+?,'
            'total_decode_analysis_seconds=total_decode_analysis_seconds+?,'
            'total_profiled_task_seconds=total_profiled_task_seconds+?,'
            'last_task_finished_at=?,updated_at=? '
            'WHERE worker_id=?',
            (
                int(succeeded),
                int(not succeeded),
                max(0.0, float(processing_seconds)),
                int(profiled),
                float(video_duration_seconds or 0) if profiled else 0.0,
                float(decode_analysis_seconds or 0) if profiled else 0.0,
                max(0.0, float(processing_seconds)) if profiled else 0.0,
                now,
                now,
                worker_id,
            ),
        )

    async def purge_excluded_content(self) -> int:
        def purge(connection: sqlite3.Connection) -> Dict[str, int]:
            session_ids = set()
            rows = connection.execute(
                'SELECT session.id,session.title,job.policy_snapshot_json,'
                'migration.title AS migration_title,'
                'imported.title AS import_title '
                'FROM recording_sessions session '
                'LEFT JOIN upload_jobs job ON job.session_id=session.id '
                'LEFT JOIN archive_migration_items migration '
                'ON migration.session_id=session.id '
                'LEFT JOIN vainglory_archive_imports imported '
                'ON imported.session_id=session.id'
            ).fetchall()
            for row in rows:
                if is_excluded_title(
                    row['title'],
                    row['migration_title'],
                    row['import_title'],
                    self._upload_title(row['policy_snapshot_json']),
                ):
                    session_ids.add(int(row['id']))
            import_rows = connection.execute(
                'SELECT id,session_id FROM vainglory_archive_imports '
                'WHERE instr(title,?)>0',
                (EXCLUDED_TITLE_MARKER,),
            ).fetchall()
            import_ids = {int(row['id']) for row in import_rows}
            session_ids.update(
                int(row['session_id'])
                for row in import_rows
                if row['session_id'] is not None
            )
            counts = {
                'sessions': len(session_ids),
                'imports': len(import_ids),
                'part_jobs': 0,
                'ocr_jobs': 0,
                'matches': 0,
            }
            if session_ids:
                ordered_ids = tuple(sorted(session_ids))
                placeholders = ','.join('?' for _value in ordered_ids)
                counts['matches'] = int(
                    connection.execute(
                        'SELECT COUNT(*) FROM vainglory_matches '
                        'WHERE session_id IN ({})'.format(placeholders),
                        ordered_ids,
                    ).fetchone()[0]
                )
                counts['ocr_jobs'] = connection.execute(
                    'DELETE FROM vainglory_ocr_jobs '
                    'WHERE session_id IN ({})'.format(placeholders),
                    ordered_ids,
                ).rowcount
                counts['part_jobs'] = connection.execute(
                    'DELETE FROM vainglory_part_jobs '
                    'WHERE session_id IN ({})'.format(placeholders),
                    ordered_ids,
                ).rowcount
                connection.execute(
                    'DELETE FROM vainglory_scan_jobs '
                    'WHERE session_id IN ({})'.format(placeholders),
                    ordered_ids,
                )
                connection.execute(
                    'DELETE FROM vainglory_archive_imports '
                    'WHERE session_id IN ({})'.format(placeholders),
                    ordered_ids,
                )
            connection.execute(
                'DELETE FROM vainglory_archive_imports WHERE instr(title,?)>0',
                (EXCLUDED_TITLE_MARKER,),
            )
            syncs = connection.execute(
                'SELECT account_id FROM vainglory_archive_syncs'
            ).fetchall()
            for sync in syncs:
                account_id = int(sync['account_id'])
                values = connection.execute(
                    'SELECT state,progress,retryable '
                    'FROM vainglory_archive_imports '
                    'WHERE account_id=?',
                    (account_id,),
                ).fetchall()
                total = len(values)
                completed = sum(
                    str(value['state']) in ('ready', 'skipped')
                    or (
                        str(value['state']) == 'failed' and not bool(value['retryable'])
                    )
                    for value in values
                )
                progress = (
                    1.0
                    if total == 0
                    else sum(
                        (
                            1.0
                            if str(value['state']) in ('ready', 'skipped')
                            or (
                                str(value['state']) == 'failed'
                                and not bool(value['retryable'])
                            )
                            else (
                                0.0
                                if str(value['state']) == 'failed'
                                else float(value['progress'])
                            )
                        )
                        for value in values
                    )
                    / total
                )
                connection.execute(
                    'UPDATE vainglory_archive_syncs SET progress=?,'
                    'discovered_count=?,completed_count=? WHERE account_id=?',
                    (progress, total, completed, account_id),
                )
            return counts

        counts = await self._database.write(purge)
        removed = sum(
            counts[name] for name in ('imports', 'part_jobs', 'ocr_jobs', 'matches')
        )
        if removed:
            logger.info(
                'Purged excluded Vainglory content: marker={!r} counts={}',
                EXCLUDED_TITLE_MARKER,
                counts,
            )
        return removed

    async def invalidate_outdated_results(self) -> int:
        now = self._now()
        obsolete_frame_paths: List[str] = []

        def invalidate(connection: sqlite3.Connection) -> int:
            session_rows = connection.execute(
                'SELECT DISTINCT match.session_id '
                'FROM vainglory_matches match '
                'LEFT JOIN vainglory_part_jobs job '
                'ON job.part_id=match.result_part_id '
                'WHERE (job.part_id IS NULL OR job.algorithm_version<?) '
                'AND NOT EXISTS(SELECT 1 FROM vainglory_scan_suppressions '
                'suppression WHERE suppression.session_id=match.session_id) '
                'UNION '
                'SELECT DISTINCT job.session_id FROM vainglory_part_jobs job '
                'WHERE job.algorithm_version<? AND NOT EXISTS('
                'SELECT 1 FROM vainglory_scan_suppressions suppression '
                'WHERE suppression.session_id=job.session_id)',
                (self.ALGORITHM_VERSION, self.ALGORITHM_VERSION),
            ).fetchall()
            import_rows = connection.execute(
                'SELECT DISTINCT import_id FROM vainglory_archive_parts '
                'WHERE recording_part_id IN('
                'SELECT job.part_id FROM vainglory_part_jobs job '
                'WHERE job.algorithm_version<? AND NOT EXISTS('
                'SELECT 1 FROM vainglory_scan_suppressions suppression '
                'WHERE suppression.session_id=job.session_id))',
                (self.ALGORITHM_VERSION,),
            ).fetchall()
            obsolete_frame_paths.extend(
                str(row['result_frame_path'])
                for row in connection.execute(
                    'SELECT result_frame_path FROM vainglory_matches '
                    'WHERE result_frame_path IS NOT NULL AND NOT EXISTS('
                    'SELECT 1 FROM vainglory_part_jobs job '
                    'WHERE job.part_id=vainglory_matches.result_part_id '
                    'AND job.algorithm_version>=?) AND NOT EXISTS('
                    'SELECT 1 FROM vainglory_scan_suppressions suppression '
                    'WHERE suppression.session_id=vainglory_matches.session_id)',
                    (self.ALGORITHM_VERSION,),
                ).fetchall()
            )
            deleted = connection.execute(
                'DELETE FROM vainglory_matches WHERE NOT EXISTS('
                'SELECT 1 FROM vainglory_part_jobs job '
                'WHERE job.part_id=vainglory_matches.result_part_id '
                'AND job.algorithm_version>=?) AND NOT EXISTS('
                'SELECT 1 FROM vainglory_scan_suppressions suppression '
                'WHERE suppression.session_id=vainglory_matches.session_id)',
                (self.ALGORITHM_VERSION,),
            ).rowcount
            connection.execute(
                'DELETE FROM vainglory_ocr_jobs WHERE part_id IN('
                'SELECT job.part_id FROM vainglory_part_jobs job '
                'WHERE job.algorithm_version<? AND NOT EXISTS('
                'SELECT 1 FROM vainglory_scan_suppressions suppression '
                'WHERE suppression.session_id=job.session_id))',
                (self.ALGORITHM_VERSION,),
            )
            connection.execute(
                "UPDATE vainglory_archive_parts SET state='queued',progress=0,"
                'error=NULL,updated_at=? WHERE recording_part_id IN('
                'SELECT job.part_id FROM vainglory_part_jobs job '
                'WHERE job.algorithm_version<? AND NOT EXISTS('
                'SELECT 1 FROM vainglory_scan_suppressions suppression '
                'WHERE suppression.session_id=job.session_id))',
                (now, self.ALGORITHM_VERSION),
            )
            connection.execute(
                "UPDATE vainglory_part_jobs SET state='pending',progress=0,"
                'algorithm_version=?,match_count=0,error=NULL,ignored_reason=NULL,'
                'started_at=NULL,'
                'completed_at=NULL,updated_at=? WHERE algorithm_version<? '
                'AND NOT EXISTS(SELECT 1 FROM vainglory_scan_suppressions '
                'suppression WHERE suppression.session_id='
                'vainglory_part_jobs.session_id)',
                (self.ALGORITHM_VERSION, now, self.ALGORITHM_VERSION),
            )
            for import_row in import_rows:
                import_id = int(import_row['import_id'])
                completed = int(
                    connection.execute(
                        'SELECT COUNT(*) FROM vainglory_archive_parts '
                        "WHERE import_id=? AND state='ready'",
                        (import_id,),
                    ).fetchone()[0]
                )
                page_count = int(
                    connection.execute(
                        'SELECT COUNT(*) FROM vainglory_archive_parts '
                        'WHERE import_id=?',
                        (import_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    "UPDATE vainglory_archive_imports SET state='analyzing',"
                    'progress=?,completed_page_count=?,error=NULL,'
                    "content_classification='unknown',classification_reason=NULL,"
                    'retryable=0,next_retry_at=NULL,updated_at=? WHERE id=?',
                    (
                        float(completed) / float(max(1, page_count)),
                        completed,
                        now,
                        import_id,
                    ),
                )
            for row in session_rows:
                self._ensure_scan_job(connection, int(row['session_id']), now)
                self._refresh_session_job(connection, int(row['session_id']), now)
                connection.execute(
                    'UPDATE vainglory_publications SET needs_refresh=1 '
                    'WHERE session_id=?',
                    (int(row['session_id']),),
                )
            return deleted

        deleted = await self._database.write(invalidate)
        self._remove_result_frame_files(obsolete_frame_paths)
        if deleted:
            logger.info(
                'Invalidated outdated Vainglory results: matches={} algorithm={}',
                deleted,
                self.ALGORITHM_VERSION,
            )
        return deleted

    async def apply_builtin_hero_labels(self) -> int:
        now = self._now()

        def apply(connection: sqlite3.Connection) -> int:
            updated = 0
            rows = connection.execute(
                "SELECT id,fingerprint FROM vainglory_heroes WHERE label=''"
            ).fetchall()
            for row in rows:
                label = identify_builtin_hero(str(row['fingerprint']))
                if label is None:
                    continue
                cursor = connection.execute(
                    "UPDATE vainglory_heroes SET label=?,updated_at=? "
                    "WHERE id=? AND label=''",
                    (label, now, int(row['id'])),
                )
                updated += cursor.rowcount
            return updated

        return await self._database.write(apply)

    async def consolidate_hero_catalog(self) -> int:
        now = self._now()

        def consolidate(connection: sqlite3.Connection) -> int:
            return self._consolidate_heroes(connection, now)

        return await self._database.write(consolidate)

    async def sync_hero_references(self, references: Sequence[HeroReference]) -> int:
        now = self._now()

        def sync(connection: sqlite3.Connection) -> int:
            changed = 0
            labels = tuple(reference.label for reference in references)
            for reference in references:
                row = connection.execute(
                    'SELECT id,fingerprint,thumbnail_png FROM vainglory_heroes '
                    'WHERE label=? COLLATE NOCASE ORDER BY id LIMIT 1',
                    (reference.label,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        'INSERT INTO vainglory_heroes('
                        'fingerprint,thumbnail_png,label,created_at,updated_at) '
                        'VALUES(?,?,?,?,?)',
                        (
                            reference.fingerprint,
                            reference.image_jpeg,
                            reference.label,
                            now,
                            now,
                        ),
                    )
                    changed += 1
                    continue
                if (
                    str(row['fingerprint']) == reference.fingerprint
                    and bytes(row['thumbnail_png']) == reference.image_jpeg
                ):
                    continue
                connection.execute(
                    'UPDATE vainglory_heroes SET fingerprint=?,thumbnail_png=?,'
                    'updated_at=? WHERE id=?',
                    (reference.fingerprint, reference.image_jpeg, now, int(row['id'])),
                )
                changed += 1
            if labels:
                allowed = ' OR '.join('label=? COLLATE NOCASE' for _label in labels)
                changed += connection.execute(
                    "DELETE FROM vainglory_heroes WHERE label='' OR NOT ({})".format(
                        allowed
                    ),
                    labels,
                ).rowcount
            return changed

        return await self._database.write(sync)

    async def request_scan(self, session_id: int) -> ScanJob:
        job, _remote_part_ids = await self._request_scan(
            session_id, allow_remote_media=False
        )
        return job

    async def request_scan_with_remote_media(
        self, session_id: int
    ) -> Tuple[ScanJob, Tuple[int, ...]]:
        return await self._request_scan(session_id, allow_remote_media=True)

    async def _request_scan(
        self, session_id: int, *, allow_remote_media: bool
    ) -> Tuple[ScanJob, Tuple[int, ...]]:
        now = self._now()
        obsolete_frame_paths: List[str] = []

        def request(connection: sqlite3.Connection) -> Tuple[int, ...]:
            session = connection.execute(
                'SELECT session.state,session.deletion_state,session.title,'
                'job.policy_snapshot_json,migration.title AS migration_title,'
                'imported.title AS import_title,'
                'EXISTS(SELECT 1 FROM vainglory_scan_suppressions suppression '
                'WHERE suppression.session_id=session.id) AS scan_suppressed '
                'FROM recording_sessions session '
                'LEFT JOIN upload_jobs job ON job.session_id=session.id '
                'LEFT JOIN archive_migration_items migration '
                'ON migration.session_id=session.id '
                'LEFT JOIN vainglory_archive_imports imported '
                'ON imported.session_id=session.id WHERE session.id=?',
                (int(session_id),),
            ).fetchone()
            if session is None:
                raise VaingloryNotFound('录播场次不存在')
            if bool(session['scan_suppressed']):
                raise VaingloryConflict('该直播已确认无需扫描，请先恢复扫描')
            if is_excluded_title(
                session['title'],
                session['migration_title'],
                session['import_title'],
                self._upload_title(session['policy_snapshot_json']),
            ):
                raise VaingloryConflict('标题含“直播剪辑”，不进行对局识别')
            if (
                str(session['state']) in ('cancelled', 'skipped')
                or str(session['deletion_state']) != 'none'
            ):
                raise VaingloryConflict('只能分析可用且未删除的录播')
            candidate_rows = connection.execute(
                'SELECT part.id,part.source_path,part.final_path,'
                'part.media_index_state,part.artifact_state,'
                'part.video_deleted_at,'
                'EXISTS(SELECT 1 FROM vainglory_video_sources source '
                'WHERE source.part_id=part.id) OR EXISTS('
                'SELECT 1 FROM upload_jobs remote_job '
                'JOIN upload_parts remote_part '
                'ON remote_part.job_id=remote_job.id '
                'AND remote_part.part_index=part.part_index '
                'WHERE remote_job.session_id=part.session_id '
                "AND remote_job.state IN ('approved','completed') "
                "AND remote_job.bvid IS NOT NULL AND remote_job.bvid!='' "
                'AND remote_part.cid IS NOT NULL) AS remote_available '
                'FROM recording_parts part WHERE part.session_id=? '
                'ORDER BY part.part_index,part.id',
                (int(session_id),),
            ).fetchall()
            part_rows = []
            remote_part_ids: List[int] = []
            for part in candidate_rows:
                local_available = (
                    str(part['artifact_state']) == 'ready'
                    and part['video_deleted_at'] is None
                    and os.path.isfile(
                        _preferred_part_path(part['source_path'], part['final_path'])
                    )
                )
                unusable_reason = (
                    _definitely_unusable_part_reason(
                        part['source_path'],
                        part['final_path'],
                        part['media_index_state'],
                    )
                    if local_available
                    else None
                )
                if (
                    allow_remote_media
                    and bool(part['remote_available'])
                    and (not local_available or unusable_reason is not None)
                ):
                    connection.execute(
                        "UPDATE recording_parts SET artifact_state='missing',"
                        'updated_at=? WHERE id=?',
                        (now, int(part['id'])),
                    )
                    part_rows.append(part)
                    remote_part_ids.append(int(part['id']))
                    continue
                if local_available:
                    part_rows.append(part)
            if not part_rows:
                raise VaingloryConflict('该录播没有可分析的视频文件')
            analyzing = connection.execute(
                'SELECT 1 FROM vainglory_part_jobs '
                "WHERE session_id=? AND state='analyzing' LIMIT 1",
                (int(session_id),),
            ).fetchone()
            if analyzing is not None:
                raise VaingloryConflict('该录播正在分析')
            connection.execute(
                'DELETE FROM vainglory_match_review_suppressions WHERE match_id IN ('
                'SELECT id FROM vainglory_matches WHERE session_id=?)',
                (int(session_id),),
            )
            self._ensure_scan_job(connection, int(session_id), now)
            connection.executemany(
                'INSERT INTO vainglory_part_jobs('
                'part_id,session_id,state,request_kind,progress,algorithm_version,'
                'match_count,error,requested_at,started_at,completed_at,updated_at) '
                "VALUES(?,?,'pending','manual',0,?,0,NULL,?,NULL,NULL,?) "
                'ON CONFLICT(part_id) DO UPDATE SET '
                "state='pending',request_kind='manual',progress=0,"
                'algorithm_version=excluded.algorithm_version,match_count=0,'
                'error=NULL,ignored_reason=NULL,'
                'requested_at=excluded.requested_at,started_at=NULL,'
                'completed_at=NULL,updated_at=excluded.updated_at',
                (
                    (int(part['id']), int(session_id), self.ALGORITHM_VERSION, now, now)
                    for part in part_rows
                ),
            )
            for part in part_rows:
                if int(part['id']) in remote_part_ids:
                    continue
                reason = _definitely_unusable_part_reason(
                    part['source_path'], part['final_path'], part['media_index_state']
                )
                if reason is not None:
                    obsolete_frame_paths.extend(
                        self._mark_part_ignored(
                            connection, int(part['id']), reason, now
                        )
                    )
            self._refresh_session_job(connection, int(session_id), now)
            return tuple(remote_part_ids)

        remote_part_ids = await self._database.write(request)
        self._remove_result_frame_files(obsolete_frame_paths)
        job = await self.get_job(session_id)
        assert job is not None
        return job, remote_part_ids

    async def find_video_part(
        self, bvid: str, page: int
    ) -> Optional[ManualMatchMarkerRecord]:
        normalized_bvid = bvid.strip()
        if not normalized_bvid or page < 1:
            return None
        row = await self._database.fetchone(
            'SELECT target.session_id,target.part_id,target.part_index '
            'FROM ('
            'SELECT part.session_id,part.id AS part_id,part.part_index,0 priority '
            'FROM vainglory_video_sources source '
            'JOIN recording_parts part ON part.id=source.part_id '
            'WHERE source.bvid=? AND source.page=? '
            'UNION ALL '
            'SELECT part.session_id,part.id,part.part_index,1 '
            'FROM vainglory_archive_imports imported '
            'JOIN vainglory_archive_parts archived '
            'ON archived.import_id=imported.id '
            'JOIN recording_parts part ON part.id=archived.recording_part_id '
            'WHERE imported.bvid=? AND archived.page=? '
            'UNION ALL '
            'SELECT part.session_id,part.id,part.part_index,2 '
            'FROM upload_jobs upload '
            'JOIN upload_parts remote ON remote.job_id=upload.id '
            'JOIN recording_parts part ON part.session_id=upload.session_id '
            'AND part.part_index=remote.part_index '
            'WHERE upload.bvid=? AND remote.cid IS NOT NULL AND ('
            'SELECT COUNT(*) FROM upload_parts counted '
            'WHERE counted.job_id=upload.id AND counted.cid IS NOT NULL '
            'AND counted.part_index<=remote.part_index)=?'
            ') target ORDER BY target.priority LIMIT 1',
            (
                normalized_bvid,
                int(page),
                normalized_bvid,
                int(page),
                normalized_bvid,
                int(page),
            ),
        )
        if row is None:
            return None
        return ManualMatchMarkerRecord(
            id=0,
            session_id=int(row['session_id']),
            part_id=int(row['part_id']),
            part_index=int(row['part_index']),
            at_ms=0,
        )

    async def find_session_part(
        self, session_id: int, part_index: int
    ) -> Optional[ManualMatchMarkerRecord]:
        if session_id <= 0 or part_index <= 0:
            return None
        row = await self._database.fetchone(
            'SELECT session_id,id AS part_id,part_index FROM recording_parts '
            'WHERE session_id=? AND part_index=? ORDER BY id LIMIT 1',
            (int(session_id), int(part_index)),
        )
        if row is None:
            return None
        return ManualMatchMarkerRecord(
            id=0,
            session_id=int(row['session_id']),
            part_id=int(row['part_id']),
            part_index=int(row['part_index']),
            at_ms=0,
        )

    async def create_manual_match_marker_for_video(
        self, *, bvid: str, page: int, at_ms: int
    ) -> ManualMatchMarkerRecord:
        target = await self.find_video_part(bvid, page)
        if target is None:
            raise VaingloryNotFound('这个稿件分 P 尚未进入对局索引')
        return await self.create_manual_match_marker(
            target.session_id,
            part_index=target.part_index,
            at_ms=at_ms,
            source='browser_extension',
        )

    async def create_manual_match_marker(
        self,
        session_id: int,
        *,
        part_index: int,
        at_ms: int,
        source: Literal['browser_extension', 'dashboard'],
    ) -> ManualMatchMarkerRecord:
        if session_id <= 0 or part_index <= 0:
            raise ValueError('直播场次或分 P 编号无效')
        if at_ms < 0:
            raise ValueError('对局时间点无效')
        now = self._now()

        def create(connection: sqlite3.Connection) -> ManualMatchMarkerRecord:
            part = connection.execute(
                'SELECT id,record_duration_seconds FROM recording_parts '
                'WHERE session_id=? AND part_index=?',
                (int(session_id), int(part_index)),
            ).fetchone()
            if part is None:
                raise VaingloryNotFound('这场直播不存在该分 P')
            duration = part['record_duration_seconds']
            if duration is not None and at_ms > int(duration) * 1_000 + 5_000:
                raise ValueError('对局时间点超出该分 P 时长')
            part_id = int(part['id'])
            connection.execute(
                'DELETE FROM vainglory_scan_suppressions WHERE session_id=?',
                (int(session_id),),
            )
            connection.execute(
                'DELETE FROM vainglory_match_suppressions WHERE part_id=? '
                'AND ABS(at_ms-?)<=5000',
                (part_id, int(at_ms)),
            )
            connection.execute(
                'INSERT INTO vainglory_manual_match_markers('
                'session_id,part_id,at_ms,source,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?) ON CONFLICT(part_id,at_ms) DO UPDATE SET '
                'source=excluded.source,updated_at=excluded.updated_at',
                (int(session_id), part_id, int(at_ms), source, now, now),
            )
            marker = connection.execute(
                'SELECT id FROM vainglory_manual_match_markers '
                'WHERE part_id=? AND at_ms=?',
                (part_id, int(at_ms)),
            ).fetchone()
            assert marker is not None
            self._ensure_scan_job(connection, int(session_id), now)
            current = connection.execute(
                'SELECT state FROM vainglory_part_jobs WHERE part_id=?', (part_id,)
            ).fetchone()
            if current is not None and str(current['state']) == 'analyzing':
                connection.execute(
                    "UPDATE vainglory_part_jobs SET request_kind='manual',"
                    'algorithm_version=?,updated_at=? WHERE part_id=?',
                    (self.ALGORITHM_VERSION - 1, now, part_id),
                )
            else:
                connection.execute(
                    'INSERT INTO vainglory_part_jobs('
                    'part_id,session_id,state,request_kind,progress,'
                    'algorithm_version,match_count,error,requested_at,started_at,'
                    'completed_at,updated_at) '
                    "VALUES(?,?,'pending','manual',0,?,0,NULL,?,NULL,NULL,?) "
                    'ON CONFLICT(part_id) DO UPDATE SET '
                    "state='pending',request_kind='manual',progress=0,"
                    'algorithm_version=excluded.algorithm_version,match_count=0,'
                    'error=NULL,ignored_reason=NULL,'
                    'requested_at=excluded.requested_at,started_at=NULL,'
                    'completed_at=NULL,updated_at=excluded.updated_at',
                    (part_id, int(session_id), self.ALGORITHM_VERSION, now, now),
                )
                connection.execute(
                    'DELETE FROM vainglory_ocr_jobs WHERE part_id=?', (part_id,)
                )
            self._refresh_session_job(connection, int(session_id), now)
            return ManualMatchMarkerRecord(
                id=int(marker['id']),
                session_id=int(session_id),
                part_id=part_id,
                part_index=int(part_index),
                at_ms=int(at_ms),
            )

        return await self._database.write(create)

    async def discover_ready_parts(self) -> int:
        now = self._now()

        def discover(connection: sqlite3.Connection) -> Tuple[int, List[str]]:
            obsolete_frame_paths: List[str] = []
            touched: Dict[int, bool] = {}
            existing_rows = connection.execute(
                'SELECT job.part_id,job.session_id,part.source_path,'
                'part.final_path,part.media_index_state '
                'FROM vainglory_part_jobs job '
                'JOIN recording_parts part ON part.id=job.part_id '
                "WHERE job.state IN ('pending','failed') "
                'AND job.ignored_reason IS NULL '
                "AND part.artifact_state='ready' "
                'AND part.video_deleted_at IS NULL'
            ).fetchall()
            for row in existing_rows:
                reason = _definitely_unusable_part_reason(
                    row['source_path'], row['final_path'], row['media_index_state']
                )
                if reason is None:
                    continue
                obsolete_frame_paths.extend(
                    self._mark_part_ignored(
                        connection, int(row['part_id']), reason, now
                    )
                )
                touched[int(row['session_id'])] = True
            rows = connection.execute(
                'SELECT part.id AS part_id,part.session_id,session.title,'
                'part.source_path,part.final_path,part.media_index_state,'
                'upload.policy_snapshot_json,'
                'migration.title AS migration_title,'
                'imported.title AS import_title '
                'FROM recording_parts part '
                'JOIN recording_sessions session ON session.id=part.session_id '
                'LEFT JOIN vainglory_part_jobs job ON job.part_id=part.id '
                'LEFT JOIN upload_jobs upload ON upload.session_id=session.id '
                'LEFT JOIN archive_migration_items migration '
                'ON migration.session_id=session.id '
                'LEFT JOIN vainglory_archive_imports imported '
                'ON imported.session_id=session.id '
                "WHERE part.artifact_state='ready' "
                'AND part.video_deleted_at IS NULL '
                "AND session.deletion_state='none' "
                "AND session.state NOT IN ('cancelled','skipped') "
                "AND instr(COALESCE(session.title,''),'直播剪辑')=0 "
                'AND NOT EXISTS(SELECT 1 FROM vainglory_scan_suppressions '
                'suppression WHERE suppression.session_id=session.id) '
                'AND (job.part_id IS NULL OR job.algorithm_version<?) '
                'ORDER BY part.created_at,part.id',
                (self.ALGORITHM_VERSION,),
            ).fetchall()
            for row in rows:
                if is_excluded_title(
                    row['title'],
                    row['migration_title'],
                    row['import_title'],
                    self._upload_title(row['policy_snapshot_json']),
                ):
                    continue
                session_id = int(row['session_id'])
                self._ensure_scan_job(connection, session_id, now)
                connection.execute(
                    'INSERT INTO vainglory_part_jobs('
                    'part_id,session_id,state,request_kind,progress,'
                    'algorithm_version,match_count,error,requested_at,started_at,'
                    'completed_at,updated_at) '
                    "VALUES(?,?,'pending','automatic',0,?,0,NULL,?,NULL,NULL,?) "
                    'ON CONFLICT(part_id) DO UPDATE SET '
                    "state='pending',request_kind='automatic',progress=0,"
                    'algorithm_version=excluded.algorithm_version,match_count=0,'
                    'error=NULL,ignored_reason=NULL,'
                    'requested_at=excluded.requested_at,started_at=NULL,'
                    'completed_at=NULL,updated_at=excluded.updated_at',
                    (int(row['part_id']), session_id, self.ALGORITHM_VERSION, now, now),
                )
                reason = _definitely_unusable_part_reason(
                    row['source_path'], row['final_path'], row['media_index_state']
                )
                if reason is not None:
                    obsolete_frame_paths.extend(
                        self._mark_part_ignored(
                            connection, int(row['part_id']), reason, now
                        )
                    )
                touched[session_id] = True
            for session_id in touched:
                self._refresh_session_job(connection, session_id, now)
            return len(rows), obsolete_frame_paths

        discovered, obsolete_frame_paths = await self._database.write(discover)
        self._remove_result_frame_files(obsolete_frame_paths)
        return discovered

    async def claim_live_analysis(
        self, worker_id: str, *, lease_seconds: int = 300
    ) -> Optional[LiveAnalysisClaim]:
        owner = worker_id.strip()
        if not owner:
            raise ValueError('live analysis worker ID must not be empty')
        if lease_seconds < 1:
            raise ValueError('live analysis lease must be positive')
        now = self._now()
        lease_until = now + int(lease_seconds)

        def claim(connection: sqlite3.Connection) -> Optional[LiveAnalysisClaim]:
            connection.execute(
                'INSERT OR IGNORE INTO vainglory_live_analysis_state('
                'part_id,session_id,state,next_sample_at,created_at,updated_at) '
                "SELECT part.id,part.session_id,'active',?,?,? "
                'FROM recording_parts part '
                'JOIN recording_sessions session ON session.id=part.session_id '
                "WHERE part.artifact_state='recording' "
                "AND session.state='open' AND part.video_deleted_at IS NULL",
                (now, now, now),
            )
            connection.execute(
                "UPDATE vainglory_live_analysis_state SET state='active',updated_at=? "
                "WHERE state='closed' AND EXISTS("
                'SELECT 1 FROM recording_parts part '
                'JOIN recording_sessions session ON session.id=part.session_id '
                'WHERE part.id=vainglory_live_analysis_state.part_id '
                "AND part.artifact_state='recording' AND session.state='open' "
                'AND part.video_deleted_at IS NULL)',
                (now,),
            )
            connection.execute(
                "UPDATE vainglory_live_analysis_state SET state='closed',"
                'lease_owner=NULL,lease_until=NULL,updated_at=? '
                "WHERE state='active' AND NOT EXISTS("
                'SELECT 1 FROM recording_parts part '
                'JOIN recording_sessions session ON session.id=part.session_id '
                'WHERE part.id=vainglory_live_analysis_state.part_id '
                "AND part.artifact_state='recording' AND session.state='open' "
                'AND part.video_deleted_at IS NULL)',
                (now,),
            )
            connection.execute(
                "UPDATE vainglory_live_analysis_windows SET state='pending',"
                'lease_owner=NULL,lease_until=NULL,updated_at=? '
                "WHERE state='running' AND lease_until<?",
                (now, now),
            )
            fine = connection.execute(
                'SELECT live_window.id AS item_id,live_window.session_id,'
                'live_window.part_id,live_window.start_ms,live_window.end_ms,'
                'live_window.focus_ms,live_window.mode,'
                'live_window.lease_generation,part.part_index,part.source_path,'
                'session.title '
                'FROM vainglory_live_analysis_windows live_window '
                'JOIN recording_parts part ON part.id=live_window.part_id '
                'JOIN recording_sessions session '
                'ON session.id=live_window.session_id '
                "WHERE live_window.state='pending' "
                'AND live_window.available_at<=? '
                'ORDER BY live_window.created_at,live_window.id LIMIT 1',
                (now,),
            ).fetchone()
            if fine is not None:
                generation = int(fine['lease_generation']) + 1
                updated = connection.execute(
                    "UPDATE vainglory_live_analysis_windows SET state='running',"
                    'lease_owner=?,lease_generation=?,lease_until=?,attempt=attempt+1,'
                    'error=NULL,updated_at=? '
                    "WHERE id=? AND state='pending'",
                    (owner, generation, lease_until, now, int(fine['item_id'])),
                )
                if updated.rowcount == 1:
                    return LiveAnalysisClaim(
                        kind='fine',
                        item_id=int(fine['item_id']),
                        session_id=int(fine['session_id']),
                        part=VideoPart(
                            id=int(fine['part_id']),
                            index=int(fine['part_index']),
                            path=str(fine['source_path']),
                            title=str(fine['title'] or ''),
                        ),
                        lease_owner=owner,
                        lease_generation=generation,
                        window_start_ms=int(fine['start_ms']),
                        window_end_ms=int(fine['end_ms']),
                        window_focus_ms=(
                            None if fine['focus_ms'] is None else int(fine['focus_ms'])
                        ),
                        mode=str(fine['mode']),
                    )

            coarse = connection.execute(
                'SELECT state.part_id AS item_id,state.session_id,'
                'state.lease_generation,part.part_index,part.source_path,'
                'session.title '
                'FROM vainglory_live_analysis_state state '
                'JOIN recording_parts part ON part.id=state.part_id '
                'JOIN recording_sessions session ON session.id=state.session_id '
                "WHERE state.state='active' AND state.next_sample_at<=? "
                'AND (state.lease_until IS NULL OR state.lease_until<?) '
                'ORDER BY state.next_sample_at,state.part_id LIMIT 1',
                (now, now),
            ).fetchone()
            if coarse is None:
                return None
            generation = int(coarse['lease_generation']) + 1
            updated = connection.execute(
                'UPDATE vainglory_live_analysis_state SET lease_owner=?,'
                'lease_generation=?,lease_until=?,last_error=NULL,updated_at=? '
                'WHERE part_id=? AND state=\'active\' '
                'AND (lease_until IS NULL OR lease_until<?)',
                (owner, generation, lease_until, now, int(coarse['item_id']), now),
            )
            if updated.rowcount != 1:
                return None
            return LiveAnalysisClaim(
                kind='coarse',
                item_id=int(coarse['item_id']),
                session_id=int(coarse['session_id']),
                part=VideoPart(
                    id=int(coarse['item_id']),
                    index=int(coarse['part_index']),
                    path=str(coarse['source_path']),
                    title=str(coarse['title'] or ''),
                ),
                lease_owner=owner,
                lease_generation=generation,
            )

        return await self._database.write(claim)

    async def complete_live_observation(
        self,
        claim: LiveAnalysisClaim,
        observation: LiveFrameObservation,
        *,
        image_jpeg: bytes = b'',
        model_version: str = '',
    ) -> bool:
        if claim.kind != 'coarse' or claim.item_id != claim.part.id:
            raise ValueError('live observation requires a coarse claim')
        if observation.observed_at_ms < 0:
            raise ValueError('live observation timestamp must not be negative')
        confidences = (
            observation.stage_confidence,
            observation.match_flow_confidence,
            observation.hero_select_confidence,
            observation.match_mode_confidence,
            observation.result_confidence,
        )
        if any(value < 0 or value > 1 for value in confidences):
            raise ValueError('live observation confidence must be between zero and one')
        now = self._now()

        def complete(connection: sqlite3.Connection) -> Dict[str, Any]:
            state = connection.execute(
                'SELECT live.*,session.title,session.anchor_name,session.room_id '
                'FROM vainglory_live_analysis_state live '
                'JOIN recording_sessions session ON session.id=live.session_id '
                'WHERE live.part_id=?',
                (claim.part.id,),
            ).fetchone()
            if state is None:
                raise VaingloryNotFound('实时分析状态不存在')
            if (
                state['lease_owner'] != claim.lease_owner
                or int(state['lease_generation']) != claim.lease_generation
            ):
                raise VaingloryConflict('实时分析租约已经失效')
            previous_label = str(state['last_match_flow_label'] or '')
            previous_confidence = float(state['last_match_flow_confidence'] or 0)
            last_in_match_at_ms = (
                None
                if state['last_in_match_at_ms'] is None
                else int(state['last_in_match_at_ms'])
            )
            sample_count = int(state['sample_count'])
            reliable_transition = (
                previous_label == 'match_flow'
                and previous_confidence >= 0.6
                and observation.match_flow_label != 'match_flow'
                and observation.match_flow_confidence >= 0.6
                and last_in_match_at_ms is not None
                and observation.observed_at_ms > last_in_match_at_ms
            )
            mode = (
                observation.match_mode_label
                if observation.match_mode_label in ('3v3', 'aram', '5v5')
                else 'unknown'
            )
            if reliable_transition and last_in_match_at_ms is not None:
                connection.execute(
                    'INSERT OR IGNORE INTO vainglory_live_analysis_windows('
                    'part_id,session_id,start_ms,end_ms,focus_ms,mode,'
                    'available_at,state,'
                    'created_at,updated_at) '
                    "VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                    (
                        claim.part.id,
                        claim.session_id,
                        max(0, int(last_in_match_at_ms) - 10_000),
                        observation.observed_at_ms + 30_000,
                        observation.observed_at_ms,
                        mode,
                        now + 35,
                        now,
                        now,
                    ),
                )
            uncertain = any(0.35 <= value <= 0.75 for value in confidences[1:])
            label_changed = bool(previous_label) and (
                previous_label != observation.match_flow_label
            )
            selected = bool(
                image_jpeg
                and (
                    sample_count % 20 == 0
                    or uncertain
                    or label_changed
                    or observation.result_confidence >= 0.3
                )
            )
            lineup_json = json.dumps(
                tuple(observation.hero_lineup),
                ensure_ascii=False,
                separators=(',', ':'),
            )
            connection.execute(
                'INSERT INTO vainglory_live_observations('
                'part_id,session_id,observed_at_ms,stage,stage_confidence,'
                'match_flow_label,match_flow_confidence,hero_select_label,'
                'hero_select_confidence,match_mode_label,match_mode_confidence,'
                'result_confidence,hero_lineup_json,model_version,'
                'selected_for_review,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(part_id,observed_at_ms) DO UPDATE SET '
                'stage=excluded.stage,stage_confidence=excluded.stage_confidence,'
                'match_flow_label=excluded.match_flow_label,'
                'match_flow_confidence=excluded.match_flow_confidence,'
                'hero_select_label=excluded.hero_select_label,'
                'hero_select_confidence=excluded.hero_select_confidence,'
                'match_mode_label=excluded.match_mode_label,'
                'match_mode_confidence=excluded.match_mode_confidence,'
                'result_confidence=excluded.result_confidence,'
                'hero_lineup_json=excluded.hero_lineup_json,'
                'model_version=excluded.model_version,'
                'selected_for_review=excluded.selected_for_review',
                (
                    claim.part.id,
                    claim.session_id,
                    observation.observed_at_ms,
                    observation.stage,
                    observation.stage_confidence,
                    observation.match_flow_label,
                    observation.match_flow_confidence,
                    observation.hero_select_label,
                    observation.hero_select_confidence,
                    observation.match_mode_label,
                    observation.match_mode_confidence,
                    observation.result_confidence,
                    lineup_json,
                    model_version[:100],
                    int(selected),
                    now,
                ),
            )
            next_last_in_match = (
                observation.observed_at_ms
                if observation.match_flow_label == 'match_flow'
                and observation.match_flow_confidence >= 0.6
                else last_in_match_at_ms
            )
            connection.execute(
                'UPDATE vainglory_live_analysis_state SET next_sample_at=?,'
                'last_sample_at=?,last_observed_at_ms=?,last_match_flow_label=?,'
                'last_match_flow_confidence=?,last_in_match_at_ms=?,'
                'last_hero_lineup_json=?,sample_count=sample_count+1,'
                'lease_owner=NULL,lease_until=NULL,last_error=NULL,updated_at=? '
                'WHERE part_id=? AND lease_owner=? AND lease_generation=?',
                (
                    now + 30,
                    now,
                    observation.observed_at_ms,
                    observation.match_flow_label[:40],
                    observation.match_flow_confidence,
                    next_last_in_match,
                    lineup_json,
                    now,
                    claim.part.id,
                    claim.lease_owner,
                    claim.lease_generation,
                ),
            )
            return {
                'selected': selected,
                'title': str(state['title'] or ''),
                'anchor_name': str(state['anchor_name'] or ''),
                'room_id': int(state['room_id'] or 0),
                'selection_reason': (
                    '模型置信度临界或状态发生变化'
                    if uncertain or label_changed
                    else '直播时间线定期代表帧'
                ),
            }

        stored = await self._database.write(complete)
        if bool(stored['selected']):
            self._write_live_training_candidate(
                claim,
                observation,
                image_jpeg,
                model_version=model_version,
                title=str(stored['title']),
                anchor_name=str(stored['anchor_name']),
                room_id=int(stored['room_id']),
                selection_reason=str(stored['selection_reason']),
                created_at=now,
            )
        return bool(stored['selected'])

    def _write_live_training_candidate(
        self,
        claim: LiveAnalysisClaim,
        observation: LiveFrameObservation,
        image_jpeg: bytes,
        *,
        model_version: str,
        title: str,
        anchor_name: str,
        room_id: int,
        selection_reason: str,
        created_at: int,
    ) -> None:
        relative_path = self._training_candidate_relative_path(
            session_id=claim.session_id,
            part_id=claim.part.id,
            at_ms=observation.observed_at_ms,
            content=image_jpeg,
        )
        metadata_relative_path = self._training_candidate_metadata_relative_path(
            session_id=claim.session_id,
            part_id=claim.part.id,
            at_ms=observation.observed_at_ms,
            content=image_jpeg,
        )
        metadata = {
            'schema_version': 3,
            'task': 'unified_review',
            'source_id': 'live-part-{}:{}:{}'.format(
                claim.part.id,
                observation.observed_at_ms,
                hashlib.sha256(image_jpeg).hexdigest()[:16],
            ),
            'session_id': claim.session_id,
            'part_id': claim.part.id,
            'part_index': claim.part.index,
            'at_ms': observation.observed_at_ms,
            'segment_start_ms': max(0, observation.observed_at_ms - 30_000),
            'streamer': anchor_name,
            'room_id': str(room_id),
            'session_title': title,
            'filename': Path(claim.part.path).name,
            'suggestions': {
                'match_flow': {
                    'label': observation.match_flow_label,
                    'confidence': observation.match_flow_confidence,
                    'model_version': model_version,
                    'reason': selection_reason,
                },
                'hero_select': {
                    'label': observation.hero_select_label,
                    'confidence': observation.hero_select_confidence,
                    'model_version': model_version,
                    'reason': selection_reason,
                },
                'match_mode': {
                    'label': observation.match_mode_label,
                    'confidence': observation.match_mode_confidence,
                    'model_version': model_version,
                    'reason': selection_reason,
                },
                'result_panel': {
                    'label': (
                        'result_panel'
                        if observation.result_confidence >= 0.5
                        else 'no_result_panel'
                    ),
                    'confidence': max(
                        observation.result_confidence, 1 - observation.result_confidence
                    ),
                    'model_version': model_version,
                    'reason': selection_reason,
                },
            },
            'suggested_boxes': [],
            'model_outputs': [
                {
                    'task': 'live_timeline',
                    'model_version': model_version,
                    'match_flow_label': observation.match_flow_label,
                    'match_flow_confidence': observation.match_flow_confidence,
                    'hero_select_label': observation.hero_select_label,
                    'hero_select_confidence': observation.hero_select_confidence,
                    'match_mode_label': observation.match_mode_label,
                    'match_mode_confidence': observation.match_mode_confidence,
                    'result_confidence': observation.result_confidence,
                    'hero_lineup': list(observation.hero_lineup),
                    'selection_reason': selection_reason,
                }
            ],
            'image_path': relative_path,
            'image_sha256': hashlib.sha256(image_jpeg).hexdigest(),
            'created_at': created_at,
        }
        self._write_training_candidate(
            self._resolve_training_candidate_path(relative_path),
            image_jpeg,
            metadata,
            self._resolve_training_candidate_path(metadata_relative_path),
        )

    async def complete_live_window(
        self, claim: LiveAnalysisClaim, matches: Sequence[AnalyzedMatch]
    ) -> int:
        if claim.kind != 'fine':
            raise ValueError('live result completion requires a fine claim')
        now = self._now()
        written_paths: List[Path] = []
        obsolete_frame_paths: List[str] = []

        def complete(connection: sqlite3.Connection) -> int:
            window = connection.execute(
                'SELECT * FROM vainglory_live_analysis_windows WHERE id=?',
                (claim.item_id,),
            ).fetchone()
            if window is None:
                raise VaingloryNotFound('实时精扫任务不存在')
            if (
                str(window['state']) != 'running'
                or window['lease_owner'] != claim.lease_owner
                or int(window['lease_generation']) != claim.lease_generation
            ):
                raise VaingloryConflict('实时精扫租约已经失效')
            self._ensure_scan_job(connection, claim.session_id, now)
            stored = 0
            heroes = self._existing_heroes(connection)
            for match in matches:
                if match.part_id != claim.part.id:
                    raise VaingloryConflict('实时结算页不属于当前分 P')
                if (
                    claim.window_start_ms is not None
                    and match.result_at_ms < claim.window_start_ms - 5_000
                ) or (
                    claim.window_end_ms is not None
                    and match.result_at_ms > claim.window_end_ms + 5_000
                ):
                    raise VaingloryConflict('实时结算页不在当前精扫区间内')
                final = connection.execute(
                    'SELECT 1 FROM vainglory_matches '
                    "WHERE result_part_id=? AND analysis_state='final' "
                    'AND ABS(result_at_ms-?)<=30000 LIMIT 1',
                    (claim.part.id, match.result_at_ms),
                ).fetchone()
                if final is not None:
                    continue
                nearby = connection.execute(
                    'SELECT id,result_frame_path FROM vainglory_matches '
                    "WHERE result_part_id=? AND analysis_state='provisional' "
                    'AND ABS(result_at_ms-?)<=30000',
                    (claim.part.id, match.result_at_ms),
                ).fetchall()
                obsolete_frame_paths.extend(
                    str(row['result_frame_path'])
                    for row in nearby
                    if row['result_frame_path'] is not None
                )
                for row in nearby:
                    connection.execute(
                        'DELETE FROM vainglory_matches WHERE id=?', (int(row['id']),)
                    )
                self._insert_live_match(
                    connection,
                    claim.session_id,
                    match,
                    heroes=heroes,
                    now=now,
                    written_paths=written_paths,
                )
                stored += 1
            if stored:
                self._ensure_session_player(connection, claim.session_id, now)
                self._consolidate_heroes(connection, now)
            connection.execute(
                "UPDATE vainglory_live_analysis_windows SET state='ready',"
                'lease_owner=NULL,lease_until=NULL,match_count=?,error=NULL,'
                'completed_at=?,updated_at=? '
                'WHERE id=? AND lease_owner=? AND lease_generation=?',
                (
                    stored,
                    now,
                    now,
                    claim.item_id,
                    claim.lease_owner,
                    claim.lease_generation,
                ),
            )
            connection.execute(
                'UPDATE vainglory_live_analysis_state '
                'SET fine_scan_count=fine_scan_count+1,updated_at=? WHERE part_id=?',
                (now, claim.part.id),
            )
            return stored

        stored = await self._database.write(complete)
        self._remove_result_frame_files(obsolete_frame_paths, keep=written_paths)
        return stored

    def _insert_live_match(
        self,
        connection: sqlite3.Connection,
        session_id: int,
        match: AnalyzedMatch,
        *,
        heroes: List[Tuple[int, str, str]],
        now: int,
        written_paths: List[Path],
    ) -> int:
        hero_ids: Dict[Tuple[str, int], Optional[int]] = {}
        for hero in match.heroes:
            hero_ids[(hero.side, hero.slot)] = self._resolve_hero(
                connection, hero, heroes, now
            )
        header = match.ocr.header
        team_size = max((player.slot for player in match.ocr.players), default=0)
        normalized_team_size = team_size if 1 <= team_size <= 5 else None
        recorded_player = (
            match.recorded_player if normalized_team_size in (3, 5) else None
        )
        game_mode = match.game_mode
        if game_mode not in ('aram', 'other', '3v3', '5v5'):
            game_mode = (
                '3v3'
                if normalized_team_size == 3
                else '5v5' if normalized_team_size == 5 else 'unknown'
            )
        match_kind = (
            match.match_kind
            if match.match_kind in ('pvp', 'bot', 'practice')
            else 'unknown'
        )
        view_context = (
            match.view_context
            if match.view_context in ('played', 'observed')
            else 'unknown'
        )
        stats_eligible = bool(match.stats_eligible)
        stats_exclusion_reason = (
            None
            if stats_eligible
            else match.stats_exclusion_reason.strip()[:64] or 'classification'
        )
        result_frame_path: Optional[str] = None
        if match.result_frame_png:
            result_frame_path = self._result_frame_relative_path(
                session_id=session_id,
                part_id=match.part_id,
                result_at_ms=match.result_at_ms,
                content=match.result_frame_png,
            )
            destination = self._resolve_result_frame_path(result_frame_path)
            self._write_result_frame(destination, match.result_frame_png)
            written_paths.append(destination)
        cursor = connection.execute(
            'INSERT INTO vainglory_matches('
            'session_id,result_part_id,result_at_ms,duration_seconds,result_text,'
            'end_reason,left_color,right_color,winner_side,left_kills,right_kills,'
            'left_economy,right_economy,confidence,created_at,game_mode,team_size,'
            'started_at_ms,result_frame_path,hero_recognition_version,'
            'recorded_player_side,recorded_player_slot,recorded_player_confidence,'
            'recorded_player_detection_version,match_kind,view_context,'
            'stats_eligible,stats_exclusion_reason,analysis_state) '
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "'provisional')",
            (
                session_id,
                match.part_id,
                match.result_at_ms,
                header.duration_seconds,
                header.result_text,
                header.end_reason,
                match.layout.left_color,
                match.layout.right_color,
                match.layout.winner_side,
                header.left_kills,
                header.right_kills,
                header.left_economy,
                header.right_economy,
                match.confidence,
                now,
                game_mode,
                normalized_team_size,
                max(0, match.result_at_ms - (header.duration_seconds or 0) * 1_000),
                result_frame_path,
                self.HERO_RECOGNITION_VERSION,
                None if recorded_player is None else recorded_player.side,
                None if recorded_player is None else recorded_player.slot,
                None if recorded_player is None else recorded_player.confidence,
                self.RECORDED_PLAYER_DETECTION_VERSION,
                match_kind,
                view_context,
                int(stats_eligible),
                stats_exclusion_reason,
            ),
        )
        match_id = int(cursor.lastrowid)
        for player in match.ocr.players:
            connection.execute(
                'INSERT INTO vainglory_match_players('
                'match_id,side,slot,player_name,normalized_name,hero_id,hero_source,'
                'kills,deaths,assists,economy,last_hits,confidence) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    match_id,
                    player.side,
                    player.slot,
                    player.name,
                    player.normalized_name,
                    hero_ids.get((player.side, player.slot)),
                    'automatic',
                    player.stats.kills,
                    player.stats.deaths,
                    player.stats.assists,
                    player.stats.economy,
                    player.stats.last_hits,
                    player.confidence,
                ),
            )
        return match_id

    async def fail_live_analysis(
        self, claim: LiveAnalysisClaim, error: str, *, retry: bool = True
    ) -> None:
        now = self._now()
        message = error.strip()[:500] or '实时分析失败'

        def fail(connection: sqlite3.Connection) -> None:
            if claim.kind == 'coarse':
                connection.execute(
                    'UPDATE vainglory_live_analysis_state SET lease_owner=NULL,'
                    'lease_until=NULL,next_sample_at=?,last_error=?,updated_at=? '
                    'WHERE part_id=? AND lease_owner=? AND lease_generation=?',
                    (
                        now + (5 if retry else 30),
                        message,
                        now,
                        claim.part.id,
                        claim.lease_owner,
                        claim.lease_generation,
                    ),
                )
                return
            state = 'pending' if retry else 'failed'
            connection.execute(
                'UPDATE vainglory_live_analysis_windows SET state=?,'
                'lease_owner=NULL,lease_until=NULL,error=?,updated_at=? '
                'WHERE id=? AND lease_owner=? AND lease_generation=?',
                (
                    state,
                    message,
                    now,
                    claim.item_id,
                    claim.lease_owner,
                    claim.lease_generation,
                ),
            )

        await self._database.write(fail)

    async def claim_next(self, *, discover: bool = True) -> Optional[ScanClaim]:
        if discover:
            await self.discover_ready_parts()
        now = self._now()
        recent_cutoff = max(1, now - self._REALTIME_WINDOW_SECONDS)
        season_start = current_season_started_at(now)

        def claim(connection: sqlite3.Connection) -> Optional[ScanClaim]:
            row = connection.execute(
                'SELECT ranked.* FROM ('
                'SELECT job.part_id,job.session_id,part.part_index,'
                'part.source_path,part.final_path,session.title AS session_title,'
                'session.anchor_name AS session_anchor_name,'
                'part.created_at AS part_created_at,'
                'part.record_duration_seconds,'
                '(SELECT GROUP_CONCAT(marker.at_ms) '
                'FROM vainglory_manual_match_markers marker '
                'WHERE marker.part_id=job.part_id) '
                'AS manual_candidate_times_ms,'
                '(SELECT COALESCE(SUM(COALESCE(all_part.record_duration_seconds,'
                '0)),0) FROM recording_parts all_part '
                'WHERE all_part.session_id=job.session_id) '
                'AS recording_duration_seconds,'
                "CASE WHEN session.state='open' THEN 0 "
                "WHEN job.request_kind='manual' THEN 1 "
                "WHEN (source.origin IS NULL OR source.origin!='archive') "
                'AND migration_item.id IS NULL AND session.started_at>=? THEN 2 '
                'WHEN ' + self._PUBLICATION_ANALYSIS_DEBT + ' THEN 3 '
                "WHEN session.started_at>=? AND (source.origin IS NULL OR ("
                "source.origin!='archive' AND source.cache_path IS NULL)) THEN 4 "
                'WHEN session.started_at>=? THEN 5 '
                "WHEN source.origin IS NULL OR (source.origin!='archive' "
                'AND source.cache_path IS NULL) THEN 6 '
                'ELSE 7 END AS priority,'
                'COALESCE(archive_import.recording_started_at,'
                'archive_import.published_at,migration_item.published_at,'
                'session.started_at) AS priority_sort_at '
                'FROM vainglory_part_jobs job '
                'JOIN recording_parts part ON part.id=job.part_id '
                'JOIN recording_sessions session ON session.id=job.session_id '
                'LEFT JOIN vainglory_video_sources source '
                'ON source.part_id=part.id '
                'LEFT JOIN vainglory_archive_parts archive_part '
                'ON archive_part.recording_part_id=part.id '
                'LEFT JOIN vainglory_archive_imports archive_import '
                'ON archive_import.id=archive_part.import_id '
                'LEFT JOIN vainglory_archive_syncs archive_sync '
                'ON archive_sync.account_id=archive_import.account_id '
                'LEFT JOIN archive_migration_items migration_item '
                'ON migration_item.session_id=session.id '
                "WHERE job.state='pending' AND part.artifact_state='ready' "
                'AND part.video_deleted_at IS NULL '
                "AND session.deletion_state='none' "
                "AND instr(COALESCE(session.title,''),'直播剪辑')=0 "
                'AND NOT EXISTS(SELECT 1 FROM vainglory_scan_suppressions '
                'suppression WHERE suppression.session_id=session.id) '
                "AND (source.origin IS NULL OR source.origin!='archive' "
                'OR COALESCE(archive_sync.operator_paused,0)=0) '
                'AND (archive_part.import_id IS NULL OR NOT EXISTS('
                'SELECT 1 FROM vainglory_archive_parts sibling_archive '
                'WHERE sibling_archive.import_id=archive_part.import_id '
                "AND sibling_archive.state NOT IN ('analyzing','ready')))"
                ') ranked ORDER BY ranked.priority,'
                'CASE WHEN ranked.priority>=3 THEN ranked.priority_sort_at END DESC,'
                'ranked.session_id,ranked.part_index,ranked.part_created_at,'
                'ranked.part_id LIMIT 1',
                (recent_cutoff, season_start, season_start),
            ).fetchone()
            if row is None:
                return None
            session_id = int(row['session_id'])
            part_id = int(row['part_id'])
            cursor = connection.execute(
                "UPDATE vainglory_part_jobs SET state='analyzing',progress=0,"
                'analysis_summary_json=NULL,error=NULL,started_at=?,'
                'completed_at=NULL,updated_at=? '
                "WHERE part_id=? AND state='pending'",
                (now, now, part_id),
            )
            if cursor.rowcount != 1:
                return None
            self._refresh_session_job(connection, session_id, now)
            part = VideoPart(
                id=part_id,
                index=int(row['part_index']),
                path=_preferred_part_path(row['source_path'], row['final_path']),
                title=str(row['session_title'] or ''),
                manual_candidate_times_ms=self._marker_times(
                    row['manual_candidate_times_ms']
                ),
            )
            return ScanClaim(
                session_id=session_id,
                part=part,
                realtime=int(row['priority']) <= 2,
                part_duration_seconds=(
                    None
                    if row['record_duration_seconds'] is None
                    else int(row['record_duration_seconds'])
                ),
                recording_duration_seconds=int(row['recording_duration_seconds']),
                anchor_name=str(row['session_anchor_name'] or ''),
            )

        return await self._database.write(claim)

    async def enqueue_ocr(self, part_id: int, scanned: ScannedPart) -> None:
        if not scanned.candidate_times_ms:
            raise ValueError('OCR queue needs at least one result candidate')
        now = self._now()
        contexts = (
            scanned.candidate_view_contexts
            if len(scanned.candidate_view_contexts) == len(scanned.candidate_times_ms)
            else tuple('unknown' for _ in scanned.candidate_times_ms)
        )
        hero_lineups = (
            scanned.candidate_hero_lineups
            if len(scanned.candidate_hero_lineups) == len(scanned.candidate_times_ms)
            else tuple(() for _ in scanned.candidate_times_ms)
        )
        candidate_times_json = json.dumps(
            tuple(
                {
                    'at_ms': at_ms,
                    'view_context': view_context,
                    'hero_lineup': hero_lineup,
                }
                for at_ms, view_context, hero_lineup in zip(
                    scanned.candidate_times_ms, contexts, hero_lineups
                )
            ),
            separators=(',', ':'),
        )

        def enqueue(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session_id,state FROM vainglory_part_jobs WHERE part_id=?',
                (int(part_id),),
            ).fetchone()
            if row is None:
                raise VaingloryNotFound('分析任务不存在')
            if str(row['state']) != 'analyzing':
                raise VaingloryConflict('分析任务当前不能进入 OCR 队列')
            session_id = int(row['session_id'])
            connection.execute(
                'INSERT INTO vainglory_ocr_jobs('
                'part_id,session_id,state,video_duration_ms,'
                'candidate_times_json,candidate_count,requested_at,started_at,'
                'updated_at) VALUES(?,?,\'pending\',?,?,?,?,NULL,?) '
                'ON CONFLICT(part_id) DO UPDATE SET '
                'session_id=excluded.session_id,state=\'pending\','
                'video_duration_ms=excluded.video_duration_ms,'
                'candidate_times_json=excluded.candidate_times_json,'
                'candidate_count=excluded.candidate_count,'
                'requested_at=excluded.requested_at,started_at=NULL,'
                'updated_at=excluded.updated_at',
                (
                    int(part_id),
                    session_id,
                    int(scanned.video_duration_ms),
                    candidate_times_json,
                    len(scanned.candidate_times_ms),
                    now,
                    now,
                ),
            )
            connection.execute(
                'UPDATE vainglory_part_jobs SET progress=0.7,error=NULL,'
                'updated_at=? WHERE part_id=? AND state=\'analyzing\'',
                (now, int(part_id)),
            )
            self._refresh_session_job(connection, session_id, now)

        await self._database.write(enqueue)

    async def claim_next_ocr(self) -> Optional[OcrClaim]:
        now = self._now()
        recent_cutoff = max(1, now - self._REALTIME_WINDOW_SECONDS)

        def claim(connection: sqlite3.Connection) -> Optional[OcrClaim]:
            row = connection.execute(
                'SELECT ranked.* FROM ('
                'SELECT ocr.part_id,ocr.session_id,ocr.video_duration_ms,'
                'ocr.candidate_times_json,part.part_index,part.source_path,'
                'part.final_path,session.title AS session_title,'
                'job.started_at AS analysis_started_at,'
                'ocr.requested_at AS ocr_requested_at,'
                'part.record_duration_seconds,'
                '(SELECT COALESCE(SUM(COALESCE(all_part.record_duration_seconds,'
                '0)),0) FROM recording_parts all_part '
                'WHERE all_part.session_id=ocr.session_id) '
                'AS recording_duration_seconds,'
                "CASE WHEN session.state='open' THEN 0 "
                "WHEN job.request_kind='manual' THEN 1 "
                "WHEN (source.origin IS NULL OR source.origin!='archive') "
                'AND migration_item.id IS NULL AND session.started_at>=? THEN 2 '
                'WHEN ' + self._PUBLICATION_ANALYSIS_DEBT + ' THEN 3 '
                "WHEN source.origin='archive' THEN 4 ELSE 5 END AS priority,"
                'COALESCE(archive_import.published_at,'
                'migration_item.published_at,session.started_at) '
                'AS priority_sort_at '
                'FROM vainglory_ocr_jobs ocr '
                'JOIN vainglory_part_jobs job ON job.part_id=ocr.part_id '
                'JOIN recording_parts part ON part.id=ocr.part_id '
                'JOIN recording_sessions session ON session.id=ocr.session_id '
                'LEFT JOIN vainglory_video_sources source '
                'ON source.part_id=part.id '
                'LEFT JOIN vainglory_archive_parts archive_part '
                'ON archive_part.recording_part_id=part.id '
                'LEFT JOIN vainglory_archive_imports archive_import '
                'ON archive_import.id=archive_part.import_id '
                'LEFT JOIN vainglory_archive_syncs archive_sync '
                'ON archive_sync.account_id=archive_import.account_id '
                'LEFT JOIN archive_migration_items migration_item '
                'ON migration_item.session_id=session.id '
                "WHERE ocr.state='pending' AND job.state='analyzing' "
                "AND part.artifact_state='ready' "
                'AND part.video_deleted_at IS NULL '
                "AND session.deletion_state='none' "
                "AND instr(COALESCE(session.title,''),'直播剪辑')=0 "
                'AND NOT EXISTS(SELECT 1 FROM vainglory_scan_suppressions '
                'suppression WHERE suppression.session_id=session.id) '
                "AND (source.origin IS NULL OR source.origin!='archive' "
                'OR COALESCE(archive_sync.operator_paused,0)=0)'
                ') ranked ORDER BY ranked.priority,'
                'CASE WHEN ranked.priority>=3 THEN ranked.priority_sort_at END DESC,'
                'ranked.session_id,ranked.part_index,ranked.ocr_requested_at,'
                'ranked.part_id LIMIT 1',
                (recent_cutoff,),
            ).fetchone()
            if row is None:
                return None
            part_id = int(row['part_id'])
            cursor = connection.execute(
                "UPDATE vainglory_ocr_jobs SET state='running',started_at=?,"
                "updated_at=? WHERE part_id=? AND state='pending'",
                (now, now, part_id),
            )
            if cursor.rowcount != 1:
                return None
            raw_times = json.loads(str(row['candidate_times_json']))
            candidate_times = tuple(
                int(value['at_ms']) if isinstance(value, dict) else int(value)
                for value in raw_times
            )
            candidate_contexts = tuple(
                (
                    str(value.get('view_context', 'unknown'))
                    if isinstance(value, dict)
                    and str(value.get('view_context', 'unknown'))
                    in ('played', 'observed', 'unknown')
                    else 'unknown'
                )
                for value in raw_times
            )
            candidate_hero_lineups = tuple(
                (
                    tuple(str(label) for label in value.get('hero_lineup', ()))
                    if isinstance(value, dict)
                    and isinstance(value.get('hero_lineup', ()), (list, tuple))
                    else ()
                )
                for value in raw_times
            )
            return OcrClaim(
                session_id=int(row['session_id']),
                part=VideoPart(
                    id=part_id,
                    index=int(row['part_index']),
                    path=_preferred_part_path(row['source_path'], row['final_path']),
                    title=str(row['session_title'] or ''),
                ),
                scanned=ScannedPart(
                    video_duration_ms=int(row['video_duration_ms']),
                    candidate_times_ms=candidate_times,
                    candidate_view_contexts=cast(
                        Tuple[Literal['played', 'observed', 'unknown'], ...],
                        candidate_contexts,
                    ),
                    candidate_hero_lineups=candidate_hero_lineups,
                ),
                analysis_started_at=(
                    None
                    if row['analysis_started_at'] is None
                    else int(row['analysis_started_at'])
                ),
                part_duration_seconds=(
                    None
                    if row['record_duration_seconds'] is None
                    else int(row['record_duration_seconds'])
                ),
                recording_duration_seconds=int(row['recording_duration_seconds']),
            )

        return await self._database.write(claim)

    async def update_ocr_progress(self, part_id: int, progress: float) -> None:
        bounded = max(0.0, min(0.99, float(progress)))
        overall_progress = 0.7 + bounded * 0.29
        now = self._now()

        def update(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session_id FROM vainglory_ocr_jobs WHERE part_id=? '
                "AND state='running'",
                (int(part_id),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                'UPDATE vainglory_ocr_jobs SET updated_at=? WHERE part_id=?',
                (now, int(part_id)),
            )
            connection.execute(
                'UPDATE vainglory_part_jobs SET progress=?,updated_at=? '
                "WHERE part_id=? AND state='analyzing'",
                (overall_progress, now, int(part_id)),
            )
            self._refresh_session_job(connection, int(row['session_id']), now)

        await self._database.write(update)

    async def requeue_ocr(self, part_id: int) -> None:
        now = self._now()

        def requeue(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session_id FROM vainglory_ocr_jobs WHERE part_id=?',
                (int(part_id),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE vainglory_ocr_jobs SET state='pending',started_at=NULL,"
                'updated_at=? WHERE part_id=?',
                (now, int(part_id)),
            )
            connection.execute(
                'UPDATE vainglory_part_jobs SET progress=0.7,error=NULL,'
                "updated_at=? WHERE part_id=? AND state='analyzing'",
                (now, int(part_id)),
            )
            self._refresh_session_job(connection, int(row['session_id']), now)

        await self._database.write(requeue)

    async def historical_part_paused(self, part_id: int) -> bool:
        return bool(
            await self._database.scalar(
                'SELECT COALESCE(sync.operator_paused,0) '
                'FROM vainglory_archive_parts archive '
                'JOIN vainglory_archive_imports imported '
                'ON imported.id=archive.import_id '
                'JOIN vainglory_archive_syncs sync '
                'ON sync.account_id=imported.account_id '
                'WHERE archive.recording_part_id=?',
                (int(part_id),),
            )
            or False
        )

    async def has_realtime_pending(self) -> bool:
        recent_cutoff = max(1, self._now() - self._REALTIME_WINDOW_SECONDS)
        value = await self._database.scalar(
            'SELECT EXISTS('
            'SELECT 1 FROM vainglory_part_jobs job '
            'JOIN recording_parts part ON part.id=job.part_id '
            'JOIN recording_sessions session ON session.id=job.session_id '
            'LEFT JOIN vainglory_video_sources source ON source.part_id=part.id '
            'LEFT JOIN archive_migration_items migration_item '
            'ON migration_item.session_id=session.id '
            "WHERE job.state='pending' AND part.artifact_state='ready' "
            'AND part.video_deleted_at IS NULL '
            "AND session.deletion_state='none' "
            "AND instr(COALESCE(session.title,''),'直播剪辑')=0 AND ("
            "session.state='open' OR ("
            "(source.origin IS NULL OR source.origin!='archive') "
            'AND migration_item.id IS NULL AND session.started_at>=?)))',
            (recent_cutoff,),
        )
        return bool(value)

    async def analysis_queue_status(
        self, *, limit: int = 8, offset: int = 0
    ) -> AnalysisQueueStatus:
        if not 1 <= limit <= 100:
            raise ValueError('analysis queue limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('analysis queue offset must not be negative')
        now = self._now()
        recent_cutoff = max(1, now - self._REALTIME_WINDOW_SECONDS)
        season_start = current_season_started_at(now)
        category_rank_sql = (
            "CASE WHEN session.state='open' OR ((source.origin IS NULL "
            "OR source.origin!='archive') AND migration_item.id IS NULL "
            'AND session.started_at>=?) THEN 0 '
            "WHEN job.request_kind='manual' THEN 1 "
            "WHEN source.origin='archive' THEN 2 "
            'WHEN migration_item.id IS NOT NULL THEN 3 ELSE 4 END'
        )
        priority_sql = (
            "CASE WHEN session.state='open' THEN 0 "
            "WHEN job.request_kind='manual' THEN 1 "
            "WHEN (source.origin IS NULL OR source.origin!='archive') "
            'AND migration_item.id IS NULL AND session.started_at>=? THEN 2 '
            'WHEN ' + self._PUBLICATION_ANALYSIS_DEBT + ' THEN 3 '
            "WHEN session.started_at>=? AND (source.origin IS NULL OR ("
            "source.origin!='archive' AND source.cache_path IS NULL)) THEN 4 "
            'WHEN session.started_at>=? THEN 5 '
            "WHEN source.origin IS NULL OR (source.origin!='archive' "
            'AND source.cache_path IS NULL) THEN 6 '
            'ELSE 7 END'
        )
        joins = (
            ' FROM vainglory_part_jobs job '
            'JOIN recording_parts part ON part.id=job.part_id '
            'JOIN recording_sessions session ON session.id=job.session_id '
            'LEFT JOIN vainglory_scan_jobs session_job '
            'ON session_job.session_id=job.session_id '
            'LEFT JOIN vainglory_ocr_jobs ocr ON ocr.part_id=job.part_id '
            'LEFT JOIN vainglory_video_sources source ON source.part_id=part.id '
            'LEFT JOIN vainglory_archive_parts archive_part '
            'ON archive_part.recording_part_id=part.id '
            'LEFT JOIN vainglory_archive_imports archive_import '
            'ON archive_import.id=archive_part.import_id '
            'LEFT JOIN vainglory_archive_syncs archive_sync '
            'ON archive_sync.account_id=archive_import.account_id '
            'LEFT JOIN archive_migration_items migration_item '
            'ON migration_item.session_id=session.id '
        )
        claimable = (
            " AND part.artifact_state='ready' AND part.video_deleted_at IS NULL "
            "AND session.deletion_state='none' "
            "AND instr(COALESCE(session.title,''),'直播剪辑')=0 "
            "AND (source.origin IS NULL OR source.origin!='archive' "
            'OR COALESCE(archive_sync.operator_paused,0)=0) '
            'AND (archive_part.import_id IS NULL OR NOT EXISTS('
            'SELECT 1 FROM vainglory_archive_parts sibling_archive '
            'WHERE sibling_archive.import_id=archive_part.import_id '
            "AND sibling_archive.state NOT IN ('analyzing','ready'))) "
        )
        active_predicate = (
            "job.state='analyzing' AND (ocr.state='running' "
            'OR ocr.part_id IS NULL) '
            "AND instr(COALESCE(session.title,''),'直播剪辑')=0"
        )
        no_active_sibling = (
            ' AND NOT EXISTS(SELECT 1 FROM vainglory_part_jobs active_job '
            'LEFT JOIN vainglory_ocr_jobs active_ocr '
            'ON active_ocr.part_id=active_job.part_id '
            'WHERE active_job.session_id=job.session_id '
            "AND active_job.state='analyzing' AND (active_ocr.state='running' "
            'OR active_ocr.part_id IS NULL))'
        )
        task_progress = 'COALESCE(MAX(session_job.progress),AVG(job.progress))'
        part_count = (
            '(SELECT COUNT(*) FROM vainglory_part_jobs all_job '
            'WHERE all_job.session_id=job.session_id '
            'AND all_job.ignored_reason IS NULL)'
        )
        original_part_count = (
            '(SELECT COUNT(*) FROM vainglory_part_jobs all_job '
            'WHERE all_job.session_id=job.session_id)'
        )
        ignored_part_count = (
            '(SELECT COUNT(*) FROM vainglory_part_jobs ignored_job '
            'WHERE ignored_job.session_id=job.session_id '
            'AND ignored_job.ignored_reason IS NOT NULL)'
        )
        completed_part_count = (
            '(SELECT COUNT(*) FROM vainglory_part_jobs completed_job '
            'WHERE completed_job.session_id=job.session_id '
            "AND completed_job.state='ready' "
            'AND completed_job.ignored_reason IS NULL)'
        )
        live_started_at = (
            'MAX(CASE WHEN COALESCE(session.live_start_time,0)>0 '
            'THEN session.live_start_time ELSE session.started_at END)'
        )
        recording_duration = (
            '(SELECT COALESCE(SUM(COALESCE(all_part.record_duration_seconds,0)),0) '
            'FROM recording_parts all_part '
            'WHERE all_part.session_id=job.session_id '
            'AND NOT EXISTS(SELECT 1 FROM vainglory_part_jobs ignored_job '
            'WHERE ignored_job.part_id=all_part.id '
            'AND ignored_job.ignored_reason IS NOT NULL))'
        )
        active_select = (
            'SELECT COALESCE(MIN(CASE WHEN ocr.state=\'running\' '
            'THEN job.part_id END),MIN(job.part_id)) AS part_id,'
            'job.session_id,COALESCE(MIN(CASE WHEN ocr.state=\'running\' '
            'THEN part.part_index END),MIN(part.part_index)) AS part_index,'
            'MAX(session.title) AS title,MAX(session.anchor_name) AS anchor_name,'
            "'analyzing' AS state,CASE WHEN SUM(CASE WHEN ocr.state='running' "
            "THEN 1 ELSE 0 END)>0 THEN 'ocr_recognition' "
            "ELSE 'video_scan' END AS stage,MIN("
            + category_rank_sql
            + ') AS category_rank,'
            + task_progress
            + ' AS progress,MIN(job.requested_at) AS requested_at,'
            'MIN(job.started_at) AS started_at,MAX(job.updated_at) AS updated_at,'
            + live_started_at
            + ' AS live_started_at,COALESCE(MAX(CASE WHEN ocr.state='
            "'running' THEN part.record_duration_seconds END),"
            'MAX(CASE WHEN ocr.part_id IS NULL '
            'THEN part.record_duration_seconds END)) AS part_duration_seconds,'
            + recording_duration
            + ' AS recording_duration_seconds,'
            'MAX(COALESCE(session_job.match_count,0)) AS match_count,'
            + part_count
            + ' AS part_count,'
            + original_part_count
            + ' AS original_part_count,'
            + ignored_part_count
            + ' AS ignored_part_count,'
            + completed_part_count
            + ' AS completed_part_count'
            + joins
            + ' WHERE '
            + active_predicate
            + ' GROUP BY job.session_id ORDER BY MIN(job.started_at),job.session_id'
        )
        queued_select = (
            'SELECT COALESCE(MIN(CASE WHEN ocr.state=\'pending\' '
            'THEN job.part_id END),MIN(job.part_id)) AS part_id,'
            'job.session_id,COALESCE(MIN(CASE WHEN ocr.state=\'pending\' '
            'THEN part.part_index END),MIN(part.part_index)) AS part_index,'
            'MAX(session.title) AS title,MAX(session.anchor_name) AS anchor_name,'
            "CASE WHEN SUM(CASE WHEN job.state='analyzing' THEN 1 ELSE 0 END)>0 "
            "THEN 'analyzing' ELSE 'pending' END AS state,"
            "CASE WHEN SUM(CASE WHEN ocr.state='pending' THEN 1 ELSE 0 END)>0 "
            "THEN 'ocr_waiting' ELSE 'video_scan' END AS stage,MIN("
            + category_rank_sql
            + ') AS category_rank,MIN('
            + priority_sql
            + ') AS priority,'
            + task_progress
            + ' AS progress,MIN(job.requested_at) AS requested_at,'
            'MIN(job.started_at) AS started_at,MAX(job.updated_at) AS updated_at,'
            + live_started_at
            + ' AS live_started_at,MAX(CASE WHEN ocr.state='
            "'pending' THEN part.record_duration_seconds END) "
            'AS part_duration_seconds,'
            + recording_duration
            + ' AS recording_duration_seconds,'
            'MAX(COALESCE(session_job.match_count,0)) AS match_count,'
            + part_count
            + ' AS part_count,'
            + original_part_count
            + ' AS original_part_count,'
            + ignored_part_count
            + ' AS ignored_part_count,'
            + completed_part_count
            + ' AS completed_part_count,'
            'MAX(COALESCE(archive_import.recording_started_at,'
            'archive_import.published_at,'
            'migration_item.published_at,session.started_at)) AS sort_time'
            + joins
            + " WHERE (job.state='pending' OR (job.state='analyzing' "
            "AND ocr.state='pending'))"
            + claimable
            + no_active_sibling
            + ' GROUP BY job.session_id'
        )
        category_names = {
            0: 'realtime',
            1: 'manual',
            2: 'archive',
            3: 'migration',
            4: 'backlog',
        }

        def read(connection: sqlite3.Connection) -> AnalysisQueueStatus:
            active_rows = connection.execute(active_select, (recent_cutoff,)).fetchall()
            count_rows = connection.execute(
                'SELECT category_rank,COUNT(*) AS count FROM ('
                + queued_select
                + ') GROUP BY category_rank',
                (recent_cutoff, recent_cutoff, season_start, season_start),
            ).fetchall()
            queued_rows = connection.execute(
                queued_select + ' ORDER BY priority,sort_time DESC,2 LIMIT ? OFFSET ?',
                (
                    recent_cutoff,
                    recent_cutoff,
                    season_start,
                    season_start,
                    limit,
                    offset,
                ),
            ).fetchall()
            live_status = connection.execute(
                'SELECT '
                "(SELECT COUNT(*) FROM vainglory_live_analysis_state live "
                "WHERE live.state='active') AS stream_count,"
                '((SELECT COUNT(*) FROM vainglory_live_analysis_state live '
                "WHERE live.state='active' AND live.lease_owner IS NOT NULL) + "
                '(SELECT COUNT(*) FROM vainglory_live_analysis_windows '
                "live_window WHERE live_window.state='running')) AS running_count,"
                '(SELECT COUNT(*) FROM vainglory_live_analysis_windows '
                "live_window WHERE live_window.state='pending') "
                'AS pending_window_count,'
                '(SELECT COALESCE(SUM(live.sample_count),0) '
                'FROM vainglory_live_analysis_state live '
                "WHERE live.state='active') AS sample_count,"
                '(SELECT COUNT(*) FROM vainglory_matches match '
                "WHERE match.analysis_state='provisional') "
                'AS provisional_match_count,'
                '(SELECT MAX(observation.created_at) '
                'FROM vainglory_live_observations observation) '
                'AS last_observed_at'
            ).fetchone()
            assert live_status is not None
            live_rows = connection.execute(
                'SELECT live.part_id,live.session_id,part.part_index,'
                'session.title,session.anchor_name,session.room_id,'
                'CASE WHEN COALESCE(session.live_start_time,0)>0 '
                'THEN session.live_start_time ELSE session.started_at END '
                'AS live_started_at,'
                'COALESCE(part.record_duration_seconds,0) '
                'AS recording_duration_seconds,'
                'live.last_observed_at_ms,live.sample_count,live.fine_scan_count,'
                'live.last_sample_at,live.next_sample_at,'
                'live.last_match_flow_label,live.last_match_flow_confidence,'
                'COALESCE(live.lease_owner,\'\') AS worker_id,'
                '(SELECT COUNT(*) FROM vainglory_live_analysis_windows live_window '
                'WHERE live_window.part_id=live.part_id '
                "AND live_window.state='pending') "
                'AS pending_window_count,'
                '(SELECT COUNT(*) FROM vainglory_live_analysis_windows live_window '
                'WHERE live_window.part_id=live.part_id '
                "AND live_window.state='running') "
                'AS running_window_count,'
                '(SELECT COUNT(*) FROM vainglory_live_analysis_windows live_window '
                'WHERE live_window.part_id=live.part_id '
                "AND live_window.state='ready') "
                'AS completed_window_count,'
                '(SELECT COUNT(*) FROM vainglory_live_analysis_windows live_window '
                'WHERE live_window.part_id=live.part_id '
                "AND live_window.state='failed') "
                'AS failed_window_count,'
                '(SELECT COUNT(*) FROM vainglory_matches match '
                "WHERE match.result_part_id=live.part_id "
                "AND match.analysis_state='provisional') "
                'AS provisional_match_count,'
                'COALESCE(live.last_error,\'\') AS last_error '
                'FROM vainglory_live_analysis_state live '
                'JOIN recording_parts part ON part.id=live.part_id '
                'JOIN recording_sessions session ON session.id=live.session_id '
                "WHERE live.state='active' "
                'ORDER BY COALESCE(live.last_sample_at,live.created_at) DESC,'
                'live.part_id'
            ).fetchall()
            completion_rows = connection.execute(
                'SELECT job.completed_at,job.started_at,job.session_id,job.part_id,'
                'part.part_index,session.title,part.record_duration_seconds,'
                'job.candidate_count,job.match_count,job.analysis_summary_json,'
                '(SELECT COUNT(*) FROM vainglory_part_jobs effective_job '
                'WHERE effective_job.session_id=job.session_id '
                'AND effective_job.ignored_reason IS NULL) AS part_count,'
                '(SELECT COUNT(*) FROM vainglory_part_jobs original_job '
                'WHERE original_job.session_id=job.session_id) '
                'AS original_part_count,'
                '(SELECT COUNT(*) FROM vainglory_part_jobs ignored_job '
                'WHERE ignored_job.session_id=job.session_id '
                'AND ignored_job.ignored_reason IS NOT NULL) '
                'AS ignored_part_count,'
                '(SELECT COALESCE(SUM(COALESCE(all_part.record_duration_seconds,0)),0) '
                'FROM recording_parts all_part '
                'WHERE all_part.session_id=job.session_id '
                'AND NOT EXISTS(SELECT 1 FROM vainglory_part_jobs ignored_job '
                'WHERE ignored_job.part_id=all_part.id '
                'AND ignored_job.ignored_reason IS NOT NULL)) '
                'AS recording_duration_seconds,'
                '(SELECT COALESCE(SUM(COALESCE(part_match.duration_seconds,0)),0) '
                'FROM vainglory_matches part_match '
                'WHERE part_match.result_part_id=job.part_id) '
                'AS part_match_duration_seconds,'
                '(SELECT COALESCE(SUM(COALESCE(session_match.duration_seconds,0)),0) '
                'FROM vainglory_matches session_match '
                'WHERE session_match.session_id=job.session_id) '
                'AS session_match_duration_seconds '
                'FROM vainglory_part_jobs job '
                'JOIN recording_parts part ON part.id=job.part_id '
                'JOIN recording_sessions session ON session.id=job.session_id '
                "WHERE job.state='ready' AND job.completed_at IS NOT NULL "
                'AND job.ignored_reason IS NULL '
                'ORDER BY job.completed_at DESC,job.part_id DESC LIMIT ?',
                (min(limit, 20),),
            ).fetchall()
            part_ids = {
                int(row['part_id'])
                for row in (*active_rows, *queued_rows, *completion_rows)
            }
            media_by_part: Dict[int, Tuple[Optional[str], Optional[int], bool]] = {}
            if part_ids:
                placeholders = ','.join('?' for _ in part_ids)
                media_rows = connection.execute(
                    'SELECT part.id,part.session_id,part.part_index,part.source_path,'
                    'part.final_path,COALESCE('
                    '(SELECT source.bvid FROM vainglory_video_sources source '
                    'WHERE source.part_id=part.id LIMIT 1),'
                    '(SELECT upload.bvid FROM upload_jobs upload '
                    'WHERE upload.session_id=part.session_id '
                    "AND upload.bvid IS NOT NULL AND upload.bvid<>'' "
                    'ORDER BY upload.id DESC LIMIT 1),'
                    '(SELECT imported.bvid FROM vainglory_archive_parts archived '
                    'JOIN vainglory_archive_imports imported '
                    'ON imported.id=archived.import_id '
                    'WHERE archived.recording_part_id=part.id LIMIT 1),'
                    '(SELECT publication.bvid FROM vainglory_publications publication '
                    'WHERE publication.session_id=part.session_id LIMIT 1)) AS bvid,'
                    'COALESCE('
                    '(SELECT source.page FROM vainglory_video_sources source '
                    'WHERE source.part_id=part.id LIMIT 1),'
                    'NULLIF((SELECT COUNT(*) FROM upload_parts remote '
                    'JOIN upload_jobs upload ON upload.id=remote.job_id '
                    'WHERE upload.session_id=part.session_id '
                    "AND upload.bvid IS NOT NULL AND upload.bvid<>'' "
                    'AND remote.cid IS NOT NULL '
                    'AND remote.part_index<=part.part_index),0),'
                    '(SELECT archived.page FROM vainglory_archive_parts archived '
                    'WHERE archived.recording_part_id=part.id LIMIT 1),'
                    '(SELECT COUNT(*) FROM recording_parts eligible '
                    'WHERE eligible.session_id=part.session_id '
                    'AND eligible.part_index<=part.part_index '
                    'AND eligible.upload_excluded_reason IS NULL)) AS archive_page '
                    'FROM recording_parts part WHERE part.id IN (' + placeholders + ')',
                    tuple(sorted(part_ids)),
                ).fetchall()
                for media in media_rows:
                    paths = (media['final_path'], media['source_path'])
                    local_available = any(
                        path is not None and os.path.isfile(str(path)) for path in paths
                    )
                    bvid = None if media['bvid'] is None else str(media['bvid'])
                    page = (
                        None
                        if bvid is None
                        or media['archive_page'] is None
                        or int(media['archive_page']) < 1
                        else int(media['archive_page'])
                    )
                    media_by_part[int(media['id'])] = (bvid, page, local_available)
            session_ids = {
                int(row['session_id'])
                for row in (*active_rows, *queued_rows, *completion_rows)
            }
            previews_by_session: Dict[int, List[AnalysisMatchPreview]] = {}
            previews_by_part: Dict[int, List[AnalysisMatchPreview]] = {}
            image_count_by_session: Dict[int, int] = {}
            image_count_by_part: Dict[int, int] = {}
            if session_ids:
                placeholders = ','.join('?' for _ in session_ids)
                preview_rows = connection.execute(
                    'SELECT match.id AS match_id,match.session_id,'
                    'match.result_part_id AS part_id,part.part_index,'
                    "match.result_at_ms,COALESCE(match.custom_title,'') AS title "
                    'FROM vainglory_matches match '
                    'JOIN recording_parts part ON part.id=match.result_part_id '
                    'WHERE match.result_frame_path IS NOT NULL '
                    'AND match.session_id IN ('
                    + placeholders
                    + ') ORDER BY match.session_id,part.part_index,'
                    'match.result_at_ms,match.id',
                    tuple(sorted(session_ids)),
                ).fetchall()
                for preview_row in preview_rows:
                    preview = AnalysisMatchPreview(
                        match_id=int(preview_row['match_id']),
                        session_id=int(preview_row['session_id']),
                        part_id=int(preview_row['part_id']),
                        part_index=int(preview_row['part_index']),
                        result_at_ms=int(preview_row['result_at_ms']),
                        title=str(preview_row['title'] or ''),
                    )
                    session_id = preview.session_id
                    part_id = preview.part_id
                    image_count_by_session[session_id] = (
                        image_count_by_session.get(session_id, 0) + 1
                    )
                    image_count_by_part[part_id] = (
                        image_count_by_part.get(part_id, 0) + 1
                    )
                    session_previews = previews_by_session.setdefault(session_id, [])
                    if len(session_previews) < 4:
                        session_previews.append(preview)
                    part_previews = previews_by_part.setdefault(part_id, [])
                    if len(part_previews) < 4:
                        part_previews.append(preview)
            counts = {
                category_names[int(row['category_rank'])]: int(row['count'])
                for row in count_rows
            }

            def item(row: sqlite3.Row) -> AnalysisQueueItem:
                bvid, archive_page, local_available = media_by_part.get(
                    int(row['part_id']), (None, None, False)
                )
                return AnalysisQueueItem(
                    part_id=int(row['part_id']),
                    session_id=int(row['session_id']),
                    part_index=int(row['part_index']),
                    title=str(row['title'] or ''),
                    anchor_name=str(row['anchor_name'] or ''),
                    state=str(row['state']),
                    stage=str(row['stage']),
                    category=category_names[int(row['category_rank'])],
                    progress=float(row['progress']),
                    requested_at=int(row['requested_at']),
                    started_at=(
                        None if row['started_at'] is None else int(row['started_at'])
                    ),
                    updated_at=int(row['updated_at']),
                    live_started_at=int(row['live_started_at']),
                    part_duration_seconds=(
                        None
                        if row['part_duration_seconds'] is None
                        else int(row['part_duration_seconds'])
                    ),
                    recording_duration_seconds=int(row['recording_duration_seconds']),
                    match_count=int(row['match_count']),
                    part_count=int(row['part_count']),
                    completed_part_count=int(row['completed_part_count']),
                    original_part_count=int(row['original_part_count']),
                    ignored_part_count=int(row['ignored_part_count']),
                    bvid=bvid,
                    archive_page=archive_page,
                    local_video_available=local_available,
                    image_count=image_count_by_session.get(int(row['session_id']), 0),
                    match_previews=tuple(
                        previews_by_session.get(int(row['session_id']), ())
                    ),
                )

            def completion(row: sqlite3.Row) -> AnalysisQueueCompletion:
                completed_at = int(row['completed_at'])
                started_at = (
                    completed_at
                    if row['started_at'] is None
                    else int(row['started_at'])
                )
                bvid, archive_page, local_available = media_by_part.get(
                    int(row['part_id']), (None, None, False)
                )
                return AnalysisQueueCompletion(
                    completed_at=completed_at,
                    session_id=int(row['session_id']),
                    part_id=int(row['part_id']),
                    part_index=int(row['part_index']),
                    title=str(row['title'] or ''),
                    part_duration_seconds=(
                        None
                        if row['record_duration_seconds'] is None
                        else int(row['record_duration_seconds'])
                    ),
                    recording_duration_seconds=int(row['recording_duration_seconds']),
                    part_match_duration_seconds=int(row['part_match_duration_seconds']),
                    session_match_duration_seconds=int(
                        row['session_match_duration_seconds']
                    ),
                    candidate_count=(
                        None
                        if row['candidate_count'] is None
                        else int(row['candidate_count'])
                    ),
                    match_count=int(row['match_count']),
                    elapsed_seconds=max(0, completed_at - started_at),
                    part_count=int(row['part_count']),
                    original_part_count=int(row['original_part_count']),
                    ignored_part_count=int(row['ignored_part_count']),
                    bvid=bvid,
                    archive_page=archive_page,
                    local_video_available=local_available,
                    image_count=image_count_by_part.get(int(row['part_id']), 0),
                    match_previews=tuple(previews_by_part.get(int(row['part_id']), ())),
                    analysis_summary=_decode_analysis_summary(
                        row['analysis_summary_json']
                    ),
                )

            return AnalysisQueueStatus(
                active=tuple(item(row) for row in active_rows),
                queued=tuple(item(row) for row in queued_rows),
                pending_count=sum(counts.values()),
                manual_pending=counts.get('manual', 0),
                realtime_pending=counts.get('realtime', 0),
                archive_pending=counts.get('archive', 0),
                migration_pending=counts.get('migration', 0),
                backlog_pending=counts.get('backlog', 0),
                recent_completions=tuple(completion(row) for row in completion_rows),
                live_stream_count=int(live_status['stream_count']),
                live_running_count=int(live_status['running_count']),
                live_pending_window_count=int(live_status['pending_window_count']),
                live_sample_count=int(live_status['sample_count']),
                live_provisional_match_count=int(
                    live_status['provisional_match_count']
                ),
                live_last_observed_at=(
                    None
                    if live_status['last_observed_at'] is None
                    else int(live_status['last_observed_at'])
                ),
                live_items=tuple(
                    LiveAnalysisStatusItem(
                        part_id=int(row['part_id']),
                        session_id=int(row['session_id']),
                        part_index=int(row['part_index']),
                        title=str(row['title'] or ''),
                        anchor_name=str(row['anchor_name'] or ''),
                        room_id=int(row['room_id'] or 0),
                        live_started_at=int(row['live_started_at']),
                        recording_duration_seconds=int(
                            row['recording_duration_seconds']
                        ),
                        last_observed_at_ms=(
                            None
                            if row['last_observed_at_ms'] is None
                            else int(row['last_observed_at_ms'])
                        ),
                        sample_count=int(row['sample_count']),
                        fine_scan_count=int(row['fine_scan_count']),
                        last_sample_at=(
                            None
                            if row['last_sample_at'] is None
                            else int(row['last_sample_at'])
                        ),
                        next_sample_at=int(row['next_sample_at']),
                        match_flow_label=str(row['last_match_flow_label'] or ''),
                        match_flow_confidence=float(
                            row['last_match_flow_confidence'] or 0
                        ),
                        worker_id=str(row['worker_id'] or ''),
                        pending_window_count=int(row['pending_window_count']),
                        running_window_count=int(row['running_window_count']),
                        completed_window_count=int(row['completed_window_count']),
                        failed_window_count=int(row['failed_window_count']),
                        provisional_match_count=int(row['provisional_match_count']),
                        last_error=str(row['last_error'] or ''),
                    )
                    for row in live_rows
                ),
            )

        return await self._database.read(read)

    async def index_summary(self) -> IndexSummary:
        def read(connection: sqlite3.Connection) -> IndexSummary:
            totals = connection.execute(
                'SELECT COUNT(*) AS match_count,'
                'COUNT(DISTINCT match.session_id) AS session_count,'
                'COUNT(DISTINCT CASE WHEN TRIM(session.anchor_name)<>\'\' '
                'THEN LOWER(TRIM(session.anchor_name)) END) AS anchor_count,'
                'COUNT(DISTINCT CASE WHEN TRIM(session.anchor_name)=\'\' '
                'THEN session.id END) AS unassigned_session_count '
                'FROM vainglory_matches match '
                'JOIN recording_sessions session ON session.id=match.session_id'
            ).fetchone()
            outcomes = connection.execute(
                'SELECT '
                "SUM(CASE WHEN winner_color='teal' THEN 1 ELSE 0 END) AS wins,"
                "SUM(CASE WHEN winner_color='orange' THEN 1 ELSE 0 END) AS losses,"
                "SUM(CASE WHEN winner_color='unknown' THEN 1 ELSE 0 END) AS unknowns "
                'FROM (SELECT CASE match.winner_side '
                "WHEN 'left' THEN match.left_color "
                "WHEN 'right' THEN match.right_color ELSE 'unknown' END "
                'AS winner_color FROM vainglory_matches match '
                'JOIN vainglory_scan_jobs scan ON scan.session_id=match.session_id '
                'WHERE scan.stats_included=1 AND match.stats_eligible=1)'
            ).fetchone()
            heroes = connection.execute(
                'SELECT COUNT(*) AS player_slots,'
                'SUM(CASE WHEN hero_id IS NOT NULL THEN 1 ELSE 0 END) '
                'AS recognized_heroes FROM vainglory_match_players'
            ).fetchone()
            assert totals is not None and outcomes is not None and heroes is not None
            return IndexSummary(
                match_count=int(totals['match_count']),
                session_count=int(totals['session_count']),
                anchor_count=int(totals['anchor_count']),
                unassigned_session_count=int(totals['unassigned_session_count']),
                win_count=int(outcomes['wins'] or 0),
                loss_count=int(outcomes['losses'] or 0),
                unknown_count=int(outcomes['unknowns'] or 0),
                player_slot_count=int(heroes['player_slots']),
                recognized_hero_count=int(heroes['recognized_heroes'] or 0),
            )

        return await self._database.read(read)

    async def requeue(self, part_id: int) -> None:
        now = self._now()

        def requeue(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session_id FROM vainglory_part_jobs WHERE part_id=?',
                (int(part_id),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                'DELETE FROM vainglory_ocr_jobs WHERE part_id=?', (int(part_id),)
            )
            connection.execute(
                "UPDATE vainglory_part_jobs SET state='pending',progress=0,"
                'error=NULL,started_at=NULL,completed_at=NULL,updated_at=? '
                "WHERE part_id=? AND state='analyzing'",
                (now, int(part_id)),
            )
            self._refresh_session_job(connection, int(row['session_id']), now)

        await self._database.write(requeue)

    async def update_progress(self, part_id: int, progress: float) -> None:
        bounded = max(0.0, min(0.99, float(progress)))
        now = self._now()

        def update(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session_id FROM vainglory_part_jobs WHERE part_id=?',
                (int(part_id),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                'UPDATE vainglory_part_jobs SET progress=?,updated_at=? '
                "WHERE part_id=? AND state='analyzing'",
                (bounded, now, int(part_id)),
            )
            self._refresh_session_job(connection, int(row['session_id']), now)

        await self._database.write(update)

    async def fail(self, part_id: int, error: str) -> None:
        message = error.strip()[:500] or '分析失败'
        now = self._now()

        def fail(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session_id FROM vainglory_part_jobs WHERE part_id=?',
                (int(part_id),),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                'DELETE FROM vainglory_ocr_jobs WHERE part_id=?', (int(part_id),)
            )
            connection.execute(
                "UPDATE vainglory_part_jobs SET state='failed',progress=0,error=?,"
                'ignored_reason=NULL,completed_at=?,updated_at=? '
                "WHERE part_id=? AND state='analyzing'",
                (message, now, now, int(part_id)),
            )
            self._refresh_session_job(connection, int(row['session_id']), now)

        await self._database.write(fail)

    async def ignore_unusable_part(self, part_id: int, reason: str) -> None:
        message = reason.strip()[:500] or '视频文件无法解析'
        now = self._now()

        def ignore(connection: sqlite3.Connection) -> List[str]:
            return self._mark_part_ignored(
                connection, int(part_id), message, now, require_analyzing=True
            )

        obsolete_frame_paths = await self._database.write(ignore)
        self._remove_result_frame_files(obsolete_frame_paths)

    async def complete_part(
        self,
        part_id: int,
        matches: Sequence[AnalyzedMatch],
        *,
        candidate_count: Optional[int] = None,
        training_candidates: Sequence[TrainingCandidate] = (),
        analysis_summary: Optional[Mapping[str, Any]] = None,
    ) -> None:
        now = self._now()
        analysis_summary_json = _analysis_summary_json(analysis_summary)
        written_paths: List[Path] = []
        written_training_candidates: List[Path] = []
        obsolete_frame_paths: List[str] = []
        job_sql = (
            'SELECT job.state,job.session_id,job.request_kind,'
            'job.algorithm_version,session.title,session.anchor_name,'
            'session.room_id,part.part_index,'
            "COALESCE(NULLIF(part.final_path,''),part.source_path) AS source_path "
            'FROM vainglory_part_jobs job '
            'JOIN recording_sessions session ON session.id=job.session_id '
            'JOIN recording_parts part ON part.id=job.part_id '
            'WHERE job.part_id=?'
        )
        storage_job = await self._database.fetchone(job_sql, (int(part_id),))
        if storage_job is None:
            raise VaingloryNotFound('分析任务不存在')
        if str(storage_job['state']) != 'analyzing':
            raise VaingloryConflict('分析任务当前不能写入结果')
        storage_session_id = int(storage_job['session_id'])
        storage_part_index = int(storage_job['part_index'])
        storage_source_path = str(storage_job['source_path'] or '')
        storage_streamer = str(storage_job['anchor_name'] or '')
        storage_room_id = str(storage_job['room_id'] or '')
        storage_session_title = str(storage_job['title'] or '')
        for match in matches:
            if int(match.part_id) != int(part_id):
                raise VaingloryConflict('结算页不属于当前分 P')

        def store_completion_files() -> None:
            for match in matches:
                if not match.result_frame_png:
                    continue
                relative_path = self._result_frame_relative_path(
                    session_id=storage_session_id,
                    part_id=part_id,
                    result_at_ms=match.result_at_ms,
                    content=match.result_frame_png,
                )
                destination = self._resolve_result_frame_path(relative_path)
                self._write_result_frame(destination, match.result_frame_png)

            for candidate_group in _training_candidate_groups(training_candidates):
                primary = max(
                    candidate_group,
                    key=lambda item: (
                        _is_result_training_candidate(item),
                        item.suggestion_confidence,
                    ),
                )
                try:
                    relative_path = self._training_candidate_relative_path(
                        session_id=storage_session_id,
                        part_id=part_id,
                        at_ms=primary.at_ms,
                        content=primary.image_jpeg,
                    )
                    destination = self._resolve_training_candidate_path(relative_path)
                    metadata_relative_path = (
                        self._training_candidate_metadata_relative_path(
                            session_id=storage_session_id,
                            part_id=part_id,
                            at_ms=primary.at_ms,
                            content=primary.image_jpeg,
                        )
                    )
                    metadata_destination = self._resolve_training_candidate_path(
                        metadata_relative_path
                    )
                    filename = Path(storage_source_path).name or 'part-{}'.format(
                        storage_part_index
                    )
                    digest = hashlib.sha256(primary.image_jpeg).hexdigest()
                    boxes = []
                    seen_boxes: Set[Tuple[str, float, float, float, float]] = set()
                    for candidate in candidate_group:
                        for box in candidate.suggested_boxes:
                            key = (box.box_type, box.x, box.y, box.w, box.h)
                            if key in seen_boxes:
                                continue
                            seen_boxes.add(key)
                            boxes.append(
                                {
                                    'type': box.box_type,
                                    'x': box.x,
                                    'y': box.y,
                                    'w': box.w,
                                    'h': box.h,
                                }
                            )
                    metadata = {
                        'schema_version': 3,
                        'task': 'unified_review',
                        'source_id': 'part-{}:{}:{}'.format(
                            part_id, primary.at_ms, digest[:16]
                        ),
                        'session_id': storage_session_id,
                        'part_id': int(part_id),
                        'part_index': storage_part_index,
                        'at_ms': int(primary.at_ms),
                        'segment_start_ms': min(
                            int(item.segment_start_ms) for item in candidate_group
                        ),
                        'streamer': storage_streamer,
                        'room_id': storage_room_id,
                        'session_title': storage_session_title,
                        'filename': filename,
                        'suggestions': _unified_training_suggestions(candidate_group),
                        'suggested_boxes': boxes,
                        'model_outputs': [
                            {
                                'task': item.task,
                                'model_version': item.model_version,
                                'suggested_label': item.suggested_label,
                                'suggestion_confidence': item.suggestion_confidence,
                                'stage_class': item.stage_class,
                                'stage_confidence': item.stage_confidence,
                                'mode_class': item.mode_class,
                                'mode_confidence': item.mode_confidence,
                                'selection_reason': item.selection_reason,
                            }
                            for item in candidate_group
                        ],
                        'image_path': relative_path,
                        'image_sha256': digest,
                        'created_at': now,
                    }
                    self._write_training_candidate(
                        destination, primary.image_jpeg, metadata, metadata_destination
                    )
                    written_training_candidates.append(destination)
                except (OSError, ValueError) as error:
                    logger.warning(
                        'Vainglory training candidate storage skipped: '
                        'part_id={} at_ms={} error={!r}',
                        part_id,
                        primary.at_ms,
                        error,
                    )

        await asyncio.get_running_loop().run_in_executor(None, store_completion_files)

        def complete(connection: sqlite3.Connection) -> None:
            job = connection.execute(job_sql, (int(part_id),)).fetchone()
            if job is None:
                raise VaingloryNotFound('分析任务不存在')
            if str(job['state']) != 'analyzing':
                raise VaingloryConflict('分析任务当前不能写入结果')
            session_id = int(job['session_id'])
            suppressed_times = tuple(
                int(row['at_ms'])
                for row in connection.execute(
                    'SELECT at_ms FROM vainglory_match_suppressions ' 'WHERE part_id=?',
                    (int(part_id),),
                ).fetchall()
            )
            stored_matches = tuple(
                match
                for match in matches
                if not any(
                    abs(int(match.result_at_ms) - suppressed_at_ms) <= 5_000
                    for suppressed_at_ms in suppressed_times
                )
            )
            snapshot_json, snapshot_hash = _analysis_revision_snapshot(stored_matches)
            revision_no = int(
                connection.execute(
                    'SELECT COALESCE(MAX(revision_no),0)+1 '
                    'FROM vainglory_analysis_revisions WHERE part_id=?',
                    (int(part_id),),
                ).fetchone()[0]
            )
            connection.execute(
                'INSERT INTO vainglory_analysis_revisions('
                'session_id,part_id,revision_no,request_kind,algorithm_version,'
                'match_count,snapshot_hash,snapshot_json,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (
                    session_id,
                    int(part_id),
                    revision_no,
                    str(job['request_kind']),
                    int(job['algorithm_version']),
                    len(stored_matches),
                    snapshot_hash,
                    snapshot_json,
                    now,
                ),
            )
            manual_hero_overrides = {
                (int(row['result_at_ms']), str(row['side']), int(row['slot'])): int(
                    row['hero_id']
                )
                for row in connection.execute(
                    'SELECT match.result_at_ms,player.side,player.slot,'
                    'player.hero_id FROM vainglory_matches match '
                    'JOIN vainglory_match_players player '
                    'ON player.match_id=match.id '
                    'WHERE match.result_part_id=? '
                    "AND player.hero_source='manual' "
                    'AND player.hero_id IS NOT NULL',
                    (int(part_id),),
                ).fetchall()
            }
            match_overrides = tuple(
                (int(row['result_at_ms']), self._override_payload(row['payload_json']))
                for row in connection.execute(
                    'SELECT result_at_ms,payload_json '
                    'FROM vainglory_match_overrides WHERE part_id=? '
                    'ORDER BY result_at_ms',
                    (int(part_id),),
                ).fetchall()
            )
            used_match_overrides: Set[int] = set()
            used_manual_overrides: Set[Tuple[int, str, int]] = set()
            obsolete_frame_paths.extend(
                str(row['result_frame_path'])
                for row in connection.execute(
                    'SELECT result_frame_path FROM vainglory_matches '
                    'WHERE result_part_id=? AND result_frame_path IS NOT NULL',
                    (int(part_id),),
                ).fetchall()
            )
            connection.execute(
                'DELETE FROM vainglory_matches WHERE result_part_id=?', (int(part_id),)
            )
            heroes = self._existing_heroes(connection)
            for match in stored_matches:
                if int(match.part_id) != int(part_id):
                    raise VaingloryConflict('结算页不属于当前分 P')
                hero_ids: Dict[Tuple[str, int], Optional[int]] = {}
                for hero in match.heroes:
                    hero_ids[(hero.side, hero.slot)] = self._resolve_hero(
                        connection, hero, heroes, now
                    )
                header = match.ocr.header
                team_size = max(
                    (player.slot for player in match.ocr.players), default=0
                )
                normalized_team_size = team_size if 1 <= team_size <= 5 else None
                recorded_player = (
                    match.recorded_player if normalized_team_size in (3, 5) else None
                )
                game_mode = match.game_mode
                if game_mode not in ('aram', 'other', '3v3', '5v5'):
                    game_mode = (
                        '3v3'
                        if normalized_team_size == 3
                        else '5v5' if normalized_team_size == 5 else 'unknown'
                    )
                match_kind = (
                    match.match_kind
                    if match.match_kind in ('pvp', 'bot', 'practice')
                    else 'unknown'
                )
                view_context = (
                    match.view_context
                    if match.view_context in ('played', 'observed')
                    else 'unknown'
                )
                stats_eligible = bool(match.stats_eligible)
                stats_exclusion_reason = (
                    None
                    if stats_eligible
                    else match.stats_exclusion_reason.strip()[:64] or 'classification'
                )
                started_at_ms = max(
                    0, match.result_at_ms - (header.duration_seconds or 0) * 1_000
                )
                result_frame_path: Optional[str] = None
                if match.result_frame_png:
                    result_frame_path = self._result_frame_relative_path(
                        session_id=session_id,
                        part_id=part_id,
                        result_at_ms=match.result_at_ms,
                        content=match.result_frame_png,
                    )
                    destination = self._resolve_result_frame_path(result_frame_path)
                    written_paths.append(destination)
                cursor = connection.execute(
                    'INSERT INTO vainglory_matches('
                    'session_id,result_part_id,result_at_ms,duration_seconds,'
                    'result_text,end_reason,left_color,right_color,winner_side,'
                    'left_kills,right_kills,left_economy,right_economy,confidence,'
                    'created_at,game_mode,team_size,started_at_ms,'
                    'result_frame_path,hero_recognition_version,'
                    'recorded_player_side,recorded_player_slot,'
                    'recorded_player_confidence,'
                    'recorded_player_detection_version,match_kind,view_context,'
                    'stats_eligible,stats_exclusion_reason) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        session_id,
                        match.part_id,
                        match.result_at_ms,
                        header.duration_seconds,
                        header.result_text,
                        header.end_reason,
                        match.layout.left_color,
                        match.layout.right_color,
                        match.layout.winner_side,
                        header.left_kills,
                        header.right_kills,
                        header.left_economy,
                        header.right_economy,
                        match.confidence,
                        now,
                        game_mode,
                        normalized_team_size,
                        started_at_ms,
                        result_frame_path,
                        self.HERO_RECOGNITION_VERSION,
                        (None if recorded_player is None else recorded_player.side),
                        (None if recorded_player is None else recorded_player.slot),
                        (
                            None
                            if recorded_player is None
                            else recorded_player.confidence
                        ),
                        self.RECORDED_PLAYER_DETECTION_VERSION,
                        match_kind,
                        view_context,
                        1 if stats_eligible else 0,
                        stats_exclusion_reason,
                    ),
                )
                match_id = int(cursor.lastrowid)
                for player in match.ocr.players:
                    stats = player.stats
                    override_key = (
                        int(match.result_at_ms),
                        player.side,
                        int(player.slot),
                    )
                    manual_hero_id = manual_hero_overrides.get(override_key)
                    if manual_hero_id is None:
                        nearby_overrides = (
                            (abs(result_at_ms - int(match.result_at_ms)), key, hero_id)
                            for key, hero_id in manual_hero_overrides.items()
                            for result_at_ms, side, slot in (key,)
                            if key not in used_manual_overrides
                            and side == player.side
                            and slot == int(player.slot)
                            and abs(result_at_ms - int(match.result_at_ms)) <= 30_000
                        )
                        nearest = min(nearby_overrides, default=None)
                        if nearest is not None:
                            _, override_key, manual_hero_id = nearest
                    if manual_hero_id is not None:
                        used_manual_overrides.add(override_key)
                    connection.execute(
                        'INSERT INTO vainglory_match_players('
                        'match_id,side,slot,player_name,normalized_name,hero_id,'
                        'hero_source,kills,deaths,assists,economy,last_hits,'
                        'confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (
                            match_id,
                            player.side,
                            player.slot,
                            player.name,
                            player.normalized_name,
                            (
                                manual_hero_id
                                if manual_hero_id is not None
                                else hero_ids.get((player.side, player.slot))
                            ),
                            ('manual' if manual_hero_id is not None else 'automatic'),
                            stats.kills,
                            stats.deaths,
                            stats.assists,
                            stats.economy,
                            stats.last_hits,
                            player.confidence,
                        ),
                    )
                nearby_override = min(
                    (
                        (abs(at_ms - int(match.result_at_ms)), index, payload)
                        for index, (at_ms, payload) in enumerate(match_overrides)
                        if index not in used_match_overrides
                        and abs(at_ms - int(match.result_at_ms)) <= 30_000
                    ),
                    default=None,
                    key=lambda item: (item[0], item[1]),
                )
                if nearby_override is not None:
                    _distance, override_index, override_payload = nearby_override
                    used_match_overrides.add(override_index)
                    self._apply_match_override(connection, match_id, override_payload)
            if stored_matches:
                self._ensure_session_player(connection, session_id, now)
            rerun_requested = (
                str(job['request_kind']) == 'manual'
                and int(job['algorithm_version']) < self.ALGORITHM_VERSION
            )
            if rerun_requested:
                connection.execute(
                    "UPDATE vainglory_part_jobs SET state='pending',progress=0,"
                    'algorithm_version=?,match_count=?,candidate_count=COALESCE(?,'
                    'candidate_count),analysis_summary_json=?,error=NULL,'
                    'ignored_reason=NULL,requested_at=?,started_at=NULL,'
                    'completed_at=NULL,updated_at=? WHERE part_id=?',
                    (
                        self.ALGORITHM_VERSION,
                        len(stored_matches),
                        candidate_count,
                        analysis_summary_json,
                        now,
                        now,
                        int(part_id),
                    ),
                )
            else:
                connection.execute(
                    "UPDATE vainglory_part_jobs SET state='ready',progress=1,"
                    'match_count=?,candidate_count=COALESCE(?,candidate_count),'
                    'analysis_summary_json=?,error=NULL,ignored_reason=NULL,'
                    'completed_at=?,updated_at=? WHERE part_id=?',
                    (
                        len(stored_matches),
                        candidate_count,
                        analysis_summary_json,
                        now,
                        now,
                        int(part_id),
                    ),
                )
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (session_id,),
            )
            connection.execute(
                'DELETE FROM vainglory_ocr_jobs WHERE part_id=?', (int(part_id),)
            )
            self._consolidate_heroes(connection, now)
            self._refresh_session_job(connection, session_id, now)

        await self._database.write(complete)
        self._remove_result_frame_files(obsolete_frame_paths, keep=written_paths)
        if written_paths:
            logger.info(
                'Vainglory result frames stored: part_id={} frames={} directory={}',
                part_id,
                len(written_paths),
                self._result_frame_root,
            )
        if written_training_candidates:
            logger.info(
                'Vainglory training candidates stored: part_id={} frames={} '
                'directory={}',
                part_id,
                len(written_training_candidates),
                self._training_candidate_root,
            )

    async def get_job(self, session_id: int) -> Optional[ScanJob]:
        row = await self._database.fetchone(
            'SELECT scan.*,'
            '(SELECT COUNT(*) FROM vainglory_part_jobs job '
            'WHERE job.session_id=scan.session_id '
            'AND job.ignored_reason IS NULL) AS part_count,'
            '(SELECT COUNT(*) FROM vainglory_part_jobs job '
            'WHERE job.session_id=scan.session_id) AS original_part_count,'
            '(SELECT COUNT(*) FROM vainglory_part_jobs job '
            'WHERE job.session_id=scan.session_id '
            'AND job.ignored_reason IS NOT NULL) AS ignored_part_count,'
            "COALESCE((SELECT GROUP_CONCAT(job.ignored_reason, '\n') "
            'FROM vainglory_part_jobs job WHERE job.session_id=scan.session_id '
            "AND job.ignored_reason IS NOT NULL),'') AS ignored_part_reasons "
            'FROM vainglory_scan_jobs scan WHERE scan.session_id=?',
            (int(session_id),),
        )
        return None if row is None else self._scan_job(row)

    async def list_matches(
        self,
        *,
        player_name: str = '',
        hero_ids: Sequence[int] = (),
        winner_color: Optional[str] = None,
        end_reason: Optional[str] = None,
        game_mode: Optional[str] = None,
        session_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MatchPage:
        if limit < 1 or limit > 100:
            raise ValueError('limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('offset must not be negative')
        where, parameters = self._match_filters(
            player_name=player_name,
            hero_ids=hero_ids,
            winner_color=winner_color,
            end_reason=end_reason,
            game_mode=game_mode,
            session_id=session_id,
        )
        where_sql = ' AND '.join(where)
        total = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM vainglory_matches match WHERE ' + where_sql,
                tuple(parameters),
            )
        )
        rows = await self._database.fetchall(
            self._MATCH_SELECT
            + 'WHERE '
            + where_sql
            + ' ORDER BY session.started_at DESC,part.part_index DESC,'
            'match.result_at_ms DESC,match.id DESC LIMIT ? OFFSET ?',
            tuple(parameters) + (limit, offset),
        )
        return MatchPage(total=total, items=await self._hydrate_matches(rows))

    async def list_match_sessions(
        self,
        *,
        player_name: str = '',
        hero_ids: Sequence[int] = (),
        winner_color: Optional[str] = None,
        end_reason: Optional[str] = None,
        game_mode: Optional[str] = None,
        session_id: Optional[int] = None,
        source_title: str = '',
        anchor_name: Optional[str] = None,
        stats_included: Optional[bool] = None,
        sort_by: str = 'analyzed',
        limit: int = 20,
        offset: int = 0,
    ) -> MatchSessionPage:
        if limit < 1 or limit > 100:
            raise ValueError('limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('offset must not be negative')
        if sort_by not in ('analyzed', 'started'):
            raise ValueError('sort_by must be analyzed or started')
        where, parameters = self._match_filters(
            player_name=player_name,
            hero_ids=hero_ids,
            winner_color=winner_color,
            end_reason=end_reason,
            game_mode=game_mode,
            session_id=session_id,
        )
        exact_session_lookup = (
            session_id is not None
            and not normalize_player_name(player_name)
            and not hero_ids
            and winner_color is None
            and end_reason is None
            and game_mode is None
        )
        if exact_session_lookup:
            assert session_id is not None
            conditions = ['session.id=?']
            session_parameters: List[object] = [int(session_id)]
        else:
            conditions = [
                'EXISTS(SELECT 1 FROM vainglory_matches match '
                'WHERE match.session_id=session.id AND ' + ' AND '.join(where) + ')'
            ]
            session_parameters = list(parameters)
        normalized_title = source_title.strip()
        if normalized_title:
            escaped_title = (
                normalized_title.replace('\\', '\\\\')
                .replace('%', '\\%')
                .replace('_', '\\_')
            )
            conditions.append("session.title LIKE ? ESCAPE '\\'")
            session_parameters.append('%{}%'.format(escaped_title))
        if anchor_name is not None:
            normalized_anchor = anchor_name.strip()
            if normalized_anchor:
                conditions.append('session.anchor_name=?')
                session_parameters.append(normalized_anchor)
            else:
                conditions.append("trim(session.anchor_name)='' ")
        if stats_included is not None:
            conditions.append(
                'COALESCE((SELECT scan.stats_included FROM vainglory_scan_jobs '
                'scan WHERE scan.session_id=session.id),1)=?'
            )
            session_parameters.append(1 if stats_included else 0)
        matching = ' AND '.join(conditions)
        total = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM recording_sessions session WHERE ' + matching,
                tuple(session_parameters),
            )
        )
        order_by = (
            'ordering_scan.updated_at DESC,session.started_at DESC,session.id DESC'
            if sort_by == 'analyzed'
            else 'session.started_at DESC,session.id DESC'
        )
        id_rows = await self._database.fetchall(
            'SELECT session.id FROM recording_sessions session '
            'LEFT JOIN vainglory_scan_jobs ordering_scan '
            'ON ordering_scan.session_id=session.id WHERE '
            + matching
            + ' ORDER BY '
            + order_by
            + ' LIMIT ? OFFSET ?',
            tuple(session_parameters) + (limit, offset),
        )
        session_ids = [int(row['id']) for row in id_rows]
        if not session_ids:
            return MatchSessionPage(total=total, items=())
        placeholders = ','.join('?' for _ in session_ids)
        winner_color_sql = (
            "(CASE match.winner_side WHEN 'left' THEN match.left_color "
            "WHEN 'right' THEN match.right_color ELSE 'unknown' END)"
        )
        rows = await self._database.fetchall(
            'SELECT session.id AS session_id,'
            'COALESCE(scan.custom_title,session.title) AS title,'
            'session.title AS source_title,session.anchor_name,session.started_at,'
            'COALESCE(session.live_start_time,session.started_at) '
            'AS live_started_at,'
            '(SELECT COUNT(*) FROM recording_parts source_part '
            'WHERE source_part.session_id=session.id '
            'AND NOT EXISTS(SELECT 1 FROM vainglory_part_jobs ignored_job '
            'WHERE ignored_job.part_id=source_part.id '
            'AND ignored_job.ignored_reason IS NOT NULL)) AS part_count,'
            '(SELECT COUNT(*) FROM recording_parts source_part '
            'WHERE source_part.session_id=session.id) AS original_part_count,'
            '(SELECT COUNT(*) FROM recording_parts source_part '
            'WHERE source_part.session_id=session.id '
            'AND EXISTS(SELECT 1 FROM vainglory_part_jobs ignored_job '
            'WHERE ignored_job.part_id=source_part.id '
            'AND ignored_job.ignored_reason IS NOT NULL)) AS ignored_part_count,'
            '(SELECT COALESCE(SUM(COALESCE(source_part.record_duration_seconds,'
            '0)),0) FROM recording_parts source_part '
            'WHERE source_part.session_id=session.id '
            'AND NOT EXISTS(SELECT 1 FROM vainglory_part_jobs ignored_job '
            'WHERE ignored_job.part_id=source_part.id '
            'AND ignored_job.ignored_reason IS NOT NULL)) '
            'AS recording_duration_seconds,'
            'COALESCE(scan.stats_included,1) AS stats_included,'
            'COALESCE('
            '(SELECT upload.bvid FROM upload_jobs upload '
            'WHERE upload.session_id=session.id AND upload.bvid IS NOT NULL '
            "AND upload.bvid<>'' ORDER BY upload.id DESC LIMIT 1),"
            '(SELECT source.bvid FROM vainglory_video_sources source '
            'JOIN recording_parts source_part ON source_part.id=source.part_id '
            'WHERE source_part.session_id=session.id AND NOT EXISTS('
            'SELECT 1 FROM archive_migration_items source_migration '
            'WHERE source_migration.session_id=session.id) '
            'ORDER BY source.page LIMIT 1),'
            '(SELECT imported.bvid FROM vainglory_archive_imports imported '
            'JOIN vainglory_archive_parts archive '
            'ON archive.import_id=imported.id '
            'JOIN recording_parts archive_part '
            'ON archive_part.id=archive.recording_part_id '
            'WHERE archive_part.session_id=session.id '
            'ORDER BY archive.page LIMIT 1)) AS bvid,'
            '(SELECT publication.state FROM vainglory_publications publication '
            'WHERE publication.session_id=session.id '
            'ORDER BY publication.id DESC LIMIT 1) AS publication_state,'
            '(SELECT publication.description_state '
            'FROM vainglory_publications publication '
            'WHERE publication.session_id=session.id '
            'ORDER BY publication.id DESC LIMIT 1) AS description_state,'
            '(SELECT publication.pin_state FROM vainglory_publications publication '
            'WHERE publication.session_id=session.id '
            'ORDER BY publication.id DESC LIMIT 1) AS pin_state,'
            '(SELECT publication.chapter_state '
            'FROM vainglory_publications publication '
            'WHERE publication.session_id=session.id '
            'ORDER BY publication.id DESC LIMIT 1) AS chapter_state,'
            '(SELECT publication.priority FROM vainglory_publications publication '
            'WHERE publication.session_id=session.id '
            'ORDER BY publication.id DESC LIMIT 1) AS publication_priority,'
            '(SELECT publication.updated_at '
            'FROM vainglory_publications publication '
            'WHERE publication.session_id=session.id '
            'ORDER BY publication.id DESC LIMIT 1) AS publication_updated_at,'
            'COUNT(match.id) AS match_count,'
            "SUM(CASE WHEN {}='teal' THEN 1 ELSE 0 END) AS teal_win_count,"
            "SUM(CASE WHEN {}='orange' THEN 1 ELSE 0 END) AS orange_win_count,"
            "SUM(CASE WHEN match.end_reason='surrender' THEN 1 ELSE 0 END) "
            'AS surrender_count,'
            'SUM(COALESCE(match.duration_seconds,0)) AS duration_seconds,'
            'GROUP_CONCAT(DISTINCT match.game_mode) AS game_modes '
            'FROM recording_sessions session '
            'LEFT JOIN vainglory_matches match ON match.session_id=session.id '
            'LEFT JOIN vainglory_scan_jobs scan ON scan.session_id=session.id '
            'WHERE session.id IN ({}) GROUP BY session.id,scan.custom_title,'
            'scan.stats_included'.format(
                winner_color_sql, winner_color_sql, placeholders
            ),
            tuple(session_ids),
        )
        by_id = {
            int(row['session_id']): self._match_session_record(row) for row in rows
        }
        return MatchSessionPage(
            total=total, items=tuple(by_id[value] for value in session_ids)
        )

    async def suppress_zero_match_session(self, session_id: int) -> None:
        now = self._now()

        def suppress(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT session.id,scan.state,scan.match_count,'
                'EXISTS(SELECT 1 FROM vainglory_matches match '
                'WHERE match.session_id=session.id) AS has_matches '
                'FROM recording_sessions session '
                'LEFT JOIN vainglory_scan_jobs scan ON scan.session_id=session.id '
                'WHERE session.id=?',
                (int(session_id),),
            ).fetchone()
            if row is None:
                raise VaingloryNotFound('录播场次不存在')
            if (
                row['state'] is None
                or str(row['state']) != 'ready'
                or int(row['match_count'] or 0) != 0
                or bool(row['has_matches'])
            ):
                raise VaingloryConflict('只能标记扫描完成且没有对局的直播')
            connection.execute(
                'INSERT OR IGNORE INTO vainglory_scan_suppressions('
                'session_id,created_at) VALUES(?,?)',
                (int(session_id), now),
            )

        await self._database.write(suppress)

    async def restore_zero_match_session(self, session_id: int) -> None:
        def restore(connection: sqlite3.Connection) -> None:
            session = connection.execute(
                'SELECT 1 FROM recording_sessions WHERE id=?', (int(session_id),)
            ).fetchone()
            if session is None:
                raise VaingloryNotFound('录播场次不存在')
            connection.execute(
                'DELETE FROM vainglory_scan_suppressions WHERE session_id=?',
                (int(session_id),),
            )

        await self._database.write(restore)

    async def list_zero_match_sessions(
        self, *, limit: int = 20, offset: int = 0, suppressed: bool = False
    ) -> ZeroMatchSessionPage:
        if limit < 1 or limit > 100:
            raise ValueError('limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('offset must not be negative')
        condition = (
            "scan.state='ready' "
            'AND scan.match_count=0 AND scan.completed_at IS NOT NULL '
            'AND NOT EXISTS(SELECT 1 FROM vainglory_matches match '
            'WHERE match.session_id=session.id) '
            'AND {}EXISTS(SELECT 1 FROM vainglory_scan_suppressions suppression '
            'WHERE suppression.session_id=session.id)'.format(
                '' if suppressed else 'NOT '
            )
        )
        parameters: Tuple[object, ...]
        if suppressed:
            parameters = ()
        else:
            condition += ' AND scan.algorithm_version=?'
            parameters = (self.ALGORITHM_VERSION,)
        total = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM vainglory_scan_jobs scan '
                'JOIN recording_sessions session ON session.id=scan.session_id '
                'WHERE ' + condition,
                parameters,
            )
        )
        rows = await self._database.fetchall(
            'SELECT session.id AS session_id,'
            'COALESCE(scan.custom_title,session.title) AS title,'
            'session.title AS source_title,session.anchor_name,session.started_at,'
            'scan.completed_at,'
            '(SELECT COUNT(*) FROM recording_parts part '
            'WHERE part.session_id=session.id) AS part_count,'
            '(SELECT COALESCE(SUM(COALESCE(part.record_duration_seconds,0)),0) '
            'FROM recording_parts part WHERE part.session_id=session.id) '
            'AS recording_duration_seconds,'
            'COALESCE('
            '(SELECT upload.bvid FROM upload_jobs upload '
            'WHERE upload.session_id=session.id AND upload.bvid IS NOT NULL '
            "AND upload.bvid<>'' ORDER BY upload.id DESC LIMIT 1),"
            '(SELECT source.bvid FROM vainglory_video_sources source '
            'JOIN recording_parts source_part ON source_part.id=source.part_id '
            'WHERE source_part.session_id=session.id AND NOT EXISTS('
            'SELECT 1 FROM archive_migration_items source_migration '
            'WHERE source_migration.session_id=session.id) '
            'ORDER BY source.page LIMIT 1),'
            '(SELECT imported.bvid FROM vainglory_archive_imports imported '
            'JOIN vainglory_archive_parts archive ON archive.import_id=imported.id '
            'JOIN recording_parts archive_part '
            'ON archive_part.id=archive.recording_part_id '
            'WHERE archive_part.session_id=session.id '
            'ORDER BY archive.page LIMIT 1)) AS bvid '
            'FROM vainglory_scan_jobs scan '
            'JOIN recording_sessions session ON session.id=scan.session_id '
            'WHERE '
            + condition
            + ' ORDER BY scan.completed_at DESC,session.id DESC LIMIT ? OFFSET ?',
            parameters + (limit, offset),
        )
        return ZeroMatchSessionPage(
            total=total,
            items=tuple(
                ZeroMatchSessionRecord(
                    session_id=int(row['session_id']),
                    title=str(row['title'] or ''),
                    source_title=str(row['source_title'] or ''),
                    anchor_name=str(row['anchor_name'] or ''),
                    started_at=int(row['started_at']),
                    completed_at=int(row['completed_at']),
                    recording_duration_seconds=int(
                        row['recording_duration_seconds'] or 0
                    ),
                    part_count=int(row['part_count'] or 0),
                    bvid=None if row['bvid'] is None else str(row['bvid']),
                )
                for row in rows
            ),
        )

    async def list_recorded_player_reviews(
        self, *, limit: int = 50, offset: int = 0
    ) -> MatchPage:
        if limit < 1 or limit > 100:
            raise ValueError('limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('offset must not be negative')
        condition = (
            'match.team_size IN (3,5) AND match.result_frame_path IS NOT NULL '
            'AND match.recorded_player_detection_version>=? '
            'AND match.recorded_player_side IS NULL AND NOT EXISTS('
            'SELECT 1 FROM vainglory_match_review_suppressions suppression '
            "WHERE suppression.match_id=match.id "
            "AND suppression.review_type='recorded_player')"
        )
        parameters = (self.RECORDED_PLAYER_DETECTION_VERSION,)
        total = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM vainglory_matches match WHERE ' + condition,
                parameters,
            )
        )
        rows = await self._database.fetchall(
            self._MATCH_SELECT
            + 'WHERE '
            + condition
            + ' ORDER BY session.started_at DESC,part.part_index DESC,'
            'match.result_at_ms DESC,match.id DESC LIMIT ? OFFSET ?',
            parameters + (limit, offset),
        )
        return MatchPage(total=total, items=await self._hydrate_matches(rows))

    async def list_hero_reviews(self, *, limit: int = 50, offset: int = 0) -> MatchPage:
        if limit < 1 or limit > 100:
            raise ValueError('limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('offset must not be negative')
        condition = (
            'match.result_frame_path IS NOT NULL AND EXISTS('
            'SELECT 1 FROM vainglory_match_players player '
            'WHERE player.match_id=match.id AND player.hero_id IS NULL) '
            'AND NOT EXISTS('
            'SELECT 1 FROM vainglory_match_review_suppressions suppression '
            "WHERE suppression.match_id=match.id "
            "AND suppression.review_type='hero')"
        )
        total = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM vainglory_matches match WHERE ' + condition
            )
        )
        rows = await self._database.fetchall(
            self._MATCH_SELECT
            + 'WHERE '
            + condition
            + ' ORDER BY session.started_at DESC,part.part_index DESC,'
            'match.result_at_ms DESC,match.id DESC LIMIT ? OFFSET ?',
            (limit, offset),
        )
        return MatchPage(total=total, items=await self._hydrate_matches(rows))

    async def get_match(self, match_id: int) -> MatchRecord:
        rows = await self._database.fetchall(
            self._MATCH_SELECT + 'WHERE match.id=?', (int(match_id),)
        )
        if not rows:
            raise VaingloryNotFound('对局不存在')
        return (await self._hydrate_matches(rows))[0]

    async def suppress_match_review(self, match_id: int, review_type: str) -> None:
        if review_type not in ('hero', 'recorded_player'):
            raise ValueError('review type must be hero or recorded_player')
        now = self._now()

        def suppress(connection: sqlite3.Connection) -> None:
            match = connection.execute(
                'SELECT 1 FROM vainglory_matches WHERE id=?', (int(match_id),)
            ).fetchone()
            if match is None:
                raise VaingloryNotFound('对局不存在')
            connection.execute(
                'INSERT OR IGNORE INTO vainglory_match_review_suppressions('
                'match_id,review_type,created_at) VALUES(?,?,?)',
                (int(match_id), review_type, now),
            )

        await self._database.write(suppress)

    async def request_match_rerun(self, match_id: int) -> None:
        now = self._now()

        def request(connection: sqlite3.Connection) -> None:
            match = connection.execute(
                'SELECT result_part_id FROM vainglory_matches WHERE id=?',
                (int(match_id),),
            ).fetchone()
            if match is None:
                raise VaingloryNotFound('对局不存在')
            connection.execute(
                'DELETE FROM vainglory_match_review_suppressions WHERE match_id=?',
                (int(match_id),),
            )
            connection.execute(
                'INSERT INTO vainglory_match_rerun_jobs('
                'match_id,state,error,requested_at,started_at,completed_at,updated_at) '
                "VALUES(?,'pending',NULL,?,NULL,NULL,?) "
                'ON CONFLICT(match_id) DO UPDATE SET '
                "state='pending',error=NULL,requested_at=excluded.requested_at,"
                'started_at=NULL,completed_at=NULL,updated_at=excluded.updated_at',
                (int(match_id), now, now),
            )

        await self._database.write(request)

    async def requeue_match_rerun(self, match_id: int) -> None:
        now = self._now()
        await self._database.execute(
            "UPDATE vainglory_match_rerun_jobs SET state='pending',"
            'started_at=NULL,error=NULL,updated_at=? '
            "WHERE match_id=? AND state='running'",
            (now, int(match_id)),
        )

    async def claim_next_match_rerun(self) -> Optional[MatchRerunClaim]:
        now = self._now()

        def claim(connection: sqlite3.Connection) -> Optional[MatchRerunClaim]:
            row = connection.execute(
                'SELECT job.match_id,match.session_id,match.result_part_id,'
                'match.result_at_ms,match.view_context,part.part_index,'
                'part.source_path,part.final_path,session.title '
                'FROM vainglory_match_rerun_jobs job '
                'JOIN vainglory_matches match ON match.id=job.match_id '
                'JOIN recording_parts part ON part.id=match.result_part_id '
                'JOIN recording_sessions session ON session.id=match.session_id '
                "WHERE job.state='pending' AND part.artifact_state='ready' "
                'AND part.video_deleted_at IS NULL '
                "AND session.deletion_state='none' "
                'ORDER BY job.requested_at,job.match_id LIMIT 1'
            ).fetchone()
            if row is None:
                return None
            match_id = int(row['match_id'])
            changed = connection.execute(
                "UPDATE vainglory_match_rerun_jobs SET state='running',"
                'started_at=?,completed_at=NULL,error=NULL,updated_at=? '
                "WHERE match_id=? AND state='pending'",
                (now, now, match_id),
            )
            if changed.rowcount != 1:
                return None
            return MatchRerunClaim(
                match_id=match_id,
                session_id=int(row['session_id']),
                part=VideoPart(
                    id=int(row['result_part_id']),
                    index=int(row['part_index']),
                    path=_preferred_part_path(row['source_path'], row['final_path']),
                    title=str(row['title'] or ''),
                ),
                result_at_ms=int(row['result_at_ms']),
                view_context=cast(
                    Literal['played', 'observed', 'unknown'], str(row['view_context'])
                ),
            )

        return await self._database.write(claim)

    async def fail_match_rerun(self, match_id: int, error: str) -> None:
        now = self._now()

        def fail(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE vainglory_match_rerun_jobs SET state='failed',error=?,"
                'completed_at=?,updated_at=? WHERE match_id=?',
                (error.strip()[:500] or '单局重新识别失败', now, now, int(match_id)),
            )
            connection.execute(
                'UPDATE vainglory_part_jobs SET completed_at=?,updated_at=? '
                'WHERE part_id=(SELECT result_part_id FROM vainglory_matches '
                'WHERE id=?)',
                (now, now, int(match_id)),
            )

        await self._database.write(fail)

    async def touch_match_rerun(self, match_id: int) -> None:
        await self._database.execute(
            'UPDATE vainglory_match_rerun_jobs SET updated_at=? '
            "WHERE match_id=? AND state='running'",
            (self._now(), int(match_id)),
        )

    async def complete_match_rerun(
        self, match_id: int, recognized: AnalyzedMatch
    ) -> MatchRecord:
        now = self._now()
        written_paths: List[Path] = []
        obsolete_frame_paths: List[str] = []

        def complete(connection: sqlite3.Connection) -> None:
            current = connection.execute(
                'SELECT match.session_id,match.result_part_id,'
                'match.result_frame_path,match.recorded_player_source '
                'FROM vainglory_matches match '
                'JOIN vainglory_match_rerun_jobs job ON job.match_id=match.id '
                "WHERE match.id=? AND job.state='running'",
                (int(match_id),),
            ).fetchone()
            if current is None:
                raise VaingloryConflict('单局重新识别任务当前不能写入结果')
            part_id = int(current['result_part_id'])
            session_id = int(current['session_id'])
            if int(recognized.part_id) != part_id:
                raise VaingloryConflict('重新识别结果不属于原分 P')
            if current['result_frame_path'] is not None:
                obsolete_frame_paths.append(str(current['result_frame_path']))

            heroes = self._existing_heroes(connection)
            hero_ids: Dict[Tuple[str, int], Optional[int]] = {
                (hero.side, hero.slot): self._resolve_hero(
                    connection, hero, heroes, now
                )
                for hero in recognized.heroes
            }
            header = recognized.ocr.header
            team_size = max(
                (player.slot for player in recognized.ocr.players), default=0
            )
            normalized_team_size = team_size if 1 <= team_size <= 5 else None
            game_mode = recognized.game_mode
            if game_mode not in ('aram', 'other', '3v3', '5v5'):
                game_mode = (
                    '3v3'
                    if normalized_team_size == 3
                    else '5v5' if normalized_team_size == 5 else 'unknown'
                )
            match_kind = (
                recognized.match_kind
                if recognized.match_kind in ('pvp', 'bot', 'practice')
                else 'unknown'
            )
            view_context = (
                recognized.view_context
                if recognized.view_context in ('played', 'observed')
                else 'unknown'
            )
            stats_eligible = bool(recognized.stats_eligible)
            stats_exclusion_reason = (
                None
                if stats_eligible
                else recognized.stats_exclusion_reason.strip()[:64] or 'classification'
            )
            started_at_ms = max(
                0, recognized.result_at_ms - (header.duration_seconds or 0) * 1_000
            )
            result_frame_path: Optional[str] = None
            if recognized.result_frame_png:
                result_frame_path = self._result_frame_relative_path(
                    session_id=session_id,
                    part_id=part_id,
                    result_at_ms=recognized.result_at_ms,
                    content=recognized.result_frame_png,
                )
                destination = self._resolve_result_frame_path(result_frame_path)
                self._write_result_frame(destination, recognized.result_frame_png)
                written_paths.append(destination)

            recorded_player = (
                recognized.recorded_player
                if normalized_team_size in (3, 5)
                and str(current['recorded_player_source']) != 'manual'
                else None
            )
            assignments = [
                'result_at_ms=?',
                'duration_seconds=?',
                'result_text=?',
                'end_reason=?',
                'left_color=?',
                'right_color=?',
                'winner_side=?',
                'left_kills=?',
                'right_kills=?',
                'left_economy=?',
                'right_economy=?',
                'confidence=?',
                'game_mode=?',
                'team_size=?',
                'started_at_ms=?',
                'result_frame_path=?',
                'hero_recognition_version=?',
                'match_kind=?',
                'view_context=?',
                'stats_eligible=?',
                'stats_exclusion_reason=?',
            ]
            values: List[Any] = [
                recognized.result_at_ms,
                header.duration_seconds,
                header.result_text,
                header.end_reason,
                recognized.layout.left_color,
                recognized.layout.right_color,
                recognized.layout.winner_side,
                header.left_kills,
                header.right_kills,
                header.left_economy,
                header.right_economy,
                recognized.confidence,
                game_mode,
                normalized_team_size,
                started_at_ms,
                result_frame_path,
                self.HERO_RECOGNITION_VERSION,
                match_kind,
                view_context,
                1 if stats_eligible else 0,
                stats_exclusion_reason,
            ]
            if str(current['recorded_player_source']) != 'manual':
                assignments.extend(
                    (
                        'recorded_player_side=?',
                        'recorded_player_slot=?',
                        'recorded_player_confidence=?',
                        "recorded_player_source='automatic'",
                        'recorded_player_detection_version=?',
                    )
                )
                values.extend(
                    (
                        None if recorded_player is None else recorded_player.side,
                        None if recorded_player is None else recorded_player.slot,
                        (
                            None
                            if recorded_player is None
                            else recorded_player.confidence
                        ),
                        self.RECORDED_PLAYER_DETECTION_VERSION,
                    )
                )
            connection.execute(
                'UPDATE vainglory_matches SET {} WHERE id=?'.format(
                    ','.join(assignments)
                ),
                tuple(values) + (int(match_id),),
            )
            connection.execute(
                'DELETE FROM vainglory_match_players WHERE match_id=?', (int(match_id),)
            )
            for player in recognized.ocr.players:
                stats = player.stats
                connection.execute(
                    'INSERT INTO vainglory_match_players('
                    'match_id,side,slot,player_name,normalized_name,hero_id,'
                    'hero_source,kills,deaths,assists,economy,last_hits,'
                    'confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        int(match_id),
                        player.side,
                        player.slot,
                        player.name,
                        player.normalized_name,
                        hero_ids.get((player.side, player.slot)),
                        'automatic',
                        stats.kills,
                        stats.deaths,
                        stats.assists,
                        stats.economy,
                        stats.last_hits,
                        player.confidence,
                    ),
                )
            override = connection.execute(
                'SELECT payload_json FROM vainglory_match_overrides '
                'WHERE part_id=? AND ABS(result_at_ms-?)<=30000 '
                'ORDER BY ABS(result_at_ms-?) LIMIT 1',
                (part_id, int(recognized.result_at_ms), int(recognized.result_at_ms)),
            ).fetchone()
            if override is not None:
                self._apply_match_override(
                    connection,
                    int(match_id),
                    self._override_payload(override['payload_json']),
                )
            connection.execute(
                'DELETE FROM vainglory_match_rerun_jobs WHERE match_id=?',
                (int(match_id),),
            )
            connection.execute(
                'UPDATE vainglory_part_jobs SET match_count=('
                'SELECT COUNT(*) FROM vainglory_matches '
                'WHERE result_part_id=?),completed_at=?,updated_at=? '
                'WHERE part_id=?',
                (part_id, now, now, part_id),
            )
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (session_id,),
            )
            self._consolidate_heroes(connection, now)
            self._refresh_session_job(connection, session_id, now)

        await self._database.write(complete)
        self._remove_result_frame_files(obsolete_frame_paths, keep=written_paths)
        return await self.get_match(match_id)

    async def delete_match(self, match_id: int) -> None:
        now = self._now()
        obsolete_frame_paths: List[str] = []

        def delete(connection: sqlite3.Connection) -> None:
            match = connection.execute(
                'SELECT session_id,result_part_id,result_at_ms,result_frame_path '
                'FROM vainglory_matches WHERE id=?',
                (int(match_id),),
            ).fetchone()
            if match is None:
                raise VaingloryNotFound('对局不存在')
            session_id = int(match['session_id'])
            part_id = int(match['result_part_id'])
            at_ms = int(match['result_at_ms'])
            if match['result_frame_path'] is not None:
                obsolete_frame_paths.append(str(match['result_frame_path']))
            connection.execute(
                'INSERT INTO vainglory_match_suppressions(part_id,at_ms,created_at) '
                'VALUES(?,?,?) ON CONFLICT(part_id,at_ms) DO UPDATE SET '
                'created_at=excluded.created_at',
                (part_id, at_ms, now),
            )
            connection.execute(
                'DELETE FROM vainglory_manual_match_markers WHERE part_id=? '
                'AND ABS(at_ms-?)<=5000',
                (part_id, at_ms),
            )
            connection.execute(
                'DELETE FROM vainglory_match_overrides WHERE part_id=? '
                'AND ABS(result_at_ms-?)<=5000',
                (part_id, at_ms),
            )
            connection.execute(
                'DELETE FROM vainglory_matches WHERE id=?', (int(match_id),)
            )
            connection.execute(
                'UPDATE vainglory_part_jobs SET match_count=('
                'SELECT COUNT(*) FROM vainglory_matches '
                'WHERE result_part_id=?),updated_at=? WHERE part_id=?',
                (part_id, now, part_id),
            )
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (session_id,),
            )
            self._refresh_session_job(connection, session_id, now)

        await self._database.write(delete)
        self._remove_result_frame_files(obsolete_frame_paths)

    async def result_frame_path(self, match_id: int) -> Optional[Path]:
        row = await self._database.fetchone(
            'SELECT result_frame_path FROM vainglory_matches WHERE id=?',
            (int(match_id),),
        )
        if row is None or row['result_frame_path'] is None:
            return None
        relative_path = str(row['result_frame_path'])
        try:
            path = self._resolve_result_frame_path(relative_path)
        except ValueError:
            logger.warning(
                'Ignored unsafe Vainglory result frame path: match_id={} path={!r}',
                match_id,
                relative_path,
            )
            return None
        return path if path.is_file() else None

    async def next_hero_rematch(self) -> Optional[HeroRematchClaim]:
        row = await self._database.fetchone(
            'SELECT match.id FROM vainglory_matches match '
            'WHERE match.hero_recognition_version<? '
            'AND match.result_frame_path IS NOT NULL AND EXISTS('
            'SELECT 1 FROM vainglory_match_players player '
            'WHERE player.match_id=match.id AND player.hero_id IS NULL) '
            'ORDER BY match.id LIMIT 1',
            (self.HERO_RECOGNITION_VERSION,),
        )
        return None if row is None else HeroRematchClaim(match_id=int(row['id']))

    async def complete_hero_rematch(
        self, match_id: int, heroes: Sequence[AnalyzedHero]
    ) -> int:
        now = self._now()

        def complete(connection: sqlite3.Connection) -> int:
            match = connection.execute(
                'SELECT session_id FROM vainglory_matches WHERE id=?', (int(match_id),)
            ).fetchone()
            if match is None:
                raise VaingloryNotFound('对局不存在')
            existing = self._existing_heroes(connection)
            updated = 0
            for hero in heroes:
                hero_id = self._resolve_hero(connection, hero, existing, now)
                if hero_id is None:
                    continue
                updated += connection.execute(
                    'UPDATE vainglory_match_players SET hero_id=? '
                    'WHERE match_id=? AND side=? AND slot=? AND hero_id IS NULL '
                    "AND hero_source<>'manual'",
                    (hero_id, int(match_id), hero.side, hero.slot),
                ).rowcount
            connection.execute(
                'UPDATE vainglory_matches SET hero_recognition_version=? WHERE id=?',
                (self.HERO_RECOGNITION_VERSION, int(match_id)),
            )
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (int(match['session_id']),),
            )
            return updated

        return await self._database.write(complete)

    async def next_recorded_player_backfill(
        self,
    ) -> Optional[RecordedPlayerBackfillClaim]:
        row = await self._database.fetchone(
            'SELECT id FROM vainglory_matches '
            'WHERE recorded_player_detection_version<? '
            "AND recorded_player_source<>'manual' "
            'AND result_frame_path IS NOT NULL ORDER BY id LIMIT 1',
            (self.RECORDED_PLAYER_DETECTION_VERSION,),
        )
        return (
            None
            if row is None
            else RecordedPlayerBackfillClaim(match_id=int(row['id']))
        )

    async def complete_recorded_player_backfill(
        self, match_id: int, player: Optional[RecordedPlayer]
    ) -> bool:
        def complete(connection: sqlite3.Connection) -> bool:
            match = connection.execute(
                'SELECT session_id,team_size,recorded_player_source,'
                'recorded_player_side FROM vainglory_matches WHERE id=?',
                (int(match_id),),
            ).fetchone()
            if match is None:
                raise VaingloryNotFound('对局不存在')
            if str(match['recorded_player_source']) == 'manual':
                return match['recorded_player_side'] is not None
            selected = player
            if match['team_size'] is not None and int(match['team_size']) not in (3, 5):
                selected = None
            if selected is not None:
                exists = connection.execute(
                    'SELECT 1 FROM vainglory_match_players '
                    'WHERE match_id=? AND side=? AND slot=?',
                    (int(match_id), selected.side, selected.slot),
                ).fetchone()
                if exists is None:
                    selected = None
            connection.execute(
                'UPDATE vainglory_matches SET recorded_player_side=?,'
                'recorded_player_slot=?,recorded_player_confidence=?,'
                'recorded_player_detection_version=?,'
                "recorded_player_source='automatic' "
                'WHERE id=?',
                (
                    None if selected is None else selected.side,
                    None if selected is None else selected.slot,
                    None if selected is None else selected.confidence,
                    self.RECORDED_PLAYER_DETECTION_VERSION,
                    int(match_id),
                ),
            )
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (int(match['session_id']),),
            )
            return selected is not None

        return await self._database.write(complete)

    async def set_recorded_player(
        self, match_id: int, *, side: str, slot: int
    ) -> MatchRecord:
        if side not in ('left', 'right'):
            raise ValueError('player side is invalid')
        if slot < 1 or slot > 5:
            raise ValueError('player slot is invalid')

        def update(connection: sqlite3.Connection) -> None:
            match = connection.execute(
                'SELECT session_id,result_part_id,result_at_ms,team_size,'
                'left_color,right_color '
                'FROM vainglory_matches WHERE id=?',
                (int(match_id),),
            ).fetchone()
            if match is None:
                raise VaingloryNotFound('对局不存在')
            if match['team_size'] is None or int(match['team_size']) not in (3, 5):
                raise VaingloryConflict('当前对局不支持确认主播英雄')
            teal_side = (
                'left'
                if str(match['left_color']) == 'teal'
                else 'right' if str(match['right_color']) == 'teal' else ''
            )
            if side != teal_side:
                raise VaingloryConflict('只能从主播所在的蓝绿色一方选择')
            player = connection.execute(
                'SELECT 1 FROM vainglory_match_players '
                'WHERE match_id=? AND side=? AND slot=?',
                (int(match_id), side, int(slot)),
            ).fetchone()
            if player is None:
                raise VaingloryNotFound('对局中的玩家位置不存在')
            connection.execute(
                'UPDATE vainglory_matches SET recorded_player_side=?,'
                'recorded_player_slot=?,recorded_player_confidence=1,'
                "recorded_player_source='manual',"
                'recorded_player_detection_version=? WHERE id=?',
                (
                    side,
                    int(slot),
                    self.RECORDED_PLAYER_DETECTION_VERSION,
                    int(match_id),
                ),
            )
            self._merge_match_override(
                connection,
                part_id=int(match['result_part_id']),
                result_at_ms=int(match['result_at_ms']),
                patch={'recorded_player': {'side': side, 'slot': int(slot)}},
                now=self._now(),
            )
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (int(match['session_id']),),
            )

        await self._database.write(update)
        return await self.get_match(match_id)

    async def set_player_hero(
        self, match_id: int, *, side: str, slot: int, hero_id: int
    ) -> MatchRecord:
        if side not in ('left', 'right'):
            raise ValueError('player side is invalid')
        if slot < 1 or slot > 5:
            raise ValueError('player slot is invalid')
        if hero_id < 1:
            raise ValueError('hero id is invalid')

        def update(connection: sqlite3.Connection) -> None:
            match = connection.execute(
                'SELECT session_id,result_part_id,result_at_ms '
                'FROM vainglory_matches WHERE id=?',
                (int(match_id),),
            ).fetchone()
            if match is None:
                raise VaingloryNotFound('对局不存在')
            hero = connection.execute(
                "SELECT 1 FROM vainglory_heroes WHERE id=? AND label<>''",
                (int(hero_id),),
            ).fetchone()
            if hero is None:
                raise VaingloryNotFound('英雄不存在')
            changed = connection.execute(
                'UPDATE vainglory_match_players SET hero_id=?,hero_source='
                "'manual' WHERE match_id=? AND side=? AND slot=?",
                (int(hero_id), int(match_id), side, int(slot)),
            ).rowcount
            if changed != 1:
                raise VaingloryNotFound('对局中的玩家位置不存在')
            self._merge_match_override(
                connection,
                part_id=int(match['result_part_id']),
                result_at_ms=int(match['result_at_ms']),
                patch={
                    'players': {
                        '{}:{}'.format(side, int(slot)): {'hero_id': int(hero_id)}
                    }
                },
                now=self._now(),
            )
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (int(match['session_id']),),
            )

        await self._database.write(update)
        return await self.get_match(match_id)

    async def update_match_title(self, match_id: int, title: str) -> MatchRecord:
        normalized = title.strip()
        if len(normalized) > 200:
            raise ValueError('match title is too long')
        return await self.update_match_fields(match_id, {'title': normalized})

    async def update_match_fields(
        self, match_id: int, changes: Mapping[str, Any]
    ) -> MatchRecord:
        patch = self._normalize_match_override(changes)
        if not patch:
            raise ValueError('没有需要保存的对局信息')
        now = self._now()

        def update(connection: sqlite3.Connection) -> None:
            match = connection.execute(
                'SELECT session_id,result_part_id,result_at_ms '
                'FROM vainglory_matches WHERE id=?',
                (int(match_id),),
            ).fetchone()
            if match is None:
                raise VaingloryNotFound('对局不存在')
            self._merge_match_override(
                connection,
                part_id=int(match['result_part_id']),
                result_at_ms=int(match['result_at_ms']),
                patch=patch,
                now=now,
            )
            self._apply_match_override(connection, int(match_id), patch)
            connection.execute(
                'UPDATE vainglory_publications SET needs_refresh=1 '
                'WHERE session_id=?',
                (int(match['session_id']),),
            )

        await self._database.write(update)
        return await self.get_match(match_id)

    async def update_session_title(
        self, session_id: int, title: str
    ) -> MatchSessionRecord:
        normalized = title.strip()
        if len(normalized) > 200:
            raise ValueError('session title is too long')
        count = await self._database.execute(
            'UPDATE vainglory_scan_jobs SET custom_title=? WHERE session_id=?',
            (normalized or None, int(session_id)),
        )
        if count != 1:
            raise VaingloryNotFound('直播场次不存在')
        page = await self.list_match_sessions(
            session_id=int(session_id), limit=1, offset=0
        )
        if not page.items:
            raise VaingloryNotFound('直播场次暂无对局')
        return page.items[0]

    async def update_session_anchor(
        self, session_id: int, anchor_name: str
    ) -> MatchSessionRecord:
        normalized = anchor_name.strip()
        if len(normalized) > 200:
            raise ValueError('anchor name is too long')

        def update(connection: sqlite3.Connection) -> None:
            session = connection.execute(
                'SELECT id FROM recording_sessions WHERE id=?', (int(session_id),)
            ).fetchone()
            if session is None:
                raise VaingloryNotFound('直播场次不存在')
            self._set_session_anchor(
                connection, int(session_id), normalized, self._now()
            )

        await self._database.write(update)
        page = await self.list_match_sessions(
            session_id=int(session_id), limit=1, offset=0
        )
        if not page.items:
            raise VaingloryNotFound('直播场次暂无对局')
        return page.items[0]

    async def bulk_update_sessions(
        self,
        session_ids: Sequence[int],
        *,
        anchor_name: Optional[str] = None,
        stats_included: Optional[bool] = None,
    ) -> int:
        unique_ids = tuple(dict.fromkeys(int(value) for value in session_ids))
        if not unique_ids or len(unique_ids) > 100:
            raise ValueError('session count must be between 1 and 100')
        if any(value < 1 for value in unique_ids):
            raise ValueError('session ID must be positive')
        if anchor_name is None and stats_included is None:
            raise ValueError('no session update was requested')
        normalized_anchor = None if anchor_name is None else anchor_name.strip()
        if normalized_anchor is not None and len(normalized_anchor) > 200:
            raise ValueError('anchor name is too long')

        def update(connection: sqlite3.Connection) -> int:
            placeholders = ','.join('?' for _ in unique_ids)
            rows = connection.execute(
                'SELECT id FROM recording_sessions WHERE id IN ({})'.format(
                    placeholders
                ),
                unique_ids,
            ).fetchall()
            found_ids = {int(row['id']) for row in rows}
            if found_ids != set(unique_ids):
                raise VaingloryNotFound('部分直播场次不存在')
            if normalized_anchor is not None:
                for selected_id in unique_ids:
                    self._set_session_anchor(
                        connection, selected_id, normalized_anchor, self._now()
                    )
            if stats_included is not None:
                changed = connection.execute(
                    'UPDATE vainglory_scan_jobs SET stats_included=? '
                    'WHERE session_id IN ({})'.format(placeholders),
                    (1 if stats_included else 0,) + unique_ids,
                ).rowcount
                if changed != len(unique_ids):
                    raise VaingloryNotFound('部分直播场次暂无对局索引')
            return len(unique_ids)

        return await self._database.write(update)

    async def _hydrate_matches(
        self, rows: Sequence[sqlite3.Row]
    ) -> Tuple[MatchRecord, ...]:
        match_ids = [int(row['id']) for row in rows]
        players_by_match: Dict[int, List[MatchPlayerRecord]] = {
            match_id: [] for match_id in match_ids
        }
        if match_ids:
            placeholders = ','.join('?' for _ in match_ids)
            player_rows = await self._database.fetchall(
                'SELECT player.*,COALESCE(hero.label,\'\') AS hero_label,'
                'CASE WHEN player.side=source_match.recorded_player_side '
                'AND player.slot=source_match.recorded_player_slot '
                'THEN 1 ELSE 0 END AS is_recorded_player '
                'FROM vainglory_match_players player '
                'JOIN vainglory_matches source_match '
                'ON source_match.id=player.match_id '
                'LEFT JOIN vainglory_heroes hero ON hero.id=player.hero_id '
                'WHERE player.match_id IN ({}) '
                'ORDER BY player.match_id,'
                "CASE player.side WHEN 'left' THEN 0 ELSE 1 END,player.slot".format(
                    placeholders
                ),
                tuple(match_ids),
            )
            for player in player_rows:
                players_by_match[int(player['match_id'])].append(
                    self._match_player(player)
                )
        return tuple(
            self._match_record(row, tuple(players_by_match[int(row['id'])]))
            for row in rows
        )

    async def list_heroes(self) -> Tuple[HeroRecord, ...]:
        rows = await self._database.fetchall(
            'SELECT id,label,fingerprint FROM vainglory_heroes '
            "WHERE label!='' ORDER BY label COLLATE NOCASE,id"
        )
        return tuple(
            HeroRecord(
                id=int(row['id']),
                label=str(row['label']),
                fingerprint=str(row['fingerprint']),
            )
            for row in rows
        )

    async def list_players(self) -> Tuple[PlayerRecord, ...]:
        player_rows = await self._database.fetchall(
            'SELECT id,name,origin,created_at,updated_at '
            'FROM vainglory_players ORDER BY name COLLATE NOCASE,id'
        )
        room_rows = await self._database.fetchall(
            'SELECT room.player_id,room.room_id,'
            '(SELECT known.anchor_uid FROM recording_sessions known '
            'WHERE known.room_id=room.room_id AND known.anchor_uid IS NOT NULL '
            'AND known.anchor_uid>0 ORDER BY known.started_at DESC,known.id DESC '
            'LIMIT 1) AS anchor_uid,'
            'COALESCE((SELECT known.anchor_name FROM recording_sessions known '
            "WHERE known.room_id=room.room_id AND trim(known.anchor_name)<>'' "
            'ORDER BY known.started_at DESC,known.id DESC LIMIT 1),\'\') '
            'AS anchor_name '
            'FROM vainglory_player_rooms room '
            'ORDER BY room.player_id,room.room_id'
        )
        rooms_by_player: Dict[int, List[PlayerRoomRecord]] = {
            int(row['id']): [] for row in player_rows
        }
        for row in room_rows:
            rooms_by_player.setdefault(int(row['player_id']), []).append(
                PlayerRoomRecord(
                    room_id=int(row['room_id']),
                    anchor_uid=(
                        None if row['anchor_uid'] is None else int(row['anchor_uid'])
                    ),
                    anchor_name=str(row['anchor_name'] or ''),
                )
            )
        return tuple(
            PlayerRecord(
                id=int(row['id']),
                name=str(row['name']),
                origin=cast(Literal['automatic', 'manual'], str(row['origin'])),
                rooms=tuple(rooms_by_player.get(int(row['id']), ())),
                created_at=int(row['created_at']),
                updated_at=int(row['updated_at']),
            )
            for row in player_rows
        )

    async def get_player(self, player_id: int) -> PlayerRecord:
        selected = next(
            (player for player in await self.list_players() if player.id == player_id),
            None,
        )
        if selected is None:
            raise VaingloryNotFound('玩家不存在')
        return selected

    async def create_player(self, name: str) -> PlayerRecord:
        normalized = self._normalize_player_display_name(name)
        now = self._now()

        def create(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                'INSERT INTO vainglory_players('
                'name,origin,created_at,updated_at) VALUES(?,\'manual\',?,?)',
                (normalized, now, now),
            )
            player_id = int(cursor.lastrowid)
            self._insert_player_alias(
                connection, player_id=player_id, alias=normalized, now=now
            )
            return player_id

        return await self.get_player(await self._database.write(create))

    async def ensure_players_for_rooms(
        self, rooms: Sequence[Tuple[int, str]]
    ) -> Tuple[PlayerRecord, ...]:
        normalized_rooms = tuple(
            (int(room_id), self._normalize_player_display_name(name))
            for room_id, name in rooms
            if int(room_id) > 0
        )
        now = self._now()

        def ensure(connection: sqlite3.Connection) -> None:
            seen: Set[int] = set()
            for room_id, name in normalized_rooms:
                if room_id in seen:
                    continue
                seen.add(room_id)
                if (
                    connection.execute(
                        'SELECT 1 FROM vainglory_player_rooms WHERE room_id=?',
                        (room_id,),
                    ).fetchone()
                    is not None
                ):
                    continue
                if (
                    connection.execute(
                        'SELECT 1 FROM vainglory_player_room_suppressions '
                        'WHERE room_id=?',
                        (room_id,),
                    ).fetchone()
                    is not None
                ):
                    continue
                candidates = self._player_ids_for_anchor_name(connection, name)
                if len(candidates) > 1:
                    continue
                if candidates:
                    player_id = candidates[0]
                else:
                    cursor = connection.execute(
                        'INSERT INTO vainglory_players('
                        'name,origin,created_at,updated_at) '
                        "VALUES(?,'automatic',?,?)",
                        (name, now, now),
                    )
                    player_id = int(cursor.lastrowid)
                self._insert_player_alias(
                    connection, player_id=player_id, alias=name, now=now
                )
                connection.execute(
                    'INSERT INTO vainglory_player_rooms('
                    'room_id,player_id,created_at,updated_at) VALUES(?,?,?,?)',
                    (room_id, player_id, now, now),
                )

        await self._database.write(ensure)
        return await self.list_players()

    async def rename_player(self, player_id: int, name: str) -> PlayerRecord:
        normalized = self._normalize_player_display_name(name)
        selected_player_id = int(player_id)
        now = self._now()

        def rename(connection: sqlite3.Connection) -> int:
            current = connection.execute(
                'SELECT name FROM vainglory_players WHERE id=?', (selected_player_id,)
            ).fetchone()
            if current is None:
                return 0
            self._insert_player_alias(
                connection,
                player_id=selected_player_id,
                alias=str(current['name']),
                now=now,
            )
            self._insert_player_alias(
                connection, player_id=selected_player_id, alias=normalized, now=now
            )
            return connection.execute(
                'UPDATE vainglory_players SET name=?,updated_at=? WHERE id=?',
                (normalized, now, selected_player_id),
            ).rowcount

        count = await self._database.write(rename)
        if count != 1:
            raise VaingloryNotFound('玩家不存在')
        return await self.get_player(selected_player_id)

    async def bind_player_alias(self, player_id: int, alias: str) -> PlayerRecord:
        selected_player_id = int(player_id)
        normalized = self._normalize_player_display_name(alias)
        now = self._now()

        def bind(connection: sqlite3.Connection) -> None:
            if (
                connection.execute(
                    'SELECT 1 FROM vainglory_players WHERE id=?', (selected_player_id,)
                ).fetchone()
                is None
            ):
                raise VaingloryNotFound('玩家不存在')
            previous_player_ids = {
                int(row['player_id'])
                for row in connection.execute(
                    'SELECT DISTINCT direct.player_id '
                    'FROM recording_sessions session '
                    'JOIN vainglory_player_sessions direct '
                    'ON direct.session_id=session.id '
                    'WHERE lower(trim(session.anchor_name))=lower(?) '
                    'AND direct.player_id<>?',
                    (normalized, selected_player_id),
                ).fetchall()
            }
            connection.execute(
                'INSERT INTO vainglory_player_aliases('
                'alias,player_id,created_at,updated_at) VALUES(?,?,?,?) '
                'ON CONFLICT(alias) DO UPDATE SET '
                'player_id=excluded.player_id,updated_at=excluded.updated_at',
                (normalized, selected_player_id, now, now),
            )
            connection.execute(
                'UPDATE vainglory_player_sessions SET player_id=?,updated_at=? '
                'WHERE session_id IN ('
                'SELECT id FROM recording_sessions '
                'WHERE lower(trim(anchor_name))=lower(?))',
                (selected_player_id, now, normalized),
            )
            connection.execute(
                'UPDATE vainglory_players SET updated_at=? WHERE id=?',
                (now, selected_player_id),
            )
            for previous_player_id in previous_player_ids:
                connection.execute(
                    "DELETE FROM vainglory_players WHERE id=? AND origin='automatic' "
                    'AND NOT EXISTS(SELECT 1 FROM vainglory_player_rooms room '
                    'WHERE room.player_id=vainglory_players.id) '
                    'AND NOT EXISTS(SELECT 1 FROM vainglory_player_sessions direct '
                    'WHERE direct.player_id=vainglory_players.id)',
                    (previous_player_id,),
                )

        await self._database.write(bind)
        return await self.get_player(selected_player_id)

    async def reconcile_recorded_session_identity(
        self,
        session_id: int,
        *,
        title: str,
        description: str,
        excluded_anchor_uid: Optional[int] = None,
        excluded_anchor_name: str = '',
    ) -> Tuple[int, Optional[int], str]:
        """Apply a historical archive's best known room and player identity."""
        selected_session_id = int(session_id)
        now = self._now()

        def reconcile(connection: sqlite3.Connection) -> Tuple[int, Optional[int], str]:
            session = connection.execute(
                'SELECT room_id,anchor_uid,anchor_name FROM recording_sessions '
                'WHERE id=?',
                (selected_session_id,),
            ).fetchone()
            if session is None:
                raise VaingloryNotFound('历史稿件对应的录播不存在')
            room_id, anchor_uid, anchor_name = infer_recorded_anchor(
                connection,
                title,
                description,
                excluded_anchor_uids=(
                    () if excluded_anchor_uid is None else (excluded_anchor_uid,)
                ),
                excluded_anchor_names=(excluded_anchor_name,),
            )
            if room_id <= 0 and anchor_uid is None and not anchor_name:
                return room_id, anchor_uid, anchor_name

            current_room_id = int(session['room_id'])
            current_room_is_bound = (
                current_room_id > 0
                and connection.execute(
                    'SELECT 1 FROM vainglory_player_rooms WHERE room_id=?',
                    (current_room_id,),
                ).fetchone()
                is not None
            )
            target_room_id = (
                room_id
                if room_id > 0
                else (current_room_id if current_room_is_bound else 0)
            )
            target_anchor_uid = (
                anchor_uid
                if anchor_uid is not None
                else (
                    None
                    if session['anchor_uid'] is None
                    else int(session['anchor_uid'])
                )
            )
            target_anchor_name = anchor_name or str(session['anchor_name'] or '')
            connection.execute(
                'UPDATE recording_sessions SET room_id=?,anchor_uid=?,'
                'anchor_name=? WHERE id=?',
                (
                    target_room_id,
                    target_anchor_uid,
                    target_anchor_name[:80],
                    selected_session_id,
                ),
            )

            previous_player_ids = {
                int(row['player_id'])
                for row in connection.execute(
                    'SELECT player_id FROM vainglory_player_sessions '
                    'WHERE session_id=?',
                    (selected_session_id,),
                ).fetchall()
            }
            player_id: Optional[int] = None
            if target_room_id > 0:
                bound = connection.execute(
                    'SELECT player_id FROM vainglory_player_rooms WHERE room_id=?',
                    (target_room_id,),
                ).fetchone()
                if bound is not None:
                    player_id = int(bound['player_id'])
            if player_id is None and anchor_name:
                candidates = self._player_ids_for_anchor_name(connection, anchor_name)
                if len(candidates) == 1:
                    player_id = candidates[0]
            if player_id is None and anchor_uid is not None:
                uid_candidates = connection.execute(
                    'SELECT DISTINCT player_id FROM ('
                    'SELECT room.player_id FROM recording_sessions known '
                    'JOIN vainglory_player_rooms room ON room.room_id=known.room_id '
                    'WHERE known.anchor_uid=? AND known.id<>? '
                    'UNION '
                    'SELECT direct.player_id FROM recording_sessions known '
                    'JOIN vainglory_player_sessions direct '
                    'ON direct.session_id=known.id '
                    'WHERE known.anchor_uid=? AND known.id<>?) candidate '
                    'ORDER BY player_id',
                    (anchor_uid, selected_session_id, anchor_uid, selected_session_id),
                ).fetchall()
                if len(uid_candidates) == 1:
                    player_id = int(uid_candidates[0]['player_id'])
            if player_id is None:
                return room_id, anchor_uid, anchor_name

            if target_room_id > 0:
                connection.execute(
                    'INSERT INTO vainglory_player_rooms('
                    'room_id,player_id,created_at,updated_at) VALUES(?,?,?,?) '
                    'ON CONFLICT(room_id) DO UPDATE SET '
                    'player_id=excluded.player_id,updated_at=excluded.updated_at',
                    (target_room_id, player_id, now, now),
                )
                connection.execute(
                    'DELETE FROM vainglory_player_sessions WHERE session_id=?',
                    (selected_session_id,),
                )
            else:
                connection.execute(
                    'INSERT INTO vainglory_player_sessions('
                    'session_id,player_id,created_at,updated_at) VALUES(?,?,?,?) '
                    'ON CONFLICT(session_id) DO UPDATE SET '
                    'player_id=excluded.player_id,updated_at=excluded.updated_at',
                    (selected_session_id, player_id, now, now),
                )
            if anchor_name:
                self._insert_player_alias(
                    connection, player_id=player_id, alias=anchor_name, now=now
                )
            for previous_player_id in previous_player_ids - {player_id}:
                connection.execute(
                    "DELETE FROM vainglory_players WHERE id=? AND origin='automatic' "
                    'AND NOT EXISTS(SELECT 1 FROM vainglory_player_rooms room '
                    'WHERE room.player_id=vainglory_players.id) '
                    'AND NOT EXISTS(SELECT 1 FROM vainglory_player_sessions direct '
                    'WHERE direct.player_id=vainglory_players.id)',
                    (previous_player_id,),
                )
            return room_id, anchor_uid, anchor_name

        return await self._database.write(reconcile)

    async def bind_player_room(self, player_id: int, room_id: int) -> PlayerRecord:
        selected_player_id = int(player_id)
        selected_room_id = int(room_id)
        if selected_room_id <= 0:
            raise ValueError('room ID must be positive')
        now = self._now()

        def bind(connection: sqlite3.Connection) -> None:
            player = connection.execute(
                'SELECT id FROM vainglory_players WHERE id=?', (selected_player_id,)
            ).fetchone()
            if player is None:
                raise VaingloryNotFound('玩家不存在')
            previous = connection.execute(
                'SELECT player_id FROM vainglory_player_rooms WHERE room_id=?',
                (selected_room_id,),
            ).fetchone()
            previous_player_id = (
                None if previous is None else int(previous['player_id'])
            )
            connection.execute(
                'INSERT INTO vainglory_player_rooms('
                'room_id,player_id,created_at,updated_at) VALUES(?,?,?,?) '
                'ON CONFLICT(room_id) DO UPDATE SET '
                'player_id=excluded.player_id,updated_at=excluded.updated_at',
                (selected_room_id, selected_player_id, now, now),
            )
            connection.execute(
                'DELETE FROM vainglory_player_room_suppressions WHERE room_id=?',
                (selected_room_id,),
            )
            connection.execute(
                'UPDATE vainglory_players SET updated_at=? WHERE id=?',
                (now, selected_player_id),
            )
            known_names = connection.execute(
                'SELECT DISTINCT trim(anchor_name) AS anchor_name '
                'FROM recording_sessions WHERE room_id=? '
                "AND trim(anchor_name)<>''",
                (selected_room_id,),
            ).fetchall()
            for known_name in known_names:
                alias = str(known_name['anchor_name'])
                if (
                    connection.execute(
                        'SELECT 1 FROM vainglory_player_aliases WHERE alias=?', (alias,)
                    ).fetchone()
                    is None
                ):
                    self._insert_player_alias(
                        connection, player_id=selected_player_id, alias=alias, now=now
                    )
            if previous_player_id not in (None, selected_player_id):
                connection.execute(
                    'UPDATE vainglory_players SET updated_at=? WHERE id=?',
                    (now, previous_player_id),
                )
                connection.execute(
                    "DELETE FROM vainglory_players WHERE id=? AND origin='automatic' "
                    'AND NOT EXISTS(SELECT 1 FROM vainglory_player_rooms room '
                    'WHERE room.player_id=vainglory_players.id) '
                    'AND NOT EXISTS(SELECT 1 FROM vainglory_player_sessions '
                    'session_player WHERE session_player.player_id='
                    'vainglory_players.id)',
                    (previous_player_id,),
                )

        await self._database.write(bind)
        return await self.get_player(selected_player_id)

    async def unbind_player_room(self, player_id: int, room_id: int) -> PlayerRecord:
        selected_player_id = int(player_id)
        selected_room_id = int(room_id)
        if selected_room_id <= 0:
            raise ValueError('room ID must be positive')
        now = self._now()

        def unbind(connection: sqlite3.Connection) -> None:
            player = connection.execute(
                'SELECT id FROM vainglory_players WHERE id=?', (selected_player_id,)
            ).fetchone()
            if player is None:
                raise VaingloryNotFound('玩家不存在')
            changed = connection.execute(
                'DELETE FROM vainglory_player_rooms ' 'WHERE room_id=? AND player_id=?',
                (selected_room_id, selected_player_id),
            ).rowcount
            if changed != 1:
                raise VaingloryNotFound('直播间未绑定到该玩家')
            connection.execute(
                'INSERT INTO vainglory_player_room_suppressions(room_id,created_at) '
                'VALUES(?,?) ON CONFLICT(room_id) DO NOTHING',
                (selected_room_id, now),
            )
            connection.execute(
                'UPDATE vainglory_players SET updated_at=? WHERE id=?',
                (now, selected_player_id),
            )

        await self._database.write(unbind)
        return await self.get_player(selected_player_id)

    async def delete_player(self, player_id: int) -> None:
        selected_player_id = int(player_id)
        now = self._now()

        def delete(connection: sqlite3.Connection) -> None:
            player = connection.execute(
                'SELECT id FROM vainglory_players WHERE id=?', (selected_player_id,)
            ).fetchone()
            if player is None:
                raise VaingloryNotFound('玩家不存在')
            room_rows = connection.execute(
                'SELECT room_id FROM vainglory_player_rooms WHERE player_id=?',
                (selected_player_id,),
            ).fetchall()
            connection.executemany(
                'INSERT INTO vainglory_player_room_suppressions(room_id,created_at) '
                'VALUES(?,?) ON CONFLICT(room_id) DO NOTHING',
                ((int(row['room_id']), now) for row in room_rows),
            )
            connection.execute(
                'DELETE FROM vainglory_players WHERE id=?', (selected_player_id,)
            )

        await self._database.write(delete)

    async def list_player_stats(self) -> Tuple[PlayerStatsRecord, ...]:
        players = await self.list_players()
        grouped = {
            player.id: _PlayerStatsAccumulator(player=player) for player in players
        }
        for row in await self._player_match_stats_rows():
            player_id = int(row['player_id'])
            value = grouped.get(player_id)
            if value is None:
                continue
            winner_color = str(row['winner_color'])
            value.session_ids.add(int(row['session_id']))
            value.outcomes.add(winner_color)
            game_mode = str(row['game_mode'])
            value.modes.setdefault(game_mode, _OutcomeAccumulator()).add(winner_color)
            if row['hero_id'] is None:
                continue
            hero_id = int(row['hero_id'])
            hero_label = str(row['hero_label'] or '')
            hero_value = value.heroes.get(hero_id)
            if hero_value is None:
                hero_value = (hero_label, _OutcomeAccumulator())
                value.heroes[hero_id] = hero_value
            hero_value[1].add(winner_color)

        mode_order = {'3v3': 0, 'aram': 1, '5v5': 2, 'other': 3, 'unknown': 4}
        result: List[PlayerStatsRecord] = []
        for value in grouped.values():
            modes = tuple(
                GameModeStatsRecord(
                    game_mode=game_mode,
                    match_count=outcomes.match_count,
                    win_count=outcomes.win_count,
                    loss_count=outcomes.loss_count,
                    unknown_count=outcomes.unknown_count,
                    win_rate=outcomes.win_rate,
                )
                for game_mode, outcomes in sorted(
                    value.modes.items(),
                    key=lambda item: (
                        mode_order.get(item[0], len(mode_order)),
                        item[0],
                    ),
                )
            )
            heroes = tuple(
                HeroStatsRecord(
                    hero_id=hero_id,
                    hero_label=hero_label,
                    player_count=1,
                    match_count=outcomes.match_count,
                    win_count=outcomes.win_count,
                    loss_count=outcomes.loss_count,
                    unknown_count=outcomes.unknown_count,
                    win_rate=outcomes.win_rate,
                )
                for hero_id, (hero_label, outcomes) in sorted(
                    value.heroes.items(),
                    key=lambda item: (
                        -item[1][1].match_count,
                        -item[1][1].win_rate,
                        item[1][0],
                        item[0],
                    ),
                )
            )
            result.append(
                PlayerStatsRecord(
                    player_id=value.player.id,
                    player_name=value.player.name,
                    rooms=value.player.rooms,
                    session_count=len(value.session_ids),
                    match_count=value.outcomes.match_count,
                    win_count=value.outcomes.win_count,
                    loss_count=value.outcomes.loss_count,
                    unknown_count=value.outcomes.unknown_count,
                    win_rate=value.outcomes.win_rate,
                    modes=modes,
                    heroes=heroes,
                )
            )
        return tuple(
            sorted(result, key=lambda item: (-item.match_count, item.player_name))
        )

    async def list_hero_stats(
        self, *, game_mode: str = ''
    ) -> Tuple[HeroStatsRecord, ...]:
        if game_mode not in ('', '3v3', '5v5', 'aram', 'other', 'unknown'):
            raise ValueError('game mode is invalid')
        outcomes_by_hero: Dict[int, _OutcomeAccumulator] = {}
        labels_by_hero: Dict[int, str] = {}
        players_by_hero: Dict[int, Set[int]] = {}
        for row in await self._player_match_stats_rows(game_mode=game_mode):
            if row['hero_id'] is None:
                continue
            hero_id = int(row['hero_id'])
            labels_by_hero[hero_id] = str(row['hero_label'] or '')
            players_by_hero.setdefault(hero_id, set()).add(int(row['player_id']))
            outcomes_by_hero.setdefault(hero_id, _OutcomeAccumulator()).add(
                str(row['winner_color'])
            )
        result = tuple(
            HeroStatsRecord(
                hero_id=hero_id,
                hero_label=labels_by_hero[hero_id],
                player_count=len(players_by_hero[hero_id]),
                match_count=outcomes.match_count,
                win_count=outcomes.win_count,
                loss_count=outcomes.loss_count,
                unknown_count=outcomes.unknown_count,
                win_rate=outcomes.win_rate,
            )
            for hero_id, outcomes in outcomes_by_hero.items()
        )
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    -item.win_rate,
                    -item.match_count,
                    item.hero_label,
                    item.hero_id,
                ),
            )
        )

    async def list_anchor_stats(self) -> Tuple[AnchorStatsRecord, ...]:
        rows = await self._database.fetchall(
            'SELECT session.id AS session_id,session.room_id,'
            'session.anchor_uid,session.anchor_name,'
            "CASE match.winner_side WHEN 'left' THEN match.left_color "
            "WHEN 'right' THEN match.right_color ELSE 'unknown' END "
            'AS winner_color '
            'FROM vainglory_matches match '
            'JOIN recording_sessions session ON session.id=match.session_id '
            'JOIN vainglory_scan_jobs scan ON scan.session_id=session.id '
            'WHERE scan.stats_included=1 AND match.stats_eligible=1 '
            'ORDER BY session.started_at,session.id,match.id'
        )
        grouped: Dict[str, _AnchorStatsAccumulator] = {}
        for row in rows:
            anchor_uid = (
                None
                if row['anchor_uid'] is None or int(row['anchor_uid']) <= 0
                else int(row['anchor_uid'])
            )
            anchor_name = str(row['anchor_name']).strip()
            key = (
                'uid:{}'.format(anchor_uid)
                if anchor_uid is not None
                else 'name:{}'.format(anchor_name.casefold() or 'unknown')
            )
            value = grouped.get(key)
            if value is None:
                value = _AnchorStatsAccumulator(
                    anchor_uid=anchor_uid,
                    anchor_name=anchor_name or '未知主播',
                    room_id=int(row['room_id']),
                    session_ids=set(),
                )
                grouped[key] = value
            elif anchor_name:
                value.anchor_name = anchor_name
                value.room_id = int(row['room_id'])
            value.session_ids.add(int(row['session_id']))
            value.match_count += 1
            winner_color = str(row['winner_color'])
            if winner_color == 'teal':
                value.win_count += 1
            elif winner_color == 'orange':
                value.loss_count += 1
            else:
                value.unknown_count += 1
        result = tuple(
            AnchorStatsRecord(
                anchor_uid=value.anchor_uid,
                anchor_name=value.anchor_name,
                room_id=value.room_id,
                session_count=len(value.session_ids),
                match_count=value.match_count,
                win_count=value.win_count,
                loss_count=value.loss_count,
                unknown_count=value.unknown_count,
                win_rate=(
                    0.0
                    if value.match_count == 0
                    else value.win_count / value.match_count
                ),
            )
            for value in grouped.values()
        )
        return tuple(
            sorted(result, key=lambda item: (-item.match_count, item.anchor_name))
        )

    async def label_hero(self, hero_id: int, label: str) -> HeroRecord:
        normalized = label.strip()
        if len(normalized) > 80:
            raise ValueError('hero label is too long')
        count = await self._database.execute(
            'UPDATE vainglory_heroes SET label=?,updated_at=? WHERE id=?',
            (normalized, self._now(), int(hero_id)),
        )
        if count != 1:
            raise VaingloryNotFound('英雄不存在')
        row = await self._database.fetchone(
            'SELECT id,label,fingerprint FROM vainglory_heroes WHERE id=?',
            (int(hero_id),),
        )
        assert row is not None
        return HeroRecord(
            id=int(row['id']),
            label=str(row['label']),
            fingerprint=str(row['fingerprint']),
        )

    async def hero_thumbnail(self, hero_id: int) -> Optional[bytes]:
        row = await self._database.fetchone(
            'SELECT thumbnail_png FROM vainglory_heroes WHERE id=?', (int(hero_id),)
        )
        return None if row is None else bytes(row['thumbnail_png'])

    @staticmethod
    def _result_frame_relative_path(
        *, session_id: int, part_id: int, result_at_ms: int, content: bytes
    ) -> str:
        digest = hashlib.sha256(content).hexdigest()[:16]
        return 'session-{}/part-{}-{}-{}.png'.format(
            session_id, part_id, result_at_ms, digest
        )

    def _resolve_result_frame_path(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute() or '..' in candidate.parts:
            raise ValueError('result frame path must stay inside its storage directory')
        resolved = (self._result_frame_root / candidate).resolve()
        try:
            resolved.relative_to(self._result_frame_root)
        except ValueError as error:
            raise ValueError(
                'result frame path must stay inside its storage directory'
            ) from error
        return resolved

    def _write_result_frame(self, destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self._result_frame_root, 0o700)
        os.chmod(destination.parent, 0o700)
        if destination.is_file():
            return
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='wb',
                prefix='.result-frame-',
                suffix='.tmp',
                dir=str(destination.parent),
                delete=False,
            ) as output:
                temporary_path = Path(output.name)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(str(temporary_path), str(destination))
            os.chmod(destination, 0o600)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _training_candidate_relative_path(
        *, session_id: int, part_id: int, at_ms: int, content: bytes
    ) -> str:
        del session_id, part_id, at_ms
        digest = hashlib.sha256(content).hexdigest()
        return 'objects/{}/{}.jpg'.format(digest[:2], digest)

    @staticmethod
    def _training_candidate_metadata_relative_path(
        *, session_id: int, part_id: int, at_ms: int, content: bytes
    ) -> str:
        digest = hashlib.sha256(content).hexdigest()[:16]
        return 'items/session-{}/part-{}/{:012d}-{}.json'.format(
            session_id, part_id, at_ms, digest
        )

    def _resolve_training_candidate_path(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute() or '..' in candidate.parts:
            raise ValueError(
                'training candidate path must stay inside its storage directory'
            )
        resolved = (self._training_candidate_root / candidate).resolve()
        try:
            resolved.relative_to(self._training_candidate_root)
        except ValueError as error:
            raise ValueError(
                'training candidate path must stay inside its storage directory'
            ) from error
        return resolved

    def _write_training_candidate(
        self,
        destination: Path,
        content: bytes,
        metadata: Mapping[str, Any],
        metadata_destination: Optional[Path] = None,
    ) -> None:
        if not content:
            raise ValueError('training candidate image must not be empty')
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self._training_candidate_root, 0o700)
        os.chmod(destination.parent, 0o700)
        if not destination.is_file():
            self._write_training_candidate_file(
                destination, content, prefix='.training-image-'
            )
        sidecar = (
            destination.with_suffix('.json')
            if metadata_destination is None
            else metadata_destination
        )
        sidecar.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(sidecar.parent, 0o700)
        payload = json.dumps(
            metadata, ensure_ascii=False, separators=(',', ':'), sort_keys=True
        ).encode('utf8')
        self._write_training_candidate_file(
            sidecar, payload, prefix='.training-metadata-'
        )

    @staticmethod
    def _write_training_candidate_file(
        destination: Path, content: bytes, *, prefix: str
    ) -> None:
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='wb',
                prefix=prefix,
                suffix='.tmp',
                dir=str(destination.parent),
                delete=False,
            ) as output:
                temporary_path = Path(output.name)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(str(temporary_path), str(destination))
            os.chmod(destination, 0o600)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _remove_result_frame_files(
        self, relative_paths: Sequence[str], *, keep: Sequence[Path] = ()
    ) -> None:
        preserved = {path.resolve() for path in keep}
        for relative_path in relative_paths:
            try:
                path = self._resolve_result_frame_path(relative_path)
            except ValueError:
                logger.warning(
                    'Skipped invalid Vainglory result frame path: path={}',
                    relative_path,
                )
                continue
            if path in preserved:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                logger.warning(
                    'Failed to remove obsolete Vainglory result frame: '
                    'path={} error={}',
                    path,
                    error,
                )

    def _resolve_hero(
        self,
        connection: sqlite3.Connection,
        hero: AnalyzedHero,
        existing: List[Tuple[int, str, str]],
        now: int,
    ) -> Optional[int]:
        del connection, now
        label = hero.label or identify_builtin_hero(hero.fingerprint)
        if not label:
            return None
        return next(
            (
                hero_id
                for hero_id, _, existing_label in existing
                if existing_label.casefold() == label.casefold()
            ),
            None,
        )

    @staticmethod
    def _consolidate_heroes(connection: sqlite3.Connection, now: int) -> int:
        rows = connection.execute(
            'SELECT id,label,fingerprint,thumbnail_png,updated_at '
            'FROM vainglory_heroes ORDER BY id'
        ).fetchall()
        canonical_by_label: Dict[str, Tuple[int, int]] = {}
        removed = 0
        for row in rows:
            hero_id = int(row['id'])
            label = str(row['label'])
            if not label:
                continue
            normalized = label.casefold()
            canonical = canonical_by_label.setdefault(
                normalized, (hero_id, int(row['updated_at']))
            )
            if canonical[0] == hero_id:
                continue
            connection.execute(
                'UPDATE vainglory_match_players SET hero_id=? WHERE hero_id=?',
                (canonical[0], hero_id),
            )
            removed += connection.execute(
                'DELETE FROM vainglory_heroes WHERE id=?', (hero_id,)
            ).rowcount
            if int(row['updated_at']) >= canonical[1]:
                connection.execute(
                    'UPDATE vainglory_heroes SET fingerprint=?,thumbnail_png=?,'
                    'updated_at=? WHERE id=?',
                    (
                        str(row['fingerprint']),
                        bytes(row['thumbnail_png']),
                        now,
                        canonical[0],
                    ),
                )
                canonical_by_label[normalized] = (canonical[0], now)
            connection.execute(
                'UPDATE vainglory_heroes SET updated_at=? WHERE id=?',
                (now, canonical[0]),
            )
        removed += connection.execute(
            "DELETE FROM vainglory_heroes WHERE label='' AND NOT EXISTS("
            'SELECT 1 FROM vainglory_match_players player '
            'WHERE player.hero_id=vainglory_heroes.id)'
        ).rowcount
        return removed

    def _ensure_scan_job(
        self, connection: sqlite3.Connection, session_id: int, now: int
    ) -> None:
        connection.execute(
            'INSERT OR IGNORE INTO vainglory_scan_jobs('
            'session_id,state,progress,algorithm_version,match_count,error,'
            'requested_at,started_at,completed_at,updated_at) '
            "VALUES(?,'pending',0,?,0,NULL,?,NULL,NULL,?)",
            (session_id, self.ALGORITHM_VERSION, now, now),
        )

    def _mark_part_ignored(
        self,
        connection: sqlite3.Connection,
        part_id: int,
        reason: str,
        now: int,
        *,
        require_analyzing: bool = False,
    ) -> List[str]:
        job = connection.execute(
            'SELECT session_id,state FROM vainglory_part_jobs WHERE part_id=?',
            (int(part_id),),
        ).fetchone()
        if job is None:
            return []
        if require_analyzing and str(job['state']) != 'analyzing':
            raise VaingloryConflict('分析任务当前不能标记为损坏分 P')
        obsolete_frame_paths = [
            str(row['result_frame_path'])
            for row in connection.execute(
                'SELECT result_frame_path FROM vainglory_matches '
                'WHERE result_part_id=? AND result_frame_path IS NOT NULL',
                (int(part_id),),
            ).fetchall()
        ]
        connection.execute(
            'DELETE FROM vainglory_matches WHERE result_part_id=?', (int(part_id),)
        )
        connection.execute(
            'DELETE FROM vainglory_ocr_jobs WHERE part_id=?', (int(part_id),)
        )
        connection.execute(
            "UPDATE vainglory_part_jobs SET state='ready',progress=1,"
            'match_count=0,candidate_count=0,error=NULL,ignored_reason=?,'
            'completed_at=?,updated_at=? WHERE part_id=?',
            (reason, now, now, int(part_id)),
        )
        connection.execute(
            "UPDATE vainglory_archive_parts SET state='ready',progress=1,"
            'error=NULL,updated_at=? WHERE recording_part_id=?',
            (now, int(part_id)),
        )
        session_id = int(job['session_id'])
        connection.execute(
            'UPDATE vainglory_publications SET needs_refresh=1,updated_at=? '
            'WHERE session_id=?',
            (now, session_id),
        )
        self._refresh_session_job(connection, session_id, now)
        return obsolete_frame_paths

    def _refresh_session_job(
        self, connection: sqlite3.Connection, session_id: int, now: int
    ) -> None:
        refresh_session_scan_job(connection, session_id, now)

    @staticmethod
    def _existing_heroes(connection: sqlite3.Connection) -> List[Tuple[int, str, str]]:
        return [
            (int(row['id']), str(row['fingerprint']), str(row['label']))
            for row in connection.execute(
                'SELECT id,fingerprint,label FROM vainglory_heroes ORDER BY id'
            ).fetchall()
        ]

    @staticmethod
    def _scan_job(row: sqlite3.Row) -> ScanJob:
        ignored_reasons = str(row['ignored_part_reasons'] or '')
        return ScanJob(
            session_id=int(row['session_id']),
            state=str(row['state']),
            progress=float(row['progress']),
            algorithm_version=int(row['algorithm_version']),
            match_count=int(row['match_count']),
            error=None if row['error'] is None else str(row['error']),
            requested_at=int(row['requested_at']),
            started_at=(None if row['started_at'] is None else int(row['started_at'])),
            completed_at=(
                None if row['completed_at'] is None else int(row['completed_at'])
            ),
            updated_at=int(row['updated_at']),
            part_count=int(row['part_count'] or 0),
            original_part_count=int(row['original_part_count'] or 0),
            ignored_part_count=int(row['ignored_part_count'] or 0),
            ignored_part_reasons=tuple(
                reason for reason in ignored_reasons.split('\n') if reason
            ),
        )

    async def _player_match_stats_rows(
        self, *, game_mode: str = ''
    ) -> List[sqlite3.Row]:
        conditions = ['scan.stats_included=1', 'match.stats_eligible=1']
        parameters: List[object] = []
        if game_mode:
            conditions.append('match.game_mode=?')
            parameters.append(game_mode)
        return await self._database.fetchall(
            'SELECT COALESCE(room.player_id,direct.player_id) AS player_id,'
            'session.id AS session_id,match.game_mode,'
            "CASE match.winner_side WHEN 'left' THEN match.left_color "
            "WHEN 'right' THEN match.right_color ELSE 'unknown' END "
            'AS winner_color,hero.id AS hero_id,'
            "COALESCE(hero.label,'') AS hero_label "
            'FROM vainglory_matches match '
            'JOIN recording_sessions session ON session.id=match.session_id '
            'JOIN vainglory_scan_jobs scan ON scan.session_id=session.id '
            'LEFT JOIN vainglory_player_rooms room '
            'ON room.room_id=session.room_id AND session.room_id>0 '
            'LEFT JOIN vainglory_player_sessions direct '
            'ON direct.session_id=session.id '
            'LEFT JOIN vainglory_match_players recorded '
            'ON recorded.match_id=match.id '
            'AND recorded.side=match.recorded_player_side '
            'AND recorded.slot=match.recorded_player_slot '
            'LEFT JOIN vainglory_heroes hero ON hero.id=recorded.hero_id '
            'WHERE COALESCE(room.player_id,direct.player_id) IS NOT NULL AND '
            + ' AND '.join(conditions)
            + ' ORDER BY session.started_at,session.id,match.id',
            tuple(parameters),
        )

    @staticmethod
    def _is_reliable_anchor_name(anchor_name: str) -> bool:
        normalized = anchor_name.strip().casefold()
        if normalized in ('', '未知主播', '账号已注销', 'unknown'):
            return False
        if normalized.startswith('玩家 ') and normalized[3:].isdigit():
            return False
        return True

    @staticmethod
    def _player_ids_for_anchor_name(
        connection: sqlite3.Connection, anchor_name: str
    ) -> Tuple[int, ...]:
        if not VaingloryRepository._is_reliable_anchor_name(anchor_name):
            return ()
        explicit = connection.execute(
            'SELECT player_id FROM vainglory_player_aliases '
            'WHERE lower(alias)=lower(?)',
            (anchor_name[:80],),
        ).fetchall()
        if explicit:
            return tuple(int(row['player_id']) for row in explicit)
        rows = connection.execute(
            'SELECT player_id FROM ('
            'SELECT player.id AS player_id FROM vainglory_players player '
            'WHERE lower(player.name)=lower(?) '
            'UNION '
            'SELECT room.player_id FROM recording_sessions known '
            'JOIN vainglory_player_rooms room ON room.room_id=known.room_id '
            'WHERE known.room_id>0 AND lower(trim(known.anchor_name))=lower(?) '
            'UNION '
            'SELECT direct.player_id FROM recording_sessions known '
            'JOIN vainglory_player_sessions direct ON direct.session_id=known.id '
            'WHERE lower(trim(known.anchor_name))=lower(?)'
            ') candidate ORDER BY player_id',
            (anchor_name[:80], anchor_name, anchor_name),
        ).fetchall()
        return tuple(int(row['player_id']) for row in rows)

    @staticmethod
    def _ensure_session_player(
        connection: sqlite3.Connection,
        session_id: int,
        now: int,
        *,
        allow_roomless_create: bool = False,
    ) -> None:
        session = connection.execute(
            'SELECT id,room_id,anchor_uid,anchor_name '
            'FROM recording_sessions WHERE id=?',
            (int(session_id),),
        ).fetchone()
        if session is None:
            return
        stored_room_id = int(session['room_id'])
        room_id = 0 if stored_room_id == _EXTERNAL_IMPORT_ROOM_ID else stored_room_id
        anchor_uid = (
            None
            if session['anchor_uid'] is None or int(session['anchor_uid']) <= 0
            else int(session['anchor_uid'])
        )
        anchor_name = str(session['anchor_name'] or '').strip()
        if room_id > 0:
            existing = connection.execute(
                'SELECT player_id FROM vainglory_player_rooms WHERE room_id=?',
                (room_id,),
            ).fetchone()
            if existing is not None:
                return
        else:
            existing = connection.execute(
                'SELECT player_id FROM vainglory_player_sessions WHERE session_id=?',
                (int(session_id),),
            ).fetchone()
            if existing is not None:
                return
        reliable_anchor_name = VaingloryRepository._is_reliable_anchor_name(anchor_name)
        if anchor_uid is None and not reliable_anchor_name:
            return

        player_id: Optional[int] = None
        if anchor_uid is not None:
            known = connection.execute(
                'SELECT DISTINCT candidate.player_id FROM ('
                'SELECT room.player_id,known.started_at,known.id '
                'FROM vainglory_player_rooms room '
                'JOIN recording_sessions known ON known.room_id=room.room_id '
                'WHERE known.anchor_uid=? '
                'UNION ALL '
                'SELECT direct.player_id,known.started_at,known.id '
                'FROM vainglory_player_sessions direct '
                'JOIN recording_sessions known ON known.id=direct.session_id '
                'WHERE known.anchor_uid=?'
                ') candidate ORDER BY candidate.player_id',
                (anchor_uid, anchor_uid),
            ).fetchall()
            if len(known) > 1:
                return
            if known:
                player_id = int(known[0]['player_id'])
        if player_id is None and reliable_anchor_name:
            known_player_ids = VaingloryRepository._player_ids_for_anchor_name(
                connection, anchor_name
            )
            if len(known_player_ids) > 1:
                return
            if known_player_ids:
                player_id = known_player_ids[0]
        if player_id is None:
            if not reliable_anchor_name:
                return
            if room_id <= 0 and not allow_roomless_create:
                return
            cursor = connection.execute(
                'INSERT INTO vainglory_players('
                'name,origin,created_at,updated_at) '
                "VALUES(?,'automatic',?,?)",
                (anchor_name[:80], now, now),
            )
            player_id = int(cursor.lastrowid)
            VaingloryRepository._insert_player_alias(
                connection, player_id=player_id, alias=anchor_name[:80], now=now
            )
        if room_id > 0:
            connection.execute(
                'INSERT OR IGNORE INTO vainglory_player_rooms('
                'room_id,player_id,created_at,updated_at) VALUES(?,?,?,?)',
                (room_id, player_id, now, now),
            )
        else:
            connection.execute(
                'INSERT OR IGNORE INTO vainglory_player_sessions('
                'session_id,player_id,created_at,updated_at) VALUES(?,?,?,?)',
                (int(session_id), player_id, now, now),
            )

    @staticmethod
    def _normalize_player_display_name(name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ValueError('player name must not be empty')
        if len(normalized) > 80:
            raise ValueError('player name is too long')
        return normalized

    @staticmethod
    def _insert_player_alias(
        connection: sqlite3.Connection, *, player_id: int, alias: str, now: int
    ) -> None:
        normalized = alias.strip()[:80]
        if not VaingloryRepository._is_reliable_anchor_name(normalized):
            return
        connection.execute(
            'INSERT OR IGNORE INTO vainglory_player_aliases('
            'alias,player_id,created_at,updated_at) VALUES(?,?,?,?)',
            (normalized, int(player_id), int(now), int(now)),
        )

    @staticmethod
    def _override_payload(value: object) -> Dict[str, Any]:
        try:
            payload = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning('Ignored invalid Vainglory match override payload')
            return {}
        if not isinstance(payload, dict):
            logger.warning('Ignored non-object Vainglory match override payload')
            return {}
        return dict(payload)

    @staticmethod
    def _normalize_match_override(changes: Mapping[str, Any]) -> Dict[str, Any]:
        allowed = {
            'title',
            'game_mode',
            'duration_seconds',
            'result_text',
            'end_reason',
            'winner_color',
            'match_kind',
            'view_context',
            'stats_eligible',
            'left_kills',
            'right_kills',
            'left_economy',
            'right_economy',
            'players',
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError('存在不支持修改的对局字段')
        patch: Dict[str, Any] = {}
        if 'title' in changes:
            title = str(changes['title'] or '').strip()
            if len(title) > 200:
                raise ValueError('对局标题过长')
            patch['title'] = title
        enums = {
            'game_mode': ('3v3', '5v5', 'aram', 'other', 'unknown'),
            'end_reason': ('normal', 'surrender', 'unknown'),
            'winner_color': ('teal', 'orange', 'unknown'),
            'match_kind': ('pvp', 'bot', 'practice', 'unknown'),
            'view_context': ('played', 'observed', 'unknown'),
        }
        for name, choices in enums.items():
            if name not in changes:
                continue
            value = str(changes[name])
            if value not in choices:
                raise ValueError('对局字段 {} 无效'.format(name))
            patch[name] = value
        if 'result_text' in changes:
            result_text = str(changes['result_text'] or '').strip()
            if len(result_text) > 32:
                raise ValueError('对局结果文字过长')
            patch['result_text'] = result_text
        if 'stats_eligible' in changes:
            if type(changes['stats_eligible']) is not bool:
                raise ValueError('是否计入统计的值无效')
            patch['stats_eligible'] = bool(changes['stats_eligible'])
        nullable_numbers = (
            'duration_seconds',
            'left_kills',
            'right_kills',
            'left_economy',
            'right_economy',
        )
        for name in nullable_numbers:
            if name not in changes:
                continue
            value = changes[name]
            if value is not None:
                if type(value) is not int or cast(int, value) < 0:
                    raise ValueError('对局数值 {} 无效'.format(name))
            if name == 'duration_seconds' and value == 0:
                raise ValueError('对局时长必须大于 0')
            patch[name] = value
        if 'players' in changes:
            players = changes['players']
            if not isinstance(players, Sequence) or isinstance(players, (str, bytes)):
                raise ValueError('玩家信息无效')
            normalized_players: Dict[str, Dict[str, Any]] = {}
            for raw_player in players:
                if not isinstance(raw_player, Mapping):
                    raise ValueError('玩家信息无效')
                side = str(raw_player.get('side') or '')
                slot = raw_player.get('slot')
                if side not in ('left', 'right') or type(slot) is not int:
                    raise ValueError('玩家位置无效')
                if slot < 1 or slot > 5:
                    raise ValueError('玩家位置无效')
                player_patch: Dict[str, Any] = {}
                if 'name' in raw_player:
                    name = str(raw_player.get('name') or '').strip()
                    if len(name) > 80:
                        raise ValueError('玩家名过长')
                    player_patch['name'] = name
                for field_name in (
                    'hero_id',
                    'kills',
                    'deaths',
                    'assists',
                    'economy',
                    'last_hits',
                ):
                    if field_name not in raw_player:
                        continue
                    value = raw_player[field_name]
                    minimum = 1 if field_name == 'hero_id' else 0
                    if value is not None:
                        if type(value) is not int or cast(int, value) < minimum:
                            raise ValueError('玩家数值 {} 无效'.format(field_name))
                    player_patch[field_name] = value
                if player_patch:
                    normalized_players['{}:{}'.format(side, slot)] = player_patch
            if normalized_players:
                patch['players'] = normalized_players
        return patch

    @staticmethod
    def _merge_match_override(
        connection: sqlite3.Connection,
        *,
        part_id: int,
        result_at_ms: int,
        patch: Mapping[str, Any],
        now: int,
    ) -> None:
        row = connection.execute(
            'SELECT payload_json,created_at FROM vainglory_match_overrides '
            'WHERE part_id=? AND result_at_ms=?',
            (int(part_id), int(result_at_ms)),
        ).fetchone()
        payload = (
            {}
            if row is None
            else VaingloryRepository._override_payload(row['payload_json'])
        )
        for name, value in patch.items():
            if name == 'players' and isinstance(value, Mapping):
                current_players = payload.get('players')
                merged_players = (
                    dict(current_players) if isinstance(current_players, dict) else {}
                )
                for position, player_patch in value.items():
                    current_player = merged_players.get(position)
                    merged_player = (
                        dict(current_player) if isinstance(current_player, dict) else {}
                    )
                    if isinstance(player_patch, Mapping):
                        merged_player.update(player_patch)
                    merged_players[str(position)] = merged_player
                payload['players'] = merged_players
            elif name == 'recorded_player' and isinstance(value, Mapping):
                payload[name] = dict(value)
            else:
                payload[name] = value
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        connection.execute(
            'INSERT INTO vainglory_match_overrides('
            'part_id,result_at_ms,payload_json,created_at,updated_at) '
            'VALUES(?,?,?,?,?) ON CONFLICT(part_id,result_at_ms) DO UPDATE SET '
            'payload_json=excluded.payload_json,updated_at=excluded.updated_at',
            (int(part_id), int(result_at_ms), encoded, now, now),
        )

    @staticmethod
    def _apply_match_override(
        connection: sqlite3.Connection, match_id: int, payload: Mapping[str, Any]
    ) -> None:
        match = connection.execute(
            'SELECT result_at_ms,left_color,right_color FROM vainglory_matches '
            'WHERE id=?',
            (int(match_id),),
        ).fetchone()
        if match is None:
            return
        assignments: List[str] = []
        values: List[Any] = []
        columns = {
            'game_mode': 'game_mode',
            'duration_seconds': 'duration_seconds',
            'result_text': 'result_text',
            'end_reason': 'end_reason',
            'match_kind': 'match_kind',
            'view_context': 'view_context',
            'left_kills': 'left_kills',
            'right_kills': 'right_kills',
            'left_economy': 'left_economy',
            'right_economy': 'right_economy',
        }
        for name, column in columns.items():
            if name in payload:
                assignments.append('{}=?'.format(column))
                values.append(payload[name])
        if 'game_mode' in payload:
            team_size = {'3v3': 3, 'aram': 3, '5v5': 5}.get(str(payload['game_mode']))
            if team_size is not None:
                assignments.append('team_size=?')
                values.append(team_size)
        if 'title' in payload:
            assignments.append('custom_title=?')
            values.append(str(payload['title']) or None)
        if 'duration_seconds' in payload:
            duration = payload['duration_seconds']
            assignments.append('started_at_ms=?')
            values.append(
                0
                if duration is None
                else max(0, int(match['result_at_ms']) - int(duration) * 1_000)
            )
        if 'winner_color' in payload:
            winner_color = str(payload['winner_color'])
            winner_side = (
                'left'
                if str(match['left_color']) == winner_color
                else 'right' if str(match['right_color']) == winner_color else 'unknown'
            )
            assignments.append('winner_side=?')
            values.append(winner_side)
        if 'stats_eligible' in payload:
            eligible = bool(payload['stats_eligible'])
            assignments.extend(('stats_eligible=?', 'stats_exclusion_reason=?'))
            values.extend((1 if eligible else 0, None if eligible else 'manual'))
        recorded_player = payload.get('recorded_player')
        if isinstance(recorded_player, Mapping):
            side = str(recorded_player.get('side') or '')
            slot = recorded_player.get('slot')
            exists = connection.execute(
                'SELECT 1 FROM vainglory_match_players '
                'WHERE match_id=? AND side=? AND slot=?',
                (int(match_id), side, slot),
            ).fetchone()
            if exists is not None:
                assignments.extend(
                    (
                        'recorded_player_side=?',
                        'recorded_player_slot=?',
                        'recorded_player_confidence=1',
                        "recorded_player_source='manual'",
                    )
                )
                values.extend((side, slot))
        if assignments:
            connection.execute(
                'UPDATE vainglory_matches SET {} WHERE id=?'.format(
                    ','.join(assignments)
                ),
                tuple(values) + (int(match_id),),
            )
        players = payload.get('players')
        if not isinstance(players, Mapping):
            return
        for position, player_patch in players.items():
            side, separator, slot_text = str(position).partition(':')
            if not separator or side not in ('left', 'right'):
                continue
            try:
                slot = int(slot_text)
            except ValueError:
                continue
            if not isinstance(player_patch, Mapping):
                continue
            player_assignments: List[str] = []
            player_values: List[Any] = []
            if 'name' in player_patch:
                name = str(player_patch['name'])
                player_assignments.extend(('player_name=?', 'normalized_name=?'))
                player_values.extend((name, normalize_player_name(name)))
            for name, column in (
                ('kills', 'kills'),
                ('deaths', 'deaths'),
                ('assists', 'assists'),
                ('economy', 'economy'),
                ('last_hits', 'last_hits'),
            ):
                if name in player_patch:
                    player_assignments.append('{}=?'.format(column))
                    player_values.append(player_patch[name])
            if 'hero_id' in player_patch:
                hero_id = player_patch['hero_id']
                if (
                    hero_id is None
                    or connection.execute(
                        "SELECT 1 FROM vainglory_heroes WHERE id=? AND label<>''",
                        (hero_id,),
                    ).fetchone()
                    is not None
                ):
                    player_assignments.extend(('hero_id=?', "hero_source='manual'"))
                    player_values.append(hero_id)
            if player_assignments:
                connection.execute(
                    'UPDATE vainglory_match_players SET {} '
                    'WHERE match_id=? AND side=? AND slot=?'.format(
                        ','.join(player_assignments)
                    ),
                    tuple(player_values) + (int(match_id), side, slot),
                )

    @staticmethod
    def _match_player(row: sqlite3.Row) -> MatchPlayerRecord:
        return MatchPlayerRecord(
            side=str(row['side']),
            slot=int(row['slot']),
            name=clean_player_name(str(row['player_name'])),
            normalized_name=str(row['normalized_name']),
            hero_id=None if row['hero_id'] is None else int(row['hero_id']),
            hero_label=str(row['hero_label']),
            hero_source=cast(Literal['automatic', 'manual'], str(row['hero_source'])),
            kills=None if row['kills'] is None else int(row['kills']),
            deaths=None if row['deaths'] is None else int(row['deaths']),
            assists=None if row['assists'] is None else int(row['assists']),
            economy=None if row['economy'] is None else int(row['economy']),
            last_hits=(None if row['last_hits'] is None else int(row['last_hits'])),
            confidence=float(row['confidence']),
            is_recorded_player=bool(int(row['is_recorded_player'])),
        )

    @staticmethod
    def _set_session_anchor(
        connection: sqlite3.Connection, session_id: int, anchor_name: str, now: int
    ) -> None:
        room_id = 0
        anchor_uid: Optional[int] = None
        if anchor_name:
            known = connection.execute(
                'SELECT room_id,anchor_uid FROM recording_sessions '
                'WHERE id!=? AND anchor_name=? AND anchor_uid IS NOT NULL '
                'AND anchor_uid>0 ORDER BY '
                "CASE WHEN broadcast_session_key LIKE 'bili-migration:%' "
                "OR broadcast_session_key LIKE 'bili-archive:%' "
                'THEN 1 ELSE 0 END,started_at DESC,id DESC LIMIT 1',
                (int(session_id), anchor_name),
            ).fetchone()
            if known is not None:
                room_id = int(known['room_id'])
                anchor_uid = int(known['anchor_uid'])
        previous = connection.execute(
            'SELECT player.id,player.name '
            'FROM vainglory_player_sessions direct '
            'JOIN vainglory_players player ON player.id=direct.player_id '
            'WHERE direct.session_id=?',
            (int(session_id),),
        ).fetchone()
        connection.execute(
            'UPDATE recording_sessions SET room_id=?,anchor_uid=?,anchor_name=? '
            'WHERE id=?',
            (room_id, anchor_uid, anchor_name, int(session_id)),
        )
        preserved = (
            previous is not None
            and room_id <= 0
            and bool(anchor_name)
            and str(previous['name']).casefold() == anchor_name.casefold()
        )
        if previous is not None and not preserved:
            previous_player_id = int(previous['id'])
            connection.execute(
                'DELETE FROM vainglory_player_sessions WHERE session_id=?',
                (int(session_id),),
            )
            connection.execute(
                "DELETE FROM vainglory_players WHERE id=? AND origin='automatic' "
                'AND NOT EXISTS(SELECT 1 FROM vainglory_player_rooms room '
                'WHERE room.player_id=vainglory_players.id) '
                'AND NOT EXISTS(SELECT 1 FROM vainglory_player_sessions direct '
                'WHERE direct.player_id=vainglory_players.id)',
                (previous_player_id,),
            )
        if not preserved:
            VaingloryRepository._ensure_session_player(
                connection, session_id, now, allow_roomless_create=True
            )

    @staticmethod
    def _match_session_record(row: sqlite3.Row) -> MatchSessionRecord:
        mode_order = ('3v3', '5v5', 'aram', 'other', 'unknown')
        present_modes = set(str(row['game_modes'] or '').split(','))
        return MatchSessionRecord(
            session_id=int(row['session_id']),
            title=str(row['title'] or ''),
            source_title=str(row['source_title'] or ''),
            anchor_name=str(row['anchor_name'] or ''),
            started_at=int(row['started_at']),
            live_started_at=int(row['live_started_at']),
            part_count=int(row['part_count'] or 0),
            original_part_count=int(row['original_part_count'] or 0),
            ignored_part_count=int(row['ignored_part_count'] or 0),
            recording_duration_seconds=int(row['recording_duration_seconds'] or 0),
            match_count=int(row['match_count']),
            teal_win_count=int(row['teal_win_count'] or 0),
            orange_win_count=int(row['orange_win_count'] or 0),
            win_count=int(row['teal_win_count'] or 0),
            loss_count=int(row['orange_win_count'] or 0),
            unknown_count=max(
                0,
                int(row['match_count'])
                - int(row['teal_win_count'] or 0)
                - int(row['orange_win_count'] or 0),
            ),
            surrender_count=int(row['surrender_count'] or 0),
            duration_seconds=int(row['duration_seconds'] or 0),
            game_modes=tuple(mode for mode in mode_order if mode in present_modes),
            stats_included=bool(row['stats_included']),
            bvid=None if row['bvid'] is None else str(row['bvid']),
            publication_state=(
                None
                if row['publication_state'] is None
                else str(row['publication_state'])
            ),
            description_state=(
                None
                if row['description_state'] is None
                else str(row['description_state'])
            ),
            pin_state=None if row['pin_state'] is None else str(row['pin_state']),
            chapter_state=(
                None if row['chapter_state'] is None else str(row['chapter_state'])
            ),
            publication_priority=bool(row['publication_priority'] or 0),
            publication_updated_at=(
                None
                if row['publication_updated_at'] is None
                else int(row['publication_updated_at'])
            ),
        )

    @staticmethod
    def _match_filters(
        *,
        player_name: str,
        hero_ids: Sequence[int],
        winner_color: Optional[str],
        end_reason: Optional[str],
        game_mode: Optional[str],
        session_id: Optional[int],
    ) -> Tuple[List[str], List[object]]:
        if winner_color not in (None, 'teal', 'orange'):
            raise ValueError('winner color is invalid')
        if end_reason not in (None, 'normal', 'surrender', 'unknown'):
            raise ValueError('end reason is invalid')
        if game_mode not in (None, '3v3', '5v5', 'aram', 'other', 'unknown'):
            raise ValueError('game mode is invalid')
        where = ['1=1']
        parameters: List[object] = []
        normalized = normalize_player_name(player_name)
        if normalized:
            where.append(
                'EXISTS(SELECT 1 FROM vainglory_match_players searched '
                'WHERE searched.match_id=match.id '
                'AND searched.normalized_name LIKE ?)'
            )
            parameters.append('%{}%'.format(normalized))
        for hero_id in dict.fromkeys(int(value) for value in hero_ids):
            if hero_id < 1:
                raise ValueError('hero ID must be positive')
            where.append(
                'EXISTS(SELECT 1 FROM vainglory_match_players searched '
                'LEFT JOIN vainglory_heroes searched_hero '
                'ON searched_hero.id=searched.hero_id '
                'WHERE searched.match_id=match.id AND (searched.hero_id=? OR '
                "(searched_hero.label<>'' AND searched_hero.label=("
                'SELECT selected.label FROM vainglory_heroes selected '
                'WHERE selected.id=?) COLLATE NOCASE)))'
            )
            parameters.extend((hero_id, hero_id))
        if winner_color is not None:
            where.append(
                "(CASE match.winner_side WHEN 'left' THEN match.left_color "
                "WHEN 'right' THEN match.right_color ELSE 'unknown' END)=?"
            )
            parameters.append(winner_color)
        if end_reason is not None:
            where.append('match.end_reason=?')
            parameters.append(end_reason)
        if game_mode is not None:
            where.append('match.game_mode=?')
            parameters.append(game_mode)
        if session_id is not None:
            where.append('match.session_id=?')
            parameters.append(int(session_id))
        return where, parameters

    def _match_record(
        self, row: sqlite3.Row, players: Tuple[MatchPlayerRecord, ...]
    ) -> MatchRecord:
        winner_side = str(row['winner_side'])
        winner_color = (
            str(row['left_color'])
            if winner_side == 'left'
            else str(row['right_color']) if winner_side == 'right' else 'unknown'
        )
        source_title = str(row['session_title'] or '')
        upload_title = VaingloryRepository._upload_title(row['upload_title_source'])
        custom_title = '' if row['custom_title'] is None else str(row['custom_title'])
        archive_page = (
            None
            if row['archive_page'] is None or int(row['archive_page']) <= 0
            else int(row['archive_page'])
        )
        previous_archive_page = (
            None
            if row['previous_archive_page'] is None
            or int(row['previous_archive_page']) <= 0
            else int(row['previous_archive_page'])
        )
        previous_archive_duration_seconds = (
            None
            if row['previous_archive_duration_seconds'] is None
            or int(row['previous_archive_duration_seconds']) <= 0
            else int(row['previous_archive_duration_seconds'])
        )
        previous_archive_segments = self._archive_segments(
            row['previous_archive_segments']
        )
        has_result_frame = False
        if row['result_frame_path'] is not None:
            try:
                has_result_frame = self._resolve_result_frame_path(
                    str(row['result_frame_path'])
                ).is_file()
            except ValueError:
                pass
        recorded_player = next(
            (player for player in players if player.is_recorded_player), None
        )
        if row['team_size'] is None or int(row['team_size']) not in (3, 5):
            recorded_player_state = 'unsupported'
        elif recorded_player is not None:
            recorded_player_state = (
                'manual'
                if str(row['recorded_player_source']) == 'manual'
                else 'automatic'
            )
        elif (
            int(row['recorded_player_detection_version'])
            < self.RECORDED_PLAYER_DETECTION_VERSION
        ):
            recorded_player_state = 'pending'
        else:
            recorded_player_state = 'uncertain'
        return MatchRecord(
            id=int(row['id']),
            session_id=int(row['session_id']),
            session_title=source_title,
            session_started_at=int(row['session_started_at']),
            part_id=int(row['result_part_id']),
            part_index=int(row['part_index']),
            title=custom_title or upload_title or source_title,
            source_title=source_title,
            upload_title=upload_title,
            game_mode=str(row['game_mode']),
            team_size=(None if row['team_size'] is None else int(row['team_size'])),
            match_kind=cast(
                Literal['pvp', 'bot', 'practice', 'unknown'], str(row['match_kind'])
            ),
            view_context=cast(
                Literal['played', 'observed', 'unknown'], str(row['view_context'])
            ),
            stats_eligible=bool(int(row['stats_eligible'])),
            stats_exclusion_reason=(
                None
                if row['stats_exclusion_reason'] is None
                else str(row['stats_exclusion_reason'])
            ),
            started_at_ms=int(row['started_at_ms']),
            result_at_ms=int(row['result_at_ms']),
            duration_seconds=(
                None
                if row['duration_seconds'] is None
                else int(row['duration_seconds'])
            ),
            result_text=str(row['result_text']),
            end_reason=str(row['end_reason']),
            left_color=str(row['left_color']),
            right_color=str(row['right_color']),
            winner_side=winner_side,
            winner_color=winner_color,
            left_kills=(None if row['left_kills'] is None else int(row['left_kills'])),
            right_kills=(
                None if row['right_kills'] is None else int(row['right_kills'])
            ),
            left_economy=(
                None if row['left_economy'] is None else int(row['left_economy'])
            ),
            right_economy=(
                None if row['right_economy'] is None else int(row['right_economy'])
            ),
            confidence=float(row['confidence']),
            account_id=(None if row['account_id'] is None else int(row['account_id'])),
            bvid=None if row['bvid'] is None else str(row['bvid']),
            archive_page=archive_page,
            has_result_frame=has_result_frame,
            recorded_player_confidence=(
                None
                if row['recorded_player_confidence'] is None
                else float(row['recorded_player_confidence'])
            ),
            recorded_player_source=str(row['recorded_player_source']),
            recorded_player_state=recorded_player_state,
            rerun_state=(
                None if row['rerun_state'] is None else str(row['rerun_state'])
            ),
            rerun_error=(
                None if row['rerun_error'] is None else str(row['rerun_error'])
            ),
            players=players,
            previous_archive_page=previous_archive_page,
            previous_archive_duration_seconds=previous_archive_duration_seconds,
            previous_archive_segments=previous_archive_segments,
            analysis_state=cast(
                Literal['provisional', 'final'], str(row['analysis_state'])
            ),
        )

    @staticmethod
    def _archive_segments(value: object) -> Tuple[Tuple[int, int], ...]:
        if value is None:
            return ()
        segments: Dict[int, int] = {}
        for encoded in str(value).split(','):
            page_text, separator, duration_text = encoded.partition(':')
            if not separator:
                continue
            try:
                page = int(page_text)
                duration = int(duration_text)
            except ValueError:
                continue
            if page > 0 and duration > 0:
                segments[page] = duration
        return tuple(sorted(segments.items(), reverse=True))

    @staticmethod
    def _marker_times(value: object) -> Tuple[int, ...]:
        if value is None:
            return ()
        times = set()
        for encoded in str(value).split(','):
            try:
                at_ms = int(encoded)
            except ValueError:
                continue
            if at_ms >= 0:
                times.add(at_ms)
        return tuple(sorted(times))

    @staticmethod
    def _upload_title(value: object) -> str:
        if value is None:
            return ''
        try:
            payload = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ''
        if not isinstance(payload, dict):
            return ''
        title = payload.get('title')
        return title.strip() if isinstance(title, str) else ''

    def _now(self) -> int:
        return max(1, int(self._clock()))
