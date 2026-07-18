from pathlib import Path

import pytest

from blrec.bili_upload.recording_thumbnail import RecordingThumbnailProvider


@pytest.mark.asyncio
async def test_thumbnail_provider_uses_fast_seek_and_caches_the_result(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'recording.flv'
    source.write_bytes(b'video')
    calls = []

    async def run(command, timeout_seconds):
        calls.append((command, timeout_seconds))
        return b'jpeg'

    provider = RecordingThumbnailProvider(runner=run)

    first, first_cached = await provider.get(str(source), 4_499, 240)
    second, second_cached = await provider.get(str(source), 4_500, 240)

    assert first == second == b'jpeg'
    assert first_cached is False
    assert second_cached is True
    assert len(calls) == 1
    command, timeout_seconds = calls[0]
    assert command.index('-ss') < command.index('-i')
    assert command[command.index('-ss') + 1] == '4.000'
    assert timeout_seconds == 8.0
