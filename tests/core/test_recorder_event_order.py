from unittest.mock import AsyncMock, Mock

import pytest

import blrec.setting  # noqa: F401  # Initialize settings before its core import.
from blrec.core.recorder import Recorder, RecorderEventListener


class _RecordingListener(RecorderEventListener):
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def on_recording_started(self, recorder: Recorder) -> None:
        self._events.append('recording_started')

    async def on_video_file_created(self, recorder: Recorder, path: str) -> None:
        self._events.append('video_file_created')


class _StreamRecorder:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self._listener: Recorder | None = None

    def add_listener(self, listener: Recorder) -> None:
        self._listener = listener

    async def start(self) -> None:
        self._events.append('stream_started')
        assert self._listener is not None
        await self._listener.on_video_file_created('/recording/p1.flv', 900)


@pytest.mark.asyncio
async def test_recording_started_is_persisted_before_video_file_creation() -> None:
    events: list[str] = []
    recorder = object.__new__(Recorder)
    recorder._listeners = [_RecordingListener(events)]
    recorder._recording = False
    recorder._stream_available = True
    recorder.save_raw_danmaku = False
    recorder._danmaku_dumper = Mock()
    recorder._danmaku_receiver = Mock()
    recorder._cover_downloader = Mock()
    recorder._stream_recorder = _StreamRecorder(events)
    recorder._logger = Mock()
    recorder._prepare = AsyncMock()

    await recorder._start_recording()

    assert events == ['recording_started', 'stream_started', 'video_file_created']


@pytest.mark.asyncio
async def test_suppressed_live_does_not_restart_until_next_broadcast() -> None:
    recorder = object.__new__(Recorder)
    recorder._live = Mock()
    recorder._live.room_info.live_start_time = 900
    recorder._logger = Mock()
    recorder._recording = True
    recorder._suppressed_live_start_time = None
    recorder._stop_recording = AsyncMock()
    recorder._start_recording = AsyncMock()

    await recorder.suppress_current_live()
    recorder._recording = False
    await recorder.on_live_stream_reset(recorder._live)

    recorder._stop_recording.assert_awaited_once_with(cancelled=True)
    recorder._start_recording.assert_not_awaited()

    recorder._live.room_info.live_start_time = 901
    await recorder.on_live_began(recorder._live)

    recorder._start_recording.assert_awaited_once_with()
