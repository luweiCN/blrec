from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple


class RecordingThumbnailUnavailable(RuntimeError):
    pass


class RecordingThumbnailProvider:
    """Generate small timeline previews without decoding the whole recording."""

    def __init__(
        self,
        *,
        ffmpeg: str = 'ffmpeg',
        max_items: int = 128,
        timeout_seconds: float = 8.0,
        runner: Optional[Callable[[Tuple[str, ...], float], Awaitable[bytes]]] = None,
    ) -> None:
        self._ffmpeg = ffmpeg
        self._max_items = max(1, max_items)
        self._timeout_seconds = timeout_seconds
        self._runner = runner or self._run_ffmpeg
        self._cache: OrderedDict[Tuple[str, int, int], bytes] = OrderedDict()
        self._render_slots = asyncio.Semaphore(1)

    async def get(self, path: str, time_ms: int, width: int) -> Tuple[bytes, bool]:
        rounded_ms = max(0, int(time_ms) // 1_000 * 1_000)
        key = (path, rounded_ms, width)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached, True

        if not Path(path).is_file():
            raise RecordingThumbnailUnavailable('录像文件不存在')

        command = (
            self._ffmpeg,
            '-hide_banner',
            '-loglevel',
            'error',
            '-nostdin',
            '-ss',
            '{:.3f}'.format(rounded_ms / 1_000.0),
            '-i',
            path,
            '-frames:v',
            '1',
            '-vf',
            'scale={}:{}:force_original_aspect_ratio=decrease'.format(width, -2),
            '-threads',
            '1',
            '-q:v',
            '5',
            '-f',
            'image2pipe',
            '-vcodec',
            'mjpeg',
            'pipe:1',
        )
        async with self._render_slots:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached, True
            content = await self._runner(command, self._timeout_seconds)
            if not content:
                raise RecordingThumbnailUnavailable('该时间点暂时无法生成预览')
            self._cache[key] = content
            while len(self._cache) > self._max_items:
                self._cache.popitem(last=False)
            return content, False

    async def _run_ffmpeg(
        self, command: Tuple[str, ...], timeout_seconds: float
    ) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except OSError as error:
            raise RecordingThumbnailUnavailable(
                '无法启动 FFmpeg，请确认已经安装 ffmpeg'
            ) from error
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError as error:
            process.kill()
            await process.communicate()
            raise RecordingThumbnailUnavailable('生成预览超时') from error
        if process.returncode != 0:
            message = stderr.decode('utf8', errors='replace').strip()
            raise RecordingThumbnailUnavailable(
                message[-500:] or 'FFmpeg 无法读取该时间点'
            )
        return stdout

    def clear(self) -> None:
        self._cache.clear()
