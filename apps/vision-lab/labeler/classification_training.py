"""Ultralytics 分类训练的 16:9 全图数据集。"""

from __future__ import annotations

from typing import Any

from ultralytics.data.dataset import ClassificationDataset
from ultralytics.models.yolo.classify.train import ClassificationTrainer

from .classification_preprocessing import (
    CLASSIFICATION_INPUT_HEIGHT,
    CLASSIFICATION_INPUT_WIDTH,
    TrainingLetterboxTransform,
    ValidationLetterboxTransform,
)


class FullFrameClassificationDataset(ClassificationDataset):
    def __init__(
        self,
        root: str,
        args: Any,
        augment: bool = False,
        prefix: str = '',
        *,
        width: int = CLASSIFICATION_INPUT_WIDTH,
        height: int = CLASSIFICATION_INPUT_HEIGHT,
    ) -> None:
        super().__init__(root=root, args=args, augment=augment, prefix=prefix)
        self.torch_transforms = (
            TrainingLetterboxTransform(width=width, height=height)
            if augment
            else ValidationLetterboxTransform(width=width, height=height)
        )


class FullFrameClassificationTrainer(ClassificationTrainer):
    input_width = CLASSIFICATION_INPUT_WIDTH
    input_height = CLASSIFICATION_INPUT_HEIGHT

    def build_dataset(
        self, img_path: str, mode: str = 'train', batch: Any = None
    ) -> FullFrameClassificationDataset:
        return FullFrameClassificationDataset(
            root=img_path,
            args=self.args,
            augment=mode == 'train',
            prefix=mode,
            width=self.input_width,
            height=self.input_height,
        )
