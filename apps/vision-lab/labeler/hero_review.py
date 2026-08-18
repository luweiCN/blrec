"""英雄目录、人工头像框与旧积分板／结算图布局兼容工具。

统一复核页的主动预填由 model_prefill 调用新模型；这里保留旧 SIFT 函数，
只供历史数据转换与回归测试使用。
"""

from __future__ import annotations

import os
import sys
import threading
from io import BytesIO
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from PIL import Image

_SCREEN_TYPES = {'gameplay_hud', 'scoreboard', 'result_page'}
_TEAM_SIZES = {3, 5}
_recognizer_lock = threading.Lock()
_HERO_CHINESE_NAMES = {
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
class _Transform:
    name: str
    left: float
    top: float
    width: float
    height: float

    def source_x(self, value: float) -> float:
        return (value - self.left) / self.width

    def source_y(self, value: float) -> float:
        return (value - self.top) / self.height


@dataclass(frozen=True)
class _HeroReference:
    label: str
    image_jpeg: bytes


def _ensure_blrec_source() -> None:
    configured = os.environ.get('VISION_LAB_BLREC_SOURCE_DIR', '').strip()
    source = (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parents[3] / 'src'
    )
    if source.is_dir() and str(source) not in sys.path:
        sys.path.insert(0, str(source))


@lru_cache(maxsize=1)
def _shared() -> Dict[str, Any]:
    _ensure_blrec_source()
    try:
        from blrec.vainglory.hero_recognition import SiftHeroRecognizer
        from blrec.vainglory.vision import RgbFrame
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            '无法加载 BLREC 当前英雄识别算法，请检查 '
            'VISION_LAB_BLREC_SOURCE_DIR 和 Vision Lab 依赖'
        ) from exc
    return {'RgbFrame': RgbFrame, 'SiftHeroRecognizer': SiftHeroRecognizer}


@lru_cache(maxsize=1)
def _references() -> Tuple[_HeroReference, ...]:
    root = Path(__file__).resolve().parent / 'resources' / 'heroes'
    return tuple(
        _HeroReference(
            label='SAW' if path.stem.casefold() == 'saw' else path.stem.capitalize(),
            image_jpeg=path.read_bytes(),
        )
        for path in sorted(root.glob('*.jpg'))
        if path.stat().st_size > 0
    )


@lru_cache(maxsize=1)
def _recognizer() -> Any:
    return _shared()['SiftHeroRecognizer'](_references())


def hero_catalog() -> List[Dict[str, str]]:
    return [
        {
            'label': str(reference.label),
            'name': _HERO_CHINESE_NAMES.get(str(reference.label), str(reference.label)),
        }
        for reference in sorted(
            _references(), key=lambda value: str(value.label).casefold()
        )
    ]


def hero_image_bytes(label: str) -> Optional[bytes]:
    normalized = label.strip().casefold()
    for reference in _references():
        if str(reference.label).casefold() == normalized:
            return bytes(reference.image_jpeg)
    return None


def allowed_hero_labels() -> set[str]:
    return {str(reference.label) for reference in _references()}


def infer_lineup_context(
    item: Mapping[str, Any],
    *,
    screen_type_hint: Optional[str] = None,
    team_size_hint: Optional[int] = None,
) -> Optional[Tuple[str, Optional[int]]]:
    if screen_type_hint is not None and screen_type_hint not in _SCREEN_TYPES:
        raise ValueError('英雄阵容界面类型无效')
    if team_size_hint is not None and team_size_hint not in _TEAM_SIZES:
        raise ValueError('英雄阵容人数必须是 3 或 5')
    screen_type = screen_type_hint
    if screen_type is None and item.get('result_panel_label') == 'result_panel':
        screen_type = 'result_page'
    suggestions = item.get('suggestions') or {}
    result_suggestion = suggestions.get('result_panel') or {}
    if screen_type is None and result_suggestion.get('label') == 'result_panel':
        screen_type = 'result_page'

    mode = str(item.get('match_mode_label') or '')
    for source in item.get('sources') or []:
        metadata = source.get('metadata') or {}
        source_screen = str(metadata.get('screen_type') or '')
        stage_class = str(metadata.get('stage_class') or '')
        suggested_label = str(metadata.get('suggested_label') or '')
        if screen_type is None and (
            source_screen in ('scoreboard', 'death_scoreboard')
            or stage_class == 'scoreboard'
            or suggested_label == 'scoreboard'
        ):
            screen_type = 'scoreboard'
        if screen_type is None and (
            source_screen == 'result_page'
            or source.get('source_type') == 'result_archive'
            or suggested_label == 'result_page'
        ):
            screen_type = 'result_page'
    if screen_type is None:
        return None
    team_size = team_size_hint
    if team_size is None:
        if mode == '5v5':
            team_size = 5
        elif mode in ('3v3', 'aram'):
            team_size = 3
    return screen_type, team_size


def panel_box_from_item(
    item: Mapping[str, Any], screen_type: str
) -> Optional[Dict[str, float]]:
    box_type = 'scoreboard_panel' if screen_type == 'scoreboard' else 'result_panel'
    boxes = item.get('boxes') or {}
    if box_type in boxes:
        box = boxes[box_type]
        return {
            name: float(box[name]) for name in ('x', 'y', 'w', 'h')
        }
    if screen_type != 'result_page':
        return None
    for source in item.get('sources') or []:
        metadata = source.get('metadata') or {}
        for box in metadata.get('suggested_boxes') or []:
            kind = str(box.get('type') or box.get('box_type') or 'result_panel')
            if kind == 'result_panel':
                return {
                    name: float(box[name]) for name in ('x', 'y', 'w', 'h')
                }
    return None


def _responsive_transform(strength: float) -> _Transform:
    maximum_left = 138 / 1920
    maximum_top = 93 / 1080
    maximum_width = 1597 / 1920
    maximum_height = 866 / 1080
    return _Transform(
        name='responsive-{:.3f}'.format(strength),
        left=maximum_left * strength,
        top=maximum_top * strength,
        width=1.0 - (1.0 - maximum_width) * strength,
        height=1.0 - (1.0 - maximum_height) * strength,
    )


def _candidate_transforms(
    width: int,
    height: int,
    *,
    team_size: int,
    panel_box: Optional[Mapping[str, float]],
) -> List[_Transform]:
    transforms: List[_Transform] = []
    if panel_box is not None:
        panel_x = float(panel_box['x'])
        panel_y = float(panel_box['y'])
        panel_w = float(panel_box['w'])
        panel_h = float(panel_box['h'])
        reference_top, reference_bottom = (
            (0.09, 0.91) if team_size == 5 else (0.22, 0.78)
        )
        source_height = panel_h / (reference_bottom - reference_top)
        source_top = panel_y - reference_top * source_height
        transforms.append(
            _Transform(
                name='panel-box',
                left=-panel_x / panel_w,
                top=-source_top / source_height,
                width=1.0 / panel_w,
                height=1.0 / source_height,
            )
        )
    aspect_ratio = width / height
    deviation = abs(aspect_ratio / (16 / 9) - 1)
    if deviation < 0.04:
        transforms.extend(
            [
                _Transform('standard', 0.0, 0.0, 1.0, 1.0),
                _responsive_transform(1.0),
            ]
        )
    else:
        strength = min(1.0, deviation / 0.20)
        transforms.extend(
            [
                _responsive_transform(strength),
                _responsive_transform(1.0),
                _Transform('standard', 0.0, 0.0, 1.0, 1.0),
            ]
        )
    unique = []
    keys = set()
    for transform in transforms:
        key = tuple(
            round(value, 5)
            for value in (
                transform.left,
                transform.top,
                transform.width,
                transform.height,
            )
        )
        if key not in keys:
            unique.append(transform)
            keys.add(key)
    return unique


def _slot_boxes(
    transform: _Transform, *, team_size: int, center_shift: float
) -> List[Dict[str, Any]]:
    separator = 0.5 + center_shift
    if team_size == 5:
        center_offset = 0.046
        row_centers = (0.255, 0.379, 0.503, 0.627, 0.750)
        half_width = 0.038
    else:
        center_offset = 0.039
        row_centers = (0.375, 0.5, 0.625)
        half_width = 0.032
    result = []
    for side, center_x in (
        ('left', separator - center_offset),
        ('right', separator + center_offset),
    ):
        for slot, center_y in enumerate(row_centers, 1):
            left = max(0.0, min(1.0, transform.source_x(center_x - half_width)))
            right = max(0.0, min(1.0, transform.source_x(center_x + half_width)))
            top = max(0.0, min(1.0, transform.source_y(center_y - 0.057)))
            bottom = max(0.0, min(1.0, transform.source_y(center_y + 0.057)))
            side_length = min(right - left, bottom - top)
            square_left = left + (right - left - side_length) / 2
            square_top = top + (bottom - top - side_length) / 2
            result.append(
                {
                    'side': side,
                    'slot': slot,
                    'crop': {
                        'x': square_left,
                        'y': square_top,
                        'w': side_length,
                        'h': side_length,
                    },
                }
            )
    return result


def _crop_image(image: Image.Image, box: Mapping[str, float]) -> Image.Image:
    width, height = image.size
    left = max(0, min(width - 1, round(float(box['x']) * width)))
    top = max(0, min(height - 1, round(float(box['y']) * height)))
    right = max(left + 1, min(width, round((box['x'] + box['w']) * width)))
    bottom = max(top + 1, min(height, round((box['y'] + box['h']) * height)))
    return image.crop((left, top, right, bottom)).resize(
        (96, 96), Image.Resampling.NEAREST
    )


def crop_image_bytes(frame_path: Path, box: Mapping[str, float]) -> bytes:
    with Image.open(frame_path) as source:
        crop = _crop_image(source.convert('RGB'), box)
    output = BytesIO()
    crop.save(output, format='JPEG', quality=90)
    return output.getvalue()


def crop_image_content(content: bytes, box: Mapping[str, float]) -> bytes:
    with Image.open(BytesIO(content)) as source:
        crop = _crop_image(source.convert('RGB'), box)
    output = BytesIO()
    crop.save(output, format='JPEG', quality=90)
    return output.getvalue()


def _current_sift_recognize(image: Image.Image) -> Dict[str, Any]:
    shared = _shared()
    rgb = image.convert('RGB')
    frame = shared['RgbFrame'](rgb.width, rgb.height, rgb.tobytes())
    with _recognizer_lock:
        match = _recognizer().recognize(frame)
    if match is None:
        return {'label': '', 'confidence': 0.0, 'inliers': 0, 'margin': 0}
    return {
        'label': str(match.label),
        'confidence': float(match.confidence),
        'inliers': int(match.inliers),
        'margin': int(match.margin),
    }


def recognize_slots(
    frame_path: Path,
    slots: List[Dict[str, Any]],
    *,
    recognize_crop: Optional[
        Callable[[Image.Image], Mapping[str, Any]]
    ] = None,
) -> List[Dict[str, Any]]:
    """只识别人工给定的头像框，不再猜测裁剪位置。"""
    matcher = _current_sift_recognize if recognize_crop is None else recognize_crop
    with Image.open(frame_path) as source:
        image = source.convert('RGB')
    result = []
    for value in slots:
        crop = value.get('crop') or {}
        try:
            normalized_crop = {
                name: float(crop[name]) for name in ('x', 'y', 'w', 'h')
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('英雄头像框无效') from exc
        match = dict(matcher(_crop_image(image, normalized_crop)))
        result.append(
            {
                'side': str(value.get('side') or ''),
                'slot': int(value.get('slot')),
                'crop': normalized_crop,
                'suggested_label': str(match.get('label') or ''),
                'suggestion_confidence': float(match.get('confidence', 0)),
            }
        )
    return result


def _recognize_variant(
    image: Image.Image,
    *,
    transform: _Transform,
    team_size: int,
    center_shift: float,
    recognize_crop: Callable[[Image.Image], Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], Tuple[float, ...]]:
    slots = []
    recognized = 0
    confidence = 0.0
    inliers = 0
    margin = 0
    for value in _slot_boxes(
        transform, team_size=team_size, center_shift=center_shift
    ):
        match = dict(recognize_crop(_crop_image(image, value['crop'])))
        label = str(match.get('label') or '')
        score = float(match.get('confidence', 0))
        if label:
            recognized += 1
            confidence += score
            inliers += int(match.get('inliers', 0))
            margin += int(match.get('margin', 0))
        slots.append(
            {
                **value,
                'crop': {
                    name: round(float(value['crop'][name]), 6)
                    for name in ('x', 'y', 'w', 'h')
                },
                'suggested_label': label,
                'suggestion_confidence': score,
            }
        )
    score_key = (
        recognized / (team_size * 2),
        float(recognized),
        confidence,
        float(inliers),
        float(margin),
        -abs(center_shift),
    )
    return slots, score_key


def recognize_lineup(
    frame_path: Path,
    *,
    screen_type: str,
    team_size: Optional[int],
    panel_box: Optional[Mapping[str, float]] = None,
    recognize_crop: Optional[
        Callable[[Image.Image], Mapping[str, Any]]
    ] = None,
) -> Tuple[int, List[Dict[str, Any]]]:
    if screen_type not in _SCREEN_TYPES:
        raise ValueError('英雄阵容画面类型无效')
    if team_size is not None and team_size not in _TEAM_SIZES:
        raise ValueError('英雄阵容人数必须是 3 或 5')
    matcher = _current_sift_recognize if recognize_crop is None else recognize_crop
    with Image.open(frame_path) as source:
        image = source.convert('RGB')
    best_slots: Optional[List[Dict[str, Any]]] = None
    best_key: Optional[Tuple[float, ...]] = None
    best_transform: Optional[_Transform] = None
    selected_team_size = team_size
    for candidate_size in ((team_size,) if team_size is not None else (3, 5)):
        for transform in _candidate_transforms(
            image.width,
            image.height,
            team_size=candidate_size,
            panel_box=panel_box,
        ):
            slots, key = _recognize_variant(
                image,
                transform=transform,
                team_size=candidate_size,
                center_shift=0.0,
                recognize_crop=matcher,
            )
            if best_key is None or key > best_key:
                best_slots = slots
                best_key = key
                best_transform = transform
                selected_team_size = candidate_size
    if best_slots is None or best_transform is None or selected_team_size is None:
        raise RuntimeError('没有生成英雄头像位置')
    for shift in (-0.01, 0.01, -0.02, 0.02):
        slots, key = _recognize_variant(
            image,
            transform=best_transform,
            team_size=selected_team_size,
            center_shift=shift,
            recognize_crop=matcher,
        )
        if best_key is None or key > best_key:
            best_slots = slots
            best_key = key
    return selected_team_size, best_slots
