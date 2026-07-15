from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .upos import FileIdentity

__all__ = ('RemuxedArtifact', 'TranscodeRemuxError', 'TranscodeRemuxer')


class TranscodeRemuxError(RuntimeError):
    pass


@dataclass(frozen=True)
class _MediaProfile:
    duration_seconds: float
    video_streams: int
    audio_streams: int


@dataclass(frozen=True)
class RemuxedArtifact:
    path: str
    identity: FileIdentity
    diagnostic: str


class TranscodeRemuxer:
    def __init__(
        self,
        work_directory: Path,
        *,
        ffmpeg: str = 'ffmpeg',
        ffprobe: str = 'ffprobe',
        remux_timeout_seconds: int = 3600,
        probe_timeout_seconds: int = 30,
    ) -> None:
        if remux_timeout_seconds <= 0 or probe_timeout_seconds <= 0:
            raise ValueError('FFmpeg timeout must be positive')
        self._work_directory = Path(work_directory)
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._remux_timeout_seconds = remux_timeout_seconds
        self._probe_timeout_seconds = probe_timeout_seconds

    def remux(self, source_path: str, *, part_id: int) -> RemuxedArtifact:
        source = Path(source_path).resolve()
        if not source.is_file() or source.stat().st_size <= 0:
            raise TranscodeRemuxError('待重新封装的视频不存在或为空')
        source_profile = self._probe(str(source))
        self._work_directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(str(self._work_directory), 0o700)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix='part-{}-'.format(part_id),
            suffix='.mp4',
            dir=str(self._work_directory),
        )
        os.close(descriptor)
        try:
            command = (
                self._ffmpeg,
                '-hide_banner',
                '-nostdin',
                '-fflags',
                '+genpts',
                '-i',
                str(source),
                '-map',
                '0',
                '-c',
                'copy',
                '-avoid_negative_ts',
                'make_zero',
                '-y',
                temporary_path,
            )
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                    shell=False,
                    timeout=self._remux_timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise TranscodeRemuxError('FFmpeg 重新封装超时') from error
            except OSError as error:
                raise TranscodeRemuxError(
                    '无法启动 FFmpeg，请确认已经安装 ffmpeg'
                ) from error
            diagnostic = self._diagnostic(result.stderr)
            if result.returncode != 0:
                raise TranscodeRemuxError(
                    'FFmpeg 重新封装失败{}'.format(
                        '：{}'.format(diagnostic) if diagnostic else ''
                    )
                )
            output_profile = self._probe(temporary_path)
            self._validate_profiles(source_profile, output_profile)
            identity = FileIdentity.from_path(temporary_path)
            return RemuxedArtifact(
                path=temporary_path,
                identity=identity,
                diagnostic=diagnostic or 'FFmpeg 流复制重新封装完成',
            )
        except BaseException:
            self.remove(temporary_path)
            raise

    @staticmethod
    def remove(path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _probe(self, path: str) -> _MediaProfile:
        command = (
            self._ffprobe,
            '-v',
            'error',
            '-show_entries',
            'stream=codec_type:format=duration',
            '-of',
            'json',
            path,
        )
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                shell=False,
                timeout=self._probe_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise TranscodeRemuxError('ffprobe 检查视频超时') from error
        except OSError as error:
            raise TranscodeRemuxError(
                '无法启动 ffprobe，请确认已经安装 ffprobe'
            ) from error
        if result.returncode != 0:
            raise TranscodeRemuxError('ffprobe 无法读取视频')
        try:
            document = json.loads(result.stdout.decode('utf8'))
            return self._profile(document)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise TranscodeRemuxError('ffprobe 返回了无效的视频信息') from error

    @staticmethod
    def _profile(document: Mapping[str, Any]) -> _MediaProfile:
        if not isinstance(document, Mapping):
            raise ValueError('invalid probe document')
        streams = document.get('streams')
        file_format = document.get('format')
        if not isinstance(streams, Sequence) or isinstance(streams, (str, bytes)):
            raise ValueError('missing streams')
        if not isinstance(file_format, Mapping):
            raise ValueError('missing format')
        raw_duration = file_format.get('duration')
        if isinstance(raw_duration, bool) or not isinstance(
            raw_duration, (int, float, str, bytes)
        ):
            raise ValueError('invalid duration')
        duration = float(raw_duration)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('invalid duration')
        video_streams = sum(
            1
            for stream in streams
            if isinstance(stream, Mapping) and stream.get('codec_type') == 'video'
        )
        audio_streams = sum(
            1
            for stream in streams
            if isinstance(stream, Mapping) and stream.get('codec_type') == 'audio'
        )
        if video_streams < 1:
            raise ValueError('missing video stream')
        return _MediaProfile(duration, video_streams, audio_streams)

    @staticmethod
    def _validate_profiles(source: _MediaProfile, output: _MediaProfile) -> None:
        if output.video_streams < 1:
            raise TranscodeRemuxError('重新封装结果缺少视频流')
        if source.audio_streams > 0 and output.audio_streams < 1:
            raise TranscodeRemuxError('重新封装结果丢失了音频流')
        tolerance = max(2.0, source.duration_seconds * 0.02)
        if abs(source.duration_seconds - output.duration_seconds) > tolerance:
            raise TranscodeRemuxError('重新封装前后视频时长差异过大')

    @staticmethod
    def _diagnostic(value: bytes) -> str:
        try:
            text = value.decode('utf8', errors='replace')
        except AttributeError:
            return ''
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return ' | '.join(lines[-3:])[-500:]
