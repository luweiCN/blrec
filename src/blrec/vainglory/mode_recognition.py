from __future__ import annotations

import importlib
from itertools import combinations
from typing import Any, Protocol, Sequence, Tuple

from .vision import RgbFrame

NormalizedCircle = Tuple[float, float, float]


class AramDetector(Protocol):
    def is_visible(self, frame: RgbFrame) -> bool: ...  # noqa: E704


def has_aligned_talent_cards(circles: Sequence[NormalizedCircle]) -> bool:
    candidates = tuple(
        circle
        for circle in circles
        if 0.05 <= circle[0] <= 0.68
        and 0.42 <= circle[1] <= 0.62
        and 0.07 <= circle[2] <= 0.145
    )
    for selected in combinations(candidates, 3):
        ordered = tuple(sorted(selected, key=lambda circle: circle[0]))
        y_values = tuple(circle[1] for circle in ordered)
        radii = tuple(circle[2] for circle in ordered)
        gaps = (ordered[1][0] - ordered[0][0], ordered[2][0] - ordered[1][0])
        if (
            max(y_values) - min(y_values) <= 0.035
            and 0.18 <= gaps[0] <= 0.28
            and 0.18 <= gaps[1] <= 0.28
            and abs(gaps[0] - gaps[1]) <= 0.05
            and max(radii) - min(radii) <= 0.03
        ):
            return True
    return False


class AramTalentSelectionDetector:
    """Detects the fixed three-card talent selector shown before an ARAM match."""

    def __init__(self) -> None:
        self._cv2: Any = importlib.import_module('cv2')
        self._numpy: Any = importlib.import_module('numpy')

    def is_visible(self, frame: RgbFrame) -> bool:
        image = self._numpy.frombuffer(frame.pixels, dtype=self._numpy.uint8).reshape(
            frame.height, frame.width, 3
        )
        gray = self._cv2.cvtColor(image, self._cv2.COLOR_RGB2GRAY)
        top = int(round(frame.height * 0.25))
        bottom = int(round(frame.height * 0.80))
        right = int(round(frame.width * 0.70))
        roi = self._cv2.GaussianBlur(gray[top:bottom, :right], (5, 5), 0)
        detected = self._cv2.HoughCircles(
            roi,
            self._cv2.HOUGH_GRADIENT,
            dp=1,
            minDist=max(1, int(round(frame.height * 0.12))),
            param1=80,
            param2=25,
            minRadius=max(1, int(round(frame.height * 0.07))),
            maxRadius=max(2, int(round(frame.height * 0.14))),
        )
        if detected is None:
            return False
        circles = tuple(
            (
                float(center_x) / frame.width,
                float(center_y + top) / frame.height,
                float(radius) / frame.height,
            )
            for center_x, center_y, radius in detected[0]
        )
        return has_aligned_talent_cards(circles)
