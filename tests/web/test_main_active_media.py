from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import blrec.web.main as web_main
from blrec.bili_upload.active_media import ActiveMediaBusy, ActiveMediaMetadata
from blrec.bili_upload.journal import RecordingPart
from blrec.bili_upload.recording_content import FlvMediaSnapshot, MediaResource


def _active_part() -> RecordingPart:
    return RecordingPart(
        id=7,
        session_id=3,
        run_id='run-3',
        part_index=4,
        source_path='/rec/p4.flv',
        final_path=None,
        xml_path='/rec/p4.xml',
        record_start_time=900,
        artifact_state='recording',
        xml_completed=False,
        source_exists=True,
        final_exists=False,
        error_message=None,
    )


def _active_resource() -> MediaResource:
    return MediaResource(
        path='/rec/p4.flv',
        size=2_048,
        content_type='video/x-flv',
        recording=True,
        room_id=100,
        part_index=4,
        bvid=None,
        remote_available=False,
        playback_mode='active_snapshot',
        index_state='pending',
    )


def test_active_metadata_capture_does_not_resolve_paths_on_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = SimpleNamespace(lastkeyframelocation=100.0, lastkeyframetimestamp=20.0)
    monkeypatch.setattr(web_main, '_application_started', True)
    monkeypatch.setattr(
        web_main,
        'app',
        SimpleNamespace(
            get_task_data=lambda _room_id: SimpleNamespace(
                task_status=SimpleNamespace(recording_path='/rec/current.flv')
            ),
            get_task_metadata=lambda _room_id: metadata,
        ),
    )
    monkeypatch.setattr(
        web_main.os.path,
        'realpath',
        lambda _path: (_ for _ in ()).throw(AssertionError('event-loop realpath')),
    )
    resource = _active_resource()
    resource = MediaResource(**{**resource.__dict__, 'path': '/rec/current.flv'})

    captured = web_main._active_recording_metadata(resource)

    assert captured == ActiveMediaMetadata('/rec/current.flv', metadata)


@pytest.mark.asyncio
async def test_active_highlight_duration_reads_only_the_current_active_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    part = _active_part()
    journal = SimpleNamespace(active_part_for_session=AsyncMock(return_value=part))
    reader = SimpleNamespace(media=AsyncMock(return_value=_active_resource()))
    snapshot = FlvMediaSnapshot(
        path='/rec/p4.flv',
        source_size=2_048,
        source_tail_start=10,
        prefix=b'prefix',
        duration_ms=81_000,
    )
    service = SimpleNamespace(snapshot=AsyncMock(return_value=snapshot))
    metadata = {'lastkeyframelocation': 1_900, 'lastkeyframetimestamp': 81.0}
    monkeypatch.setattr(web_main, '_application_started', True)
    monkeypatch.setattr(
        web_main,
        '_bili_account_runtime',
        SimpleNamespace(journal=journal, content_reader=reader),
    )
    monkeypatch.setattr(web_main, '_active_media_service', service)
    monkeypatch.setattr(
        web_main, '_active_recording_metadata', lambda _resource: metadata
    )

    result = await web_main._active_highlight_durations(3)

    assert result == {7: 81_000}
    journal.active_part_for_session.assert_awaited_once_with(3)
    reader.media.assert_awaited_once_with(7)
    service.snapshot.assert_awaited_once_with(7, '/rec/p4.flv', 2_048, metadata)


@pytest.mark.asyncio
async def test_active_highlight_duration_treats_saturation_as_temporarily_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = SimpleNamespace(
        active_part_for_session=AsyncMock(return_value=_active_part())
    )
    reader = SimpleNamespace(media=AsyncMock(return_value=_active_resource()))
    service = SimpleNamespace(
        snapshot=AsyncMock(side_effect=ActiveMediaBusy(retry_after=1))
    )
    monkeypatch.setattr(web_main, '_application_started', True)
    monkeypatch.setattr(
        web_main,
        '_bili_account_runtime',
        SimpleNamespace(journal=journal, content_reader=reader),
    )
    monkeypatch.setattr(web_main, '_active_media_service', service)
    monkeypatch.setattr(
        web_main, '_active_recording_metadata', lambda _resource: {'duration': 10}
    )

    assert await web_main._active_highlight_durations(3) == {}
