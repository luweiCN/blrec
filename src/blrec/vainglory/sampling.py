from __future__ import annotations

import json
import os
import select
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any, Dict, Iterator, List, Literal, Optional, Sequence, Tuple

from .vision import RgbFrame


class InvalidImage(ValueError):
    pass


@dataclass(frozen=True)
class VideoProfile:
    width: int
    height: int
    duration_ms: int


@dataclass(frozen=True)
class TimedFrame:
    at_ms: int
    frame: RgbFrame


@dataclass(frozen=True)
class CoarseObservation:
    at_ms: int
    hud_signature: Optional[str]
    result_visible: bool
    team_size: Optional[int] = None
    visible_portraits: int = 0
    view_context: Literal['played', 'observed', 'unknown'] = 'unknown'
    hero_lineup: Tuple[str, ...] = ()
    scene_signature: str = ''


@dataclass(frozen=True)
class ScanWindow:
    start_ms: int
    end_ms: int
    view_context: Literal['played', 'observed', 'unknown'] = 'unknown'
    hero_lineup: Tuple[str, ...] = ()
    focus_ms: Optional[int] = None


def hud_lineup_similarity(left: str, right: str) -> float:
    if left == right and left:
        return 1.0
    left_hashes = left.split(':')
    right_hashes = right.split(':')
    if not left_hashes or len(left_hashes) != len(right_hashes):
        return 0.0
    similarities: List[float] = []
    for left_hash, right_hash in zip(left_hashes, right_hashes):
        if len(left_hash) != len(right_hash) or not left_hash:
            return 0.0
        try:
            difference = int(left_hash, 16) ^ int(right_hash, 16)
        except ValueError:
            return 0.0
        bit_count = len(left_hash) * 4
        similarities.append(1.0 - bin(difference).count('1') / bit_count)
    return sum(similarities) / len(similarities)


def same_gameplay_run(
    previous: CoarseObservation,
    current: CoarseObservation,
    *,
    maximum_gap_ms: int = 75_000,
) -> bool:
    if previous.hud_signature is None or current.hud_signature is None:
        return False
    if previous.view_context != current.view_context:
        return False
    if current.at_ms <= previous.at_ms:
        return False
    lineup_evidence = hero_lineup_evidence(previous.hero_lineup, current.hero_lineup)
    if lineup_evidence == 'mismatched':
        return False
    if current.at_ms - previous.at_ms <= maximum_gap_ms:
        return True
    return lineup_evidence == 'matched'


def hero_lineup_evidence(
    previous: Sequence[str], current: Sequence[str]
) -> Literal['matched', 'mismatched', 'unknown']:
    if not previous or len(previous) != len(current) or len(previous) % 2 != 0:
        return 'unknown'
    team_size = len(previous) // 2
    minimum_matches = 2 if team_size == 3 else 3
    previous_sides = (Counter(previous[:team_size]), Counter(previous[team_size:]))
    current_sides = (Counter(current[:team_size]), Counter(current[team_size:]))
    for values in (*previous_sides, *current_sides):
        values.pop('', None)
    same_side = sum(
        sum((left & right).values())
        for left, right in zip(previous_sides, current_sides)
    )
    swapped_side = sum(
        sum((left & right).values())
        for left, right in zip(previous_sides, reversed(current_sides))
    )
    if same_side >= minimum_matches and same_side > swapped_side:
        return 'matched'
    if swapped_side >= minimum_matches and swapped_side > same_side:
        return 'mismatched'
    previous_known: Counter[str] = sum(previous_sides, Counter())
    current_known: Counter[str] = sum(current_sides, Counter())
    if (
        sum(previous_known.values()) >= team_size
        and sum(current_known.values()) >= team_size
        and max(same_side, swapped_side) < minimum_matches
    ):
        return 'mismatched'
    return 'unknown'


def _merge_hero_lineups(
    previous: Sequence[str], current: Sequence[str]
) -> Tuple[str, ...]:
    if not previous:
        return tuple(current)
    if not current or len(previous) != len(current):
        return tuple(previous)
    return tuple(left or right for left, right in zip(previous, current))


def fit_frame_dimensions(
    source_width: int, source_height: int, maximum_width: int, maximum_height: int
) -> Tuple[int, int]:
    if min(source_width, source_height, maximum_width, maximum_height) <= 0:
        raise ValueError('frame dimensions must be positive')
    scale = min(maximum_width / source_width, maximum_height / source_height)
    return (
        max(1, min(maximum_width, int(round(source_width * scale)))),
        max(1, min(maximum_height, int(round(source_height * scale)))),
    )


def result_search_windows(
    observations: Sequence[CoarseObservation],
    *,
    duration_ms: int,
    hud_gap_ms: int = 75_000,
    before_end_ms: int = 10_000,
    after_end_ms: int = 60_000,
) -> Tuple[ScanWindow, ...]:
    if duration_ms <= 0:
        raise ValueError('video duration must be positive')
    if hud_gap_ms <= 0:
        raise ValueError('HUD gap must be positive')
    if any(observation.scene_signature for observation in observations):
        return _transition_result_search_windows(
            observations,
            duration_ms=duration_ms,
            hud_gap_ms=hud_gap_ms,
            before_end_ms=before_end_ms,
            after_end_ms=after_end_ms,
        )
    windows: List[ScanWindow] = []
    gameplay = sorted(
        (
            observation
            for observation in observations
            if observation.hud_signature is not None
        ),
        key=lambda observation: observation.at_ms,
    )
    if gameplay:
        run_last = gameplay[0]
        run_lineup = run_last.hero_lineup
        for observation in gameplay[1:]:
            reference = replace(run_last, hero_lineup=run_lineup)
            if not same_gameplay_run(reference, observation, maximum_gap_ms=hud_gap_ms):
                windows.append(
                    _bounded_window(
                        run_last.at_ms - before_end_ms,
                        run_last.at_ms + after_end_ms,
                        duration_ms,
                        view_context=run_last.view_context,
                        hero_lineup=run_lineup,
                        focus_ms=run_last.at_ms,
                    )
                )
                run_lineup = observation.hero_lineup
            else:
                run_lineup = _merge_hero_lineups(run_lineup, observation.hero_lineup)
            run_last = observation
        windows.append(
            _bounded_window(
                run_last.at_ms - before_end_ms,
                run_last.at_ms + after_end_ms,
                duration_ms,
                view_context=run_last.view_context,
                hero_lineup=run_lineup,
                focus_ms=run_last.at_ms,
            )
        )
    windows.extend(
        _bounded_window(
            observation.at_ms - 3_000,
            observation.at_ms + 3_000,
            duration_ms,
            focus_ms=observation.at_ms,
        )
        for observation in observations
        if observation.result_visible
    )
    return _merge_windows(windows)


def _transition_result_search_windows(
    observations: Sequence[CoarseObservation],
    *,
    duration_ms: int,
    hud_gap_ms: int,
    before_end_ms: int,
    after_end_ms: int,
    scene_change_bits: int = 24,
) -> Tuple[ScanWindow, ...]:
    ordered = tuple(sorted(observations, key=lambda item: item.at_ms))
    gameplay_indexes = tuple(
        index
        for index, observation in enumerate(ordered)
        if observation.hud_signature is not None
    )
    windows: List[ScanWindow] = []
    if gameplay_indexes:
        run_last_index = gameplay_indexes[0]
        run_lineup = ordered[run_last_index].hero_lineup
        for index in gameplay_indexes[1:]:
            observation = ordered[index]
            reference = replace(ordered[run_last_index], hero_lineup=tuple(run_lineup))
            if observation.at_ms - reference.at_ms <= hud_gap_ms and same_gameplay_run(
                reference, observation, maximum_gap_ms=hud_gap_ms
            ):
                run_lineup = _merge_hero_lineups(run_lineup, observation.hero_lineup)
                run_last_index = index
                continue
            windows.extend(
                _gameplay_end_transition_windows(
                    ordered,
                    last_hud_index=run_last_index,
                    search_end_ms=min(observation.at_ms, duration_ms),
                    duration_ms=duration_ms,
                    before_end_ms=before_end_ms,
                    after_end_ms=after_end_ms,
                    hero_lineup=run_lineup,
                    scene_change_bits=scene_change_bits,
                )
            )
            run_last_index = index
            run_lineup = observation.hero_lineup
        windows.extend(
            _gameplay_end_transition_windows(
                ordered,
                last_hud_index=run_last_index,
                search_end_ms=duration_ms,
                duration_ms=duration_ms,
                before_end_ms=before_end_ms,
                after_end_ms=after_end_ms,
                hero_lineup=run_lineup,
                scene_change_bits=scene_change_bits,
            )
        )
    windows.extend(
        _bounded_window(
            observation.at_ms - 3_000,
            observation.at_ms + 3_000,
            duration_ms,
            focus_ms=observation.at_ms,
        )
        for observation in ordered
        if observation.result_visible
    )
    return _merge_transition_windows(windows)


def _gameplay_end_transition_windows(
    observations: Sequence[CoarseObservation],
    *,
    last_hud_index: int,
    search_end_ms: int,
    duration_ms: int,
    before_end_ms: int,
    after_end_ms: int,
    hero_lineup: Sequence[str],
    scene_change_bits: int,
) -> Tuple[ScanWindow, ...]:
    last_hud = observations[last_hud_index]
    bounded_end_ms = min(duration_ms, search_end_ms, last_hud.at_ms + after_end_ms)
    if bounded_end_ms <= last_hud.at_ms:
        return ()
    following = tuple(
        observation
        for observation in observations[last_hud_index + 1 :]
        if last_hud.at_ms < observation.at_ms <= bounded_end_ms
        and observation.hud_signature is None
    )
    first_end_ms = following[0].at_ms if following else bounded_end_ms
    windows = [
        _bounded_window(
            last_hud.at_ms - before_end_ms,
            first_end_ms,
            duration_ms,
            view_context=last_hud.view_context,
            hero_lineup=hero_lineup,
            focus_ms=last_hud.at_ms,
        )
    ]
    for previous, current in zip(following, following[1:]):
        if not _visual_state_changed(
            previous.scene_signature,
            current.scene_signature,
            minimum_bits=scene_change_bits,
        ):
            continue
        windows.append(
            _bounded_window(
                previous.at_ms,
                current.at_ms,
                duration_ms,
                view_context=last_hud.view_context,
                hero_lineup=hero_lineup,
                focus_ms=last_hud.at_ms,
            )
        )
    if following and following[-1].at_ms < bounded_end_ms:
        windows.append(
            _bounded_window(
                following[-1].at_ms,
                bounded_end_ms,
                duration_ms,
                view_context=last_hud.view_context,
                hero_lineup=hero_lineup,
                focus_ms=last_hud.at_ms,
            )
        )
    return tuple(windows)


def _visual_state_changed(left: str, right: str, *, minimum_bits: int) -> bool:
    if not left or len(left) != len(right):
        return False
    try:
        difference = int(left, 16) ^ int(right, 16)
    except ValueError:
        return False
    return bin(difference).count('1') >= minimum_bits


class FfmpegSampler:
    def __init__(
        self,
        *,
        ffmpeg: str = 'ffmpeg',
        ffprobe: str = 'ffprobe',
        coarse_interval_seconds: int = 5,
        fine_frames_per_second: int = 4,
    ) -> None:
        if coarse_interval_seconds < 1:
            raise ValueError('coarse interval must be positive')
        if fine_frames_per_second < 1:
            raise ValueError('fine frame rate must be positive')
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._coarse_interval_seconds = coarse_interval_seconds
        self._fine_frames_per_second = fine_frames_per_second
        self._profile_cache: Dict[str, Tuple[int, int, VideoProfile]] = {}

    def probe(self, path: str) -> VideoProfile:
        resolved = self._regular_file(path)
        stat = Path(resolved).stat()
        cached = self._profile_cache.get(resolved)
        if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
        command = [
            self._ffprobe,
            '-v',
            'error',
            '-select_streams',
            'v:0',
            '-show_entries',
            'stream=width,height:format=duration',
            '-of',
            'json',
            resolved,
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except FileNotFoundError as error:
            raise RuntimeError('未安装 FFprobe') from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError('FFprobe 读取视频超时') from error
        if result.returncode != 0:
            raise RuntimeError(_process_error('FFprobe', result))
        try:
            payload = json.loads(result.stdout.decode('utf8'))
            stream = payload['streams'][0]
            duration_ms = int(round(float(payload['format']['duration']) * 1_000))
            width = int(stream['width'])
            height = int(stream['height'])
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise RuntimeError('FFprobe 没有返回有效的视频信息') from error
        if duration_ms <= 0 or width <= 0 or height <= 0:
            raise RuntimeError('视频尺寸或时长无效')
        profile = VideoProfile(width=width, height=height, duration_ms=duration_ms)
        self._profile_cache[resolved] = (stat.st_mtime_ns, stat.st_size, profile)
        return profile

    def coarse_frames(self, path: str) -> Iterator[TimedFrame]:
        interval = self._coarse_interval_seconds
        profile = self.probe(path)
        width, height = fit_frame_dimensions(profile.width, profile.height, 480, 270)
        yield from self._frames(
            path,
            width=width,
            height=height,
            filter_value='fps=1/{},scale={}:{}:flags=fast_bilinear'.format(
                interval, width, height
            ),
            frame_step_ms=interval * 1_000,
            skip_frame='nokey',
        )

    def fine_frames(self, path: str, window: ScanWindow) -> Iterator[TimedFrame]:
        if window.end_ms <= window.start_ms:
            return
        fps = self._fine_frames_per_second
        profile = self.probe(path)
        width, height = fit_frame_dimensions(profile.width, profile.height, 960, 540)
        yield from self._frames(
            path,
            width=width,
            height=height,
            filter_value='fps={},scale={}:{}:flags=fast_bilinear'.format(
                fps, width, height
            ),
            frame_step_ms=1_000 // fps,
            start_ms=window.start_ms,
            duration_ms=window.end_ms - window.start_ms,
        )

    def result_preview_frames(
        self, path: str, window: ScanWindow, *, keyframes_only: bool
    ) -> Iterator[TimedFrame]:
        if window.end_ms <= window.start_ms:
            return
        interval = 2 if keyframes_only else 1
        profile = self.probe(path)
        width, height = fit_frame_dimensions(profile.width, profile.height, 480, 270)
        yield from self._frames(
            path,
            width=width,
            height=height,
            filter_value='fps=1/{},scale={}:{}:flags=fast_bilinear'.format(
                interval, width, height
            ),
            frame_step_ms=interval * 1_000,
            skip_frame='nokey' if keyframes_only else None,
            start_ms=window.start_ms,
            duration_ms=window.end_ms - window.start_ms,
        )

    def frame_at(self, path: str, at_ms: int) -> RgbFrame:
        if at_ms < 0:
            raise ValueError('frame time must not be negative')
        resolved = self._regular_file(path)
        profile = self.probe(path)
        width, height = fit_frame_dimensions(profile.width, profile.height, 1920, 1080)
        command = [
            self._ffmpeg,
            '-nostdin',
            '-v',
            'error',
            '-threads',
            '1',
            '-ss',
            '{:.3f}'.format(at_ms / 1_000),
            '-i',
            resolved,
            '-frames:v',
            '1',
            '-vf',
            'scale={}:{}:flags=bicubic'.format(width, height),
            '-an',
            '-sn',
            '-dn',
            '-f',
            'rawvideo',
            '-pix_fmt',
            'rgb24',
            'pipe:1',
        ]
        result = self._run_ffmpeg(command, timeout=60)
        expected = width * height * 3
        if len(result.stdout) != expected:
            raise RuntimeError('FFmpeg 未能读取指定时间的完整画面')
        return RgbFrame(width, height, result.stdout)

    def decode_image(self, content: bytes) -> RgbFrame:
        source_width, source_height = self._probe_image_dimensions(content)
        width, height = fit_frame_dimensions(source_width, source_height, 1920, 1080)
        return self._decode_image(content, width=width, height=height)

    def decode_wide_result_image(self, content: bytes) -> RgbFrame:
        return self.decode_image(content)

    def _decode_image(self, content: bytes, *, width: int, height: int) -> RgbFrame:
        if not content:
            raise InvalidImage('截图内容为空')
        command = [
            self._ffmpeg,
            '-nostdin',
            '-v',
            'error',
            '-threads',
            '1',
            '-i',
            'pipe:0',
            '-frames:v',
            '1',
            '-vf',
            'scale={}:{}:flags=bicubic'.format(width, height),
            '-an',
            '-sn',
            '-dn',
            '-f',
            'rawvideo',
            '-pix_fmt',
            'rgb24',
            'pipe:1',
        ]
        try:
            result = self._run_ffmpeg(command, timeout=30, input_data=content)
        except RuntimeError as error:
            raise InvalidImage('无法读取截图，请上传 PNG、JPEG 或 WebP 图片') from error
        expected = width * height * 3
        if len(result.stdout) != expected:
            raise InvalidImage('截图没有完整的画面')
        return RgbFrame(width, height, result.stdout)

    def _probe_image_dimensions(self, content: bytes) -> Tuple[int, int]:
        if not content:
            raise InvalidImage('截图内容为空')
        command = [
            self._ffprobe,
            '-v',
            'error',
            '-select_streams',
            'v:0',
            '-show_entries',
            'stream=width,height',
            '-of',
            'json',
            'pipe:0',
        ]
        try:
            result = subprocess.run(
                command,
                input=content,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except FileNotFoundError as error:
            raise RuntimeError('未安装 FFprobe') from error
        except subprocess.TimeoutExpired as error:
            raise InvalidImage('读取截图尺寸超时') from error
        if result.returncode != 0:
            raise InvalidImage('无法读取截图，请上传 PNG、JPEG 或 WebP 图片')
        try:
            stream = json.loads(result.stdout.decode('utf8'))['streams'][0]
            width = int(stream['width'])
            height = int(stream['height'])
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise InvalidImage('截图没有有效的画面尺寸') from error
        if width <= 0 or height <= 0:
            raise InvalidImage('截图没有有效的画面尺寸')
        return width, height

    def _frames(
        self,
        path: str,
        *,
        width: int,
        height: int,
        filter_value: str,
        frame_step_ms: int,
        skip_frame: Optional[str] = None,
        start_ms: int = 0,
        duration_ms: Optional[int] = None,
    ) -> Iterator[TimedFrame]:
        resolved = self._regular_file(path)
        command = [self._ffmpeg, '-nostdin', '-v', 'error', '-threads', '1']
        if skip_frame is not None:
            command.extend(('-skip_frame', skip_frame))
        if start_ms:
            command.extend(('-ss', '{:.3f}'.format(start_ms / 1_000)))
        command.extend(('-i', resolved))
        if duration_ms is not None:
            command.extend(('-t', '{:.3f}'.format(duration_ms / 1_000)))
        command.extend(
            (
                '-vf',
                filter_value,
                '-an',
                '-sn',
                '-dn',
                '-f',
                'rawvideo',
                '-pix_fmt',
                'rgb24',
                'pipe:1',
            )
        )
        frame_size = width * height * 3
        with tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=stderr
                )
            except FileNotFoundError as error:
                raise RuntimeError('未安装 FFmpeg') from error
            assert process.stdout is not None
            index = 0
            try:
                while True:
                    pixels = _read_exact(process.stdout, frame_size, timeout=60)
                    if not pixels:
                        break
                    if len(pixels) != frame_size:
                        raise RuntimeError('FFmpeg 返回了不完整的视频画面')
                    yield TimedFrame(
                        at_ms=start_ms + index * frame_step_ms,
                        frame=RgbFrame(width, height, pixels),
                    )
                    index += 1
                try:
                    return_code = process.wait(timeout=30)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError('FFmpeg 读取视频超时') from error
                if return_code != 0:
                    stderr.seek(0)
                    message = stderr.read().decode('utf8', errors='replace').strip()
                    raise RuntimeError(
                        'FFmpeg 读取视频失败：{}'.format(message or return_code)
                    )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    @staticmethod
    def _regular_file(path: str) -> str:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise ValueError('video path must be an existing regular file')
        return str(resolved)

    @staticmethod
    def _run_ffmpeg(
        command: Sequence[str], *, timeout: float, input_data: Optional[bytes] = None
    ) -> subprocess.CompletedProcess[Any]:
        try:
            result = subprocess.run(
                command,
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
            )
        except FileNotFoundError as error:
            raise RuntimeError('未安装 FFmpeg') from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError('FFmpeg 读取视频超时') from error
        if result.returncode != 0:
            raise RuntimeError(_process_error('FFmpeg', result))
        return result


def _bounded_window(
    start_ms: int,
    end_ms: int,
    duration_ms: int,
    *,
    view_context: Literal['played', 'observed', 'unknown'] = 'unknown',
    hero_lineup: Sequence[str] = (),
    focus_ms: Optional[int] = None,
) -> ScanWindow:
    return ScanWindow(
        start_ms=max(0, min(duration_ms, start_ms)),
        end_ms=max(0, min(duration_ms, end_ms)),
        view_context=view_context,
        hero_lineup=tuple(hero_lineup),
        focus_ms=(None if focus_ms is None else max(0, min(duration_ms, focus_ms))),
    )


def _merge_windows(windows: Sequence[ScanWindow]) -> Tuple[ScanWindow, ...]:
    ordered = sorted(
        (window for window in windows if window.end_ms > window.start_ms),
        key=lambda window: (window.start_ms, window.end_ms),
    )
    merged: List[ScanWindow] = []
    for window in ordered:
        if not merged or window.start_ms > merged[-1].end_ms:
            merged.append(window)
            continue
        previous = merged[-1]
        contexts = {previous.view_context, window.view_context}
        if contexts == {'played', 'observed'}:
            merged.append(window)
            continue
        if (
            previous.hero_lineup
            and window.hero_lineup
            and hero_lineup_evidence(previous.hero_lineup, window.hero_lineup)
            == 'mismatched'
        ):
            merged.append(window)
            continue
        view_context = (
            previous.view_context
            if previous.view_context != 'unknown'
            else window.view_context
        )
        merged[-1] = ScanWindow(
            start_ms=previous.start_ms,
            end_ms=max(previous.end_ms, window.end_ms),
            view_context=view_context,
            hero_lineup=_merge_hero_lineups(previous.hero_lineup, window.hero_lineup),
            focus_ms=(
                window.focus_ms if window.focus_ms is not None else previous.focus_ms
            ),
        )
    return tuple(merged)


def _merge_transition_windows(windows: Sequence[ScanWindow]) -> Tuple[ScanWindow, ...]:
    ordered = sorted(
        (window for window in windows if window.end_ms > window.start_ms),
        key=lambda window: (window.start_ms, window.end_ms),
    )
    merged: List[ScanWindow] = []
    for window in ordered:
        if not merged or window.start_ms >= merged[-1].end_ms:
            merged.append(window)
            continue
        previous = merged[-1]
        if previous.view_context != window.view_context and 'unknown' not in (
            previous.view_context,
            window.view_context,
        ):
            merged.append(window)
            continue
        merged[-1] = ScanWindow(
            start_ms=previous.start_ms,
            end_ms=max(previous.end_ms, window.end_ms),
            view_context=(
                previous.view_context
                if previous.view_context != 'unknown'
                else window.view_context
            ),
            hero_lineup=_merge_hero_lineups(previous.hero_lineup, window.hero_lineup),
            focus_ms=(
                previous.focus_ms if previous.focus_ms is not None else window.focus_ms
            ),
        )
    return tuple(merged)


def _read_exact(stream: IO[bytes], size: int, *, timeout: float) -> bytes:
    chunks = bytearray()
    deadline = time.monotonic() + timeout
    while len(chunks) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('FFmpeg 超过 60 秒没有返回视频画面')
        readable, _, _ = select.select((stream,), (), (), remaining)
        if not readable:
            raise RuntimeError('FFmpeg 超过 60 秒没有返回视频画面')
        try:
            chunk = os.read(stream.fileno(), size - len(chunks))
        except InterruptedError:
            continue
        if not chunk:
            break
        chunks.extend(chunk)
        deadline = time.monotonic() + timeout
    return bytes(chunks)


def _process_error(name: str, result: subprocess.CompletedProcess[Any]) -> str:
    message = result.stderr.decode('utf8', errors='replace').strip()
    return '{} 执行失败：{}'.format(name, message or result.returncode)
