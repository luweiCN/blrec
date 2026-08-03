from __future__ import annotations

import time
from dataclasses import dataclass
from statistics import mean
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Set, Tuple

from loguru import logger

from .hero_recognition import HeroMatch, SiftHeroRecognizer
from .mode_recognition import AramDetector, AramTalentSelectionDetector
from .ocr import ResultHeader, ResultOcr, TesseractResultReader, merge_result_headers
from .sampling import (
    CoarseObservation,
    FfmpegSampler,
    ScanWindow,
    result_search_windows,
)
from .vision import (
    HeroFrame,
    RecordedPlayer,
    ResultLayout,
    RgbFrame,
    TeamSide,
    TeamSize,
    ViewportTransform,
    detect_gameplay_hud,
    detect_recorded_player,
    detect_result_layout,
    detect_result_layouts,
    extract_result_heroes,
    hero_fingerprint,
    png_bytes,
    result_frame_quality,
)


class AnalysisCancelled(RuntimeError):
    pass


class ResultReader(Protocol):
    def read_header(  # noqa: E704
        self,
        frame: RgbFrame,
        *,
        viewport: ViewportTransform = ...,
        team_size: int = ...,
    ) -> ResultHeader: ...  # noqa: E704

    def read(  # noqa: E704
        self,
        frame: RgbFrame,
        *,
        header: Optional[ResultHeader] = ...,
        viewport: ViewportTransform = ...,
        name_frames: Sequence[RgbFrame] = ...,
        team_size: int = ...,
    ) -> ResultOcr: ...  # noqa: E704

    def read_wide_screenshot(  # noqa: E704
        self,
        frame: RgbFrame,
        *,
        header: Optional[ResultHeader] = ...,
        viewport: ViewportTransform = ...,
        name_frames: Sequence[RgbFrame] = ...,
        team_size: int = ...,
    ) -> ResultOcr: ...  # noqa: E704


@dataclass(frozen=True)
class VideoPart:
    id: int
    index: int
    path: str
    title: str = ''


@dataclass(frozen=True)
class ResultHit:
    at_ms: int
    layout: ResultLayout


@dataclass(frozen=True)
class _WindowScanResult:
    hits: Tuple[ResultHit, ...]
    keyframe_preview_frames: int
    fallback_preview_frames: int
    refinement_frames: int
    refinement_windows: int


@dataclass(frozen=True)
class AnalyzedHero:
    side: TeamSide
    slot: int
    fingerprint: str
    thumbnail_png: bytes
    label: str = ''
    confidence: float = 0


@dataclass(frozen=True)
class AnalyzedMatch:
    part_id: int
    part_index: int
    result_at_ms: int
    layout: ResultLayout
    ocr: ResultOcr
    heroes: Tuple[AnalyzedHero, ...]
    confidence: float
    result_frame_png: bytes = b''
    game_mode: str = 'unknown'
    recorded_player: Optional[RecordedPlayer] = None


@dataclass(frozen=True)
class ScannedPart:
    video_duration_ms: int
    candidate_times_ms: Tuple[int, ...]


def collapse_result_hits(
    hits: Sequence[ResultHit], *, maximum_gap_ms: int = 1_000
) -> Tuple[ResultHit, ...]:
    if maximum_gap_ms < 0:
        raise ValueError('maximum result gap must not be negative')
    strongest_by_time: Dict[int, ResultHit] = {}
    for hit in hits:
        previous = strongest_by_time.get(hit.at_ms)
        if previous is None or hit.layout.confidence > previous.layout.confidence:
            strongest_by_time[hit.at_ms] = hit
    ordered = sorted(strongest_by_time.values(), key=lambda hit: hit.at_ms)
    groups: List[List[ResultHit]] = []
    for hit in ordered:
        if not groups or hit.at_ms - groups[-1][-1].at_ms > maximum_gap_ms:
            groups.append([hit])
        else:
            groups[-1].append(hit)
    result: List[ResultHit] = []
    for group in groups:
        middle = group[len(group) // 2]
        strongest = max(group, key=lambda hit: hit.layout.confidence)
        result.append(ResultHit(at_ms=middle.at_ms, layout=strongest.layout))
    return tuple(result)


def _result_refinement_windows(
    hits: Sequence[ResultHit], *, outer_window: ScanWindow, padding_ms: int = 2_000
) -> Tuple[ScanWindow, ...]:
    candidates = collapse_result_hits(hits, maximum_gap_ms=5_000)
    windows = [
        ScanWindow(
            start_ms=max(outer_window.start_ms, hit.at_ms - padding_ms),
            end_ms=min(outer_window.end_ms, hit.at_ms + padding_ms),
        )
        for hit in candidates
    ]
    merged: List[ScanWindow] = []
    for window in windows:
        if window.end_ms <= window.start_ms:
            continue
        if not merged or window.start_ms > merged[-1].end_ms:
            merged.append(window)
            continue
        previous = merged[-1]
        merged[-1] = ScanWindow(
            start_ms=previous.start_ms, end_ms=max(previous.end_ms, window.end_ms)
        )
    return tuple(merged)


def collapse_analyzed_matches(
    matches: Sequence[AnalyzedMatch], *, maximum_start_gap_ms: int = 90_000
) -> Tuple[AnalyzedMatch, ...]:
    if maximum_start_gap_ms < 0:
        raise ValueError('maximum game start gap must not be negative')
    groups: List[Tuple[Optional[int], AnalyzedMatch]] = []
    for match in sorted(matches, key=lambda item: item.result_at_ms):
        duration = match.ocr.header.duration_seconds
        if duration is None:
            groups.append((None, match))
            continue
        estimated_start = max(0, match.result_at_ms - duration * 1_000)
        duplicate_index = next(
            (
                index
                for index, (group_start, _) in enumerate(groups)
                if group_start is not None
                and abs(group_start - estimated_start) <= maximum_start_gap_ms
            ),
            None,
        )
        if duplicate_index is None:
            groups.append((estimated_start, match))
            continue
        group_start, previous = groups[duplicate_index]
        if match.result_at_ms >= previous.result_at_ms:
            groups[duplicate_index] = (group_start, match)
    return tuple(
        match for _, match in sorted(groups, key=lambda item: item[1].result_at_ms)
    )


class VaingloryVideoAnalyzer:
    def __init__(
        self,
        *,
        sampler: Optional[FfmpegSampler] = None,
        result_reader: Optional[ResultReader] = None,
        hero_recognizer: Optional[SiftHeroRecognizer] = None,
        aram_detector: Optional[AramDetector] = None,
        minimum_match_seconds: int = 300,
    ) -> None:
        if minimum_match_seconds < 0:
            raise ValueError('minimum match duration must not be negative')
        self._sampler = sampler or FfmpegSampler()
        self._result_reader = result_reader or TesseractResultReader()
        self._hero_recognizer = hero_recognizer
        self._aram_detector = aram_detector or AramTalentSelectionDetector()
        self._minimum_match_seconds = minimum_match_seconds

    def analyze_part(
        self,
        part: VideoPart,
        *,
        progress: Optional[Callable[[float], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Tuple[AnalyzedMatch, ...]:
        def scan_progress(value: float) -> None:
            if progress is not None:
                progress(value * 0.7)

        def recognition_progress(value: float) -> None:
            if progress is not None:
                progress(0.7 + value * 0.3)

        scanned = self.scan_part(part, progress=scan_progress, cancelled=cancelled)
        return self.recognize_scanned_part(
            part, scanned, progress=recognition_progress, cancelled=cancelled
        )

    def scan_part(
        self,
        part: VideoPart,
        *,
        progress: Optional[Callable[[float], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> ScannedPart:
        scan_started = time.monotonic()
        probe_started = time.monotonic()
        profile = self._sampler.probe(part.path)
        probe_seconds = time.monotonic() - probe_started
        logger.info(
            'Vainglory analysis started: part_id={} part_index={} '
            'size={}x{} aspect={:.4f} duration_ms={}',
            part.id,
            part.index,
            profile.width,
            profile.height,
            profile.width / profile.height,
            profile.duration_ms,
        )
        coarse_started = time.monotonic()
        observations: List[CoarseObservation] = []
        for timed in self._sampler.coarse_frames(part.path):
            self._raise_if_cancelled(cancelled)
            hud_signature = detect_gameplay_hud(timed.frame)
            observations.append(
                CoarseObservation(
                    at_ms=timed.at_ms,
                    hud_signature=hud_signature,
                    result_visible=(
                        hud_signature is None
                        and detect_result_layout(timed.frame) is not None
                    ),
                )
            )
            if progress is not None:
                progress(min(0.6, timed.at_ms / profile.duration_ms * 0.6))

        coarse_seconds = time.monotonic() - coarse_started
        windows = result_search_windows(observations, duration_ms=profile.duration_ms)
        logger.info(
            'Vainglory coarse scan completed: part_id={} frames={} hud_hits={} '
            'result_hits={} windows={} elapsed_seconds={:.3f}',
            part.id,
            len(observations),
            sum(item.hud_signature is not None for item in observations),
            sum(item.result_visible for item in observations),
            len(windows),
            coarse_seconds,
        )
        logger.debug(
            'Vainglory result search windows: part_id={} windows={}',
            part.id,
            tuple((window.start_ms, window.end_ms) for window in windows),
        )
        hits: List[ResultHit] = []
        keyframe_preview_frames = 0
        fallback_preview_frames = 0
        refinement_frames = 0
        refinement_windows = 0
        total_window_ms = sum(window.end_ms - window.start_ms for window in windows)
        scanned_window_ms = 0
        fine_started = time.monotonic()
        for window in windows:
            self._raise_if_cancelled(cancelled)
            scanned = self._scan_window(part.path, window, cancelled=cancelled)
            hits.extend(scanned.hits)
            keyframe_preview_frames += scanned.keyframe_preview_frames
            fallback_preview_frames += scanned.fallback_preview_frames
            refinement_frames += scanned.refinement_frames
            refinement_windows += scanned.refinement_windows
            scanned_window_ms += window.end_ms - window.start_ms
            if progress is not None:
                fine_progress = scanned_window_ms / max(1, total_window_ms)
                progress(0.6 + fine_progress * 0.4)

        fine_seconds = time.monotonic() - fine_started
        candidates = collapse_result_hits(hits)
        logger.info(
            'Vainglory fine scan completed: part_id={} windows={} hits={} '
            'candidates={} keyframe_preview_frames={} '
            'fallback_preview_frames={} refinement_windows={} '
            'refinement_frames={} elapsed_seconds={:.3f}',
            part.id,
            len(windows),
            len(hits),
            len(candidates),
            keyframe_preview_frames,
            fallback_preview_frames,
            refinement_windows,
            refinement_frames,
            fine_seconds,
        )
        if progress is not None:
            progress(1.0)
        logger.info(
            'Vainglory video scan completed: part_id={} candidates={} '
            'probe_seconds={:.3f} coarse_seconds={:.3f} fine_seconds={:.3f} '
            'total_seconds={:.3f}',
            part.id,
            len(candidates),
            probe_seconds,
            coarse_seconds,
            fine_seconds,
            time.monotonic() - scan_started,
        )
        return ScannedPart(
            video_duration_ms=profile.duration_ms,
            candidate_times_ms=tuple(candidate.at_ms for candidate in candidates),
        )

    def recognize_scanned_part(
        self,
        part: VideoPart,
        scanned: ScannedPart,
        *,
        progress: Optional[Callable[[float], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Tuple[AnalyzedMatch, ...]:
        recognition_run_started = time.monotonic()
        candidates = scanned.candidate_times_ms
        matches: List[AnalyzedMatch] = []
        rejected_layout = 0
        rejected_header = 0
        rejected_short = 0
        candidate_started = time.monotonic()
        candidate_frame_seconds = 0.0
        header_ocr_seconds = 0.0
        nearby_frame_seconds = 0.0
        match_recognition_seconds = 0.0
        for index, candidate_at_ms in enumerate(candidates):
            self._raise_if_cancelled(cancelled)
            if progress is not None:
                progress(index / max(1, len(candidates)))
            frame_started = time.monotonic()
            frame = self._sampler.frame_at(part.path, candidate_at_ms)
            layouts = detect_result_layouts(frame)
            candidate_frame_seconds += time.monotonic() - frame_started
            if not layouts:
                rejected_layout += 1
                continue
            header_started = time.monotonic()
            attempts = self._read_layout_headers(
                frame,
                layouts,
                require_completed=True,
                part_id=part.id,
                at_ms=candidate_at_ms,
            )
            header_ocr_seconds += time.monotonic() - header_started
            layout, header = self._select_ocr_context(layouts, attempts)
            nearby_started = time.monotonic()
            nearby_contexts = self._sample_nearby_result_frames(
                part.path, at_ms=candidate_at_ms, duration_ms=scanned.video_duration_ms
            )
            nearby_frame_seconds += time.monotonic() - nearby_started
            ranked_contexts = sorted(
                ((frame, layout), *nearby_contexts),
                key=lambda item: result_frame_quality(item[0], item[1]),
                reverse=True,
            )
            frame, layout = ranked_contexts[0]
            name_frames = tuple(item[0] for item in ranked_contexts[1:])
            match_started = time.monotonic()
            recognized = self._recognize_frame(
                frame,
                part=part,
                at_ms=candidate_at_ms,
                layout=layout,
                header=header,
                name_frames=name_frames,
                hero_frames=name_frames,
                video_duration_ms=scanned.video_duration_ms,
            )
            match_recognition_seconds += time.monotonic() - match_started
            recognized_header = recognized.ocr.header
            if not self._is_completed_match(recognized_header):
                if self._is_result_header(recognized_header):
                    rejected_short += 1
                    reason = 'short'
                else:
                    rejected_header += 1
                    reason = 'header'
                logger.info(
                    'Vainglory result candidate rejected after full OCR: '
                    'part_id={} at_ms={} reason={} result_text={!r} duration={}',
                    part.id,
                    candidate_at_ms,
                    reason,
                    recognized_header.result_text,
                    recognized_header.duration_seconds,
                )
                continue
            matches.append(recognized)
            logger.info(
                'Vainglory match recognized: part_id={} at_ms={} viewport={} '
                'winner_color={} winner_side={} layout_confidence={:.4f} '
                'result_text={!r} duration={}',
                part.id,
                candidate_at_ms,
                layout.viewport.name,
                layout.winner_color,
                layout.winner_side,
                layout.confidence,
                recognized_header.result_text,
                recognized_header.duration_seconds,
            )
        recognized_match_count = len(matches)
        matches = list(collapse_analyzed_matches(matches))
        if progress is not None:
            progress(1.0)
        candidate_seconds = time.monotonic() - candidate_started
        logger.info(
            'Vainglory recognition completed: part_id={} candidates={} '
            'matches={} timeline_duplicates={} rejected_layout={} '
            'rejected_header={} rejected_short={} '
            'candidate_seconds={:.3f} candidate_frame_seconds={:.3f} '
            'header_ocr_seconds={:.3f} nearby_frame_seconds={:.3f} '
            'match_recognition_seconds={:.3f} total_seconds={:.3f}',
            part.id,
            len(candidates),
            len(matches),
            recognized_match_count - len(matches),
            rejected_layout,
            rejected_header,
            rejected_short,
            candidate_seconds,
            candidate_frame_seconds,
            header_ocr_seconds,
            nearby_frame_seconds,
            match_recognition_seconds,
            time.monotonic() - recognition_run_started,
        )
        return tuple(matches)

    def _scan_window(
        self,
        path: str,
        window: ScanWindow,
        *,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> _WindowScanResult:
        keyframe_hits, keyframe_frames = self._scan_preview(
            path, window, keyframes_only=True, cancelled=cancelled
        )
        refinement_hits: Tuple[ResultHit, ...] = ()
        refinement_frames = 0
        refinement_windows = 0
        if keyframe_hits:
            refinement_hits, frame_count, window_count = self._refine_preview_hits(
                path, window, keyframe_hits, cancelled=cancelled
            )
            refinement_frames += frame_count
            refinement_windows += window_count
        if refinement_hits:
            return _WindowScanResult(
                hits=refinement_hits,
                keyframe_preview_frames=keyframe_frames,
                fallback_preview_frames=0,
                refinement_frames=refinement_frames,
                refinement_windows=refinement_windows,
            )

        fallback_hits, fallback_frames = self._scan_preview(
            path, window, keyframes_only=False, cancelled=cancelled
        )
        if fallback_hits:
            refinement_hits, frame_count, window_count = self._refine_preview_hits(
                path, window, fallback_hits, cancelled=cancelled
            )
            refinement_frames += frame_count
            refinement_windows += window_count
        hits = refinement_hits or collapse_result_hits(
            fallback_hits or keyframe_hits, maximum_gap_ms=5_000
        )
        return _WindowScanResult(
            hits=hits,
            keyframe_preview_frames=keyframe_frames,
            fallback_preview_frames=fallback_frames,
            refinement_frames=refinement_frames,
            refinement_windows=refinement_windows,
        )

    def _scan_preview(
        self,
        path: str,
        window: ScanWindow,
        *,
        keyframes_only: bool,
        cancelled: Optional[Callable[[], bool]],
    ) -> Tuple[Tuple[ResultHit, ...], int]:
        hits: List[ResultHit] = []
        frame_count = 0
        for timed in self._sampler.result_preview_frames(
            path, window, keyframes_only=keyframes_only
        ):
            self._raise_if_cancelled(cancelled)
            frame_count += 1
            layout = detect_result_layout(timed.frame)
            if layout is not None:
                hits.append(ResultHit(at_ms=timed.at_ms, layout=layout))
        return tuple(hits), frame_count

    def _refine_preview_hits(
        self,
        path: str,
        outer_window: ScanWindow,
        preview_hits: Sequence[ResultHit],
        *,
        cancelled: Optional[Callable[[], bool]],
    ) -> Tuple[Tuple[ResultHit, ...], int, int]:
        windows = _result_refinement_windows(preview_hits, outer_window=outer_window)
        hits: List[ResultHit] = []
        frame_count = 0
        for window in windows:
            for timed in self._sampler.fine_frames(path, window):
                self._raise_if_cancelled(cancelled)
                frame_count += 1
                layout = detect_result_layout(timed.frame)
                if layout is not None:
                    hits.append(ResultHit(at_ms=timed.at_ms, layout=layout))
        return collapse_result_hits(hits), frame_count, len(windows)

    def _recognize_frame(
        self,
        frame: RgbFrame,
        *,
        part: VideoPart,
        at_ms: int,
        layout: ResultLayout,
        header: Optional[ResultHeader],
        name_frames: Sequence[RgbFrame] = (),
        hero_frames: Sequence[RgbFrame] = (),
        video_duration_ms: int,
    ) -> AnalyzedMatch:
        ocr_started = time.monotonic()
        if layout.viewport.ocr_profile == 'wide':
            recognized = self._result_reader.read_wide_screenshot(
                frame,
                header=header,
                viewport=layout.viewport,
                name_frames=name_frames,
                team_size=layout.team_size,
            )
        else:
            recognized = self._result_reader.read(
                frame,
                header=header,
                viewport=layout.viewport,
                name_frames=name_frames,
                team_size=layout.team_size,
            )
        ocr_seconds = time.monotonic() - ocr_started
        heroes_started = time.monotonic()
        heroes = self._recognize_heroes(frame, layout, nearby_frames=hero_frames)
        recorded_player = detect_recorded_player(frame, layout)
        hero_seconds = time.monotonic() - heroes_started
        frame_encode_started = time.monotonic()
        result_frame_png = png_bytes(frame)
        frame_encode_seconds = time.monotonic() - frame_encode_started
        player_confidence = mean(player.confidence for player in recognized.players)
        confidence = min(1.0, layout.confidence * 0.6 + player_confidence * 0.4)
        logger.info(
            'Vainglory match extraction timings: part_id={} at_ms={} '
            'name_stats_ocr_seconds={:.3f} hero_seconds={:.3f} '
            'result_frame_encode_seconds={:.3f} result_frame_bytes={}',
            part.id,
            at_ms,
            ocr_seconds,
            hero_seconds,
            frame_encode_seconds,
            len(result_frame_png),
        )
        return AnalyzedMatch(
            part_id=part.id,
            part_index=part.index,
            result_at_ms=at_ms,
            layout=layout,
            ocr=recognized,
            heroes=heroes,
            confidence=confidence,
            result_frame_png=result_frame_png,
            game_mode=self._detect_game_mode(
                part.path,
                result_at_ms=at_ms,
                duration_seconds=recognized.header.duration_seconds,
                video_duration_ms=video_duration_ms,
                team_size=layout.team_size,
            ),
            recorded_player=recorded_player,
        )

    def _detect_game_mode(
        self,
        path: str,
        *,
        result_at_ms: int,
        duration_seconds: Optional[int],
        video_duration_ms: int,
        team_size: TeamSize,
    ) -> str:
        if team_size == 5:
            return '5v5'
        if duration_seconds is None:
            return 'unknown'
        estimated_start_ms = result_at_ms - duration_seconds * 1_000
        if estimated_start_ms < 0:
            return 'unknown'
        sampled_at: List[int] = []
        for offset_ms in (1_000, 3_000, 5_000):
            at_ms = estimated_start_ms + offset_ms
            if at_ms < 0 or at_ms >= video_duration_ms or at_ms in sampled_at:
                continue
            sampled_at.append(at_ms)
            try:
                frame = self._sampler.frame_at(path, at_ms)
            except RuntimeError as error:
                logger.warning(
                    'Skipped unreadable optional Vainglory mode frame: '
                    'at_ms={} error={}',
                    at_ms,
                    error,
                )
                continue
            if self._aram_detector.is_visible(frame):
                logger.info(
                    'Vainglory ARAM talent selector recognized: at_ms={}', at_ms
                )
                return 'aram'
        return '3v3'

    def _recognize_heroes(
        self,
        frame: RgbFrame,
        layout: ResultLayout,
        *,
        nearby_frames: Sequence[RgbFrame] = (),
    ) -> Tuple[AnalyzedHero, ...]:
        variants = [
            (
                0.0,
                self._recognize_hero_variant(
                    frame,
                    viewport=layout.viewport,
                    team_size=layout.team_size,
                    center_shift=0.0,
                ),
            )
        ]
        if self._hero_recognizer is not None and any(
            match is None for _, match in variants[0][1]
        ):
            for center_shift in (-0.01, 0.01, -0.02, 0.02):
                variants.append(
                    (
                        center_shift,
                        self._recognize_hero_variant(
                            frame,
                            viewport=layout.viewport,
                            team_size=layout.team_size,
                            center_shift=center_shift,
                        ),
                    )
                )
        center_shift, selected = max(
            variants,
            key=lambda variant: self._hero_variant_score(variant[0], variant[1]),
        )
        logger.debug(
            'Vainglory hero layout selected: viewport={} center_shift={:.3f} '
            'recognized={}/{}',
            layout.viewport.name,
            center_shift,
            sum(match is not None for _, match in selected),
            layout.team_size * 2,
        )
        unresolved = {
            (hero.side, hero.slot) for hero, match in selected if match is None
        }
        nearby_matches = self._recognize_nearby_heroes(
            nearby_frames, unresolved, team_size=layout.team_size
        )
        if nearby_matches:
            logger.info(
                'Vainglory nearby frames filled hero positions: filled={} '
                'remaining={}',
                len(nearby_matches),
                len(unresolved) - len(nearby_matches),
            )
        resolved = tuple(
            (
                nearby_matches.get((hero.side, hero.slot), (hero, match))
                if match is None
                else (hero, match)
            )
            for hero, match in selected
        )
        return tuple(
            AnalyzedHero(
                side=hero.side,
                slot=hero.slot,
                fingerprint=hero_fingerprint(hero.frame),
                thumbnail_png=png_bytes(hero.frame),
                label='' if match is None else match.label,
                confidence=0 if match is None else match.confidence,
            )
            for hero, match in resolved
        )

    def _recognize_nearby_heroes(
        self,
        frames: Sequence[RgbFrame],
        positions: Set[Tuple[TeamSide, int]],
        *,
        team_size: TeamSize,
    ) -> Dict[Tuple[TeamSide, int], Tuple[HeroFrame, HeroMatch]]:
        if self._hero_recognizer is None or not frames or not positions:
            return {}
        candidates: Dict[Tuple[TeamSide, int], List[Tuple[HeroFrame, HeroMatch]]] = {
            position: [] for position in positions
        }
        for frame in frames:
            layout = detect_result_layout(frame)
            if layout is None or layout.team_size != team_size:
                continue
            best_in_frame: Dict[
                Tuple[TeamSide, int], Tuple[HeroFrame, HeroMatch, float]
            ] = {}
            for center_shift in (0.0, -0.01, 0.01, -0.02, 0.02):
                for hero in extract_result_heroes(
                    frame,
                    viewport=layout.viewport,
                    team_size=layout.team_size,
                    center_shift=center_shift,
                ):
                    position = (hero.side, hero.slot)
                    if position not in positions:
                        continue
                    match = self._hero_recognizer.recognize(hero.frame)
                    if match is None:
                        continue
                    current = best_in_frame.get(position)
                    score = (
                        match.confidence,
                        match.inliers,
                        match.margin,
                        -abs(center_shift),
                    )
                    if current is None:
                        best_in_frame[position] = (hero, match, center_shift)
                        continue
                    current_match = current[1]
                    current_score = (
                        current_match.confidence,
                        current_match.inliers,
                        current_match.margin,
                        -abs(current[2]),
                    )
                    if score > current_score:
                        best_in_frame[position] = (hero, match, center_shift)
            for position, (hero, match, _) in best_in_frame.items():
                candidates[position].append((hero, match))

        resolved: Dict[Tuple[TeamSide, int], Tuple[HeroFrame, HeroMatch]] = {}
        for position, position_candidates in candidates.items():
            by_label: Dict[str, List[Tuple[HeroFrame, HeroMatch]]] = {}
            for candidate in position_candidates:
                by_label.setdefault(candidate[1].label, []).append(candidate)
            ordered = sorted(
                by_label.values(),
                key=lambda group: (
                    len(group),
                    max(item[1].confidence for item in group),
                    sum(item[1].inliers for item in group),
                    sum(item[1].margin for item in group),
                ),
                reverse=True,
            )
            if not ordered:
                continue
            winner = ordered[0]
            strongest = max(
                winner,
                key=lambda item: (item[1].confidence, item[1].inliers, item[1].margin),
            )
            stable_consensus = (
                len(winner) >= 2
                and (len(ordered) == 1 or len(winner) > len(ordered[1]))
                and strongest[1].confidence >= 0.70
                and strongest[1].inliers >= 8
            )
            single_strong_match = (
                len(by_label) == 1
                and strongest[1].confidence >= 0.85
                and strongest[1].inliers >= 10
            )
            if stable_consensus or single_strong_match:
                resolved[position] = strongest
        return resolved

    def recognize_saved_heroes(self, content: bytes) -> Tuple[AnalyzedHero, ...]:
        frame = self._sampler.decode_image(content)
        layout = detect_result_layout(frame)
        if layout is None:
            raise ValueError('保存的图片不是可识别的结算画面')
        return self._recognize_heroes(frame, layout)

    def detect_saved_recorded_player(self, content: bytes) -> Optional[RecordedPlayer]:
        frame = self._sampler.decode_image(content)
        layout = detect_result_layout(frame)
        if layout is None:
            raise ValueError('保存的图片不是可识别的结算画面')
        return detect_recorded_player(frame, layout)

    def _recognize_hero_variant(
        self,
        frame: RgbFrame,
        *,
        viewport: ViewportTransform,
        team_size: TeamSize,
        center_shift: float,
    ) -> Tuple[Tuple[HeroFrame, Optional[HeroMatch]], ...]:
        return tuple(
            (
                hero,
                (
                    None
                    if self._hero_recognizer is None
                    else self._hero_recognizer.recognize(hero.frame)
                ),
            )
            for hero in extract_result_heroes(
                frame, viewport=viewport, team_size=team_size, center_shift=center_shift
            )
        )

    @staticmethod
    def _hero_variant_score(
        center_shift: float, recognized: Sequence[Tuple[HeroFrame, Optional[HeroMatch]]]
    ) -> Tuple[int, float, int, int]:
        matches = tuple(match for _, match in recognized if match is not None)
        return (
            len(matches),
            -abs(center_shift),
            sum(match.inliers for match in matches),
            sum(match.margin for match in matches),
        )

    def _sample_nearby_result_frames(
        self, path: str, *, at_ms: int, duration_ms: int
    ) -> Tuple[Tuple[RgbFrame, ResultLayout], ...]:
        frames: List[Tuple[RgbFrame, ResultLayout]] = []
        attempted_at: List[int] = []
        accepted_at: List[int] = []
        offsets_ms = (-5_000, -3_000, -1_000, 1_000, 3_000, 5_000)
        for offset_ms in offsets_ms:
            candidate_at = max(0, min(duration_ms - 1, at_ms + offset_ms))
            if candidate_at == at_ms or candidate_at in attempted_at:
                continue
            attempted_at.append(candidate_at)
            try:
                frame = self._sampler.frame_at(path, candidate_at)
            except RuntimeError as error:
                logger.warning(
                    'Skipped unreadable optional Vainglory nearby frame: '
                    'at_ms={} error={}',
                    candidate_at,
                    error,
                )
                continue
            layout = detect_result_layout(frame)
            if layout is None:
                continue
            frames.append((frame, layout))
            accepted_at.append(candidate_at)
        logger.debug(
            'Vainglory nearby OCR frames: at_ms={} sampled_at={} accepted={}',
            at_ms,
            tuple(at_ms + offset for offset in offsets_ms),
            tuple(accepted_at),
        )
        return tuple(frames)

    def _read_layout_headers(
        self,
        frame: RgbFrame,
        layouts: Sequence[ResultLayout],
        *,
        require_completed: bool,
        part_id: Optional[int] = None,
        at_ms: Optional[int] = None,
    ) -> Tuple[Tuple[ResultLayout, ResultHeader], ...]:
        attempts: List[Tuple[ResultLayout, ResultHeader]] = []
        for layout in layouts:
            header = self._result_reader.read_header(
                frame, viewport=layout.viewport, team_size=layout.team_size
            )
            attempts.append((layout, header))
            logger.debug(
                'Vainglory result OCR attempt: part_id={} at_ms={} viewport={} '
                'team_size={} result_text={!r} duration={}',
                part_id,
                at_ms,
                layout.viewport.name,
                layout.team_size,
                header.result_text,
                header.duration_seconds,
            )
            accepted = (
                self._is_completed_match(header)
                if require_completed
                else self._is_result_header(header)
            )
            if accepted:
                break
        return tuple(attempts)

    def _is_completed_match(self, header: ResultHeader) -> bool:
        return (
            self._is_result_header(header)
            and header.duration_seconds is not None
            and header.duration_seconds >= self._minimum_match_seconds
        )

    def _select_ocr_context(
        self,
        layouts: Sequence[ResultLayout],
        attempts: Sequence[Tuple[ResultLayout, ResultHeader]],
    ) -> Tuple[ResultLayout, Optional[ResultHeader]]:
        primary_layout = layouts[0]
        accepted = tuple(
            (attempt_layout, header)
            for attempt_layout, header in attempts
            if self._is_completed_match(header)
        )
        if not accepted:
            return primary_layout, None
        accepted_team_size = accepted[0][0].team_size
        layout = next(item for item in layouts if item.team_size == accepted_team_size)
        header = merge_result_headers(
            tuple(
                (attempt_header, attempt_layout.confidence)
                for attempt_layout, attempt_header in accepted
                if attempt_layout.team_size == accepted_team_size
            )
        )
        return layout, header

    @staticmethod
    def _is_result_header(header: ResultHeader) -> bool:
        return header.duration_seconds is not None

    @staticmethod
    def _raise_if_cancelled(cancelled: Optional[Callable[[], bool]]) -> None:
        if cancelled is not None and cancelled():
            raise AnalysisCancelled('对局分析已停止')
