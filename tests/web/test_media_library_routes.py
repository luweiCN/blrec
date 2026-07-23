from typing import AsyncIterable, Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from blrec.bili_upload.media_library import (
    MediaLibraryConflict,
    MediaLibraryItem,
    MediaLibraryPart,
    SubmissionHistoryEntry,
)
from blrec.web import security
from blrec.web.routers import media_library


def item() -> MediaLibraryItem:
    return MediaLibraryItem(
        id=3,
        session_id=9,
        kind='broadcast',
        origin='recording',
        storage_key='a' * 32,
        display_name='主播 7 月 23 日直播',
        note='值得长期保留',
        state='ready',
        error=None,
        created_at=100,
        updated_at=101,
        room_id=100,
        title='原始直播标题',
        anchor_name='主播',
        started_at=90,
        tags=('精选', '访谈'),
        parts=(
            MediaLibraryPart(
                item_id=3,
                part_index=1,
                recording_part_id=11,
                original_filename='first.flv',
                storage_path='/favorites/key/part-0001.flv',
                expected_size=6,
                received_size=6,
                state='ready',
                error=None,
                duration_seconds=60,
            ),
        ),
    )


def submission() -> SubmissionHistoryEntry:
    return SubmissionHistoryEntry(
        aid=42,
        bvid='BV1test',
        state='approved',
        account_id=1,
        account_name='投稿账号',
        occurred_at=102,
        current=True,
    )


class FakeMediaLibrary:
    def __init__(self) -> None:
        self.list_items = AsyncMock(return_value=(1, (item(),)))
        self.get_item = AsyncMock(return_value=item())
        self.favorite = AsyncMock(return_value=item())
        self.create_import = AsyncMock(return_value=item())
        self.complete_import = AsyncMock(return_value=item())
        self.update_item = AsyncMock(return_value=item())
        self.submission_history = AsyncMock(return_value=(submission(),))
        self.submission_histories = AsyncMock(return_value={item().id: (submission(),)})
        self.uploaded = b''

    async def upload_part(
        self, item_id: int, part_index: int, chunks: AsyncIterable[bytes]
    ) -> MediaLibraryPart:
        assert item_id == 3
        assert part_index == 1
        self.uploaded = b''.join([chunk async for chunk in chunks])
        return item().parts[0]


@pytest.fixture(autouse=True)
def restore_router_state() -> Iterator[None]:
    old_library = media_library.library
    old_deleter = media_library.item_deleter
    old_reason = media_library.unavailable_reason
    old_key = security.api_key
    yield
    media_library.library = old_library
    media_library.item_deleter = old_deleter
    media_library.unavailable_reason = old_reason
    security.api_key = old_key


@pytest.fixture
def client() -> Iterator[TestClient]:
    api = FastAPI(dependencies=[Depends(security.authenticate)])
    api.include_router(media_library.router, prefix='/api/v1')
    security.api_key = 'test-api-key'
    media_library.library = FakeMediaLibrary()  # type: ignore[assignment]
    media_library.item_deleter = AsyncMock(return_value=2)
    media_library.unavailable_reason = None
    with TestClient(api) as value:
        yield value


def auth() -> dict:
    return {'x-api-key': 'test-api-key'}


def test_media_library_list_includes_parts_and_bilibili_history(
    client: TestClient,
) -> None:
    response = client.get(
        '/api/v1/media-library?kind=broadcast&limit=20&offset=0&q=主播', headers=auth()
    )

    assert response.status_code == 200
    body = response.json()
    assert body['total'] == 1
    assert body['items'][0]['sessionId'] == 9
    assert body['items'][0]['tags'] == ['精选', '访谈']
    assert body['items'][0]['parts'][0]['recordingPartId'] == 11
    assert 'storagePath' not in body['items'][0]['parts'][0]
    assert body['items'][0]['submissions'][0] == {
        'aid': 42,
        'bvid': 'BV1test',
        'state': 'approved',
        'accountId': 1,
        'accountName': '投稿账号',
        'occurredAt': 102,
        'current': True,
    }
    library = media_library.library
    assert isinstance(library, FakeMediaLibrary)
    library.list_items.assert_awaited_once_with(
        kind='broadcast', limit=20, offset=0, query='主播'
    )
    library.submission_histories.assert_awaited_once_with((3,))
    library.submission_history.assert_not_awaited()


def test_favorite_and_update_media_library_item(client: TestClient) -> None:
    favorite = client.post('/api/v1/media-library/favorites/9', headers=auth())
    updated = client.patch(
        '/api/v1/media-library/3',
        headers=auth(),
        json={
            'displayName': '重命名后的直播',
            'note': '新备注',
            'tags': ['教程', '长期'],
        },
    )

    assert favorite.status_code == 201
    assert updated.status_code == 200
    library = media_library.library
    assert isinstance(library, FakeMediaLibrary)
    assert library.favorite.await_args.kwargs['manager_subject']
    library.favorite.assert_awaited_once()
    library.update_item.assert_awaited_once()
    assert library.update_item.await_args.kwargs['display_name'] == '重命名后的直播'
    assert library.update_item.await_args.kwargs['tags'] == ['教程', '长期']


def test_external_import_uploads_each_part_then_completes(client: TestClient) -> None:
    created = client.post(
        '/api/v1/media-library/imports',
        headers=auth(),
        json={
            'kind': 'broadcast',
            'displayName': '外部直播',
            'tags': ['采集'],
            'parts': [
                {'filename': '第一段.mp4', 'sizeBytes': 6},
                {'filename': '第二段.mp4', 'sizeBytes': 8},
            ],
        },
    )
    uploaded = client.put(
        '/api/v1/media-library/3/parts/1/content',
        headers={**auth(), 'content-type': 'application/octet-stream'},
        content=b'video!',
    )
    completed = client.post('/api/v1/media-library/3/complete', headers=auth())

    assert created.status_code == 201
    assert uploaded.status_code == 200
    assert completed.status_code == 200
    library = media_library.library
    assert isinstance(library, FakeMediaLibrary)
    assert [
        part.filename for part in library.create_import.await_args.kwargs['parts']
    ] == ['第一段.mp4', '第二段.mp4']
    assert library.uploaded == b'video!'
    library.complete_import.assert_awaited_once()


def test_delete_media_library_item_uses_restart_safe_session_deletion(
    client: TestClient,
) -> None:
    response = client.delete('/api/v1/media-library/3', headers=auth())

    assert response.status_code == 202
    assert response.json() == {'state': 'requested', 'generation': 2}
    deleter = media_library.item_deleter
    assert isinstance(deleter, AsyncMock)
    deleter.assert_awaited_once()
    assert deleter.await_args.args[0] == 3
    assert deleter.await_args.kwargs['manager_subject']


def test_media_library_conflict_is_returned_as_409(client: TestClient) -> None:
    library = media_library.library
    assert isinstance(library, FakeMediaLibrary)
    library.favorite.side_effect = MediaLibraryConflict('录制尚未结束')

    response = client.post('/api/v1/media-library/favorites/9', headers=auth())

    assert response.status_code == 409
    assert response.json()['detail'] == '录制尚未结束'
