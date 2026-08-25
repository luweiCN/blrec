import asyncio
import stat
from pathlib import Path
from typing import List

import pytest

from blrec.bili_upload.bili_download import (
    YtDlpMediaDownloader,
    _LatestProgressReporter,
)
from blrec.bili_upload.crypto import CookieRecord


class FakeStdout:
    def __init__(self, lines: List[bytes]) -> None:
        self._lines = list(lines)
        self.readline_calls = 0

    async def readline(self) -> bytes:
        self.readline_calls += 1
        return self._lines.pop(0) if self._lines else b''


class FakeStderr:
    async def read(self, _size: int) -> bytes:
        return b''


class FakeProcess:
    def __init__(self, stdout: FakeStdout) -> None:
        self.stdout = stdout
        self.stderr = FakeStderr()
        self.returncode = 0

    async def wait(self) -> int:
        return 0


def test_builds_resource_friendly_highest_quality_command_on_selected_ip(
    tmp_path: Path,
) -> None:
    downloader = YtDlpMediaDownloader(executable='/usr/local/bin/yt-dlp')

    command = downloader.build_command(
        cookie_path=tmp_path / 'cookies.txt',
        output_template=str(tmp_path / 'video.%(ext)s'),
        bvid='BV1abcdefgh',
        page=2,
        source_address='192.168.50.10',
        write_danmaku=True,
    )

    assert command[0] == '/usr/local/bin/yt-dlp'
    assert command[command.index('--format') + 1] == 'bv*+ba/b'
    assert command[command.index('--format-sort') + 1] == 'res,codec:avc'
    assert command[command.index('--concurrent-fragments') + 1] == '1'
    assert command[command.index('--retries') + 1] == '20'
    assert command[command.index('--fragment-retries') + 1] == '20'
    assert '--continue' in command
    assert command[command.index('--source-address') + 1] == '192.168.50.10'
    assert command[command.index('--sub-langs') + 1] == 'danmaku'
    assert command[command.index('--sub-format') + 1] == 'xml'
    assert command[-1] == 'https://www.bilibili.com/video/BV1abcdefgh?p=2'


def test_builds_danmaku_only_command_without_downloading_video(tmp_path: Path) -> None:
    downloader = YtDlpMediaDownloader(executable='/usr/local/bin/yt-dlp')

    command = downloader.build_danmaku_command(
        cookie_path=tmp_path / 'cookies.txt',
        output_template=str(tmp_path / 'video.%(ext)s'),
        bvid='BV1abcdefgh',
        page=3,
        source_address=None,
    )

    assert '--skip-download' in command
    assert command[command.index('--sub-langs') + 1] == 'danmaku'
    assert command[command.index('--sub-format') + 1] == 'xml'
    assert command[-1] == 'https://www.bilibili.com/video/BV1abcdefgh?p=3'


def test_cookie_sidecar_keeps_the_unique_download_token(tmp_path: Path) -> None:
    first = tmp_path / '.video.mp4.yt-dlp-first'
    second = tmp_path / '.video.mp4.yt-dlp-second'

    assert YtDlpMediaDownloader._sidecar_path(
        first, '.cookies.txt'
    ) != YtDlpMediaDownloader._sidecar_path(second, '.cookies.txt')
    assert YtDlpMediaDownloader._sidecar_path(first, '.cookies.txt').name.endswith(
        'yt-dlp-first.cookies.txt'
    )


def test_video_partial_prefix_is_stable_for_the_same_cid(tmp_path: Path) -> None:
    target = tmp_path / 'video.mp4'

    first = YtDlpMediaDownloader._download_prefix(target, 123)
    second = YtDlpMediaDownloader._download_prefix(target, 123)
    replacement = YtDlpMediaDownloader._download_prefix(target, 456)

    assert first == second
    assert first != replacement


def test_writes_short_lived_netscape_cookie_file_with_owner_only_permissions(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'cookies.txt'
    cookies = (
        CookieRecord(
            name='SESSDATA',
            value='secret',
            domain='.bilibili.com',
            path='/',
            expires_at=2_000,
            secure=True,
            http_only=True,
        ),
        CookieRecord(
            name='expired',
            value='old',
            domain='.bilibili.com',
            path='/',
            expires_at=900,
            secure=True,
            http_only=False,
        ),
    )

    YtDlpMediaDownloader.write_cookie_file(cookies, path, now=1_000)

    content = path.read_text()
    assert '#HttpOnly_.bilibili.com' in content
    assert 'SESSDATA\tsecret' in content
    assert 'expired' not in content
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_parses_only_private_progress_protocol() -> None:
    assert YtDlpMediaDownloader.parse_progress('BLREC_PROGRESS:80:1024:4096') == (
        '80',
        1024,
        4096,
    )
    assert YtDlpMediaDownloader.parse_progress('BLREC_PROGRESS:30280:512:NA') == (
        '30280',
        512,
        None,
    )
    assert YtDlpMediaDownloader.parse_progress('[download] 50%') is None


@pytest.mark.asyncio
async def test_slow_progress_callback_does_not_block_yt_dlp_stdout() -> None:
    progress_lines = [
        'BLREC_PROGRESS:80:{}:100\n'.format(downloaded).encode()
        for downloaded in range(1, 101)
    ]
    stdout = FakeStdout(progress_lines + [b'BLREC_FILE:/tmp/downloaded.mp4\n'])
    process = FakeProcess(stdout)
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    calls = []

    async def slow_progress(downloaded: int, total: int) -> None:
        calls.append((downloaded, total))
        if len(calls) == 1:
            callback_started.set()
            await release_callback.wait()

    monitor = asyncio.create_task(
        YtDlpMediaDownloader()._monitor(
            process, slow_progress, interface_name=None  # type: ignore[arg-type]
        )
    )
    try:
        await asyncio.wait_for(callback_started.wait(), timeout=1)
        readline_calls_while_blocked = stdout.readline_calls
    finally:
        release_callback.set()

    output_path, error = await asyncio.wait_for(monitor, timeout=1)

    assert readline_calls_while_blocked == len(progress_lines) + 2
    assert output_path == '/tmp/downloaded.mp4'
    assert error == ''
    assert calls[-1] == (100, 112)
    assert len(calls) <= 2


@pytest.mark.asyncio
async def test_progress_reporter_rate_limits_and_flushes_latest_value() -> None:
    calls = []
    first_callback = asyncio.Event()

    async def progress(downloaded: int, total: int) -> None:
        calls.append((downloaded, total))
        first_callback.set()

    reporter = _LatestProgressReporter(progress, interval_seconds=60)
    reporter.update(1, 100)
    await asyncio.wait_for(first_callback.wait(), timeout=1)

    for downloaded in range(2, 101):
        reporter.update(downloaded, 100)
    await asyncio.sleep(0)

    assert calls == [(1, 100)]

    await asyncio.wait_for(reporter.close(), timeout=1)

    assert calls == [(1, 100), (100, 100)]
