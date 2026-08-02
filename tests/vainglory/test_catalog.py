import pytest

from blrec.vainglory.catalog import (
    BUILTIN_HEROES,
    HERO_CHINESE_NAMES,
    hero_chinese_name,
    identify_builtin_hero,
)
from blrec.vainglory.hero_recognition import load_hero_references


def test_builtin_catalog_identifies_every_prototype_and_nearby_hash() -> None:
    for hero in BUILTIN_HEROES:
        for fingerprint in hero.fingerprints:
            nearby = '{:0{}x}'.format(int(fingerprint, 16) ^ 0xF, len(fingerprint))

            assert identify_builtin_hero(fingerprint) == hero.label
            assert identify_builtin_hero(nearby) == hero.label


def test_builtin_catalog_rejects_unknown_hash() -> None:
    assert identify_builtin_hero('0000000000000000') is None


def test_builtin_catalog_rejects_negative_distance() -> None:
    with pytest.raises(ValueError, match='must not be negative'):
        identify_builtin_hero('0000000000000000', maximum_distance=-1)


def test_all_reference_heroes_have_a_chinese_display_name() -> None:
    labels = {reference.label for reference in load_hero_references()}

    assert set(HERO_CHINESE_NAMES) == labels
    assert hero_chinese_name('Caine') == '凯恩'
    assert hero_chinese_name('未识别') == '未识别'
