from __future__ import annotations

import asyncio
import ctypes
import hashlib
import ntpath
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Iterator, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

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
_IS_WINDOWS = os.name == 'nt'
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x10
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


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
    source_device: int
    source_inode: int
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
    for candidate in candidates:
        file: Optional[BinaryIO] = None
        try:
            candidate_path = os.path.normcase(os.path.abspath(candidate.path))
            file = _open_candidate_file(candidate.path, expected_root)
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
                    int(file_stat.st_dev) != snapshot.source_device
                    or int(file_stat.st_ino) != snapshot.source_inode
                    or snapshot.source_device < 0
                    or snapshot.source_inode < 0
                    or snapshot.source_tail_start < 0
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


def _open_candidate_file(path: str, expected_root: Optional[str]) -> BinaryIO:
    read_flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    if expected_root is None:
        resolved = os.path.realpath(path)
        descriptor = os.open(resolved, read_flags | nofollow)
        return _fdopen(descriptor)
    if _IS_WINDOWS:
        return _open_windows_candidate_file(path, expected_root)

    resolved_root = os.path.realpath(expected_root)
    root_descriptor = os.open(
        resolved_root, read_flags | getattr(os, 'O_DIRECTORY', 0) | nofollow
    )
    current_descriptor = root_descriptor
    try:
        root_stat = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise MediaResourceUnavailable('media root is unavailable')
        resolved_path = os.path.realpath(path)
        normalized_root = os.path.normcase(os.path.abspath(resolved_root))
        normalized_path = os.path.normcase(os.path.abspath(resolved_path))
        if not _is_within_root(normalized_path, normalized_root):
            raise MediaResourceUnavailable('media resource is outside its root')
        relative = os.path.relpath(resolved_path, resolved_root)
        components = tuple(part for part in relative.split(os.sep) if part)
        if not components or any(part in ('.', '..') for part in components):
            raise MediaResourceUnavailable('media resource is unavailable')
        directory_flags = read_flags | getattr(os, 'O_DIRECTORY', 0) | nofollow
        for component in components[:-1]:
            next_descriptor = os.open(
                component, directory_flags, dir_fd=current_descriptor
            )
            if current_descriptor != root_descriptor:
                os.close(current_descriptor)
            current_descriptor = next_descriptor
        descriptor = os.open(
            components[-1], read_flags | nofollow, dir_fd=current_descriptor
        )
        return _fdopen(descriptor)
    finally:
        if current_descriptor != root_descriptor:
            os.close(current_descriptor)
        os.close(root_descriptor)


def _open_windows_candidate_file(
    path: str, expected_root: str, *, api: Optional[Any] = None
) -> BinaryIO:
    native = _WindowsNativeFileApi() if api is None else api
    root_path = _normalize_windows_path(expected_root)
    candidate_path = _normalize_windows_path(path)
    if not _is_within_windows_root(candidate_path, root_path):
        raise MediaResourceUnavailable('media resource is outside its root')
    relative = ntpath.relpath(candidate_path, root_path)
    components = tuple(
        component for component in relative.replace('/', '\\').split('\\') if component
    )
    if not components or any(
        component in ('.', '..') or ':' in component for component in components
    ):
        raise MediaResourceUnavailable('media resource is unavailable')

    directory_handles = []
    file_handle: Optional[int] = None
    opened_file: Optional[BinaryIO] = None
    failure: Optional[BaseException] = None
    try:
        root_handle = native.open_root(root_path)
        directory_handles.append(root_handle)
        _validate_windows_directory(native, root_handle)
        canonical_root = _normalize_windows_path(native.final_path(root_handle))

        current_handle = root_handle
        for component in components[:-1]:
            directory_handle = native.open_directory(current_handle, component)
            directory_handles.append(directory_handle)
            _validate_windows_directory(native, directory_handle)
            current_handle = directory_handle

        file_handle = native.open_file(current_handle, components[-1])
        attributes = int(native.attributes(file_handle))
        if attributes & (
            _WINDOWS_FILE_ATTRIBUTE_DIRECTORY | _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise MediaResourceUnavailable('media resource is unavailable')
        canonical_file = _normalize_windows_path(native.final_path(file_handle))
        if not _is_within_windows_root(canonical_file, canonical_root):
            raise MediaResourceUnavailable('media resource is outside its root')
        adopted_handle, file_handle = file_handle, None
        opened_file = native.adopt_file(adopted_handle)
    except BaseException as error:
        failure = error

    close_error: Optional[BaseException] = None
    handles = (() if file_handle is None else (file_handle,)) + tuple(
        reversed(directory_handles)
    )
    for handle in handles:
        try:
            native.close_handle(handle)
        except BaseException as error:
            if close_error is None:
                close_error = error
    if failure is not None:
        raise failure
    if close_error is not None:
        if opened_file is not None:
            opened_file.close()
        raise close_error
    if opened_file is None:
        raise MediaResourceUnavailable('media resource is unavailable')
    return opened_file


def _validate_windows_directory(api: Any, handle: int) -> None:
    attributes = int(api.attributes(handle))
    if not attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY or attributes & (
        _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise MediaResourceUnavailable('media root is unavailable')


def _normalize_windows_path(path: str) -> str:
    value = path
    upper = value.upper()
    if upper.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif upper.startswith('\\\\?\\'):
        value = value[4:]
    return ntpath.normpath(ntpath.abspath(value))


def _is_within_windows_root(path: str, root: str) -> bool:
    normalized_path = ntpath.normcase(path)
    normalized_root = ntpath.normcase(root)
    try:
        return ntpath.commonpath((normalized_path, normalized_root)) == normalized_root
    except ValueError:
        return False


class _WindowsNativeFileApi:
    _FILE_READ_ATTRIBUTES = 0x80
    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _FILE_SHARE_DELETE = 0x4
    _SHARE_ACCESS = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
    _OPEN_EXISTING = 3
    _FILE_OPEN = 1
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_DIRECTORY_FILE = 0x1
    _FILE_SEQUENTIAL_ONLY = 0x4
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x20
    _FILE_NON_DIRECTORY_FILE = 0x40
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _FILE_TRAVERSE = 0x20
    _SYNCHRONIZE = 0x00100000
    _OBJ_CASE_INSENSITIVE = 0x40
    _FILE_ATTRIBUTE_TAG_INFO = 9

    def __init__(self) -> None:
        import msvcrt
        from ctypes import wintypes

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = (
                ('file_attributes', wintypes.DWORD),
                ('reparse_tag', wintypes.DWORD),
            )

        class UnicodeString(ctypes.Structure):
            _fields_ = (
                ('length', wintypes.USHORT),
                ('maximum_length', wintypes.USHORT),
                ('buffer', wintypes.LPWSTR),
            )

        class ObjectAttributes(ctypes.Structure):
            _fields_ = (
                ('length', wintypes.ULONG),
                ('root_directory', wintypes.HANDLE),
                ('object_name', ctypes.POINTER(UnicodeString)),
                ('attributes', wintypes.ULONG),
                ('security_descriptor', wintypes.LPVOID),
                ('security_quality_of_service', wintypes.LPVOID),
            )

        class IoStatusBlock(ctypes.Structure):
            _fields_ = (
                ('status_or_pointer', ctypes.c_void_p),
                ('information', ctypes.c_size_t),
            )

        self._open_osfhandle = getattr(msvcrt, 'open_osfhandle')
        self._wintypes = wintypes
        self._get_last_error = getattr(ctypes, 'get_last_error')
        self._win_error = getattr(ctypes, 'WinError')
        self._attribute_info_type = FileAttributeTagInfo
        self._unicode_string_type = UnicodeString
        self._object_attributes_type = ObjectAttributes
        self._io_status_block_type = IoStatusBlock
        kernel32 = getattr(ctypes, 'WinDLL')('kernel32', use_last_error=True)
        self._create_file = kernel32.CreateFileW
        self._create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        self._create_file.restype = wintypes.HANDLE
        self._get_attributes = kernel32.GetFileInformationByHandleEx
        self._get_attributes.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        self._get_attributes.restype = wintypes.BOOL
        self._get_final_path = kernel32.GetFinalPathNameByHandleW
        self._get_final_path.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        self._get_final_path.restype = wintypes.DWORD
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = (wintypes.HANDLE,)
        self._close_handle.restype = wintypes.BOOL
        ntdll = getattr(ctypes, 'WinDLL')('ntdll')
        self._nt_create_file = ntdll.NtCreateFile
        self._nt_create_file.argtypes = (
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(ObjectAttributes),
            ctypes.POINTER(IoStatusBlock),
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        self._nt_create_file.restype = ctypes.c_long
        self._nt_status_to_dos_error = ntdll.RtlNtStatusToDosError
        self._nt_status_to_dos_error.argtypes = (ctypes.c_long,)
        self._nt_status_to_dos_error.restype = wintypes.ULONG
        self._invalid_handle = ctypes.c_void_p(-1).value

    def open_root(self, path: str) -> int:
        return self._open_absolute(
            path,
            self._FILE_READ_ATTRIBUTES | self._FILE_TRAVERSE,
            self._FILE_FLAG_BACKUP_SEMANTICS | self._FILE_FLAG_OPEN_REPARSE_POINT,
        )

    def open_directory(self, parent_handle: int, component: str) -> int:
        return self._open_relative(
            parent_handle,
            component,
            self._FILE_READ_ATTRIBUTES | self._FILE_TRAVERSE | self._SYNCHRONIZE,
            self._FILE_DIRECTORY_FILE
            | self._FILE_SYNCHRONOUS_IO_NONALERT
            | self._FILE_OPEN_REPARSE_POINT,
        )

    def open_file(self, parent_handle: int, component: str) -> int:
        return self._open_relative(
            parent_handle,
            component,
            self._GENERIC_READ | self._SYNCHRONIZE,
            self._FILE_NON_DIRECTORY_FILE
            | self._FILE_SEQUENTIAL_ONLY
            | self._FILE_SYNCHRONOUS_IO_NONALERT
            | self._FILE_OPEN_REPARSE_POINT,
        )

    def _open_absolute(self, path: str, access: int, flags: int) -> int:
        handle = self._create_file(
            path, access, self._SHARE_ACCESS, None, self._OPEN_EXISTING, flags, None
        )
        if handle in (None, self._invalid_handle):
            raise self._win_error(self._get_last_error())
        return int(handle)

    def _open_relative(
        self, parent_handle: int, component: str, access: int, options: int
    ) -> int:
        name_buffer = ctypes.create_unicode_buffer(component)
        encoded_length = len(component.encode('utf-16-le'))
        name = self._unicode_string_type(
            encoded_length,
            encoded_length + 2,
            ctypes.cast(name_buffer, self._wintypes.LPWSTR),
        )
        object_attributes = self._object_attributes_type(
            ctypes.sizeof(self._object_attributes_type),
            self._wintypes.HANDLE(parent_handle),
            ctypes.pointer(name),
            self._OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        status_block = self._io_status_block_type()
        handle = self._wintypes.HANDLE()
        status = int(
            self._nt_create_file(
                ctypes.byref(handle),
                access,
                ctypes.byref(object_attributes),
                ctypes.byref(status_block),
                None,
                0,
                self._SHARE_ACCESS,
                self._FILE_OPEN,
                options,
                None,
                0,
            )
        )
        if status < 0:
            error_code = int(self._nt_status_to_dos_error(status))
            raise self._win_error(error_code)
        handle_value = handle.value
        if handle_value is None or handle_value == self._invalid_handle:
            raise self._win_error(self._get_last_error())
        return int(handle_value)

    def attributes(self, handle: int) -> int:
        info = self._attribute_info_type()
        succeeded = self._get_attributes(
            self._wintypes.HANDLE(handle),
            self._FILE_ATTRIBUTE_TAG_INFO,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not succeeded:
            raise self._win_error(self._get_last_error())
        return int(info.file_attributes)

    def final_path(self, handle: int) -> str:
        size = 32_768
        buffer = ctypes.create_unicode_buffer(size)
        length = int(
            self._get_final_path(self._wintypes.HANDLE(handle), buffer, size, 0)
        )
        if length == 0:
            raise self._win_error(self._get_last_error())
        if length >= size:
            size = length + 1
            buffer = ctypes.create_unicode_buffer(size)
            length = int(
                self._get_final_path(self._wintypes.HANDLE(handle), buffer, size, 0)
            )
            if length == 0 or length >= size:
                raise self._win_error(self._get_last_error())
        return str(buffer.value)

    def adopt_file(self, handle: int) -> BinaryIO:
        try:
            descriptor = self._open_osfhandle(
                handle, os.O_RDONLY | getattr(os, 'O_BINARY', 0)
            )
        except BaseException:
            self.close_handle(handle)
            raise
        try:
            os.set_inheritable(descriptor, False)
        except BaseException:
            os.close(descriptor)
            raise
        return _fdopen(descriptor)

    def close_handle(self, handle: int) -> None:
        if not self._close_handle(self._wintypes.HANDLE(handle)):
            raise self._win_error(self._get_last_error())


def _fdopen(descriptor: int) -> BinaryIO:
    try:
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


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
    try:
        return _MediaStreamingResponse(
            iter(stream),
            media_stream=stream,
            status_code=response_status,
            media_type=resource.content_type,
            headers=headers,
            background=BackgroundTask(stream.finish_if_incomplete),
        )
    except BaseException:
        stream.finish('error')
        raise


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
        self._iteration_completed = False
        self._finish_lock = threading.Lock()

    def __iter__(self) -> Iterator[bytes]:
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
            self.finish('disconnect')
            raise
        except BaseException:
            self.finish('error')
            raise
        else:
            with self._finish_lock:
                self._iteration_completed = True

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

    def finish_if_incomplete(self) -> None:
        with self._finish_lock:
            completed = self._iteration_completed
        if not completed:
            self.finish('disconnect')

    def finish_after_response(self) -> None:
        with self._finish_lock:
            completed = self._iteration_completed
        self.finish('completed' if completed else 'disconnect')


class _MediaStreamingResponse(StreamingResponse):
    def __init__(
        self,
        content: Iterator[bytes],
        *,
        media_stream: _MediaStream,
        status_code: int,
        media_type: str,
        headers: Mapping[str, str],
        background: BackgroundTask,
    ) -> None:
        self._media_stream = media_stream
        super().__init__(
            content,
            status_code=status_code,
            media_type=media_type,
            headers=headers,
            background=background,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            self._media_stream.finish('disconnect')
            raise
        except BaseException:
            self._media_stream.finish('error')
            raise
        else:
            self._media_stream.finish_after_response()
        finally:
            self._media_stream.finish('disconnect')


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
