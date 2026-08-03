from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .vision import hamming_distance

HERO_CHINESE_NAMES = {
    'Adagio': '奥达基',
    'Alpha': '阿尔法',
    'Amael': '阿玛尔',
    'Anka': '安卡',
    'Ardan': '亚丹',
    'Baptiste': '巴蒂斯特',
    'Baron': '巴隆',
    'Blackfeather': '黑羽',
    'Caine': '凯恩',
    'Catherine': '凯瑟琳',
    'Celeste': '星乐斯',
    'Churnwalker': '沃克尔',
    'Flicker': '弗利克',
    'Fortress': '福彻斯',
    'Glaive': '格雷',
    'Grace': '格瑞丝',
    'Grumpjaw': '格兰卓',
    'Gwen': '格温',
    'Idris': '伊德瑞',
    'Inara': '伊娜',
    'Ishtar': '伊丝塔',
    'Joule': '朱尔',
    'Karas': '鸦',
    'Kensei': '肯赛',
    'Kestrel': '凯思卓',
    'Kinetic': '基妮',
    'Koshka': '柯思卡',
    'Krul': '骷髅',
    'Lance': '兰斯',
    'Leo': '里昂',
    'Lorelai': '洛姬',
    'Lyra': '莱拉',
    'Magnus': '玛格纳斯',
    'Malene': '梅兰妮',
    'Miho': '美惠',
    'Ozo': '奥佐',
    'Petal': '佩兔',
    'Phinn': '费恩',
    'Reim': '莱姆',
    'Reza': '雷萨',
    'Ringo': '林戈',
    'Rona': '罗娜',
    'Samuel': '萨缪尔',
    'Sanfeng': '三风',
    'SAW': '索尔',
    'Shin': '哪吒',
    'Silvernail': '西弗尔',
    'Skaarf': '史卡夫',
    'Skye': '丝凯伊',
    'Taka': '塔卡',
    'Tony': '托尼',
    'Varya': '瓦妮亚',
    'Viola': '维奥拉',
    'Vox': '舞司',
    'Warhawk': '尼尔',
    'Yates': '耶茨',
    'Ylva': '伊娃',
}


@dataclass(frozen=True)
class BuiltinHero:
    label: str
    fingerprints: Tuple[str, ...]


# These non-reversible dHash prototypes use only the inner portrait area so team
# rings and level overlays do not create a separate hero entry.
BUILTIN_HEROES: Tuple[BuiltinHero, ...] = (
    BuiltinHero('Baptiste', ('04b97662da912c61d9',)),
    BuiltinHero('Baron', ('0497702ac3ac6e617a',)),
    BuiltinHero('Blackfeather', ('048d617e9f314dc258',)),
    BuiltinHero('Caine', ('04df9130fe4c0c7a32',)),
    BuiltinHero('Catherine', ('0487707cc3728eec51',)),
    BuiltinHero('Grace', ('049f7eebca70219085',)),
    BuiltinHero('Gwen', ('04d768ffc0c0c30c3e',)),
    BuiltinHero('Joule', ('049562d6b57a84392b',)),
    BuiltinHero('Kinetic', ('04cc72548b54d5c6e5',)),
    BuiltinHero('Koshka', ('04b960c38fb916f438',)),
    BuiltinHero('Lance', ('04dedf0bc5706a8a82', '04dedf0bc5616e8282')),
    BuiltinHero('Lorelai', ('04d37863e30ce50c67',)),
    BuiltinHero('Lyra', ('04c3619ede0e1d3c61',)),
    BuiltinHero('Malene', ('04c0c81f3e32de3ce1',)),
    BuiltinHero('Phinn', ('04c3c71e4b83b8585b',)),
    BuiltinHero('Reim', ('04c638391a3b9e1b36',)),
    BuiltinHero('Ringo', ('048d626ab8b1621d7d',)),
    BuiltinHero('Samuel', ('04956a17a862e5fa91', '04916a17e872e5fa11')),
    BuiltinHero('Tony', ('04ab541df633e49c84',)),
    BuiltinHero('Viola', ('048a7b2549168aaefa', '048a7b2549558aa67b')),
)


def identify_builtin_hero(
    fingerprint: str, *, maximum_distance: int = 16
) -> Optional[str]:
    if maximum_distance < 0:
        raise ValueError('maximum distance must not be negative')
    nearest_label: Optional[str] = None
    nearest_distance: Optional[int] = None
    ambiguous = False
    for hero in BUILTIN_HEROES:
        compatible = tuple(
            prototype
            for prototype in hero.fingerprints
            if len(prototype) == len(fingerprint)
        )
        if not compatible:
            continue
        distance = min(hamming_distance(fingerprint, value) for value in compatible)
        if nearest_distance is None or distance < nearest_distance:
            nearest_label = hero.label
            nearest_distance = distance
            ambiguous = False
        elif distance == nearest_distance:
            ambiguous = True
    if (
        nearest_label is None
        or nearest_distance is None
        or nearest_distance > maximum_distance
        or ambiguous
    ):
        return None
    return nearest_label


def hero_chinese_name(label: str) -> str:
    return HERO_CHINESE_NAMES.get(label, label)
