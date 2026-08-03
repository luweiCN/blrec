from __future__ import annotations

import asyncio
import math
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from blrec.networking.manager import (
    NetworkRouteManager,
    NetworkUnavailable,
    RouteSelection,
)

from .crypto import CookieRecord, CredentialBundle

__all__ = ('BiliDownloadContractError', 'YtDlpMediaDownloader')


class BiliDownloadContractError(RuntimeError):
    pass


class YtDlpMediaDownloader:
    _PROGRESS_PREFIX = 'BLREC_PROGRESS:'
    _FILE_PREFIX = 'BLREC_FILE:'
    _MAX_ERROR_BYTES = 32 * 1024

    def __init__(
        self,
        *,
        network_manager: Optional[NetworkRouteManager] = None,
        executable: str = 'yt-dlp',
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._network_manager = network_manager
        self._executable = executable
        self._clock = clock

    async def download(
        self,
        bundle: CredentialBundle,
        *,
        bvid: str,
        cid: int,
        page: int,
        target: Path,
        danmaku_target: Optional[Path] = None,
        progress: Callable[[int, Optional[int]], Awaitable[None]],
    ) -> None:
        self._validate_source(bvid, cid, page)
        await asyncio.get_running_loop().run_in_executor(
            None, target.parent.mkdir, 0o700, True, True
        )
        token = secrets.token_hex(8)
        prefix = self._download_prefix(target, cid)
        await asyncio.get_running_loop().run_in_executor(
            None, self._cleanup_cookie_sidecars, prefix
        )
        cookie_path = self._sidecar_path(prefix, '.cookies-{}.txt'.format(token))
        self.write_cookie_file(bundle.cookies, cookie_path, now=int(self._clock()))
        selection = None
        affinity_key = 'archive:{}:{}'.format(bundle.mid, bvid)
        if self._network_manager is not None:
            try:
                selection = self._network_manager.select(
                    'archive_download', anonymous=False, affinity_key=affinity_key
                )
            except NetworkUnavailable:
                self._unlink_if_present(cookie_path)
                raise BiliDownloadContractError('历史稿件下载网络当前不可用') from None
        command = self.build_command(
            cookie_path=cookie_path,
            output_template=str(prefix) + '.%(ext)s',
            bvid=bvid,
            page=page,
            source_address=(None if selection is None else selection.source_address),
            write_danmaku=danmaku_target is not None,
        )
        environment = self.subprocess_environment(self._network_manager, selection)
        completed = False
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=environment,
                )
            except OSError:
                raise BiliDownloadContractError('NAS 容器中没有可用的 yt-dlp') from None
            try:
                output_path, error = await self._monitor(
                    process,
                    progress,
                    interface_name=(
                        None if selection is None else selection.interface_name
                    ),
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                await self._terminate(process)
                raise
            if process.returncode != 0:
                if self._network_manager is not None and selection is not None:
                    self._network_manager.report_failure(
                        'archive_download', selection.interface_name
                    )
                raise BiliDownloadContractError(
                    'yt-dlp 下载失败{}'.format('：' + error if error else '')
                )
            if self._network_manager is not None and selection is not None:
                self._network_manager.report_success(
                    'archive_download', selection.interface_name
                )
            source = self._validated_output(output_path, prefix)
            danmaku_source = (
                None
                if danmaku_target is None
                else self._validated_danmaku_output(prefix)
            )
            await asyncio.get_running_loop().run_in_executor(
                None, os.replace, str(source), str(target)
            )
            if danmaku_target is not None and danmaku_source is not None:
                await asyncio.get_running_loop().run_in_executor(
                    None, os.replace, str(danmaku_source), str(danmaku_target)
                )
            completed = True
        finally:
            await asyncio.get_running_loop().run_in_executor(
                None, self._unlink_if_present, cookie_path
            )
            if completed:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._cleanup_prefix, prefix
                )

    async def download_danmaku(
        self, bundle: CredentialBundle, *, bvid: str, cid: int, page: int, target: Path
    ) -> None:
        self._validate_source(bvid, cid, page)
        await asyncio.get_running_loop().run_in_executor(
            None, target.parent.mkdir, 0o700, True, True
        )
        token = secrets.token_hex(8)
        prefix = target.parent / '.{}.yt-dlp-{}'.format(target.name, token)
        cookie_path = self._sidecar_path(prefix, '.cookies.txt')
        self.write_cookie_file(bundle.cookies, cookie_path, now=int(self._clock()))
        selection = None
        affinity_key = 'archive:{}:{}'.format(bundle.mid, bvid)
        if self._network_manager is not None:
            try:
                selection = self._network_manager.select(
                    'archive_download', anonymous=False, affinity_key=affinity_key
                )
            except NetworkUnavailable:
                self._cleanup_prefix(prefix)
                raise BiliDownloadContractError('历史稿件下载网络当前不可用') from None
        command = self.build_danmaku_command(
            cookie_path=cookie_path,
            output_template=str(prefix) + '.%(ext)s',
            bvid=bvid,
            page=page,
            source_address=(None if selection is None else selection.source_address),
        )
        environment = self.subprocess_environment(self._network_manager, selection)
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=environment,
                )
            except OSError:
                raise BiliDownloadContractError('NAS 容器中没有可用的 yt-dlp') from None
            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=120
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                await self._terminate(process)
                raise
            except asyncio.TimeoutError:
                await self._terminate(process)
                raise BiliDownloadContractError('历史稿件弹幕下载超时') from None
            error = (stderr or b'')[-self._MAX_ERROR_BYTES :].decode(
                'utf8', errors='replace'
            )
            if process.returncode != 0:
                if self._network_manager is not None and selection is not None:
                    self._network_manager.report_failure(
                        'archive_download', selection.interface_name
                    )
                raise BiliDownloadContractError(
                    'yt-dlp 弹幕下载失败{}'.format(
                        '：' + error.strip()[:500] if error.strip() else ''
                    )
                )
            if self._network_manager is not None and selection is not None:
                self._network_manager.report_success(
                    'archive_download', selection.interface_name
                )
            source = self._validated_danmaku_output(prefix)
            await asyncio.get_running_loop().run_in_executor(
                None, os.replace, str(source), str(target)
            )
        finally:
            await asyncio.get_running_loop().run_in_executor(
                None, self._cleanup_prefix, prefix
            )

    def build_command(
        self,
        *,
        cookie_path: Path,
        output_template: str,
        bvid: str,
        page: int,
        source_address: Optional[str],
        write_danmaku: bool = False,
    ) -> Tuple[str, ...]:
        command: List[str] = [
            self._executable,
            '--no-config',
            '--no-playlist',
            '--cookies',
            str(cookie_path),
            '--format',
            'bv*+ba/b',
            '--format-sort',
            'res,codec:avc',
            '--merge-output-format',
            'mp4',
            '--remux-video',
            'mp4',
            '--concurrent-fragments',
            '1',
            '--retries',
            '20',
            '--fragment-retries',
            '20',
            '--continue',
            '--socket-timeout',
            '30',
            '--newline',
            '--progress',
            '--progress-template',
            (
                'download:{}%(info.format_id)s:'
                '%(progress.downloaded_bytes)s:'
                '%(progress.total_bytes_estimate)s'
            ).format(self._PROGRESS_PREFIX),
            '--print',
            'after_move:{}%(filepath)s'.format(self._FILE_PREFIX),
            '--output',
            output_template,
        ]
        if write_danmaku:
            command.extend(
                ('--write-subs', '--sub-langs', 'danmaku', '--sub-format', 'xml')
            )
        if source_address:
            command.extend(('--source-address', source_address))
        command.append('https://www.bilibili.com/video/{}?p={}'.format(bvid, int(page)))
        return tuple(command)

    @staticmethod
    def subprocess_environment(
        network_manager: Optional[NetworkRouteManager],
        selection: Optional[RouteSelection],
    ) -> Optional[Dict[str, str]]:
        if (
            network_manager is None
            or selection is None
            or selection.source_address is None
        ):
            return None
        interface = network_manager.interface(selection.interface_name)
        if interface is None:
            return None
        dns_servers = tuple(
            dict.fromkeys(
                value
                for value in (interface.gateway, *interface.dns_servers)
                if value is not None
            )
        )
        if not dns_servers:
            return None
        environment = dict(os.environ)
        site_directory = str(
            Path(__file__).resolve().parents[1] / 'networking' / 'source_bound_site'
        )
        python_path = environment.get('PYTHONPATH')
        environment['PYTHONPATH'] = (
            site_directory
            if not python_path
            else site_directory + os.pathsep + python_path
        )
        environment['BLREC_SOURCE_ADDRESS'] = selection.source_address
        environment['BLREC_DNS_SERVERS'] = ','.join(dns_servers)
        return environment

    def build_danmaku_command(
        self,
        *,
        cookie_path: Path,
        output_template: str,
        bvid: str,
        page: int,
        source_address: Optional[str],
    ) -> Tuple[str, ...]:
        command: List[str] = [
            self._executable,
            '--no-config',
            '--no-playlist',
            '--cookies',
            str(cookie_path),
            '--skip-download',
            '--write-subs',
            '--sub-langs',
            'danmaku',
            '--sub-format',
            'xml',
            '--retries',
            '3',
            '--socket-timeout',
            '30',
            '--output',
            output_template,
        ]
        if source_address:
            command.extend(('--source-address', source_address))
        command.append('https://www.bilibili.com/video/{}?p={}'.format(bvid, int(page)))
        return tuple(command)

    @classmethod
    def write_cookie_file(
        cls, cookies: Tuple[CookieRecord, ...], path: Path, *, now: int
    ) -> None:
        lines = ['# Netscape HTTP Cookie File', '']
        usable = 0
        for cookie in cookies:
            if cookie.expires_at is not None and cookie.expires_at <= now:
                continue
            values = (cookie.domain, cookie.path, cookie.name, cookie.value)
            if any(
                not value or any(character in value for character in '\r\n\t')
                for value in values
            ):
                raise BiliDownloadContractError('账号 Cookie 格式无效')
            domain = cookie.domain
            if cookie.http_only:
                domain = '#HttpOnly_' + domain
            include_subdomains = str(cookie.domain.startswith('.')).upper()
            lines.append(
                '\t'.join(
                    (
                        domain,
                        include_subdomains,
                        cookie.path,
                        str(cookie.secure).upper(),
                        str(cookie.expires_at or 0),
                        cookie.name,
                        cookie.value,
                    )
                )
            )
            usable += 1
        if usable == 0:
            raise BiliDownloadContractError('账号没有可用于下载的有效 Cookie')
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(str(path), flags, 0o600)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf8', newline='\n') as output:
                output.write('\n'.join(lines))
                output.write('\n')
        except BaseException:
            cls._unlink_if_present(path)
            raise

    async def _monitor(
        self,
        process: asyncio.subprocess.Process,
        progress: Callable[[int, Optional[int]], Awaitable[None]],
        *,
        interface_name: Optional[str],
    ) -> Tuple[Optional[str], str]:
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = process.stdout
        stderr = process.stderr
        output_path: Optional[str] = None
        phase: Optional[str] = None
        completed_bytes = 0
        phase_downloaded = 0
        phase_total = 0
        traffic_observed = 0

        async def read_stdout() -> None:
            nonlocal output_path
            nonlocal phase
            nonlocal completed_bytes
            nonlocal phase_downloaded
            nonlocal phase_total
            nonlocal traffic_observed
            while True:
                raw = await stdout.readline()
                if not raw:
                    return
                line = raw.decode('utf8', errors='replace').strip()
                if line.startswith(self._FILE_PREFIX):
                    output_path = line[len(self._FILE_PREFIX) :]
                    continue
                parsed = self.parse_progress(line)
                if parsed is None:
                    continue
                current_phase, downloaded, total = parsed
                if phase is not None and current_phase != phase:
                    completed_bytes += max(phase_downloaded, phase_total)
                    phase_downloaded = 0
                    phase_total = 0
                phase = current_phase
                phase_downloaded = max(phase_downloaded, downloaded)
                phase_total = max(phase_total, total or downloaded)
                observed = completed_bytes + phase_downloaded
                delta = max(0, observed - traffic_observed)
                traffic_observed = max(traffic_observed, observed)
                if delta and self._network_manager is not None:
                    self._network_manager.traffic_meter.record(
                        interface_name, 'archive_download', 'down', delta
                    )
                combined_total = completed_bytes + phase_total
                reserved_total = (
                    None
                    if combined_total <= 0
                    else max(observed + 1, int(math.ceil(float(combined_total) / 0.9)))
                )
                await progress(observed, reserved_total)

        async def read_stderr() -> str:
            kept = bytearray()
            while True:
                chunk = await stderr.read(4096)
                if not chunk:
                    break
                kept.extend(chunk)
                if len(kept) > self._MAX_ERROR_BYTES:
                    del kept[: len(kept) - self._MAX_ERROR_BYTES]
            return bytes(kept).decode('utf8', errors='replace').strip()[:500]

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())
        await process.wait()
        await stdout_task
        error = await stderr_task
        return output_path, error

    @classmethod
    def parse_progress(cls, line: str) -> Optional[Tuple[str, int, Optional[int]]]:
        if not line.startswith(cls._PROGRESS_PREFIX):
            return None
        values = line[len(cls._PROGRESS_PREFIX) :].split(':', 2)
        if len(values) != 3 or not values[0]:
            return None
        try:
            downloaded = max(0, int(values[1]))
        except ValueError:
            return None
        try:
            total: Optional[int] = max(1, int(values[2]))
        except ValueError:
            total = None
        return values[0], downloaded, total

    @staticmethod
    def _validate_source(bvid: str, cid: int, page: int) -> None:
        if (
            not bvid.startswith('BV')
            or not 10 <= len(bvid) <= 20
            or not bvid.isalnum()
            or cid <= 0
            or page <= 0
        ):
            raise BiliDownloadContractError('B 站稿件分 P 信息无效')

    @staticmethod
    def _validated_output(value: Optional[str], prefix: Path) -> Path:
        if not value:
            raise BiliDownloadContractError('yt-dlp 没有返回下载文件')
        candidate = Path(value).resolve()
        parent = prefix.parent.resolve()
        try:
            candidate.relative_to(parent)
        except ValueError:
            raise BiliDownloadContractError('yt-dlp 返回的文件路径越界') from None
        if not candidate.name.startswith(prefix.name + '.'):
            raise BiliDownloadContractError('yt-dlp 返回了意外的文件路径')
        try:
            result = candidate.stat()
        except OSError:
            raise BiliDownloadContractError('yt-dlp 下载文件不存在') from None
        if not stat.S_ISREG(result.st_mode) or result.st_size <= 0:
            raise BiliDownloadContractError('yt-dlp 下载文件无效')
        return candidate

    @staticmethod
    def _validated_danmaku_output(prefix: Path) -> Path:
        candidate = YtDlpMediaDownloader._sidecar_path(prefix, '.danmaku.xml').resolve()
        parent = prefix.parent.resolve()
        try:
            candidate.relative_to(parent)
        except ValueError:
            raise BiliDownloadContractError('yt-dlp 返回的弹幕路径越界') from None
        try:
            result = candidate.stat()
        except OSError:
            raise BiliDownloadContractError('yt-dlp 没有返回历史弹幕文件') from None
        if not stat.S_ISREG(result.st_mode) or result.st_size <= 0:
            raise BiliDownloadContractError('yt-dlp 返回的历史弹幕文件无效')
        return candidate

    @staticmethod
    def _sidecar_path(prefix: Path, suffix: str) -> Path:
        return Path(str(prefix) + suffix)

    @staticmethod
    def _download_prefix(target: Path, cid: int) -> Path:
        return target.parent / '.{}.yt-dlp-{}'.format(target.name, int(cid))

    @staticmethod
    def _cleanup_cookie_sidecars(prefix: Path) -> None:
        for path in prefix.parent.glob(prefix.name + '.cookies-*.txt'):
            YtDlpMediaDownloader._unlink_if_present(path)

    @staticmethod
    def _cleanup_prefix(prefix: Path) -> None:
        for path in prefix.parent.glob(prefix.name + '.*'):
            YtDlpMediaDownloader._unlink_if_present(path)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    def _unlink_if_present(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
