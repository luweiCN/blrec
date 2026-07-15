from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from blrec.bili_upload.transcode_remux import TranscodeRemuxer, TranscodeRemuxError


def _probe(*, duration: float, audio: bool = True) -> SimpleNamespace:
    streams = [{'codec_type': 'video'}]
    if audio:
        streams.append({'codec_type': 'audio'})
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {'streams': streams, 'format': {'duration': str(duration)}}
        ).encode('utf8'),
        stderr=b'',
    )


def test_remux_uses_safe_argument_arrays_and_validates_output(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / 'source; touch unsafe.flv'
    source.write_bytes(b'source-video')
    calls = []

    def run(command, **kwargs):
        calls.append((tuple(command), kwargs))
        if command[0] == 'ffprobe':
            return _probe(duration=120.0)
        Path(command[-1]).write_bytes(b'remuxed-video')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'ffmpeg ok')

    monkeypatch.setattr(subprocess, 'run', run)
    remuxer = TranscodeRemuxer(tmp_path / 'work')

    artifact = remuxer.remux(str(source), part_id=12)

    ffmpeg_command, ffmpeg_options = calls[1]
    assert ffmpeg_command == (
        'ffmpeg',
        '-hide_banner',
        '-nostdin',
        '-fflags',
        '+genpts',
        '-i',
        str(source),
        '-map',
        '0',
        '-c',
        'copy',
        '-avoid_negative_ts',
        'make_zero',
        '-y',
        artifact.path,
    )
    assert ffmpeg_options['shell'] is False
    assert ffmpeg_options['timeout'] == 3600
    assert Path(artifact.path).read_bytes() == b'remuxed-video'
    assert artifact.identity.canonical_path == str(Path(artifact.path).resolve())


def test_remux_rejects_lost_audio_and_removes_invalid_output(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / 'source.flv'
    source.write_bytes(b'source-video')
    probe_count = 0
    output_path = None

    def run(command, **kwargs):
        nonlocal probe_count, output_path
        if command[0] == 'ffprobe':
            probe_count += 1
            return _probe(duration=30.0, audio=probe_count == 1)
        output_path = Path(command[-1])
        output_path.write_bytes(b'remuxed-video')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', run)

    with pytest.raises(TranscodeRemuxError, match='音频流'):
        TranscodeRemuxer(tmp_path / 'work').remux(str(source), part_id=1)

    assert output_path is not None
    assert not output_path.exists()


def test_remux_rejects_duration_drift_and_timeout(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / 'source.flv'
    source.write_bytes(b'source-video')
    probe_count = 0

    def drift(command, **kwargs):
        nonlocal probe_count
        if command[0] == 'ffprobe':
            probe_count += 1
            return _probe(duration=100.0 if probe_count == 1 else 70.0)
        Path(command[-1]).write_bytes(b'remuxed-video')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', drift)
    with pytest.raises(TranscodeRemuxError, match='时长'):
        TranscodeRemuxer(tmp_path / 'work').remux(str(source), part_id=1)

    def timeout(command, **kwargs):
        if command[0] == 'ffprobe':
            return _probe(duration=100.0)
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr(subprocess, 'run', timeout)
    with pytest.raises(TranscodeRemuxError, match='超时'):
        TranscodeRemuxer(tmp_path / 'work').remux(str(source), part_id=1)
