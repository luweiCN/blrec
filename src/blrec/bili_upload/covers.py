from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import struct
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Set, Tuple, TypeVar
from urllib.parse import urlsplit

import aiohttp

from .database import BiliUploadDatabase

__all__ = (
    'CoverAssetFile',
    'CoverAssetNotFound',
    'CoverAssetView',
    'CoverLibrary',
    'CoverResolver',
    'CoverResolutionError',
    'CoverWorkCoordinator',
    'CoverWorkSaturated',
    'InvalidCover',
    'StoredCoverUnavailable',
)

T = TypeVar('T')


class InvalidCover(RuntimeError):
    pass


class CoverAssetNotFound(RuntimeError):
    pass


class StoredCoverUnavailable(RuntimeError):
    pass


class CoverResolutionError(RuntimeError):
    pass


class CoverWorkSaturated(RuntimeError):
    def __init__(self, retry_after: int = 1) -> None:
        super().__init__('cover work capacity is exhausted')
        self.retry_after = max(1, int(retry_after))


class CoverWorkCoordinator:
    def __init__(self, *, max_workers: int = 2, max_waiting: int = 8) -> None:
        if max_workers <= 0 or max_waiting < 0:
            raise ValueError('cover work capacity must be non-negative')
        self._max_admitted = max_workers + max_waiting
        self._semaphore = asyncio.Semaphore(max_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix='blrec-cover'
        )
        self._lock = threading.Lock()
        self._jobs: Set[asyncio.Task[Any]] = set()
        self._active = 0
        self._closed = False
        self._executor_closed = False

    @property
    def admitted_count(self) -> int:
        with self._lock:
            return len(self._jobs)

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active

    @property
    def waiting_count(self) -> int:
        with self._lock:
            return max(0, len(self._jobs) - self._active)

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        with self._lock:
            if self._closed:
                raise RuntimeError('cover work coordinator is closed')
            if len(self._jobs) >= self._max_admitted:
                raise CoverWorkSaturated(retry_after=1)
            job = asyncio.create_task(self._execute(operation))
            self._jobs.add(job)
        job.add_done_callback(self._release)
        return await asyncio.shield(job)

    async def offload(self, operation: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, operation)

    def close_admission(self) -> None:
        with self._lock:
            self._closed = True

    async def shutdown(self) -> None:
        self.close_admission()
        with self._lock:
            jobs = tuple(self._jobs)
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        with self._lock:
            if self._executor_closed:
                return
            self._executor_closed = True
        self._executor.shutdown(wait=True)

    async def _execute(self, operation: Callable[[], Awaitable[T]]) -> T:
        async with self._semaphore:
            with self._lock:
                self._active += 1
            try:
                return await operation()
            finally:
                with self._lock:
                    self._active -= 1

    def _release(self, job: asyncio.Task[Any]) -> None:
        with self._lock:
            self._jobs.discard(job)
        if not job.cancelled():
            job.exception()


@dataclass(frozen=True)
class CoverAssetView:
    id: int
    filename: str
    mime_type: str
    width: int
    height: int
    byte_size: int
    created_at: int


@dataclass(frozen=True)
class CoverAssetFile:
    view: CoverAssetView
    path: Path


@dataclass(frozen=True)
class _CoverInspection:
    digest: str
    mime_type: str
    width: int
    height: int
    extension: str


@dataclass
class _DigestWork:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state_lock: threading.Lock = field(default_factory=threading.Lock)
    consumers: int = 0
    created_file: bool = False


class CoverLibrary:
    MAX_BYTES = 2 * 1024 * 1024
    MIN_WIDTH = 1146
    MIN_HEIGHT = 717

    def __init__(
        self,
        database: BiliUploadDatabase,
        root: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._root = Path(os.path.abspath(os.path.expanduser(str(root))))
        self._clock = clock
        self._work = CoverWorkCoordinator(max_workers=2, max_waiting=8)
        self._digest_work: Dict[str, _DigestWork] = {}

    async def add(self, content: bytes, filename: str) -> CoverAssetView:
        return await self._work.run(lambda: self._add_admitted(content, filename))

    def close_admission(self) -> None:
        self._work.close_admission()

    async def shutdown(self) -> None:
        await self._work.shutdown()

    async def _add_admitted(self, content: bytes, filename: str) -> CoverAssetView:
        inspection = await self._work.offload(partial(self._inspect_content, content))
        digest = inspection.digest
        digest_work = self._digest_work.get(digest)
        if digest_work is None:
            digest_work = _DigestWork()
            self._digest_work[digest] = digest_work
        with digest_work.state_lock:
            digest_work.consumers += 1
        try:
            async with digest_work.lock:
                return await self._add_by_digest(
                    content, filename, inspection, digest_work
                )
        finally:
            with digest_work.state_lock:
                digest_work.consumers -= 1
                unused = digest_work.consumers == 0
            if unused:
                self._digest_work.pop(digest, None)

    async def _add_by_digest(
        self,
        content: bytes,
        filename: str,
        inspection: _CoverInspection,
        digest_work: _DigestWork,
    ) -> CoverAssetView:
        digest = inspection.digest
        existing = await self._find_record_by_digest(digest)
        if existing is not None:
            await self._work.offload(
                partial(
                    self._verify_recorded_file, existing, digest, inspection.extension
                )
            )
            return self._view(existing)

        safe_filename = self._safe_filename(filename, inspection.extension)
        path = self._content_path(digest, inspection.extension)
        created = await self._work.offload(
            partial(self._store_file, path, content, digest)
        )
        if created:
            with digest_work.state_lock:
                digest_work.created_file = True
        now = int(self._clock())
        try:
            await self._database.execute(
                'INSERT INTO cover_assets('
                'sha256,storage_path,filename,mime_type,width,height,byte_size,'
                'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (
                    digest,
                    str(path),
                    safe_filename,
                    inspection.mime_type,
                    inspection.width,
                    inspection.height,
                    len(content),
                    now,
                    now,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._cleanup_failed_insert(path, digest, digest_work)
            raise
        stored = await self._find_record_by_digest(digest)
        if stored is None:
            await self._cleanup_failed_insert(path, digest, digest_work)
            raise StoredCoverUnavailable('stored cover metadata is unavailable')
        with digest_work.state_lock:
            digest_work.created_file = False
        return self._view(stored)

    async def _cleanup_failed_insert(
        self, path: Path, digest: str, digest_work: _DigestWork
    ) -> None:
        with digest_work.state_lock:
            if not digest_work.created_file or digest_work.consumers > 1:
                return
        try:
            referenced = await self._database.scalar(
                'SELECT 1 FROM cover_assets WHERE sha256=? OR storage_path=?',
                (digest, str(path)),
            )
        except Exception:
            return
        if referenced is not None:
            with digest_work.state_lock:
                digest_work.created_file = False
            return
        try:
            await self._work.offload(
                partial(self._cleanup_file_if_unshared, path, digest, digest_work)
            )
        except (InvalidCover, OSError, StoredCoverUnavailable):
            return

    @classmethod
    def _inspect_content(cls, content: bytes) -> _CoverInspection:
        if not isinstance(content, bytes) or not content:
            raise InvalidCover('cover must be a JPEG or PNG image')
        if len(content) > cls.MAX_BYTES:
            raise InvalidCover('cover must not exceed 2 MiB')
        mime_type, width, height, extension = cls._image_info(content)
        if width < cls.MIN_WIDTH or height < cls.MIN_HEIGHT:
            raise InvalidCover('cover must be at least 1146 × 717 pixels')
        digest = hashlib.sha256(content).hexdigest()
        return _CoverInspection(digest, mime_type, width, height, extension)

    async def list(self) -> Tuple[CoverAssetView, ...]:
        rows = await self._database.fetchall(
            'SELECT id,filename,mime_type,width,height,byte_size,created_at '
            'FROM cover_assets ORDER BY created_at DESC,id DESC'
        )
        return tuple(self._view(row) for row in rows)

    async def open(self, asset_id: int) -> CoverAssetFile:
        row = await self._database.fetchone(
            'SELECT id,storage_path,filename,mime_type,width,height,byte_size,'
            'created_at FROM cover_assets WHERE id=?',
            (asset_id,),
        )
        if row is None:
            raise CoverAssetNotFound('cover asset was not found')
        path = Path(str(row['storage_path'])).resolve()
        root = self._root.resolve()
        try:
            inside_root = os.path.commonpath((str(root), str(path))) == str(root)
        except ValueError:
            inside_root = False
        if not inside_root:
            raise StoredCoverUnavailable('stored cover path is outside the cover root')
        if not path.is_file():
            raise StoredCoverUnavailable('stored cover file is unavailable')
        return CoverAssetFile(view=self._view(row), path=path)

    async def read(self, asset_id: int) -> Tuple[CoverAssetView, bytes]:
        opened = await self.open(asset_id)
        loop = asyncio.get_running_loop()
        content = await loop.run_in_executor(None, opened.path.read_bytes)
        if len(content) != opened.view.byte_size:
            raise StoredCoverUnavailable('stored cover file size has changed')
        return opened.view, content

    async def _find_record_by_digest(self, digest: str) -> Optional[Any]:
        return await self._database.fetchone(
            'SELECT id,sha256,storage_path,filename,mime_type,width,height,'
            'byte_size,created_at '
            'FROM cover_assets WHERE sha256=?',
            (digest,),
        )

    def _content_path(self, digest: str, extension: str) -> Path:
        path = self._root / '{}.{}'.format(digest, extension)
        if path.parent != self._root:
            raise StoredCoverUnavailable('stored cover path is outside the cover root')
        return path

    def _verify_recorded_file(self, row: Any, digest: str, extension: str) -> None:
        expected = self._content_path(digest, extension)
        recorded = Path(os.path.abspath(os.path.expanduser(str(row['storage_path']))))
        if recorded != expected:
            raise StoredCoverUnavailable('stored cover path is outside the cover root')
        self._verify_file(expected, digest)

    def _store_file(self, path: Path, content: bytes, digest: str) -> bool:
        self._root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self._root, 0o700)
        try:
            os.lstat(str(path))
        except FileNotFoundError:
            pass
        else:
            self._verify_file(path, digest)
            return False

        descriptor, temporary_name = tempfile.mkstemp(
            prefix='.cover-', suffix='.tmp', dir=str(self._root)
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                descriptor = -1
                os.fchmod(output.fileno(), 0o600)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(str(temporary_path), str(path))
            except FileExistsError:
                self._verify_file(path, digest)
                return False
            self._fsync_directory(self._root)
            return True
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

    @classmethod
    def _cleanup_file_if_unshared(
        cls, path: Path, digest: str, digest_work: _DigestWork
    ) -> None:
        cls._verify_file(path, digest)
        with digest_work.state_lock:
            if not digest_work.created_file or digest_work.consumers > 1:
                return
            path.unlink(missing_ok=True)
            digest_work.created_file = False

    @classmethod
    def _cleanup_file(cls, path: Path, digest: str) -> None:
        try:
            cls._verify_file(path, digest)
        except FileNotFoundError:
            return
        path.unlink(missing_ok=True)

    @staticmethod
    def _verify_file(path: Path, digest: str) -> None:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(str(path), flags)
        except FileNotFoundError:
            raise StoredCoverUnavailable('stored cover file is unavailable') from None
        except OSError:
            raise StoredCoverUnavailable('stored cover file is unavailable') from None
        with os.fdopen(descriptor, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise StoredCoverUnavailable('stored cover file is unavailable')
            hasher = hashlib.sha256()
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
            if hasher.hexdigest() != digest:
                raise InvalidCover('stored cover file does not match its hash')
            os.fchmod(source.fileno(), 0o600)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        descriptor = os.open(str(path), flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _safe_filename(filename: str, extension: str) -> str:
        name = str(filename).replace('\\', '/').split('/')[-1]
        name = ''.join(character for character in name if 32 <= ord(character) != 127)
        name = name.strip()[:255]
        return name or 'cover.{}'.format(extension)

    @classmethod
    def _image_info(cls, content: bytes) -> Tuple[str, int, int, str]:
        if content.startswith(b'\x89PNG\r\n\x1a\n'):
            if len(content) < 29 or content[12:16] != b'IHDR':
                raise InvalidCover('cover must be a valid JPEG or PNG image')
            width, height = struct.unpack('>II', content[16:24])
            return 'image/png', width, height, 'png'
        if content.startswith(b'\xff\xd8'):
            dimensions = cls._jpeg_dimensions(content)
            if dimensions is None:
                raise InvalidCover('cover must be a valid JPEG or PNG image')
            width, height = dimensions
            return 'image/jpeg', width, height, 'jpg'
        raise InvalidCover('cover must be a JPEG or PNG image')

    @staticmethod
    def _jpeg_dimensions(content: bytes) -> Optional[Tuple[int, int]]:
        offset = 2
        start_of_frame = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        while offset + 3 < len(content):
            if content[offset] != 0xFF:
                offset += 1
                continue
            while offset < len(content) and content[offset] == 0xFF:
                offset += 1
            if offset >= len(content):
                return None
            marker = content[offset]
            offset += 1
            if marker in (0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(content):
                return None
            length = struct.unpack('>H', content[offset : offset + 2])[0]
            if length < 2 or offset + length > len(content):
                return None
            if marker in start_of_frame:
                if length < 7:
                    return None
                height, width = struct.unpack('>HH', content[offset + 3 : offset + 7])
                return width, height
            offset += length
        return None

    @staticmethod
    def _view(row: Any) -> CoverAssetView:
        return CoverAssetView(
            id=int(row['id']),
            filename=str(row['filename']),
            mime_type=str(row['mime_type']),
            width=int(row['width']),
            height=int(row['height']),
            byte_size=int(row['byte_size']),
            created_at=int(row['created_at']),
        )


class CoverResolver:
    def __init__(
        self,
        database: BiliUploadDatabase,
        library: CoverLibrary,
        protocol: Any,
        *,
        bundle_loader: Callable[[int], Awaitable[Any]],
        clock: Callable[[], float] = time.time,
        remote_loader: Optional[Callable[[str], Awaitable[bytes]]] = None,
    ) -> None:
        self._database = database
        self._library = library
        self._protocol = protocol
        self._bundle_loader = bundle_loader
        self._clock = clock
        self._remote_loader = remote_loader or self._download
        self._locks: Dict[Tuple[int, int], asyncio.Lock] = {}

    async def remote_url(self, asset_id: int, account_id: int) -> str:
        cached = await self._cached(asset_id, account_id)
        if cached is not None:
            return cached
        lock = self._locks.setdefault((asset_id, account_id), asyncio.Lock())
        async with lock:
            cached = await self._cached(asset_id, account_id)
            if cached is not None:
                return cached
            view, content = await self._library.read(asset_id)
            bundle = await self._bundle_loader(account_id)
            remote_url = await self._protocol.upload_cover(
                bundle,
                filename=view.filename,
                mime_type=view.mime_type,
                content=content,
            )
            now = int(self._clock())
            await self._database.execute(
                'INSERT INTO cover_asset_uploads('
                'asset_id,account_id,remote_url,created_at,updated_at) '
                'VALUES(?,?,?,?,?) ON CONFLICT(asset_id,account_id) DO UPDATE SET '
                'remote_url=excluded.remote_url,updated_at=excluded.updated_at',
                (asset_id, account_id, remote_url, now, now),
            )
            return remote_url

    async def upload_transient(
        self, account_id: int, *, filename: str, mime_type: str, content: bytes
    ) -> str:
        bundle = await self._bundle_loader(account_id)
        return await self._protocol.upload_cover(
            bundle, filename=filename, mime_type=mime_type, content=content
        )

    async def live_url(
        self, account_id: int, *, local_path: Optional[str], source_url: str
    ) -> str:
        content: Optional[bytes] = None
        filename = 'live-cover.jpg'
        if local_path:
            path = Path(local_path)
            if path.is_file():
                loop = asyncio.get_running_loop()
                content = await loop.run_in_executor(None, self._read_limited, path)
                filename = path.name or filename
        if content is None:
            self._validate_live_cover_url(source_url)
            try:
                content = await self._remote_loader(source_url)
            except CoverResolutionError:
                raise
            except Exception:
                raise CoverResolutionError('live cover download failed') from None
        if not content or len(content) > CoverLibrary.MAX_BYTES:
            raise CoverResolutionError('live cover exceeds the supported size')
        try:
            mime_type, _width, _height, extension = CoverLibrary._image_info(content)
        except InvalidCover:
            raise CoverResolutionError(
                'live cover is not a JPEG or PNG image'
            ) from None
        if filename == 'live-cover.jpg':
            filename = 'live-cover.{}'.format(extension)
        return await self.upload_transient(
            account_id, filename=filename, mime_type=mime_type, content=content
        )

    async def _cached(self, asset_id: int, account_id: int) -> Optional[str]:
        value = await self._database.scalar(
            'SELECT remote_url FROM cover_asset_uploads '
            'WHERE asset_id=? AND account_id=?',
            (asset_id, account_id),
        )
        return None if value is None else str(value)

    @staticmethod
    def _read_limited(path: Path) -> bytes:
        if path.stat().st_size > CoverLibrary.MAX_BYTES:
            raise CoverResolutionError('live cover exceeds the supported size')
        return path.read_bytes()

    @staticmethod
    def _validate_live_cover_url(url: str) -> None:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if (
            parsed.scheme != 'https'
            or parsed.username is not None
            or parsed.password is not None
            or not any(
                host == suffix or host.endswith('.' + suffix)
                for suffix in ('hdslb.com', 'biliimg.com')
            )
        ):
            raise CoverResolutionError('live cover URL is not trusted')

    @staticmethod
    async def _download(url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise CoverResolutionError('live cover download failed')
                content = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    content.extend(chunk)
                    if len(content) > CoverLibrary.MAX_BYTES:
                        raise CoverResolutionError(
                            'live cover exceeds the supported size'
                        )
                return bytes(content)
