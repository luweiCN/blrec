import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from blrec.bili_upload.artifact_recovery import (
    RecoveredArtifact,
    probe_recording_artifact,
)


def completed_probe(*, streams: list, duration: object = None) -> SimpleNamespace:
    payload = {'streams': streams, 'format': {}}
    if duration is not None:
        payload['format']['duration'] = duration
    return SimpleNamespace(
        returncode=0, stdout=json.dumps(payload).encode('utf8'), stderr=b''
    )


def test_probe_accepts_nonempty_file_with_video_stream(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / 'part.flv'
    path.write_bytes(b'video')
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return completed_probe(streams=[{'codec_type': 'video'}], duration='12.8')

    monkeypatch.setattr(subprocess, 'run', run)

    assert probe_recording_artifact(str(path)) == RecoveredArtifact(
        path=str(path), size_bytes=5, duration_seconds=13
    )
    command, kwargs = calls[0]
    assert command[0] == 'ffprobe'
    assert command[-1] == str(path)
    assert kwargs['timeout'] == 15
    assert kwargs['check'] is False


def test_probe_rejects_file_without_video_stream(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / 'audio.flv'
    path.write_bytes(b'audio')
    monkeypatch.setattr(
        subprocess,
        'run',
        lambda *args, **kwargs: completed_probe(
            streams=[{'codec_type': 'audio'}], duration='10'
        ),
    )

    assert probe_recording_artifact(str(path)) is None


def test_probe_rejects_missing_empty_and_unreadable_files(
    tmp_path: Path, monkeypatch
) -> None:
    missing = tmp_path / 'missing.flv'
    empty = tmp_path / 'empty.flv'
    empty.touch()

    assert probe_recording_artifact(str(missing)) is None
    assert probe_recording_artifact(str(empty)) is None

    unreadable = tmp_path / 'unreadable.flv'
    unreadable.write_bytes(b'broken')
    monkeypatch.setattr(
        subprocess,
        'run',
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout=b'', stderr=b'invalid data'
        ),
    )
    assert probe_recording_artifact(str(unreadable)) is None


def test_probe_treats_timeout_as_unusable(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / 'slow.flv'
    path.write_bytes(b'video')

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd='ffprobe', timeout=15)

    monkeypatch.setattr(subprocess, 'run', timeout)

    assert probe_recording_artifact(str(path)) is None
