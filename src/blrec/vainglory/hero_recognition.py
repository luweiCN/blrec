from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, List, Optional, Sequence, Tuple

from loguru import logger

from .vision import RgbFrame, png_bytes


@dataclass(frozen=True)
class HeroReference:
    label: str
    fingerprint: str
    image_jpeg: bytes


@dataclass(frozen=True)
class HeroCandidate:
    label: str
    inliers: int
    good_matches: int
    median_distance: float


@dataclass(frozen=True)
class HeroMatch:
    label: str
    confidence: float
    inliers: int
    margin: int


def hero_reference_directory() -> Path:
    return Path(__file__).resolve().parent.parent / 'data' / 'vainglory' / 'heroes'


def load_hero_references(directory: Optional[Path] = None) -> Tuple[HeroReference, ...]:
    root = hero_reference_directory() if directory is None else Path(directory)
    references: List[HeroReference] = []
    for path in sorted(root.glob('*.jpg')):
        image = path.read_bytes()
        if not image:
            continue
        label = 'SAW' if path.stem.casefold() == 'saw' else path.stem.capitalize()
        references.append(
            HeroReference(
                label=label,
                fingerprint=hashlib.sha256(image).hexdigest(),
                image_jpeg=image,
            )
        )
    return tuple(references)


def select_hero_candidate(
    candidates: Sequence[HeroCandidate],
    *,
    minimum_inliers: int = 5,
    minimum_margin: int = 3,
) -> Optional[HeroMatch]:
    ordered = sorted(
        candidates,
        key=lambda item: (item.inliers, item.good_matches, -item.median_distance),
        reverse=True,
    )
    if not ordered:
        return None
    best = ordered[0]
    second_inliers = 0 if len(ordered) == 1 else ordered[1].inliers
    margin = best.inliers - second_inliers
    if best.inliers < minimum_inliers or margin < minimum_margin:
        return None
    confidence = min(
        1.0,
        0.55
        + min(0.3, (best.inliers - minimum_inliers) * 0.03)
        + min(0.15, (margin - minimum_margin) * 0.03),
    )
    return HeroMatch(
        label=best.label, confidence=confidence, inliers=best.inliers, margin=margin
    )


class SiftHeroRecognizer:
    def __init__(
        self,
        references: Sequence[HeroReference],
        *,
        ratio_threshold: float = 0.78,
        minimum_inliers: int = 5,
        minimum_margin: int = 3,
    ) -> None:
        if not 0 < ratio_threshold < 1:
            raise ValueError('SIFT 特征比例阈值必须在 0 和 1 之间')
        if minimum_inliers < 3 or minimum_margin < 1:
            raise ValueError('SIFT 英雄识别阈值无效')
        if not references:
            raise ValueError('SIFT 英雄识别需要参考头像')
        self._cv2: Any = importlib.import_module('cv2')
        self._numpy: Any = importlib.import_module('numpy')
        self._sift = self._cv2.SIFT_create()
        self._matcher = self._cv2.BFMatcher(self._cv2.NORM_L2)
        self._clahe = self._cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
        self._ratio_threshold = ratio_threshold
        self._minimum_inliers = minimum_inliers
        self._minimum_margin = minimum_margin
        loaded: List[Tuple[str, Sequence[Any], Any]] = []
        normalized: List[Tuple[str, Sequence[Any], Any]] = []
        for reference in references:
            image = self._decode_gray(reference.image_jpeg)
            keypoints, descriptors = self._sift.detectAndCompute(image, None)
            if descriptors is None or len(keypoints) < 4:
                logger.warning(
                    'Skipped Vainglory hero reference without enough features: {}',
                    reference.label,
                )
                continue
            loaded.append((reference.label, keypoints, descriptors))
            normalized_image = self._clahe.apply(image)
            normalized_keypoints, normalized_descriptors = self._sift.detectAndCompute(
                normalized_image, None
            )
            if normalized_descriptors is not None and len(normalized_keypoints) >= 4:
                normalized.append(
                    (reference.label, normalized_keypoints, normalized_descriptors)
                )
        if not loaded:
            raise RuntimeError('没有可用的英雄头像特征')
        self._references = tuple(loaded)
        self._normalized_references = tuple(normalized)
        logger.info(
            'Vainglory SIFT hero recognizer ready: references={}', len(self._references)
        )

    def recognize(self, frame: RgbFrame) -> Optional[HeroMatch]:
        image = self._decode_gray(png_bytes(frame))
        match = self._match_image(image, self._references)
        if match is not None or not self._normalized_references:
            return match
        return self._match_image(self._clahe.apply(image), self._normalized_references)

    def _match_image(
        self, image: Any, references: Sequence[Tuple[str, Sequence[Any], Any]]
    ) -> Optional[HeroMatch]:
        query_keypoints, query_descriptors = self._sift.detectAndCompute(image, None)
        if query_descriptors is None or len(query_keypoints) < 4:
            return None
        candidates: List[HeroCandidate] = []
        for label, reference_keypoints, reference_descriptors in references:
            try:
                pairs = self._matcher.knnMatch(
                    query_descriptors, reference_descriptors, k=2
                )
            except self._cv2.error:
                continue
            good = [
                pair[0]
                for pair in pairs
                if len(pair) == 2
                and pair[0].distance < self._ratio_threshold * pair[1].distance
            ]
            if len(good) < 4:
                candidates.append(HeroCandidate(label, 0, len(good), float('inf')))
                continue
            source = self._numpy.float32(
                [query_keypoints[item.queryIdx].pt for item in good]
            )
            target = self._numpy.float32(
                [reference_keypoints[item.trainIdx].pt for item in good]
            )
            _, mask = self._cv2.estimateAffinePartial2D(
                source,
                target,
                method=self._cv2.RANSAC,
                ransacReprojThreshold=4.0,
                maxIters=2_000,
                confidence=0.995,
                refineIters=10,
            )
            inliers = 0 if mask is None else int(mask.ravel().sum())
            candidates.append(
                HeroCandidate(
                    label=label,
                    inliers=inliers,
                    good_matches=len(good),
                    median_distance=float(median(item.distance for item in good)),
                )
            )
        return select_hero_candidate(
            candidates,
            minimum_inliers=self._minimum_inliers,
            minimum_margin=self._minimum_margin,
        )

    def _decode_gray(self, content: bytes) -> Any:
        encoded = self._numpy.frombuffer(content, dtype=self._numpy.uint8)
        image = self._cv2.imdecode(encoded, self._cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError('无法解码英雄头像')
        return image
