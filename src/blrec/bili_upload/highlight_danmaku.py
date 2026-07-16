from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from lxml import etree


class DanmakuCutError(RuntimeError):
    pass


@dataclass(frozen=True)
class DanmakuClipSource:
    xml_path: str
    actual_start_ms: int
    actual_end_ms: int
    output_offset_ms: int


@dataclass(frozen=True)
class DanmakuCutResult:
    output_path: Optional[str]
    source_count: int
    message_count: int


@dataclass(frozen=True)
class _SelectedDanmaku:
    progress_ms: int
    source_order: int
    original_order: int
    attributes: Tuple[Tuple[str, str], ...]
    text: str


class HighlightDanmakuClipper:
    def cut(
        self, sources: Sequence[DanmakuClipSource], output_path: str
    ) -> DanmakuCutResult:
        selected: List[_SelectedDanmaku] = []
        source_count = 0
        for source_order, source in enumerate(sources):
            self._validate_source(source)
            path = Path(source.xml_path)
            if not path.is_file():
                continue
            source_count += 1
            selected.extend(self._read_source(path, source, source_order))

        if source_count == 0:
            return DanmakuCutResult(None, 0, 0)

        selected.sort(
            key=lambda item: (item.progress_ms, item.source_order, item.original_order)
        )
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix='highlight-danmaku-', suffix='.xml', dir=str(output.parent)
        )
        os.close(descriptor)
        try:
            root = etree.Element('i')
            for item in selected:
                element = etree.SubElement(root, 'd', dict(item.attributes))
                element.text = item.text
            etree.ElementTree(root).write(
                temporary_path, encoding='UTF-8', xml_declaration=True
            )
            os.replace(temporary_path, str(output))
        finally:
            self._remove(temporary_path)
        return DanmakuCutResult(str(output), source_count, len(selected))

    @staticmethod
    def _read_source(
        path: Path, source: DanmakuClipSource, source_order: int
    ) -> List[_SelectedDanmaku]:
        selected = []
        original_order = 0
        try:
            parser = etree.iterparse(
                str(path),
                events=('end',),
                resolve_entities=False,
                no_network=True,
                load_dtd=False,
                huge_tree=False,
            )
            for _event, element in parser:
                if element.tag == 'd':
                    raw_progress = element.get('p')
                    progress_ms = HighlightDanmakuClipper._progress_ms(raw_progress)
                    if (
                        progress_ms is not None
                        and source.actual_start_ms <= progress_ms < source.actual_end_ms
                    ):
                        new_progress_ms = (
                            source.output_offset_ms
                            + progress_ms
                            - source.actual_start_ms
                        )
                        attributes = dict(element.attrib)
                        fields = str(raw_progress).split(',')
                        fields[0] = '{:.3f}'.format(new_progress_ms / 1000.0)
                        attributes['p'] = ','.join(fields)
                        selected.append(
                            _SelectedDanmaku(
                                progress_ms=new_progress_ms,
                                source_order=source_order,
                                original_order=original_order,
                                attributes=tuple(attributes.items()),
                                text=element.text or '',
                            )
                        )
                    original_order += 1
                element.clear()
                parent = element.getparent()
                if parent is not None:
                    while element.getprevious() is not None:
                        del parent[0]
        except (OSError, etree.XMLSyntaxError) as error:
            raise DanmakuCutError("无法读取弹幕文件 '{}'".format(path)) from error
        return selected

    @staticmethod
    def _progress_ms(value: Optional[str]) -> Optional[int]:
        if not value:
            return None
        try:
            seconds = float(value.split(',', 1)[0])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return int(round(seconds * 1000))

    @staticmethod
    def _validate_source(source: DanmakuClipSource) -> None:
        if (
            source.actual_start_ms < 0
            or source.actual_end_ms <= source.actual_start_ms
            or source.output_offset_ms < 0
        ):
            raise DanmakuCutError('弹幕剪辑时间范围无效')

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
