import asyncio
import ntpath
import os
import re
import threading
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from blrec.web import media_response
from blrec.web.media_response import (
    MediaCandidate,
    VirtualMediaSnapshot,
    build_media_response,
    open_media_resource,
)


class FakeWindowsOpenApi:
    def __init__(
        self,
        *,
        attributes: Optional[Dict[str, int]] = None,
        final_paths: Optional[Dict[str, str]] = None,
        close_failures: Optional[Tuple[str, ...]] = None,
    ) -> None:
        self._attributes = {
            self._key(path): value for path, value in (attributes or {}).items()
        }
        self._final_paths = {
            self._key(path): value for path, value in (final_paths or {}).items()
        }
        self._next_handle = 1
        self._paths: Dict[int, str] = {}
        self._kinds: Dict[int, str] = {}
        self._active: Dict[int, str] = {}
        self._close_failures = {self._key(path) for path in (close_failures or ())}
        self.open_calls: List[Tuple[str, str, Tuple[str, ...]]] = []
        self.closed_paths: List[str] = []
        self.close_attempts: List[str] = []

    @staticmethod
    def _key(path: str) -> str:
        return ntpath.normcase(ntpath.normpath(path))

    def _open(self, kind: str, path: str) -> int:
        normalized = self._key(path)
        self.open_calls.append((kind, normalized, tuple(self._active.values())))
        handle = self._next_handle
        self._next_handle += 1
        self._paths[handle] = normalized
        self._kinds[handle] = kind
        self._active[handle] = normalized
        return handle

    def open_root(self, path: str) -> int:
        return self._open('directory', path)

    def open_directory(self, parent_handle: int, component: str) -> int:
        return self._open(
            'directory', ntpath.join(self._paths[parent_handle], component)
        )

    def open_file(self, parent_handle: int, component: str) -> int:
        return self._open('file', ntpath.join(self._paths[parent_handle], component))

    def attributes(self, handle: int) -> int:
        path = self._paths[handle]
        default = 0x10 if self._kinds[handle] == 'directory' else 0
        return self._attributes.get(path, default)

    def final_path(self, handle: int) -> str:
        path = self._paths[handle]
        return self._final_paths.get(path, path)

    def adopt_file(self, handle: int):
        del self._active[handle]
        return BytesIO(b'windows-media')

    def close_handle(self, handle: int) -> None:
        path = self._active[handle]
        self.close_attempts.append(path)
        if path in self._close_failures:
            raise OSError('simulated CloseHandle failure')
        del self._active[handle]
        self.closed_paths.append(path)

    @property
    def active_paths(self) -> Tuple[str, ...]:
        return tuple(self._active.values())


@contextmanager
def _media_client(
    path: Path, *, active: bool = False, download_name: str = ''
) -> Iterator[TestClient]:
    app = FastAPI()

    @app.get('/media/{media_id}')
    async def stream(request: Request, media_id: int):
        resource = await open_media_resource(
            (
                MediaCandidate(
                    path=str(path),
                    content_type='video/mp4',
                    artifact_key='test-media:{}'.format(media_id),
                    active=active,
                ),
            )
        )
        return build_media_response(
            request,
            resource,
            request.headers.get('range'),
            request.headers.get('if-none-match'),
            request.headers.get('if-range'),
            download_name or None,
        )

    with TestClient(app) as client:
        yield client


@pytest.fixture
def media_file(tmp_path: Path) -> Path:
    path = tmp_path / 'private-recording-identity.mp4'
    path.write_bytes(b'0123456789')
    return path


@pytest.mark.asyncio
async def test_open_media_resource_runs_realpath_open_and_fstat_once_in_worker(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_loop_thread = threading.get_ident()
    calls: List[Tuple[str, int]] = []
    real_realpath = media_response.os.path.realpath
    real_open = media_response.os.open
    real_fstat = media_response.os.fstat

    def recording_realpath(value: str) -> str:
        calls.append(('realpath', threading.get_ident()))
        return real_realpath(value)

    def recording_open(value: str, flags: int, *args: Any, **kwargs: Any):
        calls.append(('os.open', threading.get_ident()))
        return real_open(value, flags, *args, **kwargs)

    def recording_fstat(fd: int):
        calls.append(('fstat', threading.get_ident()))
        return real_fstat(fd)

    def forbidden_stat(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('path stat must not run')

    monkeypatch.setattr(media_response.os.path, 'realpath', recording_realpath)
    monkeypatch.setattr(media_response.os, 'open', recording_open)
    monkeypatch.setattr(media_response.os, 'fstat', recording_fstat)
    monkeypatch.setattr(media_response.os, 'stat', forbidden_stat)
    monkeypatch.setattr(Path, 'stat', forbidden_stat)

    resource = await open_media_resource(
        (
            MediaCandidate(
                path=str(media_file),
                content_type='video/mp4',
                artifact_key='recording-part:7:final',
            ),
        )
    )
    try:
        assert resource.size == 10
        assert [name for name, _thread in calls] == ['realpath', 'os.open', 'fstat']
        assert all(thread != event_loop_thread for _name, thread in calls)
    finally:
        resource.close()


@pytest.mark.asyncio
async def test_cancelled_open_closes_a_resource_finished_by_the_worker(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    opened = []
    real_open = media_response._open_candidate_file

    def blocking_open(value: str, expected_root: Any):
        entered.set()
        release.wait()
        file = real_open(value, expected_root)
        opened.append(file)
        returned.set()
        return file

    monkeypatch.setattr(media_response, '_open_candidate_file', blocking_open)
    task = asyncio.create_task(
        open_media_resource(
            (
                MediaCandidate(
                    path=str(media_file),
                    content_type='video/mp4',
                    artifact_key='recording-part:7:final',
                ),
            )
        )
    )
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, entered.wait)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await loop.run_in_executor(None, returned.wait)
    for _attempt in range(100):
        if opened[0].closed:
            break
        await asyncio.sleep(0)

    assert opened[0].closed is True


@pytest.mark.asyncio
async def test_active_snapshot_accepts_the_same_symlink_candidate(
    media_file: Path, tmp_path: Path
) -> None:
    symlink = tmp_path / 'recording-link.mp4'
    symlink.symlink_to(media_file)
    identity = os.stat(media_file)

    resource = await open_media_resource(
        (
            MediaCandidate(
                path=str(symlink),
                content_type='video/mp4',
                artifact_key='recording-part:7:source',
                active=True,
            ),
        ),
        snapshot=VirtualMediaSnapshot(
            path=str(symlink),
            source_size=10,
            source_device=int(identity.st_dev),
            source_inode=int(identity.st_ino),
        ),
    )
    try:
        assert resource.size == 10
        assert resource.etag is None
        assert resource.cache_control == 'no-store'
    finally:
        resource.close()


@pytest.mark.asyncio
async def test_root_check_cannot_be_bypassed_by_final_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'recordings'
    root.mkdir()
    candidate = root / 'part.mp4'
    candidate.write_bytes(b'inside')
    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'outside-secret')
    real_realpath = media_response.os.path.realpath
    swapped = False

    def swap_after_resolution(value: str) -> str:
        nonlocal swapped
        resolved = real_realpath(value)
        if os.path.abspath(value) == os.path.abspath(candidate) and not swapped:
            candidate.unlink()
            candidate.symlink_to(outside)
            swapped = True
        return resolved

    monkeypatch.setattr(media_response.os.path, 'realpath', swap_after_resolution)

    with pytest.raises(media_response.MediaResourceUnavailable):
        await open_media_resource(
            (
                MediaCandidate(
                    path=str(candidate),
                    content_type='video/mp4',
                    artifact_key='recording-part:7:final',
                ),
            ),
            expected_root=str(root),
        )


@pytest.mark.asyncio
async def test_root_check_cannot_be_bypassed_by_parent_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'recordings'
    parent = root / 'room'
    parent.mkdir(parents=True)
    candidate = parent / 'part.mp4'
    candidate.write_bytes(b'inside')
    outside_parent = tmp_path / 'outside-room'
    outside_parent.mkdir()
    (outside_parent / 'part.mp4').write_bytes(b'outside-secret')
    moved_parent = root / 'room-original'
    real_realpath = media_response.os.path.realpath
    swapped = False

    def swap_parent_after_resolution(value: str) -> str:
        nonlocal swapped
        resolved = real_realpath(value)
        if os.path.abspath(value) == os.path.abspath(candidate) and not swapped:
            parent.rename(moved_parent)
            parent.symlink_to(outside_parent, target_is_directory=True)
            swapped = True
        return resolved

    monkeypatch.setattr(
        media_response.os.path, 'realpath', swap_parent_after_resolution
    )

    with pytest.raises(media_response.MediaResourceUnavailable):
        await open_media_resource(
            (
                MediaCandidate(
                    path=str(candidate),
                    content_type='video/mp4',
                    artifact_key='recording-part:7:final',
                ),
            ),
            expected_root=str(root),
        )


@pytest.mark.asyncio
async def test_active_snapshot_rejects_same_size_inode_replacement(
    media_file: Path,
) -> None:
    original = os.stat(media_file)
    snapshot = VirtualMediaSnapshot(
        path=str(media_file),
        source_size=10,
        source_device=int(original.st_dev),
        source_inode=int(original.st_ino),
    )
    replacement = media_file.with_name('replacement.mp4')
    replacement.write_bytes(b'abcdefghij')
    replacement.replace(media_file)

    with pytest.raises(media_response.MediaResourceUnavailable):
        await open_media_resource(
            (
                MediaCandidate(
                    path=str(media_file),
                    content_type='video/mp4',
                    artifact_key='recording-part:7:source',
                    active=True,
                ),
            ),
            snapshot=snapshot,
        )


def test_windows_opener_pins_root_and_parents_until_file_handle_is_adopted() -> None:
    api = FakeWindowsOpenApi()

    file = media_response._open_windows_candidate_file(
        r'C:\recordings\room\part.mp4', r'C:\recordings', api=api
    )
    try:
        assert file.read() == b'windows-media'
    finally:
        file.close()

    root = ntpath.normcase(r'C:\recordings')
    parent = ntpath.normcase(r'C:\recordings\room')
    target = ntpath.normcase(r'C:\recordings\room\part.mp4')
    assert api.open_calls == [
        ('directory', root, ()),
        ('directory', parent, (root,)),
        ('file', target, (root, parent)),
    ]
    assert api.closed_paths == [parent, root]
    assert api.active_paths == ()


@pytest.mark.parametrize(
    'reparse_path', (r'C:\recordings\room', r'C:\recordings\room\part.mp4')
)
def test_windows_opener_rejects_parent_and_final_reparse_points(
    reparse_path: str,
) -> None:
    api = FakeWindowsOpenApi(attributes={reparse_path: 0x410})

    with pytest.raises(media_response.MediaResourceUnavailable):
        media_response._open_windows_candidate_file(
            r'C:\recordings\room\part.mp4', r'C:\recordings', api=api
        )

    assert api.active_paths == ()


def test_windows_opener_rejects_handle_resolved_outside_trusted_root() -> None:
    target = r'C:\recordings\room\part.mp4'
    api = FakeWindowsOpenApi(final_paths={target: r'C:\outside\secret.mp4'})

    with pytest.raises(media_response.MediaResourceUnavailable):
        media_response._open_windows_candidate_file(target, r'C:\recordings', api=api)

    assert api.active_paths == ()


def test_windows_native_backend_allows_recorder_delete_sharing() -> None:
    assert media_response._WindowsNativeFileApi._SHARE_ACCESS & 0x4


def test_windows_close_failure_does_not_mask_security_rejection() -> None:
    target = r'C:\recordings\room\part.mp4'
    root = ntpath.normcase(r'C:\recordings')
    parent = ntpath.normcase(r'C:\recordings\room')
    api = FakeWindowsOpenApi(attributes={target: 0x400}, close_failures=(target,))

    with pytest.raises(
        media_response.MediaResourceUnavailable, match='media resource is unavailable'
    ):
        media_response._open_windows_candidate_file(target, root, api=api)

    assert api.close_attempts == [ntpath.normcase(target), parent, root]


def test_windows_branch_does_not_use_posix_dir_fd_opener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = BytesIO(b'native-windows-handle')
    calls: List[Tuple[str, str]] = []

    def native_open(path: str, root: str):
        calls.append((path, root))
        return sentinel

    monkeypatch.setattr(media_response, '_IS_WINDOWS', True)
    monkeypatch.setattr(media_response, '_open_windows_candidate_file', native_open)

    result = media_response._open_candidate_file(
        r'C:\recordings\part.mp4', r'C:\recordings'
    )

    assert result is sentinel
    assert calls == [(r'C:\recordings\part.mp4', r'C:\recordings')]


def test_completed_media_has_opaque_strong_etag_and_private_cache(
    media_file: Path,
) -> None:
    with _media_client(media_file) as client:
        response = client.get('/media/7')

    assert response.status_code == 200
    assert response.content == b'0123456789'
    assert response.headers['cache-control'] == 'private, max-age=3600'
    assert re.fullmatch(r'"[0-9a-f]{64}"', response.headers['etag'])
    assert media_file.name not in response.headers['etag']
    assert str(media_file) not in response.headers['etag']


def test_completed_media_replacement_gets_a_new_etag(media_file: Path) -> None:
    replacement = media_file.with_name('replacement.mp4')
    replacement.write_bytes(b'abcdefghij')
    with _media_client(media_file) as client:
        first = client.get('/media/7')
        replacement.replace(media_file)
        second = client.get('/media/7')

    assert first.headers['etag'] != second.headers['etag']
    assert second.content == b'abcdefghij'


@pytest.mark.parametrize(
    ('value', 'expected_range', 'expected_body'),
    (
        ('bytes=2-4', 'bytes 2-4/10', b'234'),
        ('bytes=7-', 'bytes 7-9/10', b'789'),
        ('bytes=-3', 'bytes 7-9/10', b'789'),
    ),
)
def test_completed_media_keeps_prefix_open_and_suffix_ranges(
    media_file: Path, value: str, expected_range: str, expected_body: bytes
) -> None:
    with _media_client(media_file) as client:
        response = client.get('/media/7', headers={'Range': value})

    assert response.status_code == 206
    assert response.headers['content-range'] == expected_range
    assert response.content == expected_body


@pytest.mark.parametrize(
    'value', ('bytes=20-30', 'bytes=4-2', 'bytes=0-1,4-5', 'items=0-1')
)
def test_completed_media_rejects_invalid_ranges(media_file: Path, value: str) -> None:
    with _media_client(media_file) as client:
        response = client.get('/media/7', headers={'Range': value})

    assert response.status_code == 416
    assert response.headers['content-range'] == 'bytes */10'
    assert response.content == b''


def test_if_none_match_takes_precedence_over_range(media_file: Path) -> None:
    with _media_client(media_file) as client:
        etag = client.get('/media/7').headers['etag']
        response = client.get(
            '/media/7', headers={'If-None-Match': etag, 'Range': 'bytes=2-4'}
        )

    assert response.status_code == 304
    assert response.content == b''
    assert response.headers['etag'] == etag
    assert response.headers['cache-control'] == 'private, max-age=3600'
    assert 'content-range' not in response.headers


def test_if_range_requires_the_current_strong_etag(media_file: Path) -> None:
    with _media_client(media_file) as client:
        etag = client.get('/media/7').headers['etag']
        matched = client.get(
            '/media/7', headers={'Range': 'bytes=2-4', 'If-Range': etag}
        )
        weak = client.get(
            '/media/7', headers={'Range': 'bytes=2-4', 'If-Range': 'W/' + etag}
        )
        stale = client.get(
            '/media/7', headers={'Range': 'bytes=2-4', 'If-Range': '"stale"'}
        )

    assert matched.status_code == 206
    assert matched.content == b'234'
    assert weak.status_code == 200
    assert weak.content == b'0123456789'
    assert stale.status_code == 200
    assert stale.content == b'0123456789'


def test_active_media_is_never_conditional_cached(media_file: Path) -> None:
    with _media_client(media_file, active=True) as client:
        response = client.get(
            '/media/7', headers={'If-None-Match': '*', 'Range': 'bytes=2-4'}
        )

    assert response.status_code == 206
    assert response.content == b'234'
    assert response.headers['cache-control'] == 'no-store'
    assert 'etag' not in response.headers


def test_download_uses_utf8_filename(media_file: Path) -> None:
    with _media_client(media_file, download_name='第一段高光.mp4') as client:
        response = client.get('/media/7')

    assert response.headers['content-disposition'] == (
        "attachment; filename*=UTF-8''"
        '%E7%AC%AC%E4%B8%80%E6%AE%B5%E9%AB%98%E5%85%89.mp4'
    )


@pytest.mark.asyncio
async def test_disconnect_closes_file_and_audits_only_safe_stream_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_path = tmp_path / 'must-not-log-private-path-token.mp4'
    secret_path.write_bytes(b'x' * (128 * 1024))
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        media_response, 'audit', lambda event, **fields: events.append((event, fields))
    )
    request = Request(
        {
            'type': 'http',
            'method': 'GET',
            'path': '/media/7',
            'query_string': b'media_token=must-not-log-query-token',
            'headers': [],
            'route': SimpleNamespace(path='/media/{media_id}'),
        }
    )
    resource = await open_media_resource(
        (
            MediaCandidate(
                path=str(secret_path),
                content_type='video/mp4',
                artifact_key='recording-part:7:final',
            ),
        )
    )
    response = build_media_response(request, resource, None, None, None, None)

    first = await response.body_iterator.__anext__()
    assert first
    assert resource.file.closed is False
    assert response.background is not None
    await response.background()
    await response.body_iterator.aclose()

    assert resource.file.closed is True
    assert len(events) == 1
    event, fields = events[0]
    assert event == 'media_stream'
    assert set(fields) == {
        'route',
        'status',
        'first_byte_ms',
        'bytes',
        'range',
        'reason',
    }
    assert fields['route'] == '/media/{media_id}'
    assert fields['status'] == 200
    assert fields['first_byte_ms'] is not None
    assert fields['bytes'] == len(first)
    assert fields['range'] == 'full'
    assert fields['reason'] == 'disconnect'
    assert 'must-not-log' not in str(events)


@pytest.mark.asyncio
async def test_opened_resource_can_be_closed_after_conditional_short_circuit(
    media_file: Path,
) -> None:
    request = Request(
        {
            'type': 'http',
            'method': 'GET',
            'path': '/media/7',
            'query_string': b'',
            'headers': [],
            'route': SimpleNamespace(path='/media/{media_id}'),
        }
    )
    resource = await open_media_resource(
        (
            MediaCandidate(
                path=str(media_file),
                content_type='video/mp4',
                artifact_key='recording-part:7:final',
            ),
        )
    )

    response = build_media_response(request, resource, None, resource.etag, None, None)

    assert response.status_code == 304
    assert resource.file.closed is True


@pytest.mark.asyncio
async def test_asgi_send_error_closes_resource_and_audits_once(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        media_response, 'audit', lambda event, **fields: events.append((event, fields))
    )
    request = Request(
        {
            'type': 'http',
            'method': 'GET',
            'path': '/media/7',
            'query_string': b'',
            'headers': [],
            'route': SimpleNamespace(path='/media/{media_id}'),
        }
    )
    resource = await open_media_resource(
        (
            MediaCandidate(
                path=str(media_file),
                content_type='video/mp4',
                artifact_key='recording-part:7:final',
            ),
        )
    )
    response = build_media_response(request, resource, None, None, None, None)
    never = asyncio.Event()

    async def receive() -> Dict[str, str]:
        await never.wait()
        return {'type': 'http.disconnect'}

    async def send(message: Dict[str, Any]) -> None:
        if message['type'] == 'http.response.body':
            raise RuntimeError('simulated send failure')

    with pytest.raises(BaseException) as raised:
        await response(request.scope, receive, send)

    assert 'simulated send failure' in repr(raised.value)
    assert resource.file.closed is True
    assert len(events) == 1
    assert events[0][1]['reason'] == 'error'


@pytest.mark.asyncio
async def test_asgi_cancellation_closes_resource_and_audits_once(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        media_response, 'audit', lambda event, **fields: events.append((event, fields))
    )
    request = Request(
        {
            'type': 'http',
            'method': 'GET',
            'path': '/media/7',
            'query_string': b'',
            'headers': [],
            'route': SimpleNamespace(path='/media/{media_id}'),
        }
    )
    resource = await open_media_resource(
        (
            MediaCandidate(
                path=str(media_file),
                content_type='video/mp4',
                artifact_key='recording-part:7:final',
            ),
        )
    )
    response = build_media_response(request, resource, None, None, None, None)
    body_started = asyncio.Event()
    never = asyncio.Event()

    async def receive() -> Dict[str, str]:
        await never.wait()
        return {'type': 'http.disconnect'}

    async def send(message: Dict[str, Any]) -> None:
        if message['type'] == 'http.response.body':
            body_started.set()
            await never.wait()

    streaming = asyncio.create_task(response(request.scope, receive, send))
    await asyncio.wait_for(body_started.wait(), timeout=0.5)
    streaming.cancel()
    with pytest.raises(asyncio.CancelledError):
        await streaming

    assert resource.file.closed is True
    assert len(events) == 1
    assert events[0][1]['reason'] == 'disconnect'


@pytest.mark.asyncio
async def test_terminal_body_send_error_is_audited_as_error(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        media_response, 'audit', lambda event, **fields: events.append((event, fields))
    )
    request = Request(
        {
            'type': 'http',
            'method': 'GET',
            'path': '/media/7',
            'query_string': b'',
            'headers': [],
            'route': SimpleNamespace(path='/media/{media_id}'),
        }
    )
    resource = await open_media_resource(
        (
            MediaCandidate(
                path=str(media_file),
                content_type='video/mp4',
                artifact_key='recording-part:7:final',
            ),
        )
    )
    response = build_media_response(request, resource, None, None, None, None)
    never = asyncio.Event()

    async def receive() -> Dict[str, str]:
        await never.wait()
        return {'type': 'http.disconnect'}

    async def send(message: Dict[str, Any]) -> None:
        if message['type'] == 'http.response.body' and not message.get(
            'more_body', False
        ):
            raise RuntimeError('simulated terminal body failure')

    with pytest.raises(BaseException) as raised:
        await response(request.scope, receive, send)

    assert 'simulated terminal body failure' in repr(raised.value)
    assert resource.file.closed is True
    assert len(events) == 1
    assert events[0][1]['reason'] == 'error'


@pytest.mark.asyncio
async def test_terminal_body_cancellation_is_audited_as_disconnect(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        media_response, 'audit', lambda event, **fields: events.append((event, fields))
    )
    request = Request(
        {
            'type': 'http',
            'method': 'GET',
            'path': '/media/7',
            'query_string': b'',
            'headers': [],
            'route': SimpleNamespace(path='/media/{media_id}'),
        }
    )
    resource = await open_media_resource(
        (
            MediaCandidate(
                path=str(media_file),
                content_type='video/mp4',
                artifact_key='recording-part:7:final',
            ),
        )
    )
    response = build_media_response(request, resource, None, None, None, None)
    terminal_started = asyncio.Event()
    never = asyncio.Event()

    async def receive() -> Dict[str, str]:
        await never.wait()
        return {'type': 'http.disconnect'}

    async def send(message: Dict[str, Any]) -> None:
        if message['type'] == 'http.response.body' and not message.get(
            'more_body', False
        ):
            terminal_started.set()
            await never.wait()

    streaming = asyncio.create_task(response(request.scope, receive, send))
    await asyncio.wait_for(terminal_started.wait(), timeout=0.5)
    streaming.cancel()
    with pytest.raises(asyncio.CancelledError):
        await streaming

    assert resource.file.closed is True
    assert len(events) == 1
    assert events[0][1]['reason'] == 'disconnect'


@pytest.mark.asyncio
async def test_response_construction_failure_closes_resource(
    media_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = Request(
        {
            'type': 'http',
            'method': 'GET',
            'path': '/media/7',
            'query_string': b'',
            'headers': [],
            'route': SimpleNamespace(path='/media/{media_id}'),
        }
    )
    resource = await open_media_resource(
        (
            MediaCandidate(
                path=str(media_file),
                content_type='video/mp4',
                artifact_key='recording-part:7:final',
            ),
        )
    )

    class BrokenResponse:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError('simulated constructor failure')

    monkeypatch.setattr(media_response, '_MediaStreamingResponse', BrokenResponse)

    with pytest.raises(RuntimeError, match='simulated constructor failure'):
        build_media_response(request, resource, None, None, None, None)

    assert resource.file.closed is True
