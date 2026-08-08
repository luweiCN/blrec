import blrec.vainglory.mode_recognition as mode_recognition
from blrec.vainglory.mode_recognition import (
    AramTalentSelectionDetector,
    has_aligned_talent_cards,
)


def test_aram_detector_defers_optional_vision_imports(monkeypatch) -> None:
    imported = []

    def fail_if_imported(name: str):
        imported.append(name)
        raise AssertionError(name)

    monkeypatch.setattr(mode_recognition.importlib, 'import_module', fail_if_imported)

    AramTalentSelectionDetector()

    assert imported == []


def test_three_aligned_talent_cards_are_recognized() -> None:
    assert has_aligned_talent_cards(
        ((0.160, 0.530, 0.110), (0.354, 0.531, 0.113), (0.547, 0.529, 0.114))
    )


def test_wide_aram_talent_cards_from_a_recorded_match_are_recognized() -> None:
    assert has_aligned_talent_cards(
        ((0.118, 0.525, 0.108), (0.373, 0.522, 0.112), (0.594, 0.530, 0.112))
    )


def test_unrelated_result_icons_are_not_talent_cards() -> None:
    assert not has_aligned_talent_cards(
        (
            (0.165, 0.375, 0.050),
            (0.165, 0.500, 0.050),
            (0.815, 0.375, 0.050),
            (0.815, 0.500, 0.050),
            (0.815, 0.625, 0.050),
        )
    )


def test_misaligned_large_circles_are_not_talent_cards() -> None:
    assert not has_aligned_talent_cards(
        ((0.160, 0.450, 0.110), (0.354, 0.530, 0.113), (0.547, 0.590, 0.114))
    )
