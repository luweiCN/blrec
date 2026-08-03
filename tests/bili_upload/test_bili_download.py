import stat
from pathlib import Path

from blrec.bili_upload.bili_download import YtDlpMediaDownloader
from blrec.bili_upload.crypto import CookieRecord


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
