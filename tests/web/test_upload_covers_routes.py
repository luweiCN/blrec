import struct
from pathlib import Path
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.bili_upload.covers import CoverLibrary, CoverWorkSaturated
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.web import security
from blrec.web.routers import upload_covers


def png() -> bytes:
    return (
        b'\x89PNG\r\n\x1a\n'
        + struct.pack('>I', 13)
        + b'IHDR'
        + struct.pack('>II', 1600, 1000)
        + b'\x08\x02\x00\x00\x00'
        + b'\x00\x00\x00\x00'
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    old_library = upload_covers.library
    old_reason = upload_covers.unavailable_reason
    old_key = security.api_key
    security.api_key = 'test-api-key'
    api = FastAPI()
    api.include_router(upload_covers.router, prefix='/api/v1')
    state = {}

    @api.on_event('startup')
    async def start() -> None:
        database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
        await database.open()
        state['database'] = database
        upload_covers.library = CoverLibrary(database, tmp_path / 'covers')
        upload_covers.unavailable_reason = None

    @api.on_event('shutdown')
    async def stop() -> None:
        current_library = upload_covers.library
        if current_library is not None:
            await current_library.shutdown()
        await state['database'].close()

    try:
        with TestClient(api) as value:
            yield value
    finally:
        upload_covers.library = old_library
        upload_covers.unavailable_reason = old_reason
        security.api_key = old_key


def headers() -> dict:
    return {'x-api-key': 'test-api-key', 'content-type': 'image/png'}


def test_upload_list_and_read_cover(client: TestClient) -> None:
    created = client.post(
        '/api/v1/upload-covers',
        params={'filename': '../直播封面.png'},
        headers=headers(),
        content=png(),
    )

    assert created.status_code == 201
    assert created.json() == {
        'id': 1,
        'filename': '直播封面.png',
        'mimeType': 'image/png',
        'width': 1600,
        'height': 1000,
        'byteSize': len(png()),
        'createdAt': created.json()['createdAt'],
        'contentUrl': '/api/v1/upload-covers/1/content',
    }

    listed = client.get('/api/v1/upload-covers', headers=headers())
    assert listed.status_code == 200
    assert listed.json() == [created.json()]

    content = client.get('/api/v1/upload-covers/1/content', headers=headers())
    assert content.status_code == 200
    assert content.content == png()
    assert content.headers['content-type'] == 'image/png'
    assert content.headers['content-disposition'].startswith('inline;')
    assert '\r' not in content.headers['content-disposition']
    assert '\n' not in content.headers['content-disposition']


def test_upload_rejects_oversized_body_before_storing(client: TestClient) -> None:
    response = client.post(
        '/api/v1/upload-covers',
        params={'filename': 'cover.png'},
        headers=headers(),
        content=png() + b'x' * (2 * 1024 * 1024),
    )

    assert response.status_code == 413


def test_upload_returns_retryable_503_when_cover_worker_is_saturated(
    client: TestClient,
) -> None:
    previous = upload_covers.library

    class SaturatedLibrary:
        async def add(self, _content: bytes, _filename: str) -> None:
            raise CoverWorkSaturated(retry_after=1)

    upload_covers.library = SaturatedLibrary()  # type: ignore[assignment]
    try:
        response = client.post(
            '/api/v1/upload-covers',
            params={'filename': 'cover.png'},
            headers=headers(),
            content=png(),
        )
    finally:
        upload_covers.library = previous

    assert response.status_code == 503
    assert response.headers['retry-after'] == '1'
    assert response.json() == {'detail': 'Cover processing is busy'}


@pytest.mark.parametrize('conflict', ('outside', 'missing'))
def test_upload_maps_stored_cover_conflicts_to_safe_409(
    client: TestClient, conflict: str, tmp_path: Path
) -> None:
    previous = upload_covers.library
    assert previous is not None

    class RecordedConflictLibrary:
        async def add(self, content: bytes, filename: str) -> None:
            asset = await previous.add(content, filename)
            if conflict == 'outside':
                await previous._database.execute(
                    'UPDATE cover_assets SET storage_path=? WHERE id=?',
                    (str(tmp_path / 'outside.png'), asset.id),
                )
            else:
                opened = await previous.open(asset.id)
                opened.path.unlink()
            await previous.add(content, filename)

    upload_covers.library = RecordedConflictLibrary()  # type: ignore[assignment]
    try:
        response = client.post(
            '/api/v1/upload-covers',
            params={'filename': 'cover.png'},
            headers=headers(),
            content=png(),
        )
    finally:
        upload_covers.library = previous

    assert response.status_code == 409
    assert response.json() == {'detail': 'Stored cover is unavailable'}
