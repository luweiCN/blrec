from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple


class HighlightCutError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaProfile:
    codec_name: str
    width: int
    height: int
    r_frame_rate: str
    extradata_size: int
    duration_ms: int
    has_audio: bool


@dataclass(frozen=True)
class ClipSource:
    part_id: int
    path: str
    requested_start_ms: int
    requested_end_ms: int
    keyframes_ms: Tuple[int, ...] = ()


@dataclass(frozen=True)
class InspectedClipSource:
    part_id: int
    path: str
    actual_start_ms: int
    actual_end_ms: int
    output_offset_ms: int
    profile: MediaProfile


@dataclass(frozen=True)
class ClipInspection:
    sources: Tuple[InspectedClipSource, ...]
    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: int
    actual_end_ms: int
    extra_lead_ms: int
    confirmation_required: bool

    @property
    def output_duration_ms(self) -> int:
        return sum(
            source.actual_end_ms - source.actual_start_ms for source in self.sources
        )


@dataclass(frozen=True)
class CutArtifact:
    path: str
    size_bytes: int
    duration_ms: int


Probe = Callable[[str], Tuple[MediaProfile, Sequence[int]]]


class LosslessClipper:
    def __init__(
        self,
        *,
        ffmpeg: str = 'ffmpeg',
        ffprobe: str = 'ffprobe',
        probe: Optional[Probe] = None,
        cut_timeout_seconds: int = 3600,
        probe_timeout_seconds: int = 30,
    ) -> None:
        if cut_timeout_seconds <= 0 or probe_timeout_seconds <= 0:
            raise ValueError('FFmpeg timeout must be positive')
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._probe_override = probe
        self._cut_timeout_seconds = cut_timeout_seconds
        self._probe_timeout_seconds = probe_timeout_seconds

    def inspect(
        self,
        sources: Sequence[ClipSource],
        *,
        requested_start_ms: int,
        requested_end_ms: int,
        stable_end_ms: int,
    ) -> ClipInspection:
        if not sources:
            raise HighlightCutError('没有可用于剪辑的视频分段')
        if requested_start_ms < 0 or requested_end_ms <= requested_start_ms:
            raise HighlightCutError('剪辑时间范围无效')
        if requested_end_ms > stable_end_ms:
            raise HighlightCutError('剪辑结束位置超出当前安全范围')

        inspected: List[InspectedClipSource] = []
        first_profile: Optional[MediaProfile] = None
        output_offset_ms = 0
        first_extra_lead_ms = 0
        for index, source in enumerate(sources):
            path = Path(source.path)
            if not path.is_file() or path.stat().st_size <= 0:
                raise HighlightCutError('剪辑源视频不存在或为空')
            if (
                source.requested_start_ms < 0
                or source.requested_end_ms <= source.requested_start_ms
            ):
                raise HighlightCutError('源视频剪辑范围无效')
            profile, probed_keyframes = self._probe_media(str(path))
            if source.requested_end_ms > profile.duration_ms:
                raise HighlightCutError('剪辑范围超出源视频可用时长')
            if first_profile is None:
                first_profile = profile
            elif not self._compatible(first_profile, profile):
                raise HighlightCutError('跨分段视频编码参数不兼容，无法无损拼接')
            keyframes = source.keyframes_ms or tuple(probed_keyframes)
            previous = [
                int(value)
                for value in keyframes
                if 0 <= int(value) <= source.requested_start_ms
            ]
            if not previous:
                raise HighlightCutError('找不到剪辑起点之前的关键帧')
            actual_start_ms = max(previous)
            if index == 0:
                first_extra_lead_ms = source.requested_start_ms - actual_start_ms
            inspected.append(
                InspectedClipSource(
                    part_id=source.part_id,
                    path=str(path),
                    actual_start_ms=actual_start_ms,
                    actual_end_ms=source.requested_end_ms,
                    output_offset_ms=output_offset_ms,
                    profile=profile,
                )
            )
            output_offset_ms += source.requested_end_ms - actual_start_ms

        return ClipInspection(
            sources=tuple(inspected),
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
            actual_start_ms=requested_start_ms - first_extra_lead_ms,
            actual_end_ms=requested_end_ms,
            extra_lead_ms=first_extra_lead_ms,
            confirmation_required=first_extra_lead_ms > 10_000,
        )

    def cut(self, inspection: ClipInspection, output_path: str) -> CutArtifact:
        if not inspection.sources:
            raise HighlightCutError('没有可用于剪辑的视频分段')
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        source_paths = {Path(source.path).resolve() for source in inspection.sources}
        if output.resolve() in source_paths:
            raise HighlightCutError('剪辑输出不能覆盖源视频')

        suffix = output.suffix or '.mp4'
        temporary_output = self._temporary_path(output.parent, suffix, 'highlight-')
        segment_paths: List[str] = []
        concat_path: Optional[str] = None
        try:
            if len(inspection.sources) == 1:
                self._cut_source(inspection.sources[0], temporary_output)
            else:
                for source in inspection.sources:
                    segment_path = self._temporary_path(
                        output.parent,
                        suffix,
                        'highlight-part-{}-'.format(source.part_id),
                    )
                    segment_paths.append(segment_path)
                    self._cut_source(source, segment_path)
                concat_path = self._write_concat_file(output.parent, segment_paths)
                command = (
                    self._ffmpeg,
                    '-hide_banner',
                    '-nostdin',
                    '-f',
                    'concat',
                    '-safe',
                    '0',
                    '-i',
                    concat_path,
                    '-c',
                    'copy',
                    '-avoid_negative_ts',
                    'make_zero',
                    '-y',
                    temporary_output,
                )
                self._run_ffmpeg(command)

            if (
                not os.path.isfile(temporary_output)
                or os.path.getsize(temporary_output) <= 0
            ):
                raise HighlightCutError('FFmpeg 未生成有效的剪辑文件')
            output_profile, _keyframes = self._probe_media(temporary_output)
            expected_audio = inspection.sources[0].profile.has_audio
            if expected_audio and not output_profile.has_audio:
                raise HighlightCutError('无损剪辑结果丢失了音频流')
            planned_duration_ms = inspection.output_duration_ms
            tolerance_ms = max(2_000, int(planned_duration_ms * 0.02))
            if abs(output_profile.duration_ms - planned_duration_ms) > tolerance_ms:
                raise HighlightCutError('无损剪辑结果与计划时长差异过大')
            size_bytes = os.path.getsize(temporary_output)
            os.replace(temporary_output, str(output))
            return CutArtifact(
                path=str(output),
                size_bytes=size_bytes,
                duration_ms=output_profile.duration_ms,
            )
        finally:
            self._remove(temporary_output)
            for path in segment_paths:
                self._remove(path)
            if concat_path is not None:
                self._remove(concat_path)

    def _cut_source(self, source: InspectedClipSource, output_path: str) -> None:
        command = (
            self._ffmpeg,
            '-hide_banner',
            '-nostdin',
            '-ss',
            self._seconds(source.actual_start_ms),
            '-i',
            source.path,
            '-t',
            self._seconds(source.actual_end_ms - source.actual_start_ms),
            '-map',
            '0:v:0',
            '-map',
            '0:a?',
            '-c',
            'copy',
            '-avoid_negative_ts',
            'make_zero',
            '-y',
            output_path,
        )
        self._run_ffmpeg(command)

    def _run_ffmpeg(self, command: Tuple[str, ...]) -> None:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                shell=False,
                timeout=self._cut_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise HighlightCutError('FFmpeg 无损剪辑超时') from error
        except OSError as error:
            raise HighlightCutError('无法启动 FFmpeg，请确认已经安装 ffmpeg') from error
        if result.returncode != 0:
            diagnostic = self._diagnostic(result.stderr)
            raise HighlightCutError(
                'FFmpeg 无损剪辑失败{}'.format(
                    '：{}'.format(diagnostic) if diagnostic else ''
                )
            )

    def _probe_media(self, path: str) -> Tuple[MediaProfile, Tuple[int, ...]]:
        if self._probe_override is not None:
            profile, keyframes = self._probe_override(path)
            return profile, tuple(sorted(set(int(value) for value in keyframes)))
        command = (
            self._ffprobe,
            '-v',
            'error',
            '-select_streams',
            'v:0',
            '-skip_frame',
            'nokey',
            '-show_entries',
            'frame=best_effort_timestamp_time:stream=codec_name,width,height,'
            'r_frame_rate,extradata_size:format=duration',
            '-of',
            'json',
            path,
        )
        document = self._run_ffprobe(command)
        has_audio = self._probe_has_audio(path)
        return self._parse_probe(document, has_audio=has_audio)

    def _probe_has_audio(self, path: str) -> bool:
        command = (
            self._ffprobe,
            '-v',
            'error',
            '-select_streams',
            'a',
            '-show_entries',
            'stream=codec_type',
            '-of',
            'json',
            path,
        )
        document = self._run_ffprobe(command)
        streams = document.get('streams')
        if not isinstance(streams, Sequence) or isinstance(streams, (str, bytes)):
            raise HighlightCutError('ffprobe 返回了无效的音频信息')
        return any(
            isinstance(stream, Mapping) and stream.get('codec_type') == 'audio'
            for stream in streams
        )

    def _run_ffprobe(self, command: Tuple[str, ...]) -> Mapping[str, Any]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                shell=False,
                timeout=self._probe_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise HighlightCutError('ffprobe 检查视频超时') from error
        except OSError as error:
            raise HighlightCutError(
                '无法启动 ffprobe，请确认已经安装 ffprobe'
            ) from error
        if result.returncode != 0:
            raise HighlightCutError('ffprobe 无法读取视频')
        try:
            document = json.loads(result.stdout.decode('utf8'))
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise HighlightCutError('ffprobe 返回了无效的视频信息') from error
        if not isinstance(document, Mapping):
            raise HighlightCutError('ffprobe 返回了无效的视频信息')
        return document

    @staticmethod
    def _parse_probe(
        document: Mapping[str, Any], *, has_audio: bool
    ) -> Tuple[MediaProfile, Tuple[int, ...]]:
        streams = document.get('streams')
        frames = document.get('frames')
        file_format = document.get('format')
        if (
            not isinstance(streams, Sequence)
            or isinstance(streams, (str, bytes))
            or not streams
            or not isinstance(frames, Sequence)
            or isinstance(frames, (str, bytes))
            or not isinstance(file_format, Mapping)
        ):
            raise HighlightCutError('ffprobe 返回了无效的视频信息')
        stream = streams[0]
        if not isinstance(stream, Mapping):
            raise HighlightCutError('ffprobe 返回了无效的视频流信息')
        try:
            codec_name = str(stream['codec_name'])
            width = int(stream['width'])
            height = int(stream['height'])
            r_frame_rate = str(stream['r_frame_rate'])
            extradata_size = int(stream.get('extradata_size', 0))
            duration_seconds = float(file_format['duration'])
        except (KeyError, TypeError, ValueError) as error:
            raise HighlightCutError('ffprobe 返回了无效的视频流信息') from error
        if (
            not codec_name
            or width <= 0
            or height <= 0
            or not r_frame_rate
            or extradata_size < 0
            or not math.isfinite(duration_seconds)
            or duration_seconds <= 0
        ):
            raise HighlightCutError('ffprobe 返回了无效的视频流信息')
        keyframes = []
        for frame in frames:
            if not isinstance(frame, Mapping):
                continue
            try:
                seconds = float(frame['best_effort_timestamp_time'])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(seconds) and seconds >= 0:
                keyframes.append(int(round(seconds * 1000)))
        return (
            MediaProfile(
                codec_name=codec_name,
                width=width,
                height=height,
                r_frame_rate=r_frame_rate,
                extradata_size=extradata_size,
                duration_ms=int(round(duration_seconds * 1000)),
                has_audio=has_audio,
            ),
            tuple(sorted(set(keyframes))),
        )

    @staticmethod
    def _compatible(first: MediaProfile, second: MediaProfile) -> bool:
        return (
            first.codec_name,
            first.width,
            first.height,
            first.r_frame_rate,
            first.extradata_size,
            first.has_audio,
        ) == (
            second.codec_name,
            second.width,
            second.height,
            second.r_frame_rate,
            second.extradata_size,
            second.has_audio,
        )

    @staticmethod
    def _temporary_path(directory: Path, suffix: str, prefix: str) -> str:
        descriptor, path = tempfile.mkstemp(
            prefix=prefix, suffix=suffix, dir=str(directory)
        )
        os.close(descriptor)
        os.remove(path)
        return path

    @staticmethod
    def _write_concat_file(directory: Path, segment_paths: Sequence[str]) -> str:
        descriptor, path = tempfile.mkstemp(
            prefix='highlight-concat-', suffix='.txt', dir=str(directory)
        )
        try:
            with os.fdopen(descriptor, 'wt', encoding='utf8') as file:
                for segment_path in segment_paths:
                    escaped = os.path.abspath(segment_path).replace("'", "'\\''")
                    file.write("file '{}'\n".format(escaped))
        except BaseException:
            LosslessClipper._remove(path)
            raise
        return path

    @staticmethod
    def _seconds(milliseconds: int) -> str:
        return '{:.3f}'.format(milliseconds / 1000.0)

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    @staticmethod
    def _diagnostic(value: bytes) -> str:
        try:
            text = value.decode('utf8', errors='replace')
        except AttributeError:
            return ''
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return ' | '.join(lines[-3:])[-500:]
