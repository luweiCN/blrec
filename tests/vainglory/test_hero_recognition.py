from pathlib import Path
from types import SimpleNamespace

from blrec.vainglory.hero_recognition import (
    HeroCandidate,
    HeroMatch,
    SiftHeroRecognizer,
    load_hero_references,
    select_hero_candidate,
)
from blrec.vainglory.vision import RgbFrame


def test_load_hero_references_uses_stable_labels_and_fingerprints(
    tmp_path: Path,
) -> None:
    (tmp_path / 'saw.jpg').write_bytes(b'hero-image')

    references = load_hero_references(tmp_path)

    assert len(references) == 1
    assert references[0].label == 'SAW'
    assert len(references[0].fingerprint) == 64


def test_select_hero_candidate_requires_inliers_and_clear_margin() -> None:
    accepted = select_hero_candidate(
        (HeroCandidate('Koshka', 13, 17, 42.0), HeroCandidate('Rona', 7, 11, 38.0))
    )
    partially_occluded = select_hero_candidate(
        (HeroCandidate('Ylva', 5, 7, 44.0), HeroCandidate('Rona', 2, 5, 39.0))
    )
    ambiguous = select_hero_candidate(
        (HeroCandidate('Koshka', 13, 17, 42.0), HeroCandidate('Rona', 11, 15, 38.0))
    )

    assert accepted is not None
    assert accepted.label == 'Koshka'
    assert accepted.margin == 6
    assert partially_occluded is not None
    assert partially_occluded.label == 'Ylva'
    assert ambiguous is None


def test_sift_recognizer_uses_contrast_fallback_only_after_raw_match_fails() -> None:
    recognizer = object.__new__(SiftHeroRecognizer)
    expected = HeroMatch(label='Grumpjaw', confidence=0.7, inliers=8, margin=4)
    calls = []
    recognizer._references = ('raw-reference',)
    recognizer._normalized_references = ('normalized-reference',)
    recognizer._decode_gray = lambda _content: 'raw-image'
    recognizer._clahe = SimpleNamespace(
        apply=lambda image: 'normalized-image' if image == 'raw-image' else image
    )

    def match(image, references):
        calls.append((image, references))
        return expected if references == ('normalized-reference',) else None

    recognizer._match_image = match

    result = recognizer.recognize(RgbFrame(1, 1, b'\x00\x00\x00'))

    assert result == expected
    assert calls == [
        ('raw-image', ('raw-reference',)),
        ('normalized-image', ('normalized-reference',)),
    ]
