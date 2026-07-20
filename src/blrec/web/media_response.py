from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import BinaryIO, Iterator, Optional, Sequence, Tuple
from urllib.parse import quote

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from blrec.logging.audit import audit

__all__ = (
    'MediaCandidate',
    'MediaResourceUnavailable',
    'OpenedMediaResource',
    'VirtualMediaSnapshot',
    'build_media_response',
    'open_media_resource',
)

_BYTE_RANGE = re.compile(r'bytes=(\d*)-(\d*)')
_CHUNK_SIZE = 64 * 1024


class MediaResourceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaCandidate:
    path: str
    content_type: str
    artifact_key: str
    active: bool = False


@dataclass(frozen=True)
class VirtualMediaSnapshot:
    path: str
    source_size: int
    source_tail_start: int = 0
    prefix: bytes = b''


@dataclass
class OpenedMediaResource:
    file: BinaryIO
    size: int
    content_type: str
    etag: Optional[str]
    cache_control: str
    opened_at: float
    source_offset: int
    source_size: int
    prefix: bytes = b''
    _close_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def close(self) -> None:
        with self._close_lock:
            if not self.file.closed:
                self.file.close()


async def open_media_resource(
    candidates: Sequence[MediaCandidate],
    *,
    expected_root: Optional[str] = None,
    snapshot: Optional[VirtualMediaSnapshot] = None,
) -> OpenedMediaResource:
    values = tuple(candidates)
    if not values:
        raise MediaResourceUnavailable('media resource is unavailable')
    opened_at = time.perf_counter()
    loop = asyncio.get_running_loop()
    future = asyncio.ensure_future(
        loop.run_in_executor(
            None, _open_media_resource_sync, values, expected_root, snapshot, opened_at
        )
    )
    try:
        return await asyncio.shield(future)
    except BaseException:
        future.add_done_callback(_close_cancelled_open)
        raise


def _close_cancelled_open(future: asyncio.Future) -> None:  # type: ignore
    try:
        resource = future.result()
    except BaseException:
        return
    resource.close()


def _open_media_resource_sync(
    candidates: Tuple[MediaCandidate, ...],
    expected_root: Optional[str],
    snapshot: Optional[VirtualMediaSnapshot],
    opened_at: float,
) -> OpenedMediaResource:
    normalized_root = (
        None
        if expected_root is None
        else os.path.normcase(os.path.abspath(expected_root))
    )
    for candidate in candidates:
        file: Optional[BinaryIO] = None
        try:
            candidate_path = os.path.normcase(os.path.abspath(candidate.path))
            real_path = os.path.realpath(candidate.path)
            normalized_path = os.path.normcase(os.path.abspath(real_path))
            if normalized_root is not None and not _is_within_root(
                normalized_path, normalized_root
            ):
                raise MediaResourceUnavailable('media resource is outside its root')
            file = open(real_path, 'rb')
            file_stat = os.fstat(file.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or int(file_stat.st_size) <= 0:
                file.close()
                continue
            if snapshot is not None:
                expected_path = os.path.normcase(os.path.abspath(snapshot.path))
                if candidate_path != expected_path:
                    file.close()
                    raise MediaResourceUnavailable('media snapshot identity changed')
                if (
                    snapshot.source_tail_start < 0
                    or snapshot.source_size < snapshot.source_tail_start
                    or int(file_stat.st_size) < snapshot.source_size
                ):
                    file.close()
                    raise MediaResourceUnavailable('media snapshot identity changed')
                prefix = snapshot.prefix
                source_offset = snapshot.source_tail_start
                source_size = snapshot.source_size - snapshot.source_tail_start
                logical_size = len(prefix) + source_size
                etag = None
                cache_control = 'no-store'
            else:
                prefix = b''
                source_offset = 0
                source_size = int(file_stat.st_size)
                logical_size = source_size
                if candidate.active:
                    etag = None
                    cache_control = 'no-store'
                else:
                    etag = _strong_etag(candidate.artifact_key, file_stat)
                    cache_control = 'private, max-age=3600'
            return OpenedMediaResource(
                file=file,
                size=logical_size,
                content_type=candidate.content_type,
                etag=etag,
                cache_control=cache_control,
                opened_at=opened_at,
                source_offset=source_offset,
                source_size=source_size,
                prefix=prefix,
            )
        except MediaResourceUnavailable:
            if file is not None and not file.closed:
                file.close()
            raise
        except OSError:
            if file is not None and not file.closed:
                file.close()
    raise MediaResourceUnavailable('media resource is unavailable')


def _is_within_root(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _strong_etag(artifact_key: str, file_stat: os.stat_result) -> str:
    identity = '\0'.join(
        (
            artifact_key,
            str(int(file_stat.st_dev)),
            str(int(file_stat.st_ino)),
            str(int(file_stat.st_size)),
            str(int(file_stat.st_mtime_ns)),
        )
    ).encode('utf8')
    return '"{}"'.format(hashlib.sha256(identity).hexdigest())


def build_media_response(
    request: Request,
    resource: OpenedMediaResource,
    range_header: Optional[str],
    if_none_match: Optional[str],
    if_range: Optional[str],
    download_name: Optional[str],
) -> Response:
    route = _normalized_route(request)
    headers = {'Accept-Ranges': 'bytes', 'Cache-Control': resource.cache_control}
    if resource.etag is not None:
        headers['ETag'] = resource.etag
    if download_name:
        headers['Content-Disposition'] = "attachment; filename*=UTF-8''{}".format(
            quote(download_name, safe='')
        )

    if resource.etag is not None and _if_none_match_matches(
        if_none_match, resource.etag
    ):
        resource.close()
        _audit_terminal(route, 304, 'full', 'not_modified')
        return Response(status_code=304, headers=headers)

    use_range = range_header is not None
    if use_range and if_range is not None:
        use_range = if_range.strip() == resource.etag
    start, end = 0, resource.size - 1
    response_status = 200
    range_kind = 'full'
    if use_range:
        try:
            start, end = _parse_byte_range(range_header or '', resource.size)
        except ValueError:
            headers['Content-Range'] = 'bytes */{}'.format(resource.size)
            resource.close()
            _audit_terminal(route, 416, 'invalid', 'range_not_satisfiable')
            return Response(status_code=416, headers=headers)
        response_status = 206
        range_kind = 'partial'
        headers['Content-Range'] = 'bytes {}-{}/{}'.format(start, end, resource.size)

    length = end - start + 1
    headers['Content-Length'] = str(length)
    stream = _MediaStream(
        resource,
        route=route,
        status=response_status,
        start=start,
        length=length,
        range_kind=range_kind,
    )
    return StreamingResponse(
        iter(stream),
        status_code=response_status,
        media_type=resource.content_type,
        headers=headers,
        background=BackgroundTask(stream.finish, 'disconnect'),
    )


def _normalized_route(request: Request) -> str:
    route = request.scope.get('route')
    return str(getattr(route, 'path', None) or '<media>')


def _if_none_match_matches(value: Optional[str], etag: str) -> bool:
    if value is None:
        return False
    for candidate in value.split(','):
        normalized = candidate.strip()
        if normalized == '*':
            return True
        if normalized.startswith('W/'):
            normalized = normalized[2:].strip()
        if normalized == etag:
            return True
    return False


def _parse_byte_range(value: str, size: int) -> Tuple[int, int]:
    if size <= 0 or ',' in value:
        raise ValueError('range not satisfiable')
    match = _BYTE_RANGE.fullmatch(value.strip())
    if match is None:
        raise ValueError('range not satisfiable')
    first, last = match.groups()
    if not first:
        if not last:
            raise ValueError('range not satisfiable')
        suffix = int(last)
        if suffix <= 0:
            raise ValueError('range not satisfiable')
        return max(0, size - suffix), size - 1
    start = int(first)
    end = size - 1 if not last else min(int(last), size - 1)
    if start >= size or end < start:
        raise ValueError('range not satisfiable')
    return start, end


class _MediaStream:
    def __init__(
        self,
        resource: OpenedMediaResource,
        *,
        route: str,
        status: int,
        start: int,
        length: int,
        range_kind: str,
    ) -> None:
        self._resource = resource
        self._route = route
        self._status = status
        self._start = start
        self._length = length
        self._range_kind = range_kind
        self._bytes = 0
        self._first_byte_ms: Optional[float] = None
        self._finished = False
        self._finish_lock = threading.Lock()

    def __iter__(self) -> Iterator[bytes]:
        reason = 'completed'
        try:
            for chunk in self._chunks():
                if not chunk:
                    continue
                if self._first_byte_ms is None:
                    self._first_byte_ms = round(
                        (time.perf_counter() - self._resource.opened_at) * 1_000.0, 3
                    )
                self._bytes += len(chunk)
                yield chunk
        except GeneratorExit:
            reason = 'disconnect'
            raise
        except BaseException:
            reason = 'error'
            raise
        finally:
            self.finish(reason)

    def _chunks(self) -> Iterator[bytes]:
        end = self._start + self._length
        prefix_end = min(end, len(self._resource.prefix))
        if self._start < prefix_end:
            yield self._resource.prefix[self._start : prefix_end]
        logical_source_start = max(self._start, len(self._resource.prefix))
        if logical_source_start >= end:
            return
        source_start = self._resource.source_offset + (
            logical_source_start - len(self._resource.prefix)
        )
        remaining = end - logical_source_start
        self._resource.file.seek(source_start)
        while remaining > 0:
            chunk = self._resource.file.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

    def finish(self, reason: str) -> None:
        with self._finish_lock:
            if self._finished:
                return
            self._finished = True
            try:
                self._resource.close()
            finally:
                audit(
                    'media_stream',
                    route=self._route,
                    status=self._status,
                    first_byte_ms=self._first_byte_ms,
                    bytes=self._bytes,
                    range=self._range_kind,
                    reason=reason,
                )


def _audit_terminal(route: str, status: int, range_kind: str, reason: str) -> None:
    audit(
        'media_stream',
        route=route,
        status=status,
        first_byte_ms=None,
        bytes=0,
        range=range_kind,
        reason=reason,
    )
