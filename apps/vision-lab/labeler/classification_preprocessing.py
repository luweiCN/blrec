"""分类模型共享的全图 16:9 预处理。"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Optional, Tuple

import numpy as np
from PIL import Image, ImageEnhance

CLASSIFICATION_INPUT_WIDTH = 512
CLASSIFICATION_INPUT_HEIGHT = 288
INFERENCE_PAD_VALUE = 114

_IMAGENET_MEAN = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)


def aspect_fit_letterbox(
    image: Image.Image,
    *,
    width: int = CLASSIFICATION_INPUT_WIDTH,
    height: int = CLASSIFICATION_INPUT_HEIGHT,
    pad_color: int | Tuple[int, int, int] = INFERENCE_PAD_VALUE,
    content_scale: float = 1.0,
    position: Tuple[float, float] = (0.5, 0.5),
) -> Image.Image:
    """保留整张图，等比缩放后补边到固定画布。"""
    if width <= 0 or height <= 0:
        raise ValueError('目标宽高必须为正数')
    if image.width <= 0 or image.height <= 0:
        raise ValueError('原图宽高必须为正数')
    if not 0 < content_scale <= 1:
        raise ValueError('content_scale 必须在 (0, 1] 范围内')
    position_x = max(0.0, min(1.0, float(position[0])))
    position_y = max(0.0, min(1.0, float(position[1])))
    available_width = max(1, round(width * content_scale))
    available_height = max(1, round(height * content_scale))
    scale = min(available_width / image.width, available_height / image.height)
    resized_width = max(1, min(width, round(image.width * scale)))
    resized_height = max(1, min(height, round(image.height * scale)))
    resized = image.convert('RGB').resize(
        (resized_width, resized_height), Image.Resampling.BILINEAR
    )
    if isinstance(pad_color, int):
        color = (pad_color, pad_color, pad_color)
    else:
        color = tuple(int(max(0, min(255, value))) for value in pad_color)
    canvas = Image.new('RGB', (width, height), color)
    left = round((width - resized_width) * position_x)
    top = round((height - resized_height) * position_y)
    canvas.paste(resized, (left, top))
    return canvas


def classification_tensor(
    image: Image.Image,
    *,
    width: int = CLASSIFICATION_INPUT_WIDTH,
    height: int = CLASSIFICATION_INPUT_HEIGHT,
    pad_value: int = INFERENCE_PAD_VALUE,
) -> np.ndarray:
    """生成 ONNX 分类模型输入，形状为 ``1×3×H×W``。"""
    prepared = aspect_fit_letterbox(
        image, width=width, height=height, pad_color=pad_value
    )
    array = np.asarray(prepared, dtype=np.float32) / 255.0
    array = (array - _IMAGENET_MEAN) / _IMAGENET_STD
    return array.transpose(2, 0, 1)[None]


def preprocessing_metadata(
    *,
    width: int = CLASSIFICATION_INPUT_WIDTH,
    height: int = CLASSIFICATION_INPUT_HEIGHT,
) -> dict[str, Any]:
    if width <= 0 or height <= 0:
        raise ValueError('分类模型输入宽高必须为正数')
    return {
        'input': {'width': width, 'height': height},
        'preprocessing': {
            'color': 'RGB',
            'resize': 'aspect_fit_letterbox',
            'pad_value': INFERENCE_PAD_VALUE,
            'preserve_full_image': True,
            'scale': '0_to_1',
            'normalize': 'imagenet',
            'training_augmentation': {
                'pad_color': 'random_neutral',
                'content_scale': [0.88, 1.0],
                'position': 'random_when_padded',
                'horizontal_flip': False,
            },
        },
    }


@dataclass
class TrainingLetterboxTransform:
    """训练时随机补边，避免模型把某一种边色或边宽当成类别特征。"""

    width: int = CLASSIFICATION_INPUT_WIDTH
    height: int = CLASSIFICATION_INPUT_HEIGHT
    rng: Optional[random.Random] = None

    def __call__(self, image: Image.Image) -> Any:
        generator = self.rng or random
        neutral = generator.randint(0, 140)
        # 轻微色温变化用于模拟设备、采集链路和播放器背景，不使用鲜艳颜色。
        pad_color = (
            max(0, min(160, neutral + generator.randint(-8, 8))),
            max(0, min(160, neutral + generator.randint(-8, 8))),
            max(0, min(160, neutral + generator.randint(-8, 8))),
        )
        prepared = aspect_fit_letterbox(
            image,
            width=self.width,
            height=self.height,
            pad_color=pad_color,
            content_scale=generator.uniform(0.88, 1.0),
            position=(generator.random(), generator.random()),
        )
        if generator.random() < 0.8:
            prepared = ImageEnhance.Brightness(prepared).enhance(
                generator.uniform(0.88, 1.12)
            )
            prepared = ImageEnhance.Contrast(prepared).enhance(
                generator.uniform(0.9, 1.1)
            )
            prepared = ImageEnhance.Color(prepared).enhance(
                generator.uniform(0.92, 1.08)
            )
        array = np.asarray(prepared, dtype=np.float32) / 255.0
        array = (array - _IMAGENET_MEAN) / _IMAGENET_STD
        import torch

        return torch.from_numpy(array.transpose(2, 0, 1).copy())


@dataclass
class ValidationLetterboxTransform:
    width: int = CLASSIFICATION_INPUT_WIDTH
    height: int = CLASSIFICATION_INPUT_HEIGHT
    pad_value: int = INFERENCE_PAD_VALUE

    def __call__(self, image: Image.Image) -> Any:
        array = classification_tensor(
            image, width=self.width, height=self.height, pad_value=self.pad_value
        )[0]
        import torch

        return torch.from_numpy(array.copy())
