from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple


class HighlightCutError(RuntimeError):
    pass


class _FallbackUnavailableError(HighlightCutError):
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
    duration_ms: Optional[int] = None
    keyframes_ms: Tuple[int, ...] = ()
    recording: bool = False


@dataclass(frozen=True)
class InspectedClipSource:
    part_id: int
    path: str
    actual_start_ms: int
    actual_end_ms: int
    output_offset_ms: int
    profile: MediaProfile
    recording: bool = False


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
    strategy: str = 'stream_copy'
    fallback_reason: Optional[str] = None


@dataclass(frozen=True)
class _VideoPacket:
    pts_ms: int
    keyframe: bool
    data_hash: str


Probe = Callable[[str], Tuple[MediaProfile, Sequence[int]]]

_FINALIZED_SEEK_PREROLL_MS = 30_000
_FINALIZED_FINE_SEEK_MARGIN_MS = 100
_OUTPUT_DURATION_TOLERANCE_MS = 500
_BOUNDARY_TOLERANCE_MS = 100
_BOUNDARY_WINDOW_MS = 30_000
_BOUNDARY_LOOKAHEAD_MS = 5_000
_VISUAL_PROBE_PREROLL_MS = 120_000
_VISUAL_PROBE_WINDOW_MS = 200
_VISUAL_WIDTH = 64
_VISUAL_HEIGHT = 36
_VISUAL_MAE_TOLERANCE = 16.0


class LosslessClipper:
    def __init__(
        self,
        *,
        ffmpeg: str = 'ffmpeg',
        ffprobe: str = 'ffprobe',
        probe: Optional[Probe] = None,
        cut_timeout_seconds: int = 3600,
        probe_timeout_seconds: int = 30,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if cut_timeout_seconds <= 0 or probe_timeout_seconds <= 0:
            raise ValueError('FFmpeg timeout must be positive')
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._probe_override = probe
        self._cut_timeout_seconds = cut_timeout_seconds
        self._probe_timeout_seconds = probe_timeout_seconds
        self._monotonic = monotonic

    def inspect(
        self,
        sources: Sequence[ClipSource],
        *,
        requested_start_ms: int,
        requested_end_ms: int,
        stable_end_ms: int,
        deadline_monotonic: Optional[float] = None,
    ) -> ClipInspection:
        return self._inspect(
            sources,
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
            stable_end_ms=stable_end_ms,
            deadline_monotonic=deadline_monotonic,
            allow_multiple_sources=False,
        )

    def inspect_legacy(
        self,
        sources: Sequence[ClipSource],
        *,
        requested_start_ms: int,
        requested_end_ms: int,
        stable_end_ms: int,
        deadline_monotonic: Optional[float] = None,
    ) -> ClipInspection:
        return self._inspect(
            sources,
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
            stable_end_ms=stable_end_ms,
            deadline_monotonic=deadline_monotonic,
            allow_multiple_sources=True,
        )

    def _inspect(
        self,
        sources: Sequence[ClipSource],
        *,
        requested_start_ms: int,
        requested_end_ms: int,
        stable_end_ms: int,
        deadline_monotonic: Optional[float],
        allow_multiple_sources: bool,
    ) -> ClipInspection:
        if not sources or (not allow_multiple_sources and len(sources) != 1):
            raise HighlightCutError('每次剪辑必须且只能选择一个视频分段')
        if requested_start_ms < 0 or requested_end_ms <= requested_start_ms:
            raise HighlightCutError('剪辑时间范围无效')
        if requested_end_ms > stable_end_ms:
            raise HighlightCutError('剪辑结束位置超出当前安全范围')

        inspected: List[InspectedClipSource] = []
        first_profile: Optional[MediaProfile] = None
        output_offset_ms = 0
        first_extra_lead_ms = 0
        first_actual_start_ms = requested_start_ms
        for index, source in enumerate(sources):
            path = Path(source.path)
            if not path.is_file() or path.stat().st_size <= 0:
                raise HighlightCutError('剪辑源视频不存在或为空')
            if (
                source.requested_start_ms < 0
                or source.requested_end_ms <= source.requested_start_ms
            ):
                raise HighlightCutError('源视频剪辑范围无效')
            profile, probed_keyframes = self._probe_media(
                str(path),
                keyframe_at_ms=(
                    None if source.keyframes_ms else source.requested_start_ms
                ),
                known_duration_ms=source.duration_ms,
                deadline_monotonic=deadline_monotonic,
            )
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
            if previous:
                actual_start_ms = max(previous)
            else:
                following = [
                    int(value)
                    for value in keyframes
                    if source.requested_start_ms < int(value) < source.requested_end_ms
                ]
                if not following:
                    raise HighlightCutError('剪辑范围内没有可用的视频关键帧')
                actual_start_ms = min(following)
            if index == 0:
                first_extra_lead_ms = max(
                    0, source.requested_start_ms - actual_start_ms
                )
                first_actual_start_ms = (
                    requested_start_ms + actual_start_ms - source.requested_start_ms
                )
            inspected.append(
                InspectedClipSource(
                    part_id=source.part_id,
                    path=str(path),
                    actual_start_ms=actual_start_ms,
                    actual_end_ms=source.requested_end_ms,
                    output_offset_ms=output_offset_ms,
                    profile=profile,
                    recording=source.recording,
                )
            )
            output_offset_ms += source.requested_end_ms - actual_start_ms

        return ClipInspection(
            sources=tuple(inspected),
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
            actual_start_ms=first_actual_start_ms,
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

        suffixes = output.suffixes
        suffix = (
            suffixes[-2]
            if len(suffixes) >= 2 and suffixes[-1] == '.partial'
            else (output.suffix or '.mp4')
        )
        temporary_output = self._temporary_path(output.parent, suffix, 'highlight-')
        segment_paths: List[str] = []
        concat_paths: List[str] = []
        strategy = 'stream_copy'
        fallback_reason: Optional[str] = None
        try:
            try:
                self._render_stream_copy(
                    inspection,
                    temporary_output,
                    output.parent,
                    suffix,
                    segment_paths,
                    concat_paths,
                )
                output_profile = self._validate_output(
                    inspection, temporary_output, label='流复制', required_codec='h264'
                )
                self._validate_stream_copy_boundaries(inspection, temporary_output)
            except HighlightCutError as copy_error:
                if not self._can_fallback(copy_error):
                    raise
                fallback_reason = str(copy_error)[:500]
                strategy = 'transcoded'
                self._remove(temporary_output)
                for path in segment_paths:
                    self._remove(path)
                segment_paths.clear()
                try:
                    output_profile = self._render_validated_transcode(
                        inspection,
                        temporary_output,
                        output.parent,
                        suffix,
                        segment_paths,
                        concat_paths,
                        sequential=False,
                    )
                except HighlightCutError as transcode_error:
                    if not self._can_retry_sequential(inspection, transcode_error):
                        raise HighlightCutError(
                            '流复制剪辑未通过验收（{}）；自动转码兜底也失败（{}）'.format(
                                fallback_reason, str(transcode_error)[:500]
                            )
                        ) from transcode_error
                    fast_transcode_reason = str(transcode_error)[:500]
                    self._remove(temporary_output)
                    for path in segment_paths:
                        self._remove(path)
                    segment_paths.clear()
                    try:
                        output_profile = self._render_validated_transcode(
                            inspection,
                            temporary_output,
                            output.parent,
                            suffix,
                            segment_paths,
                            concat_paths,
                            sequential=True,
                        )
                    except HighlightCutError as sequential_error:
                        raise HighlightCutError(
                            '流复制剪辑未通过验收（{}）；快速转码失败（{}）；'
                            '顺序转码也失败（{}）'.format(
                                fallback_reason,
                                fast_transcode_reason,
                                str(sequential_error)[:500],
                            )
                        ) from sequential_error
                    strategy = 'transcoded_sequential'
                    fallback_reason = '{}；快速转码失败：{}'.format(
                        fallback_reason, fast_transcode_reason
                    )[:1000]
            size_bytes = os.path.getsize(temporary_output)
            os.replace(temporary_output, str(output))
            return CutArtifact(
                path=str(output),
                size_bytes=size_bytes,
                duration_ms=output_profile.duration_ms,
                strategy=strategy,
                fallback_reason=fallback_reason,
            )
        finally:
            self._remove(temporary_output)
            for path in segment_paths:
                self._remove(path)
            for path in concat_paths:
                self._remove(path)

    def _render_stream_copy(
        self,
        inspection: ClipInspection,
        output_path: str,
        directory: Path,
        suffix: str,
        segment_paths: List[str],
        concat_paths: List[str],
    ) -> None:
        if len(inspection.sources) == 1:
            self._cut_source(inspection.sources[0], output_path)
            return
        for source in inspection.sources:
            segment_path = self._temporary_path(
                directory, suffix, 'highlight-part-{}-'.format(source.part_id)
            )
            segment_paths.append(segment_path)
            self._cut_source(source, segment_path)
        self._concat_segments(directory, segment_paths, output_path, concat_paths)

    def _render_transcoded(
        self,
        inspection: ClipInspection,
        output_path: str,
        directory: Path,
        suffix: str,
        segment_paths: List[str],
        concat_paths: List[str],
        *,
        sequential: bool,
    ) -> None:
        if len(inspection.sources) == 1:
            self._transcode_source(
                inspection.sources[0], output_path, sequential=sequential
            )
            return
        for source in inspection.sources:
            segment_path = self._temporary_path(
                directory, suffix, 'highlight-transcoded-{}-'.format(source.part_id)
            )
            segment_paths.append(segment_path)
            self._transcode_source(source, segment_path, sequential=sequential)
        self._concat_segments(directory, segment_paths, output_path, concat_paths)

    def _render_validated_transcode(
        self,
        inspection: ClipInspection,
        output_path: str,
        directory: Path,
        suffix: str,
        segment_paths: List[str],
        concat_paths: List[str],
        *,
        sequential: bool,
    ) -> MediaProfile:
        self._render_transcoded(
            inspection,
            output_path,
            directory,
            suffix,
            segment_paths,
            concat_paths,
            sequential=sequential,
        )
        output_profile = self._validate_output(
            inspection,
            output_path,
            label='顺序转码' if sequential else '自动转码',
            required_codec='h264',
        )
        self._validate_transcoded_boundaries(inspection, output_path)
        return output_profile

    def _concat_segments(
        self,
        directory: Path,
        segment_paths: Sequence[str],
        output_path: str,
        concat_paths: List[str],
    ) -> None:
        concat_path = self._write_concat_file(directory, segment_paths)
        concat_paths.append(concat_path)
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
            output_path,
        )
        self._run_ffmpeg(command)

    def _validate_output(
        self,
        inspection: ClipInspection,
        output_path: str,
        *,
        label: str,
        required_codec: Optional[str] = None,
    ) -> MediaProfile:
        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise HighlightCutError('{}未生成有效的剪辑文件'.format(label))
        output_profile, _keyframes = self._probe_media(output_path)
        expected_audio = inspection.sources[0].profile.has_audio
        if expected_audio and not output_profile.has_audio:
            raise HighlightCutError('{}结果丢失了音频流'.format(label))
        if (
            required_codec is not None
            and output_profile.codec_name.lower() != required_codec
        ):
            raise HighlightCutError(
                '{}结果视频编码为 {}，需要转换为 {}'.format(
                    label, output_profile.codec_name, required_codec
                )
            )
        planned_duration_ms = inspection.output_duration_ms
        if (
            abs(output_profile.duration_ms - planned_duration_ms)
            > _OUTPUT_DURATION_TOLERANCE_MS
        ):
            raise HighlightCutError(
                '{}结果时长异常（计划 {:.3f} 秒，实际 {:.3f} 秒，'
                '偏差 {:+.3f} 秒）'.format(
                    label,
                    planned_duration_ms / 1000.0,
                    output_profile.duration_ms / 1000.0,
                    (output_profile.duration_ms - planned_duration_ms) / 1000.0,
                )
            )
        self._validate_fully_decodable(output_path, label=label)
        return output_profile

    def _validate_fully_decodable(self, output_path: str, *, label: str) -> None:
        if self._probe_override is not None:
            return
        command = (
            self._ffmpeg,
            '-hide_banner',
            '-nostdin',
            '-v',
            'error',
            '-xerror',
            '-i',
            output_path,
            '-map',
            '0:v:0',
            '-map',
            '0:a?',
            '-f',
            'null',
            '-',
        )
        try:
            self._run_ffmpeg(command)
        except _FallbackUnavailableError:
            raise
        except HighlightCutError as error:
            raise HighlightCutError(
                '{}结果无法完整解码（{}）'.format(label, str(error)[:500])
            ) from error

    @staticmethod
    def _can_fallback(error: HighlightCutError) -> bool:
        return not isinstance(error, _FallbackUnavailableError)

    @classmethod
    def _can_retry_sequential(
        cls, inspection: ClipInspection, error: HighlightCutError
    ) -> bool:
        return cls._can_fallback(error) and any(
            not source.recording and source.actual_start_ms > 0
            for source in inspection.sources
        )

    def _cut_source(self, source: InspectedClipSource, output_path: str) -> None:
        input_options: Tuple[str, ...]
        duration_ms = source.actual_end_ms - source.actual_start_ms
        if source.recording:
            input_options = (
                '-i',
                source.path,
                '-ss',
                self._seconds(source.actual_start_ms),
            )
        elif source.actual_start_ms == 0:
            input_options = ('-i', source.path)
        else:
            # FLV indexes can make one input-side seek land on either adjacent
            # GOP. Seek near the target first, then walk to just before the
            # keyframe selected during inspection so stream copy keeps it.
            coarse_seek_ms = max(0, source.actual_start_ms - _FINALIZED_SEEK_PREROLL_MS)
            relative_start_ms = source.actual_start_ms - coarse_seek_ms
            fine_seek_ms = max(0, relative_start_ms - _FINALIZED_FINE_SEEK_MARGIN_MS)
            seek_margin_ms = relative_start_ms - fine_seek_ms
            duration_ms += seek_margin_ms
            if coarse_seek_ms == 0:
                input_options = ('-i', source.path)
                if fine_seek_ms > 0:
                    input_options += ('-ss', self._seconds(fine_seek_ms))
            else:
                input_options = (
                    '-ss',
                    self._seconds(coarse_seek_ms),
                    '-i',
                    source.path,
                    '-ss',
                    self._seconds(fine_seek_ms),
                )
        command = (
            self._ffmpeg,
            '-hide_banner',
            '-nostdin',
            *input_options,
            '-t',
            self._seconds(duration_ms),
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

    def _transcode_source(
        self, source: InspectedClipSource, output_path: str, *, sequential: bool
    ) -> None:
        duration_ms = source.actual_end_ms - source.actual_start_ms
        input_options: Tuple[str, ...]
        if source.recording or sequential:
            input_options = ('-i', source.path)
            fine_seek_ms = source.actual_start_ms
        else:
            coarse_seek_ms = max(0, source.actual_start_ms - _FINALIZED_SEEK_PREROLL_MS)
            fine_seek_ms = source.actual_start_ms - coarse_seek_ms
            input_options = (
                ('-i', source.path)
                if coarse_seek_ms == 0
                else ('-ss', self._seconds(coarse_seek_ms), '-i', source.path)
            )
        command = (
            self._ffmpeg,
            '-hide_banner',
            '-nostdin',
            '-fflags',
            '+genpts',
            *input_options,
            '-ss',
            self._seconds(fine_seek_ms),
            '-t',
            self._seconds(duration_ms),
            '-map',
            '0:v:0',
            '-map',
            '0:a?',
            '-c:v',
            'libx264',
            '-preset',
            'veryfast',
            '-crf',
            '18',
            '-pix_fmt',
            'yuv420p',
            '-c:a',
            'aac',
            '-b:a',
            '192k',
            '-movflags',
            '+faststart',
            '-avoid_negative_ts',
            'make_zero',
            '-y',
            output_path,
        )
        self._run_ffmpeg(command)

    def _validate_stream_copy_boundaries(
        self, inspection: ClipInspection, output_path: str
    ) -> None:
        if self._probe_override is not None:
            return
        first_source = inspection.sources[0]
        last_source = inspection.sources[-1]
        source_start = self._probe_video_packets(
            first_source.path,
            start_ms=max(0, first_source.actual_start_ms - _BOUNDARY_WINDOW_MS),
            duration_ms=(
                min(_BOUNDARY_WINDOW_MS, first_source.actual_start_ms)
                + _BOUNDARY_LOOKAHEAD_MS
            ),
        )
        expected_start = min(
            (
                packet
                for packet in source_start
                if packet.keyframe
                and abs(packet.pts_ms - first_source.actual_start_ms)
                <= _BOUNDARY_TOLERANCE_MS
            ),
            key=lambda packet: abs(packet.pts_ms - first_source.actual_start_ms),
            default=None,
        )
        output_start = self._probe_video_packets(
            output_path, start_ms=0, duration_ms=_BOUNDARY_LOOKAHEAD_MS
        )
        if (
            expected_start is None
            or not output_start
            or not output_start[0].keyframe
            or output_start[0].data_hash != expected_start.data_hash
        ):
            raise HighlightCutError('流复制结果没有从预检确定的关键帧开始')

        source_tail_start_ms = max(0, last_source.actual_end_ms - _BOUNDARY_WINDOW_MS)
        source_tail = self._probe_video_packets(
            last_source.path,
            start_ms=source_tail_start_ms,
            duration_ms=(
                last_source.actual_end_ms
                - source_tail_start_ms
                + _BOUNDARY_LOOKAHEAD_MS
            ),
        )
        output_tail_start_ms = max(
            0, inspection.output_duration_ms - _BOUNDARY_WINDOW_MS
        )
        output_tail = self._probe_video_packets(
            output_path,
            start_ms=output_tail_start_ms,
            duration_ms=(
                inspection.output_duration_ms
                - output_tail_start_ms
                + _BOUNDARY_LOOKAHEAD_MS
            ),
        )
        source_pts_by_hash = {packet.data_hash: packet.pts_ms for packet in source_tail}
        mapped_tail_pts = [
            source_pts_by_hash[packet.data_hash]
            for packet in output_tail
            if packet.data_hash in source_pts_by_hash
        ]
        if not mapped_tail_pts:
            raise HighlightCutError('无法核对流复制结果的结束边界')
        actual_tail_ms = max(mapped_tail_pts)
        if (
            actual_tail_ms < last_source.actual_end_ms - _BOUNDARY_TOLERANCE_MS
            or actual_tail_ms > last_source.actual_end_ms + _BOUNDARY_TOLERANCE_MS
        ):
            raise HighlightCutError(
                '流复制结果结束边界异常（计划 {:.3f} 秒，实际源位置 {:.3f} 秒）'.format(
                    last_source.actual_end_ms / 1000.0, actual_tail_ms / 1000.0
                )
            )

    def _probe_video_packets(
        self, path: str, *, start_ms: int, duration_ms: int
    ) -> Tuple[_VideoPacket, ...]:
        interval = (
            '%+{}'.format(self._seconds(duration_ms))
            if start_ms == 0
            else '{}%+{}'.format(self._seconds(start_ms), self._seconds(duration_ms))
        )
        command = (
            self._ffprobe,
            '-v',
            'error',
            '-select_streams',
            'v:0',
            '-read_intervals',
            interval,
            '-show_packets',
            '-show_entries',
            'packet=pts_time,flags,data_hash',
            '-show_data_hash',
            'sha256',
            '-of',
            'json',
            path,
        )
        document = self._run_ffprobe(command)
        packets = document.get('packets')
        if not isinstance(packets, Sequence) or isinstance(packets, (str, bytes)):
            raise HighlightCutError('ffprobe 返回了无效的视频数据包信息')
        parsed: List[_VideoPacket] = []
        for packet in packets:
            if not isinstance(packet, Mapping):
                continue
            try:
                pts = float(packet['pts_time'])
                data_hash = str(packet['data_hash'])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(pts) or not data_hash:
                continue
            parsed.append(
                _VideoPacket(
                    pts_ms=int(round(pts * 1000)),
                    keyframe='K' in str(packet.get('flags', '')),
                    data_hash=data_hash,
                )
            )
        return tuple(parsed)

    def _validate_transcoded_boundaries(
        self, inspection: ClipInspection, output_path: str
    ) -> None:
        if self._probe_override is not None:
            return
        first_source = inspection.sources[0]
        last_source = inspection.sources[-1]
        source_start = self._probe_visual_frames(
            first_source.path,
            start_ms=first_source.actual_start_ms,
            duration_ms=_VISUAL_PROBE_WINDOW_MS,
            recording=first_source.recording,
        )
        output_start = self._probe_visual_frames(
            output_path,
            start_ms=0,
            duration_ms=_VISUAL_PROBE_WINDOW_MS,
            recording=False,
        )
        if not self._visually_matches(output_start[:3], source_start):
            raise HighlightCutError('自动转码结果没有从预检确定的画面开始')

        source_tail_start_ms = max(
            last_source.actual_start_ms,
            last_source.actual_end_ms - _VISUAL_PROBE_WINDOW_MS,
        )
        output_tail_start_ms = max(
            0, inspection.output_duration_ms - _VISUAL_PROBE_WINDOW_MS
        )
        source_tail = self._probe_visual_frames(
            last_source.path,
            start_ms=source_tail_start_ms,
            duration_ms=last_source.actual_end_ms - source_tail_start_ms,
            recording=last_source.recording,
        )
        output_tail = self._probe_visual_frames(
            output_path,
            start_ms=output_tail_start_ms,
            duration_ms=_VISUAL_PROBE_WINDOW_MS * 2,
            recording=False,
        )
        if not self._visually_matches(output_tail[-3:], source_tail):
            raise HighlightCutError('自动转码结果的结束画面与计划边界不一致')

    def _probe_visual_frames(
        self, path: str, *, start_ms: int, duration_ms: int, recording: bool
    ) -> Tuple[bytes, ...]:
        if duration_ms <= 0:
            raise HighlightCutError('画面边界检查范围无效')
        input_options: Tuple[str, ...]
        if recording:
            input_options = ('-i', path)
            fine_seek_ms = start_ms
        else:
            coarse_seek_ms = max(0, start_ms - _VISUAL_PROBE_PREROLL_MS)
            fine_seek_ms = start_ms - coarse_seek_ms
            input_options = (
                ('-i', path)
                if coarse_seek_ms == 0
                else ('-ss', self._seconds(coarse_seek_ms), '-i', path)
            )
        seek_options = () if fine_seek_ms == 0 else ('-ss', self._seconds(fine_seek_ms))
        command = (
            self._ffmpeg,
            '-hide_banner',
            '-nostdin',
            *input_options,
            *seek_options,
            '-t',
            self._seconds(duration_ms),
            '-map',
            '0:v:0',
            '-an',
            '-sn',
            '-dn',
            '-vf',
            'scale={}:{}:flags=area,format=gray'.format(_VISUAL_WIDTH, _VISUAL_HEIGHT),
            '-f',
            'rawvideo',
            'pipe:1',
        )
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                shell=False,
                timeout=self._cut_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise _FallbackUnavailableError('自动转码画面边界检查超时') from error
        except OSError as error:
            raise _FallbackUnavailableError('无法启动 FFmpeg 检查转码画面') from error
        if result.returncode != 0:
            diagnostic = self._diagnostic(result.stderr)
            raise HighlightCutError(
                '自动转码画面边界检查失败{}'.format(
                    '：{}'.format(diagnostic) if diagnostic else ''
                )
            )
        frame_size = _VISUAL_WIDTH * _VISUAL_HEIGHT
        frame_count = len(result.stdout) // frame_size
        if frame_count <= 0:
            raise HighlightCutError('自动转码画面边界检查没有读取到视频帧')
        return tuple(
            result.stdout[index * frame_size : (index + 1) * frame_size]
            for index in range(frame_count)
        )

    @staticmethod
    def _visually_matches(first: Sequence[bytes], second: Sequence[bytes]) -> bool:
        if not first or not second:
            return False
        window_size = min(3, len(first), len(second))
        first_window = first[:window_size]
        for offset in range(len(second) - window_size + 1):
            second_window = second[offset : offset + window_size]
            if all(
                len(first_frame) == len(second_frame)
                and bool(first_frame)
                and sum(
                    abs(left - right) for left, right in zip(first_frame, second_frame)
                )
                / len(first_frame)
                <= _VISUAL_MAE_TOLERANCE
                for first_frame, second_frame in zip(first_window, second_window)
            ):
                return True
        return False

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
            raise _FallbackUnavailableError('FFmpeg 剪辑超时') from error
        except OSError as error:
            raise _FallbackUnavailableError(
                '无法启动 FFmpeg，请确认已经安装 ffmpeg'
            ) from error
        if result.returncode != 0:
            diagnostic = self._diagnostic(result.stderr)
            raise HighlightCutError(
                'FFmpeg 剪辑失败{}'.format(
                    '：{}'.format(diagnostic) if diagnostic else ''
                )
            )

    def _probe_media(
        self,
        path: str,
        *,
        keyframe_at_ms: Optional[int] = None,
        known_duration_ms: Optional[int] = None,
        deadline_monotonic: Optional[float] = None,
    ) -> Tuple[MediaProfile, Tuple[int, ...]]:
        if self._probe_override is not None:
            profile, keyframes = self._probe_override(path)
            return profile, tuple(sorted(set(int(value) for value in keyframes)))
        profile_command = (
            self._ffprobe,
            '-v',
            'error',
            '-show_entries',
            'stream=codec_type,codec_name,width,height,'
            'r_frame_rate,extradata_size:format=duration',
            '-of',
            'json',
            path,
        )
        profile = self._parse_profile(
            self._run_ffprobe(profile_command, deadline_monotonic),
            known_duration_ms=known_duration_ms,
        )
        if keyframe_at_ms is None:
            return profile, ()
        interval_start_ms = max(0, keyframe_at_ms - 120_000)
        interval_end_ms = min(profile.duration_ms, keyframe_at_ms + 5_000)
        interval_duration_ms = max(1, interval_end_ms - interval_start_ms)
        keyframe_command = (
            self._ffprobe,
            '-v',
            'error',
            '-select_streams',
            'v:0',
            '-skip_frame',
            'nokey',
            '-read_intervals',
            '{}%+{}'.format(
                self._seconds(interval_start_ms), self._seconds(interval_duration_ms)
            ),
            '-show_entries',
            'frame=best_effort_timestamp_time',
            '-of',
            'json',
            path,
        )
        keyframes = self._parse_keyframes(
            self._run_ffprobe(keyframe_command, deadline_monotonic)
        )
        if not any(keyframe <= keyframe_at_ms for keyframe in keyframes):
            prefix_end_ms = min(profile.duration_ms, keyframe_at_ms + 5_000)
            sequential_keyframe_command = (
                self._ffprobe,
                '-v',
                'error',
                '-select_streams',
                'v:0',
                '-skip_frame',
                'nokey',
                '-read_intervals',
                '%+{}'.format(self._seconds(max(1, prefix_end_ms))),
                '-show_entries',
                'frame=best_effort_timestamp_time',
                '-of',
                'json',
                path,
            )
            keyframes = self._parse_keyframes(
                self._run_ffprobe(sequential_keyframe_command, deadline_monotonic)
            )
        return profile, keyframes

    def _run_ffprobe(
        self, command: Tuple[str, ...], deadline_monotonic: Optional[float] = None
    ) -> Mapping[str, Any]:
        timeout = float(self._probe_timeout_seconds)
        if deadline_monotonic is not None:
            remaining = deadline_monotonic - self._monotonic()
            if remaining <= 0:
                raise _FallbackUnavailableError('ffprobe 检查视频超时')
            timeout = min(timeout, remaining)
        try:
            result = subprocess.run(
                command, capture_output=True, check=False, shell=False, timeout=timeout
            )
        except subprocess.TimeoutExpired as error:
            raise _FallbackUnavailableError('ffprobe 检查视频超时') from error
        except OSError as error:
            raise _FallbackUnavailableError(
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
    def _parse_profile(
        document: Mapping[str, Any], *, known_duration_ms: Optional[int]
    ) -> MediaProfile:
        streams = document.get('streams')
        file_format = document.get('format')
        if (
            not isinstance(streams, Sequence)
            or isinstance(streams, (str, bytes))
            or not streams
            or not isinstance(file_format, Mapping)
        ):
            raise HighlightCutError('ffprobe 返回了无效的视频信息')
        stream = next(
            (
                item
                for item in streams
                if isinstance(item, Mapping) and item.get('codec_type') == 'video'
            ),
            None,
        )
        if stream is None:
            raise HighlightCutError('ffprobe 返回了无效的视频流信息')
        try:
            codec_name = str(stream['codec_name'])
            width = int(stream['width'])
            height = int(stream['height'])
            r_frame_rate = str(stream['r_frame_rate'])
            extradata_size = int(stream.get('extradata_size', 0))
            duration_ms = (
                int(known_duration_ms)
                if known_duration_ms is not None
                else int(round(float(file_format['duration']) * 1000))
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HighlightCutError('ffprobe 返回了无效的视频流信息') from error
        if (
            not codec_name
            or width <= 0
            or height <= 0
            or not r_frame_rate
            or extradata_size < 0
            or duration_ms <= 0
        ):
            raise HighlightCutError('ffprobe 返回了无效的视频流信息')
        return MediaProfile(
            codec_name=codec_name,
            width=width,
            height=height,
            r_frame_rate=r_frame_rate,
            extradata_size=extradata_size,
            duration_ms=duration_ms,
            has_audio=any(
                isinstance(item, Mapping) and item.get('codec_type') == 'audio'
                for item in streams
            ),
        )

    @staticmethod
    def _parse_keyframes(document: Mapping[str, Any]) -> Tuple[int, ...]:
        frames = document.get('frames')
        if not isinstance(frames, Sequence) or isinstance(frames, (str, bytes)):
            raise HighlightCutError('ffprobe 返回了无效的关键帧信息')
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
        return tuple(sorted(set(keyframes)))

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
