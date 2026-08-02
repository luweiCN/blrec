from __future__ import annotations

import csv
import importlib
import io
import os
import re
import subprocess
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from itertools import product
from statistics import median
from typing import Any, Dict, Hashable, List, Optional, Sequence, Tuple, TypeVar

from loguru import logger

from .vision import STANDARD_VIEWPORT, PixelRect, RgbFrame, ViewportTransform, png_bytes

_RESULT_PATTERN = re.compile(
    r'(胜利|失败|战败|投降|victory|defeat|surrender)', re.IGNORECASE
)
_DURATION_PATTERN = re.compile(r'(?<!\d)(\d{1,2})\s*:\s*(\d{2})(?!\d)')
_ECONOMY_PATTERN = re.compile(r'((?:\d{1,2}[.,]\d|\d{1,3}))\s*[kK]')
_INTEGER_PATTERN = re.compile(r'(?<![\d.])(\d{1,3})(?![\d.])')
_KDA_PATTERN = re.compile(r'(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})')
_PLAYER_NAME_KDA_SUFFIX_PATTERN = re.compile(
    r'(?<!\d)\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{1,2}\s*$'
)
_KDA_MISSING_SLASH_PATTERN = re.compile(r'(?<!\d)(\d{1,2})\s*/\s*(\d{3,5})(?!\d)')
_NUMERIC_TRANSLATION = {
    ord('I'): '1',
    ord('l'): '1',
    ord('|'): '1',
    ord('，'): ',',
    ord('。'): '.',
    ord('／'): '/',
}
_CandidateValue = TypeVar('_CandidateValue', bound=Hashable)


@dataclass(frozen=True)
class ResultHeader:
    result_text: str
    end_reason: str
    duration_seconds: Optional[int]
    left_kills: Optional[int]
    right_kills: Optional[int]
    left_economy: Optional[int]
    right_economy: Optional[int]


@dataclass(frozen=True)
class PlayerStats:
    kills: Optional[int]
    deaths: Optional[int]
    assists: Optional[int]
    economy: Optional[int]
    last_hits: Optional[int] = None


@dataclass(frozen=True)
class OcrPlayer:
    side: str
    slot: int
    name: str
    normalized_name: str
    stats: PlayerStats
    confidence: float


@dataclass(frozen=True)
class ResultOcr:
    header: ResultHeader
    players: Tuple[OcrPlayer, ...]


@dataclass(frozen=True)
class _OcrText:
    text: str
    confidence: float


class RapidOcrNameReader:
    def __init__(self, engine: Optional[Any] = None) -> None:
        if engine is None:
            module = importlib.import_module('rapidocr')
            engine = getattr(module, 'RapidOCR')(params={'Global.log_level': 'error'})
        self._engine = engine

    def read(self, frame: RgbFrame) -> _OcrText:
        result = self._engine(
            png_bytes(frame), use_det=False, use_cls=False, use_rec=True
        )
        texts = tuple(getattr(result, 'txts', ()) or ())
        scores = tuple(getattr(result, 'scores', ()) or ())
        candidates = [
            _OcrText(str(text), float(score))
            for text, score in zip(texts, scores)
            if clean_player_name(str(text))
        ]
        return max(
            candidates, key=lambda item: item.confidence, default=_OcrText('', 0)
        )


_DEFAULT_NAME_READER = object()


class TesseractResultReader:
    _REFERENCE_WIDTH = 1920
    _REFERENCE_HEIGHT = 1080
    _ROW_Y = (330, 464, 598)
    _SIDE_X = (('left', 210), ('right', 1110))

    def __init__(
        self,
        *,
        executable: str = 'tesseract',
        languages: str = 'chi_sim+eng',
        timeout_seconds: float = 15,
        name_reader: object = _DEFAULT_NAME_READER,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError('OCR timeout must be positive')
        self._executable = executable
        self._languages = languages
        self._timeout_seconds = timeout_seconds
        if name_reader is _DEFAULT_NAME_READER:
            try:
                self._name_reader: Optional[RapidOcrNameReader] = RapidOcrNameReader()
            except (ImportError, OSError, RuntimeError) as error:
                self._name_reader = None
                logger.warning(
                    'Vainglory player name OCR fallback: engine=tesseract reason={!r}',
                    error,
                )
            else:
                logger.info('Vainglory player name OCR ready: engine=rapidocr')
        else:
            self._name_reader = name_reader  # type: ignore[assignment]

    def read(
        self,
        frame: RgbFrame,
        *,
        header: Optional[ResultHeader] = None,
        viewport: ViewportTransform = STANDARD_VIEWPORT,
        name_frames: Sequence[RgbFrame] = (),
    ) -> ResultOcr:
        return self._read_result(
            frame,
            header=header,
            wide_screenshot=False,
            viewport=viewport,
            name_frames=name_frames,
        )

    def read_wide_screenshot(
        self,
        frame: RgbFrame,
        *,
        header: Optional[ResultHeader] = None,
        viewport: ViewportTransform = STANDARD_VIEWPORT,
        name_frames: Sequence[RgbFrame] = (),
    ) -> ResultOcr:
        return self._read_result(
            frame,
            header=header,
            wide_screenshot=True,
            viewport=viewport,
            name_frames=name_frames,
        )

    def _read_result(
        self,
        frame: RgbFrame,
        *,
        header: Optional[ResultHeader],
        wide_screenshot: bool,
        viewport: ViewportTransform,
        name_frames: Sequence[RgbFrame],
    ) -> ResultOcr:
        if header is None:
            header = self.read_header(frame, viewport=viewport)

        raw_players: List[Tuple[str, int, str, float]] = []
        stats_candidates: List[List[PlayerStats]] = []
        stats_frames: List[RgbFrame] = []
        kda_frames: List[RgbFrame] = []
        for side_index, (side, x) in enumerate(self._SIDE_X):
            for slot, y in enumerate(self._ROW_Y, 1):
                name_thresholds: Tuple[Optional[int], ...]
                stats_thresholds: Tuple[Optional[int], ...]
                if wide_screenshot:
                    name_left, name_right = ((100, 480), (1080, 1350))[side_index]
                    stats_left, stats_right = ((360, 750), (1300, 1630))[side_index]
                    kda_left, kda_right = ((360, 625), (1300, 1505))[side_index]
                    name_frame = self._crop_reference(
                        frame,
                        PixelRect(name_left, y, name_right, y + 64),
                        (name_right - name_left) * 3,
                        192,
                        viewport=viewport,
                    )
                    stats_frame = self._crop_reference(
                        frame,
                        PixelRect(stats_left, y, stats_right, y + 64),
                        (stats_right - stats_left) * 3,
                        192,
                        viewport=viewport,
                    )
                    kda_frames.append(
                        self._crop_reference(
                            frame,
                            PixelRect(kda_left, y, kda_right, y + 64),
                            (kda_right - kda_left) * 4,
                            256,
                            viewport=viewport,
                        )
                    )
                    name_thresholds = (None, 55, 70, 85)
                    name_psm = 6
                    stats_thresholds = (None, 70, 110)
                    stats_psm = 6
                else:
                    name_frame = self._crop_reference(
                        frame,
                        PixelRect(x, y, x + 240, y + 64),
                        480,
                        128,
                        viewport=viewport,
                    )
                    stats_frame = self._crop_reference(
                        frame,
                        PixelRect(x + 216, y, x + 520, y + 64),
                        608,
                        128,
                        viewport=viewport,
                    )
                    kda_frames.append(stats_frame)
                    name_thresholds = (None, 40, 55, 85, 115)
                    name_psm = 7
                    stats_thresholds = (60, 80, 110)
                    stats_psm = 7
                rapid_candidates: Tuple[_OcrText, ...] = ()
                if self._name_reader is not None:
                    try:
                        rapid_candidates = tuple(
                            self._name_reader.read(candidate)
                            for candidate in (
                                name_frame,
                                *(
                                    self._crop_reference(
                                        extra,
                                        PixelRect(
                                            name_left if wide_screenshot else x,
                                            y,
                                            (
                                                name_right
                                                if wide_screenshot
                                                else x + 240
                                            ),
                                            y + 64,
                                        ),
                                        (
                                            (name_right - name_left) * 3
                                            if wide_screenshot
                                            else 480
                                        ),
                                        192 if wide_screenshot else 128,
                                        viewport=viewport,
                                    )
                                    for extra in name_frames
                                ),
                            )
                        )
                    except Exception as error:
                        logger.warning(
                            'Vainglory player name OCR failed: engine=rapidocr '
                            'side={} slot={} reason={!r}',
                            side,
                            slot,
                            error,
                        )
                name_candidates = tuple(
                    candidate
                    for candidate in rapid_candidates
                    if clean_player_name(candidate.text)
                )
                if not name_candidates:
                    name_candidates = self._read_variants(
                        name_frame,
                        thresholds=name_thresholds,
                        languages=self._languages,
                        page_segmentation_mode=name_psm,
                    )
                name, name_confidence = _select_player_name(name_candidates)
                logger.debug(
                    'Vainglory player name OCR: side={} slot={} engine={} '
                    'candidates={} selected={!r} confidence={:.4f}',
                    side,
                    slot,
                    'rapidocr' if rapid_candidates else 'tesseract',
                    tuple(
                        (clean_player_name(value.text), round(value.confidence, 4))
                        for value in name_candidates
                    ),
                    name,
                    name_confidence,
                )
                raw_players.append((side, slot, name, name_confidence))
                stats_frames.append(stats_frame)
                name_stats_candidates = [
                    stats
                    for stats in (
                        parse_player_stats(value.text) for value in name_candidates
                    )
                    if _has_complete_kda(stats)
                ]
                stats_candidates.append(
                    [
                        parse_player_stats(value.text)
                        for value in self._read_variants(
                            stats_frame,
                            thresholds=stats_thresholds,
                            languages='eng',
                            page_segmentation_mode=stats_psm,
                            whitelist='0123456789/.,kK',
                        )
                    ]
                    + name_stats_candidates
                )

        resolved_stats = resolve_player_stats(stats_candidates, header=header)
        retry_indexes = [
            index
            for index, stats in enumerate(resolved_stats)
            if stats.kills is None
            or stats.deaths is None
            or stats.assists is None
            or stats.economy is None
        ]
        for index in retry_indexes:
            retry_frame = kda_frames[index] if wide_screenshot else stats_frames[index]
            stats_candidates[index].extend(
                parse_player_stats(value.text)
                for value in self._read_variants(
                    retry_frame,
                    thresholds=(
                        (None, 50, 70, 90, 110, 130) if wide_screenshot else (55, 75)
                    ),
                    languages='eng',
                    page_segmentation_mode=6 if wide_screenshot else 7,
                    whitelist='0123456789/.,kK',
                )
            )
        if retry_indexes:
            resolved_stats = resolve_player_stats(stats_candidates, header=header)
        players: List[OcrPlayer] = []
        for raw, stats, candidates in zip(
            raw_players, resolved_stats, stats_candidates
        ):
            side, slot, name, name_confidence = raw
            parsed_fields = sum(
                value is not None
                for value in (stats.kills, stats.deaths, stats.assists, stats.economy)
            )
            agreement = _stats_agreement(stats, candidates)
            confidence = min(
                1.0, name_confidence * 0.25 + parsed_fields * 0.15 + agreement * 0.15
            )
            players.append(
                OcrPlayer(
                    side=side,
                    slot=slot,
                    name=name,
                    normalized_name=normalize_player_name(name),
                    stats=stats,
                    confidence=confidence,
                )
            )

        header = _header_with_resolved_team_totals(header, resolved_stats)
        return ResultOcr(header=header, players=tuple(players))

    def read_header(
        self, frame: RgbFrame, *, viewport: ViewportTransform = STANDARD_VIEWPORT
    ) -> ResultHeader:
        header_frame = self._crop_reference(
            frame, PixelRect(570, 240, 1350, 344), 780, 104, viewport=viewport
        )
        header_candidates = tuple(
            (parse_result_header(value.text), value.confidence)
            for value in self._read_variants(
                header_frame,
                thresholds=(None, 50, 90, 110),
                languages=self._languages,
                page_segmentation_mode=6,
            )
        )
        return merge_result_headers(header_candidates)

    def _read_variants(
        self,
        frame: RgbFrame,
        *,
        thresholds: Sequence[Optional[int]],
        languages: str,
        page_segmentation_mode: int,
        whitelist: Optional[str] = None,
    ) -> Tuple[_OcrText, ...]:
        return tuple(
            self._read_frame(
                frame if threshold is None else frame.threshold(threshold),
                languages=languages,
                page_segmentation_mode=page_segmentation_mode,
                whitelist=whitelist,
            )
            for threshold in thresholds
        )

    def _crop_reference(
        self,
        frame: RgbFrame,
        rect: PixelRect,
        output_width: int,
        output_height: int,
        *,
        viewport: ViewportTransform,
    ) -> RgbFrame:
        source = viewport.source_rect(
            frame,
            rect.left / self._REFERENCE_WIDTH,
            rect.top / self._REFERENCE_HEIGHT,
            rect.right / self._REFERENCE_WIDTH,
            rect.bottom / self._REFERENCE_HEIGHT,
        )
        return frame.crop(source).resize_nearest(output_width, output_height)

    def _read_frame(
        self,
        frame: RgbFrame,
        *,
        languages: str,
        page_segmentation_mode: int,
        whitelist: Optional[str] = None,
    ) -> _OcrText:
        command: List[str] = [
            self._executable,
            'stdin',
            'stdout',
            '-l',
            languages,
            '--psm',
            str(page_segmentation_mode),
        ]
        if whitelist is not None:
            command.extend(('-c', 'tessedit_char_whitelist={}'.format(whitelist)))
        command.append('tsv')
        try:
            result = subprocess.run(
                command,
                input=frame.ppm_bytes(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._timeout_seconds,
                env={**os.environ, 'OMP_THREAD_LIMIT': '1'},
            )
        except FileNotFoundError as error:
            raise RuntimeError('未安装 Tesseract OCR') from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError('Tesseract OCR 识别超时') from error
        if result.returncode != 0:
            message = result.stderr.decode('utf8', errors='replace').strip()
            raise RuntimeError(
                'Tesseract OCR 失败：{}'.format(message or result.returncode)
            )
        return _parse_tesseract_tsv(result.stdout.decode('utf8', errors='replace'))


def parse_result_header(text: str) -> ResultHeader:
    normalized = _normalize_numeric_ocr(text)
    result_match = _RESULT_PATTERN.search(normalized)
    result_text = '' if result_match is None else result_match.group(0)
    duration_match = _DURATION_PATTERN.search(normalized)
    duration_seconds = (
        None
        if duration_match is None
        else int(duration_match.group(1)) * 60 + int(duration_match.group(2))
    )
    left_kills: Optional[int] = None
    right_kills: Optional[int] = None
    left_economy: Optional[int] = None
    right_economy: Optional[int] = None
    if result_match is not None:
        left = normalized[: result_match.start()]
        right = normalized[result_match.end() :]
        left_economies = _economies(left)
        right_economies = _economies(right)
        left_economy = left_economies[-1] if left_economies else None
        right_economy = right_economies[0] if right_economies else None
        left_without_economy = _ECONOMY_PATTERN.sub(' ', left)
        right_without_economy = _ECONOMY_PATTERN.sub(' ', right)
        left_integers = _integers(left_without_economy)
        right_integers = _integers(right_without_economy)
        left_kills = left_integers[-1] if left_integers else None
        right_kills = right_integers[0] if right_integers else None
    else:
        economy_matches = tuple(_ECONOMY_PATTERN.finditer(normalized))
        economies = _economies(normalized)
        if len(economy_matches) >= 2 and len(economies) >= 2:
            left_economy = economies[0]
            right_economy = economies[1]
            middle = normalized[economy_matches[0].end() : economy_matches[1].start()]
            middle_integers = _integers(middle)
            if len(middle_integers) >= 2:
                left_kills = middle_integers[0]
                right_kills = middle_integers[-1]
    return ResultHeader(
        result_text=result_text,
        end_reason=result_end_reason(result_text),
        duration_seconds=duration_seconds,
        left_kills=left_kills,
        right_kills=right_kills,
        left_economy=left_economy,
        right_economy=right_economy,
    )


def parse_player_stats(text: str) -> PlayerStats:
    normalized = _normalize_numeric_ocr(text)
    kda = _KDA_PATTERN.search(normalized)
    repaired_kda = None if kda is not None else _repair_missing_kda_slash(normalized)
    economy_matches = tuple(_ECONOMY_PATTERN.finditer(normalized))
    economies = _economies(normalized)
    economy = economies[-1] if economies else None
    if economy is not None and economy > 100_000:
        economy = None
    last_hits: Optional[int] = None
    if economy_matches:
        trailing_integers = _integers(normalized[economy_matches[-1].end() :])
        if trailing_integers:
            candidate = trailing_integers[0]
            if candidate <= 999:
                last_hits = candidate
    kda_values = (
        repaired_kda
        if kda is None
        else (int(kda.group(1)), int(kda.group(2)), int(kda.group(3)))
    )
    return PlayerStats(
        kills=None if kda_values is None else kda_values[0],
        deaths=None if kda_values is None else kda_values[1],
        assists=None if kda_values is None else kda_values[2],
        economy=economy,
        last_hits=last_hits,
    )


def resolve_player_stats(
    candidates: Sequence[Sequence[PlayerStats]],
    *,
    header: Optional[ResultHeader] = None,
) -> Tuple[PlayerStats, ...]:
    if len(candidates) != 6:
        raise ValueError('exactly six player rows are required')
    if any(not row for row in candidates):
        raise ValueError('each player row requires at least one OCR candidate')

    choices = [_kda_choices(row) for row in candidates]
    best = min(
        product(*choices), key=lambda values: _score_kda(values, choices, header=header)
    )
    resolved = tuple(
        PlayerStats(
            kills=kda[0],
            deaths=kda[1],
            assists=kda[2],
            economy=_choose_economy(candidates[index]),
            last_hits=_choose_last_hits(candidates[index]),
        )
        for index, kda in enumerate(best)
    )
    return _validate_player_stats(resolved, header=header)


def result_end_reason(result_text: str) -> str:
    normalized = result_text.strip().casefold()
    if normalized in ('投降', 'surrender'):
        return 'surrender'
    if normalized in ('胜利', '失败', '战败', 'victory', 'defeat'):
        return 'normal'
    return 'unknown'


def normalize_player_name(value: str) -> str:
    return ''.join(unicodedata.normalize('NFKC', value).casefold().split())


def clean_player_name(value: str) -> str:
    compact = ''.join(unicodedata.normalize('NFKC', value).split())
    return _PLAYER_NAME_KDA_SUFFIX_PATTERN.sub('', compact)[:80]


def _normalize_numeric_ocr(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', value)
    return normalized.translate(_NUMERIC_TRANSLATION)


def _repair_missing_kda_slash(value: str) -> Optional[Tuple[int, int, int]]:
    match = _KDA_MISSING_SLASH_PATTERN.search(value)
    if match is None:
        return None
    kills = int(match.group(1))
    remainder = match.group(2)
    for index, character in enumerate(remainder[1:-1], 1):
        if character != '1':
            continue
        deaths_text = remainder[:index]
        assists_text = remainder[index + 1 :]
        if len(deaths_text) > 2 or len(assists_text) > 2:
            continue
        deaths = int(deaths_text)
        assists = int(assists_text)
        if deaths <= 50:
            return kills, deaths, assists
    return None


def _economies(value: str) -> List[int]:
    result: List[int] = []
    for match in _ECONOMY_PATTERN.finditer(value):
        number = float(match.group(1).replace(',', '.'))
        result.append(int(round(number * 1_000)))
    return result


def _integers(value: str) -> Sequence[int]:
    return tuple(int(match.group(1)) for match in _INTEGER_PATTERN.finditer(value))


def _parse_tesseract_tsv(value: str) -> _OcrText:
    lines: Dict[Tuple[str, str, str, str], List[str]] = {}
    confidences: List[Tuple[float, int]] = []
    for row in csv.DictReader(io.StringIO(value), delimiter='\t'):
        text = row.get('text', '').strip()
        if not text:
            continue
        key = (
            row.get('page_num', ''),
            row.get('block_num', ''),
            row.get('par_num', ''),
            row.get('line_num', ''),
        )
        lines.setdefault(key, []).append(text)
        try:
            confidence = float(row.get('conf', '-1'))
        except ValueError:
            confidence = -1
        if confidence >= 0:
            confidences.append((confidence, len(text)))
    text = '\n'.join(' '.join(words) for words in lines.values())
    total_weight = sum(weight for _, weight in confidences)
    confidence = (
        0.0
        if total_weight == 0
        else sum(value * weight for value, weight in confidences) / total_weight / 100
    )
    return _OcrText(text=text, confidence=max(0.0, min(1.0, confidence)))


def merge_result_headers(
    candidates: Sequence[Tuple[ResultHeader, float]]
) -> ResultHeader:
    if not candidates:
        return parse_result_header('')
    result_text = _choose_candidate_value(
        tuple((item.result_text, confidence) for item, confidence in candidates), ''
    )
    duration_seconds = _choose_candidate_value(
        tuple((item.duration_seconds, confidence) for item, confidence in candidates),
        None,
    )
    left_kills = _choose_candidate_value(
        tuple((item.left_kills, confidence) for item, confidence in candidates), None
    )
    right_kills = _choose_candidate_value(
        tuple((item.right_kills, confidence) for item, confidence in candidates), None
    )
    left_economy = _choose_candidate_value(
        tuple(
            (
                (
                    item.left_economy
                    if item.left_economy is not None and item.left_economy <= 100_000
                    else None
                ),
                confidence,
            )
            for item, confidence in candidates
        ),
        None,
    )
    right_economy = _choose_candidate_value(
        tuple(
            (
                (
                    item.right_economy
                    if item.right_economy is not None and item.right_economy <= 100_000
                    else None
                ),
                confidence,
            )
            for item, confidence in candidates
        ),
        None,
    )
    return ResultHeader(
        result_text=result_text,
        end_reason=result_end_reason(result_text),
        duration_seconds=duration_seconds,
        left_kills=left_kills,
        right_kills=right_kills,
        left_economy=left_economy,
        right_economy=right_economy,
    )


def _choose_candidate_value(
    values: Sequence[Tuple[_CandidateValue, float]], missing: _CandidateValue
) -> _CandidateValue:
    present = [(value, confidence) for value, confidence in values if value != missing]
    if not present:
        return missing
    counts = Counter(value for value, _ in present)
    confidence_sums: Dict[_CandidateValue, float] = {}
    first_index: Dict[_CandidateValue, int] = {}
    for index, (value, confidence) in enumerate(present):
        confidence_sums[value] = confidence_sums.get(value, 0.0) + confidence
        first_index.setdefault(value, index)
    return max(
        counts,
        key=lambda value: (counts[value], confidence_sums[value], -first_index[value]),
    )


def _select_player_name(candidates: Sequence[_OcrText]) -> Tuple[str, float]:
    present = [
        (clean_player_name(candidate.text), candidate.confidence)
        for candidate in candidates
        if clean_player_name(candidate.text)
    ]
    if not present:
        return '', 0.0
    normalized = [normalize_player_name(value) for value, _ in present]

    def score(index: int) -> float:
        value, confidence = present[index]
        exact_matches = sum(1 for other in normalized if other == normalized[index])
        similarity = sum(
            max(0.0, SequenceMatcher(None, normalized[index], other).ratio() - 0.55)
            for other in normalized
        )
        return (
            confidence
            + exact_matches * 0.08
            + similarity * 0.04
            + min(len(value), 20) * 0.002
        )

    selected = max(range(len(present)), key=score)
    value, confidence = present[selected]
    agreement = sum(1 for other in normalized if other == normalized[selected]) / len(
        normalized
    )
    return value, min(1.0, confidence * 0.85 + agreement * 0.15)


def _stats_agreement(selected: PlayerStats, candidates: Sequence[PlayerStats]) -> float:
    if not candidates:
        return 0.0
    fields = ('kills', 'deaths', 'assists', 'economy')
    agreement = 0.0
    for field in fields:
        value = getattr(selected, field)
        if value is None:
            continue
        agreement += sum(
            1 for candidate in candidates if getattr(candidate, field) == value
        ) / len(candidates)
    return agreement / len(fields)


def _header_with_resolved_team_totals(
    header: ResultHeader, players: Sequence[PlayerStats]
) -> ResultHeader:
    left = players[:3]
    right = players[3:]
    left_kills = _sum_complete(left, 'kills')
    right_kills = _sum_complete(right, 'kills')
    left_economy = _prefer_close_total(
        header.left_economy, _sum_complete(left, 'economy')
    )
    right_economy = _prefer_close_total(
        header.right_economy, _sum_complete(right, 'economy')
    )
    return ResultHeader(
        result_text=header.result_text,
        end_reason=header.end_reason,
        duration_seconds=header.duration_seconds,
        left_kills=left_kills if left_kills is not None else header.left_kills,
        right_kills=right_kills if right_kills is not None else header.right_kills,
        left_economy=left_economy,
        right_economy=right_economy,
    )


def _sum_complete(players: Sequence[PlayerStats], field: str) -> Optional[int]:
    values = [getattr(player, field) for player in players]
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


def _prefer_close_total(
    header_value: Optional[int], player_sum: Optional[int]
) -> Optional[int]:
    if player_sum is None:
        return header_value
    if (
        header_value is not None
        and header_value <= 100_000
        and abs(header_value - player_sum) <= 1_000
    ):
        return header_value
    return player_sum


def _kda_choices(
    candidates: Sequence[PlayerStats],
) -> Tuple[Tuple[Optional[int], Optional[int], Optional[int]], ...]:
    complete = [
        (item.kills, item.deaths, item.assists)
        for item in candidates
        if item.kills is not None
        and item.deaths is not None
        and item.assists is not None
        and max(item.kills, item.deaths, item.assists) <= 99
    ]
    if not complete:
        return ((None, None, None),)
    counts = Counter(complete)
    return tuple(
        value
        for value, _ in sorted(
            counts.items(), key=lambda item: (-item[1], complete.index(item[0]))
        )
    )


def _score_kda(
    values: Sequence[Tuple[Optional[int], Optional[int], Optional[int]]],
    choices: Sequence[Sequence[Tuple[Optional[int], Optional[int], Optional[int]]]],
    *,
    header: Optional[ResultHeader],
) -> Tuple[int, int, int]:
    missing = sum(1 for value in values if value[0] is None)
    if missing:
        return missing * 100_000, missing, 0

    complete: List[Tuple[int, int, int]] = []
    for value in values:
        kills, deaths, assists = value
        if kills is None or deaths is None or assists is None:
            continue
        complete.append((kills, deaths, assists))
    left = complete[:3]
    right = complete[3:]
    left_kills = sum(value[0] for value in left)
    right_kills = sum(value[0] for value in right)
    mismatch = max(0, abs(left_kills - sum(value[1] for value in right)) - 1)
    mismatch += max(0, abs(right_kills - sum(value[1] for value in left)) - 1)
    if header is not None:
        if header.left_kills is not None:
            mismatch += abs(left_kills - header.left_kills)
        if header.right_kills is not None:
            mismatch += abs(right_kills - header.right_kills)

    assist_overflow = _assist_overflow(left, left_kills)
    assist_overflow += _assist_overflow(right, right_kills)
    preference = sum(row.index(value) for row, value in zip(choices, values))
    return mismatch * 10_000 + assist_overflow * 100, preference, 0


def _validate_player_stats(
    players: Sequence[PlayerStats], *, header: Optional[ResultHeader]
) -> Tuple[PlayerStats, ...]:
    if header is None:
        return tuple(players)
    validated = list(players)
    for start, expected_kills in ((0, header.left_kills), (3, header.right_kills)):
        if expected_kills is None:
            continue
        indexes = tuple(range(start, start + 3))
        values = [validated[index].kills for index in indexes]
        if (
            all(value is not None for value in values)
            and sum(int(value) for value in values if value is not None)
            != expected_kills
        ):
            for index in indexes:
                validated[index] = replace(validated[index], kills=None)
    for start, opposing_kills in ((0, header.right_kills), (3, header.left_kills)):
        if opposing_kills is None:
            continue
        indexes = tuple(range(start, start + 3))
        values = [validated[index].deaths for index in indexes]
        if (
            all(value is not None for value in values)
            and abs(
                sum(int(value) for value in values if value is not None)
                - opposing_kills
            )
            > 1
        ):
            for index in indexes:
                validated[index] = replace(validated[index], deaths=None)
    for start, expected_kills in ((0, header.left_kills), (3, header.right_kills)):
        if expected_kills is None:
            continue
        for index in range(start, start + 3):
            stats = validated[index]
            if stats.assists is not None and (
                stats.assists > expected_kills
                or (
                    stats.kills is not None
                    and stats.assists > max(0, expected_kills - stats.kills)
                )
            ):
                validated[index] = replace(stats, assists=None)
    for start, team_economy in ((0, header.left_economy), (3, header.right_economy)):
        if team_economy is None:
            continue
        indexes = tuple(range(start, start + 3))
        economies = [validated[index].economy for index in indexes]
        if (
            any(value is None for value in economies)
            or abs(
                sum(int(value) for value in economies if value is not None)
                - team_economy
            )
            > 1_000
        ):
            for index in indexes:
                validated[index] = replace(validated[index], economy=None)
    return tuple(validated)


def _has_complete_kda(stats: PlayerStats) -> bool:
    return (
        stats.kills is not None
        and stats.deaths is not None
        and stats.assists is not None
    )


def _assist_overflow(team: Sequence[Tuple[int, int, int]], team_kills: int) -> int:
    overflow = max(0, sum(value[2] for value in team) - team_kills * 2)
    for kills, _, assists in team:
        overflow += max(0, assists - max(0, team_kills - kills))
    return overflow


def _choose_economy(candidates: Sequence[PlayerStats]) -> Optional[int]:
    values = [
        item.economy
        for item in candidates
        if item.economy is not None and item.economy <= 100_000
    ]
    if not values:
        return None
    counts = Counter(values)
    highest_count = max(counts.values())
    common = [value for value, count in counts.items() if count == highest_count]
    center = median(values)
    return min(common, key=lambda value: (abs(value - center), value))


def _choose_last_hits(candidates: Sequence[PlayerStats]) -> Optional[int]:
    values = [
        item.last_hits
        for item in candidates
        if item.last_hits is not None and item.last_hits <= 999
    ]
    if not values:
        return None
    counts = Counter(values)
    return max(counts, key=lambda value: (counts[value], -values.index(value)))
