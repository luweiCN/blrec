from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from blrec.bili_upload.highlight_cut import (
    ClipInspection,
    ClipSource,
    HighlightCutError,
    InspectedClipSource,
    LosslessClipper,
    MediaProfile,
)


def profile(
    *, duration_ms: int = 100_000, width: int = 1920, has_audio: bool = True
) -> MediaProfile:
    return MediaProfile(
        codec_name='h264',
        width=width,
        height=1080,
        r_frame_rate='60/1',
        extradata_size=42,
        duration_ms=duration_ms,
        has_audio=has_audio,
    )


def test_inspect_backs_up_to_the_previous_keyframe(tmp_path: Path) -> None:
    source = tmp_path / 'source.flv'
    source.write_bytes(b'video')
    clipper = LosslessClipper(probe=lambda _path: (profile(), (0, 28_600, 30_600)))

    inspection = clipper.inspect(
        (
            ClipSource(
                part_id=1,
                path=str(source),
                requested_start_ms=30_000,
                requested_end_ms=80_000,
            ),
        ),
        requested_start_ms=30_000,
        requested_end_ms=80_000,
        stable_end_ms=100_000,
    )

    assert inspection.actual_start_ms == 28_600
    assert inspection.actual_end_ms == 80_000
    assert inspection.extra_lead_ms == 1_400
    assert inspection.confirmation_required is False
    assert inspection.sources[0].actual_start_ms == 28_600
    assert inspection.sources[0].output_offset_ms == 0


def test_inspect_clamps_to_first_keyframe_when_video_starts_after_request(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'source.flv'
    source.write_bytes(b'video')
    clipper = LosslessClipper(probe=lambda _path: (profile(), (1_926, 4_926)))

    inspection = clipper.inspect(
        (ClipSource(1, str(source), 1_100, 6_100),),
        requested_start_ms=1_100,
        requested_end_ms=6_100,
        stable_end_ms=100_000,
    )

    assert inspection.actual_start_ms == 1_926
    assert inspection.actual_end_ms == 6_100
    assert inspection.extra_lead_ms == 0
    assert inspection.confirmation_required is False
    assert inspection.sources[0].actual_start_ms == 1_926
    assert inspection.output_duration_ms == 4_174


def test_inspect_rejects_range_before_first_video_keyframe(tmp_path: Path) -> None:
    source = tmp_path / 'source.flv'
    source.write_bytes(b'video')
    clipper = LosslessClipper(probe=lambda _path: (profile(), (7_000, 10_000)))

    with pytest.raises(HighlightCutError, match='范围内没有可用的视频关键帧'):
        clipper.inspect(
            (ClipSource(1, str(source), 1_100, 6_100),),
            requested_start_ms=1_100,
            requested_end_ms=6_100,
            stable_end_ms=100_000,
        )


def test_inspect_requires_confirmation_above_ten_seconds(tmp_path: Path) -> None:
    source = tmp_path / 'source.flv'
    source.write_bytes(b'video')
    clipper = LosslessClipper(probe=lambda _path: (profile(), (0, 18_000)))

    inspection = clipper.inspect(
        (ClipSource(1, str(source), 30_000, 80_000),),
        requested_start_ms=30_000,
        requested_end_ms=80_000,
        stable_end_ms=100_000,
    )

    assert inspection.extra_lead_ms == 12_000
    assert inspection.confirmation_required is True


def test_inspect_accepts_exactly_one_source(tmp_path: Path) -> None:
    first = tmp_path / 'first.flv'
    second = tmp_path / 'second.flv'
    first.write_bytes(b'first')
    second.write_bytes(b'second')
    clipper = LosslessClipper(probe=lambda _path: (profile(), (0,)))

    with pytest.raises(HighlightCutError, match='一个视频分段'):
        clipper.inspect(
            (), requested_start_ms=0, requested_end_ms=10_000, stable_end_ms=10_000
        )
    with pytest.raises(HighlightCutError, match='一个视频分段'):
        clipper.inspect(
            (
                ClipSource(1, str(first), 0, 10_000),
                ClipSource(2, str(second), 0, 10_000),
            ),
            requested_start_ms=0,
            requested_end_ms=20_000,
            stable_end_ms=20_000,
        )


def test_legacy_worker_inspection_keeps_existing_multi_source_clips(
    tmp_path: Path,
) -> None:
    first = tmp_path / 'first.flv'
    second = tmp_path / 'second.flv'
    first.write_bytes(b'first')
    second.write_bytes(b'second')
    clipper = LosslessClipper(probe=lambda _path: (profile(), (0, 5_000)))

    inspection = clipper.inspect_legacy(
        (
            ClipSource(1, str(first), 5_000, 10_000),
            ClipSource(2, str(second), 0, 10_000),
        ),
        requested_start_ms=5_000,
        requested_end_ms=20_000,
        stable_end_ms=20_000,
    )

    assert [source.part_id for source in inspection.sources] == [1, 2]


def test_inspect_uses_one_absolute_probe_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    source.write_bytes(b'video')
    now = [100.0]
    timeouts = []

    def run(command, **kwargs):
        timeouts.append(kwargs['timeout'])
        now[0] += 12.0
        document = (
            {'frames': [{'best_effort_timestamp_time': '0.0'}]}
            if '-skip_frame' in command
            else {
                'streams': [
                    {
                        'codec_type': 'video',
                        'codec_name': 'h264',
                        'width': 1920,
                        'height': 1080,
                        'r_frame_rate': '60/1',
                        'extradata_size': 42,
                    },
                    {'codec_type': 'audio'},
                ],
                'format': {'duration': '100.0'},
            }
        )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(document).encode('utf8'), stderr=b''
        )

    monkeypatch.setattr(subprocess, 'run', run)
    clipper = LosslessClipper(monotonic=lambda: now[0])

    clipper.inspect(
        (ClipSource(1, str(source), 1_000, 10_000),),
        requested_start_ms=1_000,
        requested_end_ms=10_000,
        stable_end_ms=10_000,
        deadline_monotonic=130.0,
    )

    assert timeouts == [30.0, 18.0]


def test_inspect_does_not_start_probe_after_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    source.write_bytes(b'video')
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: calls.append(args))

    with pytest.raises(HighlightCutError, match='检查视频超时'):
        LosslessClipper(monotonic=lambda: 30.0).inspect(
            (ClipSource(1, str(source), 0, 10_000),),
            requested_start_ms=0,
            requested_end_ms=10_000,
            stable_end_ms=10_000,
            deadline_monotonic=30.0,
        )

    assert calls == []


def test_inspect_rejects_unsafe_tail_and_multiple_parts(tmp_path: Path) -> None:
    first = tmp_path / 'first.flv'
    second = tmp_path / 'second.flv'
    first.write_bytes(b'first')
    second.write_bytes(b'second')

    safe = LosslessClipper(probe=lambda _path: (profile(), (0, 30_000)))
    with pytest.raises(HighlightCutError, match='安全'):
        safe.inspect(
            (ClipSource(1, str(first), 30_000, 80_000),),
            requested_start_ms=30_000,
            requested_end_ms=80_000,
            stable_end_ms=75_000,
        )

    with pytest.raises(HighlightCutError, match='一个视频分段'):
        safe.inspect(
            (
                ClipSource(1, str(first), 0, 10_000),
                ClipSource(2, str(second), 0, 10_000),
            ),
            requested_start_ms=0,
            requested_end_ms=20_000,
            stable_end_ms=20_000,
        )


def test_inspect_uses_safe_ffprobe_argument_arrays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source; touch unsafe.flv'
    source.write_bytes(b'video')
    calls = []

    def run(command, **kwargs):
        calls.append((tuple(command), kwargs))
        if '-skip_frame' in command:
            document = {
                'frames': [
                    {'best_effort_timestamp_time': '0.0'},
                    {'best_effort_timestamp_time': '28.6'},
                ]
            }
        else:
            document = {
                'streams': [
                    {
                        'codec_type': 'video',
                        'codec_name': 'h264',
                        'width': 1920,
                        'height': 1080,
                        'r_frame_rate': '60/1',
                        'extradata_size': 42,
                    },
                    {'codec_type': 'audio'},
                ],
                'format': {'duration': '100.0'},
            }
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(document).encode('utf8'), stderr=b''
        )

    monkeypatch.setattr(subprocess, 'run', run)
    inspection = LosslessClipper().inspect(
        (ClipSource(1, str(source), 30_000, 80_000),),
        requested_start_ms=30_000,
        requested_end_ms=80_000,
        stable_end_ms=100_000,
    )

    profile_command, profile_options = calls[0]
    keyframe_command, keyframe_options = calls[1]
    assert profile_command == (
        'ffprobe',
        '-v',
        'error',
        '-show_entries',
        'stream=codec_type,codec_name,width,height,'
        'r_frame_rate,extradata_size:format=duration',
        '-of',
        'json',
        str(source),
    )
    assert '-read_intervals' in keyframe_command
    interval = keyframe_command[keyframe_command.index('-read_intervals') + 1]
    assert interval == '0.000%+35.000'
    assert ('-skip_frame', 'nokey') == keyframe_command[
        keyframe_command.index('-skip_frame') : keyframe_command.index('-skip_frame')
        + 2
    ]
    assert profile_options['shell'] is False
    assert keyframe_options['shell'] is False
    assert inspection.actual_start_ms == 28_600


def test_inspect_scans_keyframes_from_start_when_indexed_probe_skips_keyframe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    source.write_bytes(b'video')
    calls = []

    def run(command, **kwargs):
        calls.append(tuple(command))
        if '-skip_frame' in command and command[
            command.index('-read_intervals') + 1
        ].startswith('%+'):
            document = {
                'frames': [
                    {'best_effort_timestamp_time': '0.067'},
                    {'best_effort_timestamp_time': '28.600'},
                    {'best_effort_timestamp_time': '31.000'},
                ]
            }
        elif '-skip_frame' in command:
            document = {'frames': [{'best_effort_timestamp_time': '31.0'}]}
        else:
            document = {
                'streams': [
                    {
                        'codec_type': 'video',
                        'codec_name': 'h264',
                        'width': 1920,
                        'height': 1080,
                        'r_frame_rate': '60/1',
                        'extradata_size': 42,
                    }
                ],
                'format': {'duration': '100.0'},
            }
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(document).encode('utf8'), stderr=b''
        )

    monkeypatch.setattr(subprocess, 'run', run)

    inspection = LosslessClipper().inspect(
        (ClipSource(1, str(source), 30_000, 80_000),),
        requested_start_ms=30_000,
        requested_end_ms=80_000,
        stable_end_ms=100_000,
    )

    assert len(calls) == 3
    fallback_command = calls[-1]
    assert ('-skip_frame', 'nokey') == fallback_command[
        fallback_command.index('-skip_frame') : fallback_command.index('-skip_frame')
        + 2
    ]
    assert fallback_command[fallback_command.index('-read_intervals') + 1] == (
        '%+35.000'
    )
    assert inspection.actual_start_ms == 28_600


def test_cut_uses_stream_copy_and_atomically_keeps_valid_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    output = tmp_path / 'clip.mp4'
    source.write_bytes(b'video')

    def probe(path: str):
        if path == str(source):
            return profile(), (0, 28_600)
        return profile(duration_ms=51_400), (0,)

    calls = []

    def run(command, **kwargs):
        calls.append((tuple(command), kwargs))
        Path(command[-1]).write_bytes(b'clip')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'ffmpeg ok')

    monkeypatch.setattr(subprocess, 'run', run)
    clipper = LosslessClipper(probe=probe)
    inspection = clipper.inspect(
        (ClipSource(1, str(source), 30_000, 80_000),),
        requested_start_ms=30_000,
        requested_end_ms=80_000,
        stable_end_ms=100_000,
    )

    artifact = clipper.cut(inspection, str(output))

    command, options = calls[0]
    assert isinstance(command, tuple)
    seek_indexes = [index for index, value in enumerate(command) if value == '-ss']
    assert [command[index + 1] for index in seek_indexes] == ['28.500']
    assert command.index('-i') < seek_indexes[0]
    assert command[command.index('-t') + 1] == '51.500'
    assert ('-c', 'copy') == command[command.index('-c') : command.index('-c') + 2]
    assert ('-avoid_negative_ts', 'make_zero') == command[
        command.index('-avoid_negative_ts') : command.index('-avoid_negative_ts') + 2
    ]
    assert options['shell'] is False
    assert output.read_bytes() == b'clip'
    assert artifact.path == str(output)
    assert artifact.duration_ms == 51_400


def test_cut_uses_two_stage_seek_to_keep_the_inspected_keyframe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    output = tmp_path / 'clip.mp4'
    source.write_bytes(b'video')
    source_profile = profile(duration_ms=120_000)
    inspection = ClipInspection(
        sources=(
            InspectedClipSource(1, str(source), 60_000, 80_000, 0, source_profile),
        ),
        requested_start_ms=62_000,
        requested_end_ms=80_000,
        actual_start_ms=60_000,
        actual_end_ms=80_000,
        extra_lead_ms=2_000,
        confirmation_required=False,
    )
    calls = []

    def probe(path: str):
        duration_ms = 120_000 if path == str(source) else 20_000
        return profile(duration_ms=duration_ms), (0,)

    def run(command, **kwargs):
        calls.append(tuple(command))
        Path(command[-1]).write_bytes(b'clip')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', run)

    LosslessClipper(probe=probe).cut(inspection, str(output))

    command = calls[0]
    seek_indexes = [index for index, value in enumerate(command) if value == '-ss']
    assert [command[index + 1] for index in seek_indexes] == ['30.000', '29.900']
    assert seek_indexes[0] < command.index('-i') < seek_indexes[1]
    assert command[command.index('-t') + 1] == '20.100'


def test_cut_does_not_input_seek_to_zero_for_a_keyframe_near_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    output = tmp_path / 'clip.mp4'
    source.write_bytes(b'video')
    source_profile = profile(duration_ms=120_000)
    inspection = ClipInspection(
        sources=(InspectedClipSource(1, str(source), 67, 6_700, 0, source_profile),),
        requested_start_ms=1_100,
        requested_end_ms=6_700,
        actual_start_ms=67,
        actual_end_ms=6_700,
        extra_lead_ms=1_033,
        confirmation_required=False,
    )
    calls = []

    def probe(path: str):
        duration_ms = 120_000 if path == str(source) else 6_633
        return profile(duration_ms=duration_ms), (0,)

    def run(command, **kwargs):
        calls.append(tuple(command))
        Path(command[-1]).write_bytes(b'clip')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', run)

    LosslessClipper(probe=probe).cut(inspection, str(output))

    command = calls[0]
    input_index = command.index('-i')
    assert '-ss' not in command[:input_index]


def test_cut_transcodes_when_stream_copy_boundary_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    output = tmp_path / 'clip.mp4'
    source.write_bytes(b'video')
    source_profile = profile(duration_ms=120_000)
    inspection = ClipInspection(
        sources=(InspectedClipSource(1, str(source), 3_000, 8_000, 0, source_profile),),
        requested_start_ms=3_500,
        requested_end_ms=8_000,
        actual_start_ms=3_000,
        actual_end_ms=8_000,
        extra_lead_ms=500,
        confirmation_required=False,
    )
    calls = []

    def probe(path: str):
        duration_ms = 120_000 if path == str(source) else 5_000
        return profile(duration_ms=duration_ms), (0,)

    def run(command, **kwargs):
        calls.append(tuple(command))
        Path(command[-1]).write_bytes(b'clip')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    clipper = LosslessClipper(probe=probe)
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(
        clipper,
        '_validate_stream_copy_boundaries',
        lambda _inspection, _path: (_ for _ in ()).throw(
            HighlightCutError('流复制结果丢失起始关键帧')
        ),
        raising=False,
    )

    artifact = clipper.cut(inspection, str(output))

    assert artifact.strategy == 'transcoded'
    assert len(calls) == 2
    assert ('-c', 'copy') == calls[0][calls[0].index('-c') :][:2]
    assert ('-c:v', 'libx264') == calls[1][calls[1].index('-c:v') :][:2]
    assert ('-c:a', 'aac') == calls[1][calls[1].index('-c:a') :][:2]


def test_cut_transcodes_when_stream_copy_is_not_fully_decodable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    output = tmp_path / 'clip.mp4'
    source.write_bytes(b'video')
    source_profile = profile(duration_ms=120_000)
    inspection = ClipInspection(
        sources=(InspectedClipSource(1, str(source), 3_000, 8_000, 0, source_profile),),
        requested_start_ms=3_500,
        requested_end_ms=8_000,
        actual_start_ms=3_000,
        actual_end_ms=8_000,
        extra_lead_ms=500,
        confirmation_required=False,
    )
    calls = []
    validation_labels = []

    def probe(path: str):
        duration_ms = 120_000 if path == str(source) else 5_000
        return profile(duration_ms=duration_ms), (0,)

    def run(command, **kwargs):
        calls.append(tuple(command))
        Path(command[-1]).write_bytes(b'clip')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    def validate_decode(_path: str, *, label: str) -> None:
        validation_labels.append(label)
        if label == '流复制':
            raise HighlightCutError('流复制结果无法完整解码')

    clipper = LosslessClipper(probe=probe)
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(clipper, '_validate_fully_decodable', validate_decode)

    artifact = clipper.cut(inspection, str(output))

    assert artifact.strategy == 'transcoded'
    assert validation_labels == ['流复制', '自动转码']
    assert len(calls) == 2


def test_transcoded_boundary_validation_requires_consecutive_matching_frames() -> None:
    def frames(values):
        return tuple(bytes([value]) * 16 for value in values)

    assert LosslessClipper._visually_matches(
        frames((0, 50, 100)), frames((200, 0, 50, 100, 200))
    )
    assert not LosslessClipper._visually_matches(
        frames((0, 50, 100)), frames((0, 200, 100))
    )


def test_cut_retries_transcode_sequentially_when_fast_seek_is_still_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    output = tmp_path / 'clip.mp4'
    source.write_bytes(b'video')
    source_profile = profile(duration_ms=180_000)
    inspection = ClipInspection(
        sources=(
            InspectedClipSource(1, str(source), 60_000, 70_000, 0, source_profile),
        ),
        requested_start_ms=61_000,
        requested_end_ms=70_000,
        actual_start_ms=60_000,
        actual_end_ms=70_000,
        extra_lead_ms=1_000,
        confirmation_required=False,
    )
    calls = []
    transcode_validations = []

    def probe(path: str):
        duration_ms = 180_000 if path == str(source) else 10_000
        return profile(duration_ms=duration_ms), (0,)

    def run(command, **kwargs):
        calls.append(tuple(command))
        Path(command[-1]).write_bytes(b'clip')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    def validate_transcode(_inspection, _path):
        transcode_validations.append(True)
        if len(transcode_validations) == 1:
            raise HighlightCutError('快速转码起点仍然错误')

    clipper = LosslessClipper(probe=probe)
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(
        clipper,
        '_validate_stream_copy_boundaries',
        lambda _inspection, _path: (_ for _ in ()).throw(
            HighlightCutError('流复制边界错误')
        ),
    )
    monkeypatch.setattr(clipper, '_validate_transcoded_boundaries', validate_transcode)

    artifact = clipper.cut(inspection, str(output))

    assert artifact.strategy == 'transcoded_sequential'
    assert '快速转码起点仍然错误' in (artifact.fallback_reason or '')
    assert len(calls) == 3
    fast_input_index = calls[1].index('-i')
    sequential_input_index = calls[2].index('-i')
    assert calls[1].index('-ss') < fast_input_index
    assert all(
        index > sequential_input_index
        for index, value in enumerate(calls[2])
        if value == '-ss'
    )


def test_cut_transcodes_when_stream_copy_process_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    output = tmp_path / 'clip.mp4'
    source.write_bytes(b'video')

    def probe(path: str):
        duration_ms = 100_000 if path == str(source) else 5_000
        return profile(duration_ms=duration_ms), (0, 3_000)

    calls = []

    def run(command, **kwargs):
        calls.append(tuple(command))
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout=b'', stderr=b'copy failed')
        Path(command[-1]).write_bytes(b'transcoded')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', run)
    clipper = LosslessClipper(probe=probe)
    inspection = clipper.inspect(
        (ClipSource(1, str(source), 3_500, 8_000),),
        requested_start_ms=3_500,
        requested_end_ms=8_000,
        stable_end_ms=100_000,
    )

    artifact = clipper.cut(inspection, str(output))

    assert artifact.strategy == 'transcoded'
    assert 'copy failed' in (artifact.fallback_reason or '')
    assert output.read_bytes() == b'transcoded'
    assert len(calls) == 2


def test_cut_does_not_repeat_a_timed_out_ffmpeg_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    output = tmp_path / 'clip.mp4'
    source.write_bytes(b'video')
    calls = []

    def run(command, **kwargs):
        calls.append(tuple(command))
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr(subprocess, 'run', run)
    clipper = LosslessClipper(probe=lambda _path: (profile(), (0,)))
    inspection = clipper.inspect(
        (ClipSource(1, str(source), 0, 5_000),),
        requested_start_ms=0,
        requested_end_ms=5_000,
        stable_end_ms=100_000,
    )

    with pytest.raises(HighlightCutError, match='超时'):
        clipper.cut(inspection, str(output))

    assert len(calls) == 1
    assert not output.exists()


def test_cut_seeks_after_opening_a_growing_flv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'growing.flv'
    output = tmp_path / 'clip.mp4'
    source.write_bytes(b'video')

    def probe(path: str):
        if path == str(source):
            return profile(), (0, 28_600)
        return profile(duration_ms=51_400), (0,)

    calls = []

    def run(command, **kwargs):
        calls.append(tuple(command))
        Path(command[-1]).write_bytes(b'clip')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', run)
    clipper = LosslessClipper(probe=probe)
    inspection = clipper.inspect(
        (ClipSource(1, str(source), 30_000, 80_000, recording=True),),
        requested_start_ms=30_000,
        requested_end_ms=80_000,
        stable_end_ms=100_000,
    )

    clipper.cut(inspection, str(output))

    command = calls[0]
    assert command.index('-i') < command.index('-ss')


def test_cut_rejects_output_that_loses_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    output = tmp_path / 'clip.mp4'
    source.write_bytes(b'video')

    def probe(path: str):
        if path == str(source):
            return profile(), (0,)
        return profile(duration_ms=20_000, has_audio=False), (0,)

    def run(command, **kwargs):
        Path(command[-1]).write_bytes(b'clip')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', run)
    clipper = LosslessClipper(probe=probe)
    inspection = clipper.inspect(
        (ClipSource(1, str(source), 0, 20_000),),
        requested_start_ms=0,
        requested_end_ms=20_000,
        stable_end_ms=20_000,
    )

    with pytest.raises(HighlightCutError, match='音频'):
        clipper.cut(inspection, str(output))
    assert not output.exists()


def test_cut_reports_expected_and_actual_duration_on_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source.flv'
    output = tmp_path / 'clip.mp4'
    source.write_bytes(b'video')

    def probe(path: str):
        duration_ms = 100_000 if path == str(source) else 20_600
        return profile(duration_ms=duration_ms), (0,)

    def run(command, **kwargs):
        Path(command[-1]).write_bytes(b'clip')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', run)
    clipper = LosslessClipper(probe=probe)
    inspection = clipper.inspect(
        (ClipSource(1, str(source), 0, 20_000),),
        requested_start_ms=0,
        requested_end_ms=20_000,
        stable_end_ms=20_000,
    )

    with pytest.raises(HighlightCutError, match=r'计划 20\.000 秒，实际 20\.600 秒'):
        clipper.cut(inspection, str(output))
    assert not output.exists()


def test_cut_concatenates_compatible_sources_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / 'first.flv'
    second = tmp_path / 'second.flv'
    output = tmp_path / 'clip.mp4'
    first.write_bytes(b'first')
    second.write_bytes(b'second')

    def probe(path: str):
        if path in (str(first), str(second)):
            return profile(duration_ms=10_000), (0,)
        return profile(duration_ms=20_000), (0,)

    calls = []
    concat_document = []

    def run(command, **kwargs):
        calls.append((tuple(command), kwargs))
        if '-f' in command and command[command.index('-f') + 1] == 'concat':
            concat_path = Path(command[command.index('-i') + 1])
            concat_document.append(concat_path.read_text(encoding='utf8'))
        Path(command[-1]).write_bytes(b'clip')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', run)
    clipper = LosslessClipper(probe=probe)
    source_profile = profile(duration_ms=10_000)
    inspection = ClipInspection(
        sources=(
            InspectedClipSource(1, str(first), 0, 10_000, 0, source_profile),
            InspectedClipSource(2, str(second), 0, 10_000, 10_000, source_profile),
        ),
        requested_start_ms=0,
        requested_end_ms=20_000,
        actual_start_ms=0,
        actual_end_ms=20_000,
        extra_lead_ms=0,
        confirmation_required=False,
    )

    clipper.cut(inspection, str(output))

    assert len(calls) == 3
    concat_command, concat_options = calls[-1]
    assert ('-f', 'concat') == concat_command[
        concat_command.index('-f') : concat_command.index('-f') + 2
    ]
    assert ('-safe', '0') == concat_command[
        concat_command.index('-safe') : concat_command.index('-safe') + 2
    ]
    assert concat_options['shell'] is False
    assert concat_document and concat_document[0].count("file '") == 2


@pytest.mark.skipif(
    not os.environ.get('BLREC_HIGHLIGHT_FIXTURE'),
    reason='real FFmpeg fixture was not requested',
)
def test_real_ffmpeg_keeps_codecs_and_uses_stream_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(os.environ['BLREC_HIGHLIGHT_FIXTURE'])
    output = tmp_path / 'real-highlight.mp4'
    original_run = subprocess.run
    calls = []

    def run(command, **kwargs):
        calls.append(tuple(command))
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, 'run', run)
    clipper = LosslessClipper()
    inspection = clipper.inspect(
        (ClipSource(1, str(source), 5_000, 18_000),),
        requested_start_ms=5_000,
        requested_end_ms=18_000,
        stable_end_ms=40_000,
    )

    artifact = clipper.cut(inspection, str(output))

    ffmpeg_calls = [command for command in calls if command[0] == 'ffmpeg']
    assert ffmpeg_calls
    assert all(
        ('-c', 'copy') == command[command.index('-c') :][:2] for command in ffmpeg_calls
    )
    output_profile, _ = clipper._probe_media(str(output))
    assert output_profile.codec_name == inspection.sources[0].profile.codec_name
    assert output_profile.has_audio is inspection.sources[0].profile.has_audio
    assert artifact.duration_ms == output_profile.duration_ms
    assert abs(artifact.duration_ms - inspection.output_duration_ms) <= 2_000
