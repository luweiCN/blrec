from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator, List

import pytest
import pytest_asyncio

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.journal import RecordingJournalBridge, RecordingJournalListener


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_part_order_is_creation_order_not_completion_order(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    await journal.video_created(run_id, '/rec/p2.flv', record_start_time=902)

    await journal.video_completed(run_id, '/rec/p2.flv')
    await journal.video_completed(run_id, '/rec/p1.flv')

    parts = await journal.parts_for_run(run_id)
    assert [(part.part_index, part.source_path) for part in parts] == [
        (1, '/rec/p1.flv'),
        (2, '/rec/p2.flv'),
    ]


@pytest.mark.asyncio
async def test_restart_of_same_live_reuses_session_and_continues_part_numbers(
    database,
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    first_run = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(first_run, '/rec/p1.flv', record_start_time=901)
    await journal.video_completed(first_run, '/rec/p1.flv')
    await journal.video_postprocessed(first_run, '/rec/p1.flv', '/rec/p1.flv')
    await journal.recording_cancelled(first_run)

    restarted_run = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(restarted_run, '/rec/p2.flv', record_start_time=902)

    first_session = await journal.session_for_run(first_run)
    restarted_session = await journal.session_for_run(restarted_run)
    restarted_parts = await journal.parts_for_run(restarted_run)
    assert restarted_session.id == first_session.id
    assert restarted_session.state == 'open'
    assert [(part.part_index, part.source_path) for part in restarted_parts] == [
        (2, '/rec/p2.flv')
    ]


@pytest.mark.asyncio
async def test_missing_live_start_time_reuses_open_surrogate_session(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)

    first_run = await journal.recording_started(100, live_start_time=0)
    restarted_run = await journal.recording_started(100, live_start_time=0)

    first_session = await journal.session_for_run(first_run)
    restarted_session = await journal.session_for_run(restarted_run)
    assert restarted_session.id == first_session.id
    assert (
        restarted_session.broadcast_session_key == first_session.broadcast_session_key
    )
    assert restarted_session.broadcast_session_key.startswith('100:local:')


@pytest.mark.asyncio
async def test_reconcile_marks_crash_interrupted_file_for_manual_review(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'interrupted.flv'
    source.write_bytes(b'partial recording')
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)

    await journal.reconcile_open_sessions()

    session = await journal.session_for_run(run_id)
    part = (await journal.parts_for_run(run_id))[0]
    assert session.state == 'manual_review'
    assert part.artifact_state == 'manual_review'
    assert (
        await database.scalar(
            'SELECT COUNT(*) FROM recording_runs '
            "WHERE id=? AND state='cancelled' AND ended_at IS NOT NULL",
            (run_id,),
        )
        == 1
    )


@pytest.mark.asyncio
async def test_remux_path_becomes_final_only_after_postprocess(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    final = tmp_path / 'part.mp4'
    source.write_bytes(b'source')
    final.write_bytes(b'final')
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))

    part = (await journal.parts_for_run(run_id))[0]
    assert part.final_path is None
    assert part.artifact_state == 'postprocessing'

    source.unlink()
    await journal.video_postprocessed(run_id, str(source), str(final))

    part = (await journal.parts_for_run(run_id))[0]
    assert part.final_path == str(final)
    assert part.artifact_state == 'ready'
    assert part.source_exists is False


@pytest.mark.asyncio
async def test_session_closes_only_after_recording_and_postprocessing_finish(
    database,
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    await journal.video_completed(run_id, '/rec/p1.flv')

    await journal.recording_finished(run_id)
    assert (await journal.session_for_run(run_id)).state == 'open'

    await journal.video_postprocessed(run_id, '/rec/p1.flv', '/rec/p1.flv')
    assert (await journal.session_for_run(run_id)).state == 'closed'


@pytest.mark.asyncio
async def test_postprocessing_failure_is_a_terminal_visible_part_state(
    database,
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    await journal.video_completed(run_id, '/rec/p1.flv')
    await journal.recording_finished(run_id)

    await journal.video_postprocessing_failed(
        run_id, '/rec/p1.flv', RuntimeError('invalid FLV')
    )

    session = await journal.session_for_run(run_id)
    assert session.state == 'closed'
    assert session.parts[0].artifact_state == 'failed'
    assert session.parts[0].error_message == 'RuntimeError: invalid FLV'


@pytest.mark.asyncio
async def test_replayed_event_is_idempotent(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(
        100, live_start_time=900, event_id='recording-started'
    )

    await journal.video_created(
        run_id, '/rec/p1.flv', record_start_time=901, event_id='video-created'
    )
    await journal.video_created(
        run_id, '/rec/p1.flv', record_start_time=901, event_id='video-created'
    )

    assert len(await journal.parts_for_run(run_id)) == 1
    assert (
        await database.scalar(
            "SELECT COUNT(*) FROM event_journal WHERE id='video-created'"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_completed_danmaku_is_bound_to_matching_part(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)

    await journal.danmaku_completed(run_id, '/rec/p1.xml')

    part = (await journal.parts_for_run(run_id))[0]
    assert part.xml_path == '/rec/p1.xml'
    assert part.xml_completed is True


class FakeEmitter:
    def __init__(self) -> None:
        self.listeners: List[object] = []

    def add_listener(self, listener: object) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener: object) -> None:
        self.listeners.remove(listener)


@pytest.mark.asyncio
async def test_listener_persists_recorder_and_postprocessor_lifecycle(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    recorder = FakeEmitter()
    recorder.live = SimpleNamespace(
        room_id=100, room_info=SimpleNamespace(room_id=100, live_start_time=900)
    )
    recorder.record_start_time = 901
    postprocessor = FakeEmitter()
    listener = RecordingJournalListener(
        journal,
        recorder,  # type: ignore[arg-type]
        postprocessor,  # type: ignore[arg-type]
    )

    await listener.on_recording_started(recorder)  # type: ignore[arg-type]
    await listener.on_video_file_created(  # type: ignore[arg-type]
        recorder, '/rec/p1.flv'
    )
    await listener.on_video_file_completed(  # type: ignore[arg-type]
        recorder, '/rec/p1.flv'
    )
    await listener.on_danmaku_file_completed(  # type: ignore[arg-type]
        recorder, '/rec/p1.xml'
    )
    await listener.on_video_postprocessing_result(  # type: ignore[arg-type]
        postprocessor, '/rec/p1.flv', '/rec/p1.mp4'
    )
    assert listener._source_runs == {}
    await listener.on_recording_finished(recorder)  # type: ignore[arg-type]

    sessions = await journal.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].state == 'closed'
    assert sessions[0].parts[0].final_path == '/rec/p1.mp4'
    assert sessions[0].parts[0].xml_path == '/rec/p1.xml'

    listener.close()
    assert recorder.listeners == []
    assert postprocessor.listeners == []
