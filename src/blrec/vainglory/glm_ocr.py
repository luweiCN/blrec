from __future__ import annotations

import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, cast

import requests
from loguru import logger

from .ocr import (
    GameTimerReading,
    OcrPlayer,
    PlayerStats,
    ResultHeader,
    ResultOcr,
    TesseractResultReader,
    clean_player_name,
    normalize_player_name,
    parse_player_stats,
    parse_result_header,
    resolve_player_stats,
)
from .vision import (
    STANDARD_VIEWPORT,
    RgbFrame,
    TeamSize,
    ViewportTransform,
    extract_result_panel,
    png_bytes,
)

_KDA_PATTERN = re.compile(r'(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})(?!\d)')
_ECONOMY_PATTERN = re.compile(r'\d{1,2}(?:[.,]\d)?\s*[kK]')
_LEADING_AUXILIARY_STAT_PATTERN = re.compile(r'^\s*\d{1,3}(?!\d)')
_TIME_TOKEN_PATTERN = re.compile(r'^\d{1,2}:\d{2}(?::\d{2})?$')
_NAME_EDGE_PATTERN = re.compile(r'^[\s,，:：;；|]+|[\s,，:：;；|]+$')
_REJECTED_NAMES = {
    '分享',
    '回放',
    '评价',
    '出装',
    '完成',
    '胜利',
    '失败',
    '战败',
    '投降',
    'share',
    'replay',
    'rate',
    'finish',
    'gameplay',
}


class GlmOcrError(RuntimeError):
    pass


@dataclass(frozen=True)
class GlmOcrResponse:
    text: str
    elapsed_ms: Optional[int]


class GlmOcrClient:
    def __init__(
        self,
        base_url: str,
        *,
        profile: str = 'standard',
        connect_timeout_seconds: float = 5,
        read_timeout_seconds: float = 180,
        maximum_width: int = 1280,
        maximum_height: int = 720,
        ffmpeg: str = 'ffmpeg',
        session: Optional[Any] = None,
    ) -> None:
        normalized_url = base_url.strip().rstrip('/')
        if not normalized_url:
            raise ValueError('GLM-OCR 服务地址不能为空')
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError('GLM-OCR 超时必须为正数')
        if maximum_width <= 0 or maximum_height <= 0:
            raise ValueError('GLM-OCR 图片尺寸必须为正数')
        self._endpoint = (
            normalized_url
            if normalized_url.endswith('/v1/ocr')
            else normalized_url + '/v1/ocr'
        )
        self._profile = profile
        self._timeout = (connect_timeout_seconds, read_timeout_seconds)
        self._maximum_width = maximum_width
        self._maximum_height = maximum_height
        self._ffmpeg = ffmpeg
        self._session = session or requests.Session()

    def recognize(self, frame: RgbFrame) -> GlmOcrResponse:
        image = self._image_payload(frame)
        started = time.monotonic()
        try:
            response = self._session.post(
                self._endpoint,
                params={'profile': self._profile},
                data=image,
                headers={'Content-Type': 'image/png'},
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as error:
            raise GlmOcrError('GLM-OCR 识别超时') from error
        except requests.RequestException as error:
            raise GlmOcrError('GLM-OCR 服务不可用') from error
        except ValueError as error:
            raise GlmOcrError('GLM-OCR 返回了无效 JSON') from error
        if not isinstance(payload, dict) or payload.get('ok') is not True:
            raise GlmOcrError('GLM-OCR 返回了失败结果')
        data = payload.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('text'), str):
            raise GlmOcrError('GLM-OCR 响应缺少识别文本')
        meta = payload.get('meta')
        if isinstance(meta, dict) and meta.get('upstream_done') is False:
            raise GlmOcrError('GLM-OCR 上游响应未完整结束')
        text = str(data['text'])
        if not any(character.isalnum() for character in text):
            raise GlmOcrError('GLM-OCR 没有返回可用文字')
        elapsed_ms = (
            int(meta['elapsed_ms'])
            if isinstance(meta, dict)
            and isinstance(meta.get('elapsed_ms'), (int, float))
            else None
        )
        logger.info(
            'Vainglory GLM-OCR completed: request_seconds={:.3f} '
            'service_elapsed_ms={} input_bytes={} input_size={}x{}',
            time.monotonic() - started,
            elapsed_ms,
            len(image),
            frame.width,
            frame.height,
        )
        return GlmOcrResponse(text=text, elapsed_ms=elapsed_ms)

    def _image_payload(self, frame: RgbFrame) -> bytes:
        scale = min(
            1.0, self._maximum_width / frame.width, self._maximum_height / frame.height
        )
        if scale >= 1:
            return png_bytes(frame)
        width = max(1, int(round(frame.width * scale)))
        height = max(1, int(round(frame.height * scale)))
        command = [
            self._ffmpeg,
            '-nostdin',
            '-v',
            'error',
            '-threads',
            '1',
            '-f',
            'rawvideo',
            '-pix_fmt',
            'rgb24',
            '-s',
            '{}x{}'.format(frame.width, frame.height),
            '-i',
            'pipe:0',
            '-frames:v',
            '1',
            '-vf',
            'scale={}:{}:flags=bicubic'.format(width, height),
            '-an',
            '-sn',
            '-dn',
            '-f',
            'image2pipe',
            '-vcodec',
            'png',
            'pipe:1',
        ]
        try:
            result = subprocess.run(
                command,
                input=frame.pixels,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            resized = frame.resize_nearest(width, height)
            return png_bytes(resized)
        if result.returncode != 0 or not result.stdout.startswith(b'\x89PNG'):
            resized = frame.resize_nearest(width, height)
            return png_bytes(resized)
        return result.stdout


class GlmOcrResultReader:
    def __init__(
        self,
        client: GlmOcrClient,
        *,
        fallback: Optional[TesseractResultReader] = None,
        maximum_remote_frames: int = 3,
    ) -> None:
        if maximum_remote_frames < 1:
            raise ValueError('GLM-OCR 至少需要尝试一帧')
        self._client = client
        self._fallback = fallback or TesseractResultReader()
        self._maximum_remote_frames = maximum_remote_frames

    def read_header(
        self,
        frame: RgbFrame,
        *,
        viewport: ViewportTransform = STANDARD_VIEWPORT,
        team_size: int = 3,
    ) -> ResultHeader:
        return self._fallback.read_header(frame, viewport=viewport, team_size=team_size)

    def read(
        self,
        frame: RgbFrame,
        *,
        header: Optional[ResultHeader] = None,
        viewport: ViewportTransform = STANDARD_VIEWPORT,
        name_frames: Sequence[RgbFrame] = (),
        team_size: int = 3,
    ) -> ResultOcr:
        return self._read_result(
            frame,
            header=header,
            viewport=viewport,
            name_frames=name_frames,
            wide_screenshot=False,
            team_size=team_size,
        )

    def read_wide_screenshot(
        self,
        frame: RgbFrame,
        *,
        header: Optional[ResultHeader] = None,
        viewport: ViewportTransform = STANDARD_VIEWPORT,
        name_frames: Sequence[RgbFrame] = (),
        team_size: int = 3,
    ) -> ResultOcr:
        return self._read_result(
            frame,
            header=header,
            viewport=viewport,
            name_frames=name_frames,
            wide_screenshot=True,
            team_size=team_size,
        )

    def read_game_timer(self, frame: RgbFrame) -> GameTimerReading:
        return self._fallback.read_game_timer(frame)

    def _read_result(
        self,
        frame: RgbFrame,
        *,
        header: Optional[ResultHeader],
        viewport: ViewportTransform,
        name_frames: Sequence[RgbFrame],
        wide_screenshot: bool,
        team_size: int,
    ) -> ResultOcr:
        if team_size not in (3, 5):
            raise ValueError('team size must be 3 or 5')
        panel_team_size = cast(TeamSize, team_size)
        local_header = header or self.read_header(
            frame, viewport=viewport, team_size=team_size
        )
        remote_results: List[ResultOcr] = []
        candidates = (frame, *name_frames)[: self._maximum_remote_frames]
        for index, candidate in enumerate(candidates, 1):
            try:
                panel = extract_result_panel(
                    candidate, viewport=viewport, team_size=panel_team_size
                )
                remote = parse_glm_result(
                    self._client.recognize(panel).text, team_size=team_size
                )
            except GlmOcrError as error:
                logger.warning('Vainglory GLM-OCR frame failed: reason={!r}', error)
                continue
            remote_results.append(remote)
            if _result_is_reliable(
                remote, fallback_header=local_header, team_size=team_size
            ):
                break
            logger.warning(
                'Vainglory GLM-OCR result failed consistency validation: '
                'frame={}/{} names={} complete_stats={}',
                index,
                len(candidates),
                sum(bool(player.name) for player in remote.players),
                sum(_has_complete_stats(player.stats) for player in remote.players),
            )
        if remote_results and any(
            player.name or _has_any_stats(player.stats)
            for result in remote_results
            for player in result.players
        ):
            return merge_glm_results(
                remote_results, header=local_header, team_size=team_size
            )
        logger.warning('Vainglory GLM-OCR yielded no player data; using local fallback')
        if wide_screenshot:
            return self._fallback.read_wide_screenshot(
                frame,
                header=local_header,
                viewport=viewport,
                name_frames=name_frames,
                team_size=team_size,
            )
        return self._fallback.read(
            frame,
            header=local_header,
            viewport=viewport,
            name_frames=name_frames,
            team_size=team_size,
        )


def parse_glm_result(text: str, *, team_size: int = 3) -> ResultOcr:
    if team_size not in (3, 5):
        raise ValueError('team size must be 3 or 5')
    expected_players = team_size * 2
    header = parse_result_header(text)
    parsed_rows: List[List[OcrPlayer]] = []
    flat_entries: List[OcrPlayer] = []
    raw_rows: List[str] = []
    for paragraph in re.split(r'(?:\r?\n){2,}', text):
        lines: List[str] = []
        pending_name = ''
        for raw_line in paragraph.splitlines():
            line = ' '.join(raw_line.split())
            match = _KDA_PATTERN.search(line)
            if match is None:
                tokens = line.split()
                pending_name = _valid_player_name(tokens[-1]) if tokens else ''
                continue
            if pending_name and not _name_before_kda(line[: match.start()]):
                line = '{} {}'.format(pending_name, line)
            pending_name = ''
            lines.append(line)
        match_count = sum(len(_KDA_PATTERN.findall(line)) for line in lines)
        if 0 < match_count <= 2:
            raw_rows.append(' '.join(lines))
        else:
            raw_rows.extend(lines)
    if len(raw_rows) >= expected_players and all(
        len(_KDA_PATTERN.findall(row)) == 1 for row in raw_rows
    ):
        raw_rows = [
            ' '.join(raw_rows[index : index + 2])
            for index in range(0, len(raw_rows), 2)
        ]
    for raw_line in raw_rows:
        line = ' '.join(raw_line.split())
        if not line:
            continue
        matches = tuple(_KDA_PATTERN.finditer(line))
        if not matches:
            continue
        row: List[OcrPlayer] = []
        for index, match in enumerate(matches[:2]):
            previous_end = 0 if index == 0 else matches[index - 1].end()
            name = _name_before_kda(line[previous_end : match.start()])
            stats_end = (
                len(line) if index + 1 >= len(matches) else matches[index + 1].start()
            )
            stats = parse_player_stats(line[match.start() : stats_end])
            row.append(_unpositioned_player(name, stats))
        if len(row) == 1:
            trailing_name = _trailing_name_after_stats(line[matches[0].end() :])
            if trailing_name:
                row.append(
                    _unpositioned_player(
                        trailing_name, PlayerStats(None, None, None, None)
                    )
                )
        parsed_rows.append(row)
        flat_entries.extend(row)

    positioned: Dict[Tuple[str, int], OcrPlayer] = {}
    paired_rows = [row for row in parsed_rows if len(row) >= 2]
    if len(paired_rows) >= 2:
        for slot, row in enumerate(parsed_rows[:team_size], 1):
            if row:
                positioned[('left', slot)] = _position_player(row[0], 'left', slot)
            if len(row) >= 2:
                positioned[('right', slot)] = _position_player(row[1], 'right', slot)
    elif len(flat_entries) >= expected_players:
        for index, player in enumerate(flat_entries[:expected_players]):
            side = 'left' if index % 2 == 0 else 'right'
            slot = index // 2 + 1
            positioned[(side, slot)] = _position_player(player, side, slot)
    else:
        for slot, row in enumerate(parsed_rows[:team_size], 1):
            if row:
                positioned[('left', slot)] = _position_player(row[0], 'left', slot)

    if len(flat_entries) >= expected_players:
        column_positioned: Dict[Tuple[str, int], OcrPlayer] = {}
        for index, player in enumerate(flat_entries[:expected_players]):
            side = 'left' if index < team_size else 'right'
            slot = index + 1 if side == 'left' else index - team_size + 1
            column_positioned[(side, slot)] = _position_player(player, side, slot)
        current_result = ResultOcr(
            header=header,
            players=_players_from_positions(positioned, team_size=team_size),
        )
        column_result = ResultOcr(
            header=header,
            players=_players_from_positions(column_positioned, team_size=team_size),
        )
        if not _result_matches_header(
            current_result, header, team_size=team_size
        ) and _result_matches_header(column_result, header, team_size=team_size):
            positioned = column_positioned

    return ResultOcr(
        header=header,
        players=_players_from_positions(positioned, team_size=team_size),
        raw_text=text,
        observed_player_count=len(_KDA_PATTERN.findall(text)),
    )


def _players_from_positions(
    positioned: Mapping[Tuple[str, int], OcrPlayer], *, team_size: int
) -> Tuple[OcrPlayer, ...]:
    players: List[OcrPlayer] = []
    for side in ('left', 'right'):
        for slot in range(1, team_size + 1):
            players.append(
                positioned.get(
                    (side, slot),
                    OcrPlayer(
                        side=side,
                        slot=slot,
                        name='',
                        normalized_name='',
                        stats=PlayerStats(None, None, None, None),
                        confidence=0,
                    ),
                )
            )
    return tuple(players)


def merge_glm_results(
    candidates: Sequence[ResultOcr], *, header: ResultHeader, team_size: int = 3
) -> ResultOcr:
    if not candidates:
        return ResultOcr(header=header, players=())
    by_position: Dict[Tuple[str, int], List[OcrPlayer]] = {}
    for candidate in candidates:
        for player in candidate.players:
            by_position.setdefault((player.side, player.slot), []).append(player)
    positions = tuple(
        (side, slot) for side in ('left', 'right') for slot in range(1, team_size + 1)
    )
    stats_candidates = tuple(
        tuple(item.stats for item in by_position.get(position, ()))
        for position in positions
    )
    merged_header, resolved_stats = _resolve_with_supported_header(
        stats_candidates,
        local_header=header,
        remote_header=_merge_remote_headers(tuple(item.header for item in candidates)),
    )
    players: List[OcrPlayer] = []
    for position, stats in zip(positions, resolved_stats):
        side, slot = position
        rows = by_position.get(position, ())
        players.append(_merge_players(rows, side=side, slot=slot, stats=stats))
    return ResultOcr(
        header=merged_header,
        players=tuple(players),
        raw_text='\n'.join(
            candidate.raw_text for candidate in candidates if candidate.raw_text
        ),
        observed_player_count=max(
            (
                candidate.observed_player_count
                for candidate in candidates
                if candidate.observed_player_count is not None
            ),
            default=None,
        ),
    )


def _resolve_with_supported_header(
    candidates: Sequence[Sequence[PlayerStats]],
    *,
    local_header: ResultHeader,
    remote_header: ResultHeader,
) -> Tuple[ResultHeader, Tuple[PlayerStats, ...]]:
    local_first = _prefer_header(local_header, remote_header)
    remote_first = _prefer_header(remote_header, local_header)
    headers = (local_first, _with_team_totals(local_first, remote_first))
    resolved = tuple(
        (candidate_header, resolve_player_stats(candidates, header=candidate_header))
        for candidate_header in headers
    )
    return max(
        resolved,
        key=lambda item: (
            sum(_has_complete_kda(stats) for stats in item[1]),
            sum(stats.economy is not None for stats in item[1]),
            -headers.index(item[0]),
        ),
    )


def _unpositioned_player(name: str, stats: PlayerStats) -> OcrPlayer:
    confidence = _player_confidence(name, stats)
    return OcrPlayer(
        side='',
        slot=0,
        name=name,
        normalized_name=normalize_player_name(name),
        stats=stats,
        confidence=confidence,
        raw_name=name,
    )


def _position_player(player: OcrPlayer, side: str, slot: int) -> OcrPlayer:
    return OcrPlayer(
        side=side,
        slot=slot,
        name=player.name,
        normalized_name=player.normalized_name,
        stats=player.stats,
        confidence=player.confidence,
        raw_name=player.raw_name,
    )


def _name_before_kda(value: str) -> str:
    tokens = value.split()
    if not tokens:
        return ''
    return _valid_player_name(tokens[-1])


def _trailing_name_after_stats(value: str) -> str:
    economy = _ECONOMY_PATTERN.search(value)
    if economy is None:
        return ''
    trailing = value[economy.end() :]
    trailing = _LEADING_AUXILIARY_STAT_PATTERN.sub('', trailing, count=1)
    tokens = trailing.split()
    if not tokens:
        return ''
    return _valid_player_name(tokens[-1])


def _valid_player_name(value: str) -> str:
    candidate = clean_player_name(_NAME_EDGE_PATTERN.sub('', value))
    if (
        not candidate
        or candidate.casefold() in _REJECTED_NAMES
        or candidate.isdecimal()
        or _ECONOMY_PATTERN.fullmatch(candidate) is not None
        or _TIME_TOKEN_PATTERN.fullmatch(candidate) is not None
        or '/' in candidate
    ):
        return ''
    if not any(character.isalnum() for character in candidate):
        return ''
    return candidate


def _player_confidence(name: str, stats: PlayerStats) -> float:
    confidence = 0.25 if name else 0.0
    if (
        stats.kills is not None
        and stats.deaths is not None
        and stats.assists is not None
    ):
        confidence += 0.45
    if stats.economy is not None:
        confidence += 0.2
    return min(1.0, confidence)


def _merge_players(
    candidates: Sequence[OcrPlayer],
    *,
    side: str,
    slot: int,
    stats: Optional[PlayerStats] = None,
) -> OcrPlayer:
    names = [item.name for item in candidates if item.name]
    if names:
        normalized = [normalize_player_name(value) for value in names]
        counts = Counter(normalized)
        confidence_sums = {
            value: sum(
                item.confidence
                for item in candidates
                if item.name and normalize_player_name(item.name) == value
            )
            for value in counts
        }
        selected_normalized = max(
            counts,
            key=lambda value: (
                counts[value],
                confidence_sums[value],
                -normalized.index(value),
            ),
        )
        name = next(
            value
            for value in names
            if normalize_player_name(value) == selected_normalized
        )
        raw_name = next(
            (
                item.raw_name
                for item in candidates
                if item.name and normalize_player_name(item.name) == selected_normalized
            ),
            name,
        )
    else:
        name = ''
        raw_name = ''
    if stats is None:
        stats = PlayerStats(
            kills=_choose_stat(candidates, 'kills'),
            deaths=_choose_stat(candidates, 'deaths'),
            assists=_choose_stat(candidates, 'assists'),
            economy=_choose_stat(candidates, 'economy'),
            last_hits=None,
        )
    return OcrPlayer(
        side=side,
        slot=slot,
        name=name,
        normalized_name=normalize_player_name(name),
        stats=stats,
        confidence=_player_confidence(name, stats),
        raw_name=raw_name,
    )


def _choose_stat(candidates: Sequence[OcrPlayer], field: str) -> Optional[int]:
    values = [
        value
        for value in (getattr(item.stats, field) for item in candidates)
        if value is not None
    ]
    if not values:
        return None
    counts = Counter(values)
    return max(counts, key=lambda value: (counts[value], -values.index(value)))


def _merge_remote_headers(candidates: Sequence[ResultHeader]) -> ResultHeader:
    def choose(field: str) -> Any:
        values = [getattr(item, field) for item in candidates]
        present = [value for value in values if value not in (None, '')]
        if not present:
            return None if field != 'result_text' else ''
        counts = Counter(present)
        return max(counts, key=lambda value: (counts[value], -present.index(value)))

    result_text = str(choose('result_text') or '')
    parsed = parse_result_header(result_text)
    return ResultHeader(
        result_text=result_text,
        end_reason=parsed.end_reason,
        duration_seconds=choose('duration_seconds'),
        left_kills=choose('left_kills'),
        right_kills=choose('right_kills'),
        left_economy=choose('left_economy'),
        right_economy=choose('right_economy'),
    )


def _prefer_header(primary: ResultHeader, fallback: ResultHeader) -> ResultHeader:
    result_text = primary.result_text or fallback.result_text
    return ResultHeader(
        result_text=result_text,
        end_reason=(
            primary.end_reason
            if primary.end_reason != 'unknown'
            else fallback.end_reason
        ),
        duration_seconds=(
            primary.duration_seconds
            if primary.duration_seconds is not None
            else fallback.duration_seconds
        ),
        left_kills=(
            primary.left_kills
            if primary.left_kills is not None
            else fallback.left_kills
        ),
        right_kills=(
            primary.right_kills
            if primary.right_kills is not None
            else fallback.right_kills
        ),
        left_economy=(
            primary.left_economy
            if primary.left_economy is not None
            else fallback.left_economy
        ),
        right_economy=(
            primary.right_economy
            if primary.right_economy is not None
            else fallback.right_economy
        ),
    )


def _with_team_totals(base: ResultHeader, totals: ResultHeader) -> ResultHeader:
    return ResultHeader(
        result_text=base.result_text,
        end_reason=base.end_reason,
        duration_seconds=base.duration_seconds,
        left_kills=totals.left_kills,
        right_kills=totals.right_kills,
        left_economy=totals.left_economy,
        right_economy=totals.right_economy,
    )


def _result_is_reliable(
    result: ResultOcr, *, fallback_header: ResultHeader, team_size: int = 3
) -> bool:
    if (
        result.header.duration_seconds is None
        and fallback_header.duration_seconds is None
    ):
        return False
    names = sum(bool(player.name) for player in result.players)
    if names != team_size * 2 or len(result.players) != team_size * 2:
        return False
    if any(not _has_complete_stats(player.stats) for player in result.players):
        return False
    local_first = _prefer_header(fallback_header, result.header)
    remote_first = _prefer_header(result.header, fallback_header)
    headers = (local_first, _with_team_totals(local_first, remote_first))
    return any(
        _result_matches_header(result, header, team_size=team_size)
        for header in headers
    )


def _result_matches_header(
    result: ResultOcr, header: ResultHeader, *, team_size: int = 3
) -> bool:
    left = result.players[:team_size]
    right = result.players[team_size:]
    left_kills = sum(int(player.stats.kills or 0) for player in left)
    right_kills = sum(int(player.stats.kills or 0) for player in right)
    left_deaths = sum(int(player.stats.deaths or 0) for player in left)
    right_deaths = sum(int(player.stats.deaths or 0) for player in right)
    if abs(left_kills - right_deaths) > 1 or abs(right_kills - left_deaths) > 1:
        return False
    if header.left_kills is not None and left_kills != header.left_kills:
        return False
    if header.right_kills is not None and right_kills != header.right_kills:
        return False
    for team, team_kills in ((left, left_kills), (right, right_kills)):
        if any(
            int(player.stats.assists or 0)
            > max(0, team_kills - int(player.stats.kills or 0))
            for player in team
        ):
            return False
    for team, team_economy in (
        (left, header.left_economy),
        (right, header.right_economy),
    ):
        if team_economy is None:
            continue
        total = sum(int(player.stats.economy or 0) for player in team)
        if abs(total - team_economy) > 1_000:
            return False
    return True


def _has_any_stats(stats: PlayerStats) -> bool:
    return any(
        value is not None
        for value in (stats.kills, stats.deaths, stats.assists, stats.economy)
    )


def _has_complete_stats(stats: PlayerStats) -> bool:
    return (
        stats.kills is not None
        and stats.deaths is not None
        and stats.assists is not None
        and stats.economy is not None
    )


def _has_complete_kda(stats: PlayerStats) -> bool:
    return (
        stats.kills is not None
        and stats.deaths is not None
        and stats.assists is not None
    )
