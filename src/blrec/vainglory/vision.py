from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass
from statistics import median
from typing import Any, Iterator, List, Literal, Optional, Sequence, Tuple, cast

from loguru import logger

TeamColor = Literal['teal', 'orange']
TeamSide = Literal['left', 'right']
TeamSize = Literal[3, 5]

_MINIMUM_RESULT_ACTION_CONTRAST = 35
_MINIMUM_LOW_CONTRAST_RESULT_ACTION = 18
_MAXIMUM_LOW_CONTRAST_RESULT_ACTION = 30
_MINIMUM_RESULT_ACTION_BALANCE = 0.45
GAMEPLAY_HUD_CENTER_VARIANTS: Tuple[Tuple[float, ...], ...] = (
    (0.365, 0.415, 0.465, 0.55, 0.60, 0.65),
    (0.33, 0.395, 0.46, 0.54, 0.605, 0.67),
    (0.32, 0.385, 0.45, 0.565, 0.635, 0.705),
    (0.38, 0.42, 0.46, 0.54, 0.58, 0.62),
    (0.40, 0.42, 0.47, 0.54, 0.57, 0.60),
)
GAMEPLAY_HUD_FIVE_CENTER_VARIANTS: Tuple[Tuple[float, ...], ...] = (
    (0.275, 0.328, 0.382, 0.435, 0.485, 0.537, 0.590, 0.643, 0.697, 0.750),
    (0.234, 0.268, 0.302, 0.336, 0.370, 0.434, 0.468, 0.502, 0.536, 0.570),
    (0.250, 0.292, 0.334, 0.376, 0.418, 0.482, 0.524, 0.566, 0.608, 0.650),
    (0.300, 0.342, 0.384, 0.426, 0.468, 0.532, 0.574, 0.616, 0.658, 0.700),
    (0.312, 0.365, 0.418, 0.471, 0.524, 0.576, 0.629, 0.682, 0.735, 0.788),
    (0.170, 0.220, 0.270, 0.320, 0.370, 0.430, 0.480, 0.530, 0.580, 0.630),
)


@dataclass(frozen=True)
class PixelRect:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if self.left < 0 or self.top < 0:
            raise ValueError('pixel rectangle must not start outside the frame')
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError('pixel rectangle must have a positive size')


@dataclass(frozen=True)
class ResultPanelDetection:
    rect: PixelRect
    confidence: float

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError('result panel confidence must be between 0 and 1')


@dataclass(frozen=True)
class HeroAvatarDetection:
    rect: PixelRect
    confidence: float

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError('hero avatar confidence must be between 0 and 1')


@dataclass(frozen=True)
class RgbFrame:
    width: int
    height: int
    pixels: bytes

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError('frame dimensions must be positive')
        if len(self.pixels) != self.width * self.height * 3:
            raise ValueError('RGB frame byte length does not match its dimensions')

    def relative_rect(
        self, left: float, top: float, right: float, bottom: float
    ) -> PixelRect:
        return PixelRect(
            max(0, min(self.width - 1, int(round(left * self.width)))),
            max(0, min(self.height - 1, int(round(top * self.height)))),
            max(1, min(self.width, int(round(right * self.width)))),
            max(1, min(self.height, int(round(bottom * self.height)))),
        )

    def colors(
        self, rect: PixelRect, *, step: int = 1
    ) -> Iterator[Tuple[int, int, int]]:
        if rect.right > self.width or rect.bottom > self.height:
            raise ValueError('pixel rectangle extends outside the frame')
        if step < 1:
            raise ValueError('pixel step must be positive')
        pixels = self.pixels
        width = self.width
        for y in range(rect.top, rect.bottom, step):
            row = y * width * 3
            for x in range(rect.left, rect.right, step):
                offset = row + x * 3
                yield pixels[offset], pixels[offset + 1], pixels[offset + 2]

    def crop(self, rect: PixelRect) -> RgbFrame:
        if rect.right > self.width or rect.bottom > self.height:
            raise ValueError('pixel rectangle extends outside the frame')
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        row_bytes = width * 3
        source_row_bytes = self.width * 3
        output = bytearray(row_bytes * height)
        for target_y, source_y in enumerate(range(rect.top, rect.bottom)):
            source_start = source_y * source_row_bytes + rect.left * 3
            target_start = target_y * row_bytes
            output[target_start : target_start + row_bytes] = self.pixels[
                source_start : source_start + row_bytes
            ]
        return RgbFrame(width, height, bytes(output))

    def resize_nearest(self, width: int, height: int) -> RgbFrame:
        if width <= 0 or height <= 0:
            raise ValueError('target dimensions must be positive')
        output = bytearray(width * height * 3)
        for target_y in range(height):
            source_y = min(self.height - 1, target_y * self.height // height)
            for target_x in range(width):
                source_x = min(self.width - 1, target_x * self.width // width)
                source = (source_y * self.width + source_x) * 3
                target = (target_y * width + target_x) * 3
                output[target : target + 3] = self.pixels[source : source + 3]
        return RgbFrame(width, height, bytes(output))

    def ppm_bytes(self) -> bytes:
        header = 'P6\n{} {}\n255\n'.format(self.width, self.height).encode('ascii')
        return header + self.pixels

    def threshold(self, value: int) -> RgbFrame:
        if value < 0 or value > 255:
            raise ValueError('threshold must be between 0 and 255')
        output = bytearray(len(self.pixels))
        for offset in range(0, len(self.pixels), 3):
            red, green, blue = self.pixels[offset : offset + 3]
            gray = 255 if (red * 3 + green * 6 + blue) // 10 > value else 0
            output[offset : offset + 3] = bytes((gray, gray, gray))
        return RgbFrame(self.width, self.height, bytes(output))


@dataclass(frozen=True)
class ViewportTransform:
    name: str
    left: float
    top: float
    width: float
    height: float
    ocr_profile: Literal['standard', 'wide']

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError('viewport must have a positive size')
        if (
            self.left >= 1
            or self.top >= 1
            or self.left + self.width <= 0
            or self.top + self.height <= 0
        ):
            raise ValueError('viewport must overlap the reference frame')

    def source_rect(
        self, frame: RgbFrame, left: float, top: float, right: float, bottom: float
    ) -> PixelRect:
        source_left = max(0.0, min(1.0, (left - self.left) / self.width))
        source_top = max(0.0, min(1.0, (top - self.top) / self.height))
        source_right = max(0.0, min(1.0, (right - self.left) / self.width))
        source_bottom = max(0.0, min(1.0, (bottom - self.top) / self.height))
        if source_right <= source_left or source_bottom <= source_top:
            raise ValueError('reference rectangle falls outside the source viewport')
        return frame.relative_rect(source_left, source_top, source_right, source_bottom)


STANDARD_VIEWPORT = ViewportTransform(
    name='standard', left=0.0, top=0.0, width=1.0, height=1.0, ocr_profile='standard'
)


@dataclass(frozen=True)
class ResultLayout:
    left_color: TeamColor
    right_color: TeamColor
    winner_color: TeamColor
    winner_side: TeamSide
    confidence: float
    team_size: TeamSize = 3
    viewport: ViewportTransform = STANDARD_VIEWPORT


@dataclass(frozen=True)
class HeroFrame:
    side: TeamSide
    slot: int
    frame: RgbFrame


@dataclass(frozen=True)
class RecordedPlayer:
    side: TeamSide
    slot: int
    confidence: float


@dataclass(frozen=True)
class GameplayHud:
    signature: str
    team_size: TeamSize
    visible_portraits: int


def detect_active_content_rect(frame: RgbFrame) -> PixelRect:
    """Locate a materially letterboxed/windowed capture inside a video frame."""
    x_step = max(1, math.ceil(frame.width / 120))
    y_step = max(1, math.ceil(frame.height / 68))
    sampled_x = tuple(range(0, frame.width, x_step))
    sampled_y = tuple(range(0, frame.height, y_step))
    row_bright = [0] * len(sampled_y)
    column_bright = [0] * len(sampled_x)
    pixels = frame.pixels
    row_bytes = frame.width * 3
    for row_index, y in enumerate(sampled_y):
        row_offset = y * row_bytes
        for column_index, x in enumerate(sampled_x):
            offset = row_offset + x * 3
            red, green, blue = pixels[offset : offset + 3]
            if red * 3 + green * 6 + blue > 240:
                row_bright[row_index] += 1
                column_bright[column_index] += 1

    minimum_row_bright = max(2, round(len(sampled_x) * 0.10))
    minimum_column_bright = max(2, round(len(sampled_y) * 0.10))
    content_rows = tuple(
        index for index, bright in enumerate(row_bright) if bright >= minimum_row_bright
    )
    content_columns = tuple(
        index
        for index, bright in enumerate(column_bright)
        if bright >= minimum_column_bright
    )
    if not content_rows or not content_columns:
        return PixelRect(0, 0, frame.width, frame.height)

    def row_has_content(y: int) -> bool:
        row_offset = y * row_bytes
        bright = 0
        for x in sampled_x:
            offset = row_offset + x * 3
            red, green, blue = pixels[offset : offset + 3]
            if red * 3 + green * 6 + blue > 240:
                bright += 1
        return bright >= minimum_row_bright

    def column_has_content(x: int) -> bool:
        bright = 0
        for y in sampled_y:
            offset = y * row_bytes + x * 3
            red, green, blue = pixels[offset : offset + 3]
            if red * 3 + green * 6 + blue > 240:
                bright += 1
        return bright >= minimum_column_bright

    first_row = sampled_y[content_rows[0]]
    last_row = sampled_y[content_rows[-1]]
    top = next(
        y
        for y in range(max(0, first_row - y_step + 1), first_row + 1)
        if row_has_content(y)
    )
    bottom = next(
        y + 1
        for y in range(min(frame.height - 1, last_row + y_step - 1), last_row - 1, -1)
        if row_has_content(y)
    )
    first_column = sampled_x[content_columns[0]]
    last_column = sampled_x[content_columns[-1]]
    left = next(
        x
        for x in range(max(0, first_column - x_step + 1), first_column + 1)
        if column_has_content(x)
    )
    right = next(
        x + 1
        for x in range(
            min(frame.width - 1, last_column + x_step - 1), last_column - 1, -1
        )
        if column_has_content(x)
    )
    candidate = PixelRect(left, top, right, bottom)
    materially_cropped = (
        max(
            candidate.left / frame.width,
            candidate.top / frame.height,
            (frame.width - candidate.right) / frame.width,
            (frame.height - candidate.bottom) / frame.height,
        )
        >= 0.025
    )
    sufficiently_large = (
        candidate.right - candidate.left >= frame.width * 0.55
        and candidate.bottom - candidate.top >= frame.height * 0.50
    )
    if not materially_cropped or not sufficiently_large:
        return PixelRect(0, 0, frame.width, frame.height)
    return candidate


def normalize_gameplay_frame(frame: RgbFrame) -> RgbFrame:
    rect = detect_active_content_rect(frame)
    if rect == PixelRect(0, 0, frame.width, frame.height):
        return frame
    return frame.crop(rect)


def detect_result_layout(
    frame: RgbFrame, *, panel_detection: Optional[ResultPanelDetection] = None
) -> Optional[ResultLayout]:
    layouts = detect_result_layouts(frame, panel_detection=panel_detection)
    if not layouts:
        return None
    return max(layouts, key=lambda layout: layout.confidence)


def detect_result_layouts(
    frame: RgbFrame, *, panel_detection: Optional[ResultPanelDetection] = None
) -> Tuple[ResultLayout, ...]:
    if panel_detection is None and detect_gameplay_hud(frame) is not None:
        return ()
    layouts: List[ResultLayout] = []
    candidates: List[Tuple[ViewportTransform, TeamSize]] = []
    team_sizes: Tuple[TeamSize, ...]
    if panel_detection is not None:
        team_sizes = _detected_panel_team_sizes(panel_detection)
        candidates.extend(
            (_panel_viewport(frame, panel_detection, team_size), team_size)
            for team_size in team_sizes
        )
        candidates.extend(
            (viewport, team_size)
            for viewport in _result_viewports(frame.width / frame.height)
            for team_size in team_sizes
        )
    else:
        team_sizes = (cast(TeamSize, 3), cast(TeamSize, 5))
        candidates.extend(
            (viewport, team_size)
            for viewport in _result_viewports(frame.width / frame.height)
            for team_size in team_sizes
        )
    for viewport, team_size in candidates:
        standard_action_contrasts = _result_action_contrasts(
            frame, STANDARD_VIEWPORT, team_size=team_size
        )
        action_contrasts = _result_action_contrasts(
            frame, viewport, team_size=team_size
        )
        action_balance = min(action_contrasts) / max(1.0, max(action_contrasts))
        strong_actions = sum(value >= 30 for value in action_contrasts)
        minimum_strong_actions = 2 if team_size == 5 else 3
        if (
            panel_detection is not None
            and action_balance < _MINIMUM_RESULT_ACTION_BALANCE
            and strong_actions < minimum_strong_actions
        ):
            _log_layout_attempt(
                frame,
                viewport,
                'result_action_balance',
                team_size=team_size,
                viewport_contrasts=tuple(round(value, 2) for value in action_contrasts),
                balance=round(action_balance, 3),
                strong_actions=strong_actions,
            )
            continue
        minimum_action_contrast: float
        if panel_detection is None:
            low_contrast_actions = (
                min(action_contrasts) >= _MINIMUM_LOW_CONTRAST_RESULT_ACTION
                and max(action_contrasts) <= _MAXIMUM_LOW_CONTRAST_RESULT_ACTION
            )
            minimum_action_contrast = (
                _MINIMUM_LOW_CONTRAST_RESULT_ACTION
                if low_contrast_actions
                else _MINIMUM_RESULT_ACTION_CONTRAST
            )
        else:
            reference_contrast = 18 if viewport.name == 'detected-desktop-3v3' else 24
            minimum_action_contrast = max(
                6.0, reference_contrast * min(1.0, frame.height / 1080)
            )
        if min(action_contrasts) < minimum_action_contrast and not (
            panel_detection is not None and strong_actions >= 2
        ):
            _log_layout_attempt(
                frame,
                viewport,
                'result_actions',
                team_size=team_size,
                standard_contrasts=tuple(
                    round(value, 2) for value in standard_action_contrasts
                ),
                viewport_contrasts=tuple(round(value, 2) for value in action_contrasts),
            )
            continue
        layout = _detect_result_layout(frame, viewport, team_size=team_size)
        if layout is not None:
            if panel_detection is not None:
                layout = ResultLayout(
                    left_color=layout.left_color,
                    right_color=layout.right_color,
                    winner_color=layout.winner_color,
                    winner_side=layout.winner_side,
                    confidence=min(
                        1.0,
                        layout.confidence * 0.55 + panel_detection.confidence * 0.45,
                    ),
                    team_size=layout.team_size,
                    viewport=layout.viewport,
                )
            layouts.append(layout)
    return tuple(layouts)


def _panel_viewport(
    frame: RgbFrame, detection: ResultPanelDetection, team_size: TeamSize
) -> ViewportTransform:
    panel_left = detection.rect.left / frame.width
    panel_top = detection.rect.top / frame.height
    panel_width = (detection.rect.right - detection.rect.left) / frame.width
    panel_height = (detection.rect.bottom - detection.rect.top) / frame.height
    reference_top, reference_bottom = (0.09, 0.91) if team_size == 5 else (0.22, 0.78)
    source_height = panel_height / (reference_bottom - reference_top)
    source_top = panel_top - reference_top * source_height
    desktop = team_size == 3 and panel_height < 0.52
    return ViewportTransform(
        name=(
            'detected-desktop-3v3'
            if desktop
            else 'detected-{}v{}'.format(team_size, team_size)
        ),
        left=-panel_left / panel_width,
        top=-source_top / source_height,
        width=1.0 / panel_width,
        height=1.0 / source_height,
        ocr_profile='wide' if desktop else 'standard',
    )


def _detected_panel_team_sizes(detection: ResultPanelDetection) -> Tuple[TeamSize, ...]:
    # The detector box follows the visible scoreboard content, whose height varies
    # with capture layout and animation state. It is therefore not team-size
    # evidence: validate both layouts and let OCR/player rows decide.
    return (3, 5)


def _detect_result_layout(
    frame: RgbFrame, viewport: ViewportTransform, *, team_size: TeamSize
) -> Optional[ResultLayout]:
    if team_size == 5:
        result_rect = (0.48, 0.10, 0.52, 0.20)
        defeat_rect = (0.42, 0.10, 0.58, 0.20)
        panel_rect = (0.01, 0.09, 0.99, 0.905)
        header_rect = (0.02, 0.095, 0.98, 0.205)
        left_team_rect = (0.02, 0.20, 0.44, 0.81)
        right_team_rect = (0.56, 0.20, 0.98, 0.81)
    else:
        result_rect = (0.48, 0.24, 0.52, 0.31)
        defeat_rect = (0.45, 0.225, 0.55, 0.315)
        panel_rect = (0.09, 0.22, 0.91, 0.78)
        header_rect = (0.1, 0.225, 0.9, 0.315)
        left_team_rect = (0.11, 0.30, 0.43, 0.68)
        right_team_rect = (0.57, 0.30, 0.89, 0.68)
    result_votes = _bright_theme_votes(
        frame, viewport.source_rect(frame, *result_rect), step=1
    )
    winner_color = _dominant_theme(result_votes, minimum=8)
    if winner_color is None:
        defeat_fraction = _dark_defeat_fraction(
            frame, viewport.source_rect(frame, *defeat_rect), step=1
        )
        if defeat_fraction < 0.04:
            _log_layout_attempt(
                frame,
                viewport,
                'result_color',
                result_votes=result_votes,
                defeat_fraction=defeat_fraction,
            )
            return None
        winner_color = 'orange'
        confidence = min(0.95, 0.75 + (defeat_fraction - 0.04) * 2)
    else:
        winner_count = result_votes[0 if winner_color == 'teal' else 1]
        other_count = result_votes[1 if winner_color == 'teal' else 0]
        confidence = winner_count / max(1, winner_count + other_count)
        if confidence < 0.75:
            _log_layout_attempt(
                frame,
                viewport,
                'result_confidence',
                result_votes=result_votes,
                confidence=confidence,
            )
            return None

    panel = viewport.source_rect(frame, *panel_rect)
    panel_dark = _dark_fraction(frame, panel, step=4)
    if panel_dark < 0.6:
        _log_layout_attempt(
            frame,
            viewport,
            'panel_dark',
            result_votes=result_votes,
            confidence=confidence,
            panel_dark=panel_dark,
        )
        return None
    header = viewport.source_rect(frame, *header_rect)
    header_dark = _dark_fraction(frame, header, step=3)
    minimum_header_dark = 0.3 if viewport.name.startswith('detected-') else 0.95
    if header_dark < minimum_header_dark:
        _log_layout_attempt(
            frame,
            viewport,
            'header_dark',
            result_votes=result_votes,
            confidence=confidence,
            panel_dark=panel_dark,
            header_dark=header_dark,
        )
        return None
    left_votes = _theme_votes(
        frame, viewport.source_rect(frame, *left_team_rect), step=3
    )
    right_votes = _theme_votes(
        frame, viewport.source_rect(frame, *right_team_rect), step=3
    )
    left_color = _dominant_theme(left_votes)
    right_color = _dominant_theme(right_votes)
    if left_color is None or right_color is None or left_color == right_color:
        _log_layout_attempt(
            frame,
            viewport,
            'team_colors',
            result_votes=result_votes,
            confidence=confidence,
            panel_dark=panel_dark,
            header_dark=header_dark,
            left_votes=left_votes,
            right_votes=right_votes,
        )
        return None

    winner_side: TeamSide = 'left' if winner_color == left_color else 'right'
    _log_layout_attempt(
        frame,
        viewport,
        'matched',
        panel_dark=panel_dark,
        header_dark=header_dark,
        left_votes=left_votes,
        right_votes=right_votes,
        result_votes=result_votes,
        confidence=confidence,
    )
    return ResultLayout(
        left_color=left_color,
        right_color=right_color,
        winner_color=winner_color,
        winner_side=winner_side,
        confidence=confidence,
        team_size=team_size,
        viewport=viewport,
    )


def detect_gameplay_hud(frame: RgbFrame) -> Optional[str]:
    detected = detect_gameplay_hud_details(frame)
    return None if detected is None else detected.signature


def detect_gameplay_hud_details(frame: RgbFrame) -> Optional[GameplayHud]:
    best_portraits: Tuple[RgbFrame, ...] = ()
    best_visible = 0
    best_team_size: TeamSize = 3
    team_sizes: Tuple[TeamSize, ...] = (3, 5)
    for team_size in team_sizes:
        variants = (
            GAMEPLAY_HUD_CENTER_VARIANTS
            if team_size == 3
            else GAMEPLAY_HUD_FIVE_CENTER_VARIANTS
        )
        for centers in variants:
            portraits, visible = _gameplay_hud_portraits(frame, centers)
            if visible > best_visible:
                best_portraits = portraits
                best_visible = visible
                best_team_size = team_size
    timer_white = _game_timer_white_ratio(frame)
    if best_visible == 1 and timer_white >= 0.02:
        return GameplayHud(
            signature=':'.join(
                perceptual_hash(portrait) for portrait in best_portraits
            ),
            team_size=3,
            visible_portraits=1,
        )
    if best_visible < 4 or timer_white < 0.012:
        return None
    return GameplayHud(
        signature=':'.join(perceptual_hash(portrait) for portrait in best_portraits),
        team_size=best_team_size,
        visible_portraits=best_visible,
    )


def detect_observer_hud(frame: RgbFrame) -> Optional[GameplayHud]:
    if _game_timer_white_ratio(frame) < 0.004:
        return None
    centers = (0.07, 0.23, 0.39, 0.65, 0.81, 0.95)
    portraits = tuple(
        frame.crop(
            frame.relative_rect(
                max(0.0, center - 0.035), 0.82, min(1.0, center + 0.035), 0.98
            )
        )
        for center in centers
    )
    visible = sum(_looks_like_portrait(portrait) for portrait in portraits)
    if visible < 5:
        return None
    bottom = tuple(frame.colors(frame.relative_rect(0.0, 0.80, 1.0, 0.99), step=2))
    bottom_dark = sum(
        (red * 3 + green * 6 + blue) // 10 < 75 for red, green, blue in bottom
    ) / max(1, len(bottom))
    minimap = tuple(frame.colors(frame.relative_rect(0.35, 0.76, 0.62, 0.99), step=2))
    minimap_neutral = sum(
        max(red, green, blue) - min(red, green, blue) < 30
        and 45 < (red * 3 + green * 6 + blue) // 10 < 210
        for red, green, blue in minimap
    ) / max(1, len(minimap))
    if bottom_dark < 0.55 or minimap_neutral < 0.18:
        return None
    return GameplayHud(
        signature=':'.join(perceptual_hash(portrait) for portrait in portraits),
        team_size=3,
        visible_portraits=visible,
    )


def _game_timer_white_ratio(frame: RgbFrame) -> float:
    timer = frame.relative_rect(0.465, 0.0, 0.53, 0.075)
    timer_pixels = tuple(frame.colors(timer))
    return sum(
        min(red, green, blue) > 140
        and max(red, green, blue) - min(red, green, blue) < 55
        for red, green, blue in timer_pixels
    ) / max(1, len(timer_pixels))


def select_gameplay_hud_centers(
    frame: RgbFrame, *, team_size: TeamSize
) -> Optional[Tuple[float, ...]]:
    variants = (
        GAMEPLAY_HUD_CENTER_VARIANTS
        if team_size == 3
        else GAMEPLAY_HUD_FIVE_CENTER_VARIANTS
    )
    selected: Optional[Tuple[float, ...]] = None
    best_visible = 0
    for centers in variants:
        _, visible = _gameplay_hud_portraits(frame, centers)
        if visible > best_visible:
            selected, best_visible = centers, visible
    minimum_visible = 4 if team_size == 3 else 6
    return selected if best_visible >= minimum_visible else None


def _gameplay_hud_portraits(
    frame: RgbFrame, centers: Sequence[float]
) -> Tuple[Tuple[RgbFrame, ...], int]:
    half_width = min(0.023, 0.032 * frame.height / frame.width)
    portraits = tuple(
        frame.crop(
            frame.relative_rect(center - half_width, 0.0, center + half_width, 0.075)
        )
        for center in centers
    )
    return portraits, sum(_looks_like_portrait(portrait) for portrait in portraits)


def extract_gameplay_hud_heroes(
    frame: RgbFrame, *, team_size: TeamSize, centers: Optional[Sequence[float]] = None
) -> Tuple[HeroFrame, ...]:
    if centers is None:
        centers = (
            GAMEPLAY_HUD_CENTER_VARIANTS[0]
            if team_size == 3
            else GAMEPLAY_HUD_FIVE_CENTER_VARIANTS[0]
        )
    if len(centers) != team_size * 2:
        raise ValueError('gameplay HUD portrait count does not match its team size')
    result: List[HeroFrame] = []
    half_width = min(0.023, 0.032 * frame.height / frame.width)
    for index, center in enumerate(centers):
        side: TeamSide = 'left' if index < team_size else 'right'
        slot = index + 1 if index < team_size else index - team_size + 1
        crop = frame.crop(
            frame.relative_rect(center - half_width, 0.0, center + half_width, 0.072)
        ).resize_nearest(96, 96)
        result.append(HeroFrame(side=side, slot=slot, frame=crop))
    return tuple(result)


def extract_result_heroes(
    frame: RgbFrame,
    *,
    viewport: ViewportTransform = STANDARD_VIEWPORT,
    team_size: TeamSize = 3,
    center_shift: float = 0.0,
) -> Tuple[HeroFrame, ...]:
    result: List[HeroFrame] = []
    separator = 0.5 + center_shift
    row_centers: Tuple[float, ...]
    if team_size == 5:
        center_offset = 0.046
        row_centers = (0.255, 0.379, 0.503, 0.627, 0.750)
        half_width = 0.038
    else:
        center_offset = 0.039
        row_centers = (0.375, 0.5, 0.625)
        half_width = 0.032
    sides: Tuple[Tuple[TeamSide, float], ...] = (
        ('left', separator - center_offset),
        ('right', separator + center_offset),
    )
    for side, center_x in sides:
        for slot, center_y in enumerate(row_centers, 1):
            rect = viewport.source_rect(
                frame,
                center_x - half_width,
                center_y - 0.057,
                center_x + half_width,
                center_y + 0.057,
            )
            side_length = min(rect.right - rect.left, rect.bottom - rect.top)
            left = rect.left + (rect.right - rect.left - side_length) // 2
            top = rect.top + (rect.bottom - rect.top - side_length) // 2
            crop = frame.crop(
                PixelRect(left, top, left + side_length, top + side_length)
            ).resize_nearest(96, 96)
            result.append(HeroFrame(side=side, slot=slot, frame=crop))
    return tuple(result)


def detect_recorded_player(
    frame: RgbFrame, layout: ResultLayout
) -> Optional[RecordedPlayer]:
    side: TeamSide = 'left' if layout.left_color == 'teal' else 'right'
    row_centers: Tuple[float, ...]
    if layout.team_size == 5:
        left, right = (0.08, 0.44) if side == 'left' else (0.56, 0.92)
        row_centers = (0.255, 0.379, 0.503, 0.627, 0.750)
        half_height = 0.045
    else:
        left, right = (0.10, 0.43) if side == 'left' else (0.57, 0.90)
        row_centers = (0.375, 0.5, 0.625)
        half_height = 0.047
    scores = tuple(
        _teal_row_highlight_score(
            frame,
            layout.viewport.source_rect(
                frame, left, center_y - half_height, right, center_y + half_height
            ),
        )
        for center_y in row_centers
    )
    ranked = sorted(enumerate(scores, 1), key=lambda item: item[1], reverse=True)
    (slot, best), (_, second) = ranked[:2]
    margin = best - second
    relative_margin = margin / max(1.0, best)
    if best < 32 or margin < 6 or relative_margin < 0.12:
        logger.debug(
            'Vainglory recorded player highlight was ambiguous: side={} '
            'scores={} best={:.1f} margin={:.1f} relative_margin={:.3f}',
            side,
            tuple(round(score, 1) for score in scores),
            best,
            margin,
            relative_margin,
        )
        return None
    confidence = min(
        0.99,
        0.70
        + min(0.10, max(0.0, best - 32) / 330)
        + min(0.14, max(0.0, margin - 6) / 180)
        + min(0.05, max(0.0, relative_margin - 0.12) / 4),
    )
    logger.debug(
        'Vainglory recorded player highlight recognized: side={} slot={} '
        'confidence={:.3f} scores={}',
        side,
        slot,
        confidence,
        tuple(round(score, 1) for score in scores),
    )
    return RecordedPlayer(side=side, slot=slot, confidence=confidence)


def result_frame_quality(frame: RgbFrame, layout: ResultLayout) -> float:
    if layout.team_size == 5:
        team_top, team_bottom = 0.20, 0.81
        left_right = (0.02, 0.44)
        right_left_right = (0.56, 0.98)
    else:
        team_top, team_bottom = 0.29, 0.69
        left_right = (0.08, 0.43)
        right_left_right = (0.57, 0.92)
    regions = (
        (
            layout.left_color,
            layout.viewport.source_rect(
                frame, left_right[0], team_top, left_right[1], team_bottom
            ),
        ),
        (
            layout.right_color,
            layout.viewport.source_rect(
                frame, right_left_right[0], team_top, right_left_right[1], team_bottom
            ),
        ),
    )
    step = max(1, min(frame.width // 640, frame.height // 360))
    standard_action_contrasts = _result_action_contrasts(
        frame, STANDARD_VIEWPORT, team_size=layout.team_size
    )
    viewport_action_contrasts = _result_action_contrasts(
        frame, layout.viewport, team_size=layout.team_size
    )
    action_contrast = max(
        min(standard_action_contrasts), min(viewport_action_contrasts)
    )
    quality = layout.confidence * 0.3 + min(1.0, action_contrast / 220) * 0.4
    hero_frames = extract_result_heroes(
        frame, viewport=layout.viewport, team_size=layout.team_size
    )
    visible_heroes = sum(_looks_like_portrait(hero.frame) for hero in hero_frames)
    quality += visible_heroes / max(1, layout.team_size * 2) * 0.5
    for expected_color, rect in regions:
        teal, orange = _theme_votes(frame, rect, step=step)
        sampled_columns = (rect.right - rect.left + step - 1) // step
        sampled_rows = (rect.bottom - rect.top + step - 1) // step
        sampled = max(1, sampled_columns * sampled_rows)
        expected = teal if expected_color == 'teal' else orange
        unexpected = orange if expected_color == 'teal' else teal
        quality += expected / sampled * 0.5
        quality -= unexpected / sampled * 0.15
    return quality


def _result_viewports(aspect_ratio: float) -> Tuple[ViewportTransform, ...]:
    reference_ratio = 16 / 9
    deviation = abs(aspect_ratio / reference_ratio - 1)
    if deviation < 0.04:
        return (STANDARD_VIEWPORT, _responsive_viewport('responsive-maximum', 1.0))

    strength = min(1.0, deviation / 0.20)
    responsive = _responsive_viewport('responsive', strength)
    maximum = _responsive_viewport('responsive-maximum', 1.0)
    if strength == 1.0:
        return maximum, STANDARD_VIEWPORT
    return responsive, maximum, STANDARD_VIEWPORT


def _responsive_viewport(name: str, strength: float) -> ViewportTransform:
    maximum_left = 138 / 1920
    maximum_top = 93 / 1080
    maximum_width = 1597 / 1920
    maximum_height = 866 / 1080
    return ViewportTransform(
        name=name,
        left=maximum_left * strength,
        top=maximum_top * strength,
        width=1.0 - (1.0 - maximum_width) * strength,
        height=1.0 - (1.0 - maximum_height) * strength,
        ocr_profile='wide',
    )


def _log_layout_attempt(
    frame: RgbFrame, viewport: ViewportTransform, stage: str, **metrics: object
) -> None:
    logger.debug(
        'Vainglory result layout candidate: size={}x{} aspect={:.4f} '
        'viewport={} stage={} metrics={}',
        frame.width,
        frame.height,
        frame.width / frame.height,
        viewport.name,
        stage,
        metrics,
    )


def perceptual_hash(frame: RgbFrame) -> str:
    values = _average_grid(frame, 9, 8)
    bits = 0
    for row in range(8):
        for column in range(8):
            bits <<= 1
            if values[row][column] < values[row][column + 1]:
                bits |= 1
    return '{:016x}'.format(bits)


def hero_fingerprint(frame: RgbFrame) -> str:
    inner = frame.crop(
        PixelRect(
            frame.width // 6,
            frame.height // 6,
            max(frame.width // 6 + 1, frame.width * 5 // 6),
            max(frame.height // 6 + 1, frame.height * 5 // 6),
        )
    )
    size = 24
    values = _average_grid(inner, size, size)
    cosines = tuple(
        tuple(
            math.cos(math.pi * (2 * offset + 1) * frequency / (2 * size))
            for offset in range(size)
        )
        for frequency in range(8)
    )
    coefficients: List[float] = []
    for vertical in range(8):
        for horizontal in range(8):
            coefficients.append(
                sum(
                    values[y][x] * cosines[horizontal][x] * cosines[vertical][y]
                    for y in range(size)
                    for x in range(size)
                )
            )
    threshold = median(coefficients[1:])
    bits = 0
    for coefficient in coefficients:
        bits <<= 1
        if coefficient > threshold:
            bits |= 1
    return '04{:016x}'.format(bits)


def hamming_distance(left: str, right: str) -> int:
    if len(left) != len(right):
        raise ValueError('fingerprints must have the same length')
    return bin(int(left, 16) ^ int(right, 16)).count('1')


def png_bytes(frame: RgbFrame) -> bytes:
    scanlines = bytearray()
    row_bytes = frame.width * 3
    for row in range(frame.height):
        scanlines.append(0)
        start = row * row_bytes
        scanlines.extend(frame.pixels[start : start + row_bytes])
    return b''.join(
        (
            b'\x89PNG\r\n\x1a\n',
            _png_chunk(
                b'IHDR',
                struct.pack('>IIBBBBB', frame.width, frame.height, 8, 2, 0, 0, 0),
            ),
            _png_chunk(b'IDAT', zlib.compress(bytes(scanlines), level=6)),
            _png_chunk(b'IEND', b''),
        )
    )


def jpeg_bytes(
    frame: RgbFrame, *, maximum_width: int = 960, quality: int = 85
) -> bytes:
    """把训练候选压成适合局域网上传的 JPEG，避免上传原始大图。"""
    if maximum_width <= 0:
        raise ValueError('maximum JPEG width must be positive')
    if quality < 1 or quality > 100:
        raise ValueError('JPEG quality must be between 1 and 100')
    import importlib

    cv2: Any = importlib.import_module('cv2')
    numpy: Any = importlib.import_module('numpy')
    image = numpy.frombuffer(frame.pixels, dtype=numpy.uint8).reshape(
        frame.height, frame.width, 3
    )
    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    if frame.width > maximum_width:
        height = max(1, round(frame.height * maximum_width / frame.width))
        image = cv2.resize(image, (maximum_width, height), interpolation=cv2.INTER_AREA)
    encoded, payload = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not encoded:
        raise RuntimeError('failed to encode JPEG')
    return bytes(payload)


def stack_vertical(
    frames: Sequence[RgbFrame],
    *,
    gap: int = 8,
    background: Tuple[int, int, int] = (0, 0, 0),
) -> RgbFrame:
    if not frames:
        raise ValueError('at least one frame is required')
    if gap < 0:
        raise ValueError('gap must not be negative')
    width = max(frame.width for frame in frames)
    height = sum(frame.height for frame in frames) + gap * (len(frames) - 1)
    pixels = bytearray(bytes(background) * width * height)
    target_y = 0
    for frame in frames:
        for row in range(frame.height):
            source_start = row * frame.width * 3
            target_start = ((target_y + row) * width) * 3
            pixels[target_start : target_start + frame.width * 3] = frame.pixels[
                source_start : source_start + frame.width * 3
            ]
        target_y += frame.height + gap
    return RgbFrame(width, height, bytes(pixels))


def _dark_fraction(frame: RgbFrame, rect: PixelRect, *, step: int) -> float:
    dark = 0
    total = 0
    for red, green, blue in frame.colors(rect, step=step):
        total += 1
        if red * 3 + green * 6 + blue < 1_050:
            dark += 1
    return dark / max(1, total)


def _teal_row_highlight_score(frame: RgbFrame, rect: PixelRect) -> float:
    step = max(1, min(frame.width // 640, frame.height // 360))
    values = []
    for red, green, blue in frame.colors(rect, step=step):
        if red * 3 + green * 6 + blue >= 1_300:
            continue
        if max(red, green, blue) - min(red, green, blue) <= 8:
            continue
        values.append(max(green, blue) - red)
    if len(values) < 100:
        return 0
    values.sort()
    return float(values[(len(values) - 1) * 65 // 100])


def _looks_like_portrait(frame: RgbFrame) -> bool:
    luminance: List[int] = []
    colorful = 0
    for red, green, blue in frame.colors(PixelRect(0, 0, frame.width, frame.height)):
        luminance.append((red * 3 + green * 6 + blue) // 10)
        if max(red, green, blue) - min(red, green, blue) > 35:
            colorful += 1
    mean = sum(luminance) / max(1, len(luminance))
    variance = sum((value - mean) ** 2 for value in luminance) / max(1, len(luminance))
    return variance**0.5 > 30 and colorful / max(1, len(luminance)) > 0.08


def _result_action_contrasts(
    frame: RgbFrame, viewport: ViewportTransform, *, team_size: TeamSize = 3
) -> Tuple[float, ...]:
    if team_size == 5:
        top, bottom = 0.815, 0.905
        spans = ((0.01, 0.205), (0.215, 0.405), (0.595, 0.79), (0.80, 0.99))
        return tuple(
            _horizontal_border_contrast(
                frame, viewport.source_rect(frame, left, top, right, bottom)
            )
            for left, right in spans
        )
    spans = (
        ((0.002, 0.12), (0.125, 0.24), (0.735, 0.875), (0.88, 0.998))
        if viewport.name == 'detected-desktop-3v3'
        else ((0.01, 0.19), (0.205, 0.405), (0.595, 0.795), (0.81, 0.99))
    )
    if viewport.name == 'detected-desktop-3v3':
        return tuple(
            _horizontal_border_contrast(
                frame, viewport.source_rect(frame, left, 0.675, right, 0.73)
            )
            for left, right in spans
        )
    rows = tuple(
        tuple(
            _horizontal_border_contrast(
                frame, viewport.source_rect(frame, left, top, right, bottom)
            )
            for left, right in spans
        )
        for top, bottom in ((0.675, 0.73), (0.738, 0.80))
    )
    return max(rows, key=min)


def _horizontal_border_contrast(frame: RgbFrame, rect: PixelRect) -> float:
    trim = max(1, (rect.right - rect.left) * 3 // 100)
    left = min(rect.right - 1, rect.left + trim)
    right = max(left + 1, rect.right - trim)
    row_medians: List[float] = []
    row_bytes = frame.width * 3
    for y in range(rect.top, rect.bottom):
        luminances = []
        row = y * row_bytes
        for x in range(left, right):
            offset = row + x * 3
            red, green, blue = frame.pixels[offset : offset + 3]
            luminances.append((red * 3 + green * 6 + blue) // 10)
        row_medians.append(float(median(luminances)))

    row_count = len(row_medians)
    gap = max(1, round(row_count * 0.06))
    span = max(2, round(row_count * 0.10))
    contrasts = []
    for index in range(gap + span, row_count - gap - span):
        before = median(row_medians[index - gap - span : index - gap])
        after = median(row_medians[index + gap : index + gap + span])
        contrasts.append(row_medians[index] - (before + after) / 2)
    return max(contrasts, default=0.0)


def _theme_votes(frame: RgbFrame, rect: PixelRect, *, step: int) -> Tuple[int, int]:
    teal = 0
    orange = 0
    for red, green, blue in frame.colors(rect, step=step):
        spread = max(red, green, blue) - min(red, green, blue)
        if spread < 18:
            continue
        if green * 100 > red * 108 and blue * 100 > red * 105:
            teal += 1
        if red * 100 > blue * 120 and green * 100 > blue * 75:
            orange += 1
    return teal, orange


def _bright_theme_votes(
    frame: RgbFrame, rect: PixelRect, *, step: int
) -> Tuple[int, int]:
    teal = 0
    orange = 0
    for red, green, blue in frame.colors(rect, step=step):
        if max(red, green, blue) < 110:
            continue
        spread = max(red, green, blue) - min(red, green, blue)
        if spread < 30:
            continue
        if green * 100 > red * 108 and blue * 100 > red * 105:
            teal += 1
        if red * 100 > blue * 120 and green * 100 > blue * 75:
            orange += 1
    return teal, orange


def _dark_defeat_fraction(frame: RgbFrame, rect: PixelRect, *, step: int) -> float:
    defeat = 0
    sampled = 0
    for red, green, blue in frame.colors(rect, step=step):
        sampled += 1
        spread = max(red, green, blue) - min(red, green, blue)
        if (
            max(red, green, blue) >= 55
            and spread >= 25
            and red * 100 > green * 135
            and red * 100 > blue * 115
        ):
            defeat += 1
    return defeat / max(1, sampled)


def _dominant_theme(
    votes: Tuple[int, int], *, minimum: int = 20
) -> Optional[TeamColor]:
    teal, orange = votes
    if max(teal, orange) < minimum:
        return None
    if teal > orange * 1.2:
        return 'teal'
    if orange > teal * 1.2:
        return 'orange'
    return None


def _average_grid(frame: RgbFrame, width: int, height: int) -> List[List[int]]:
    values: List[List[int]] = []
    for target_y in range(height):
        top = target_y * frame.height // height
        bottom = max(top + 1, (target_y + 1) * frame.height // height)
        row: List[int] = []
        for target_x in range(width):
            left = target_x * frame.width // width
            right = max(left + 1, (target_x + 1) * frame.width // width)
            total = 0
            count = 0
            for red, green, blue in frame.colors(PixelRect(left, top, right, bottom)):
                total += red * 3 + green * 6 + blue
                count += 10
            row.append(total // max(1, count))
        values.append(row)
    return values


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(payload, checksum)
    return (
        struct.pack('>I', len(payload))
        + kind
        + payload
        + struct.pack('>I', checksum & 0xFFFFFFFF)
    )
