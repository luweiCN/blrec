from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Sequence, Tuple

import pytest

from blrec.bili_upload.highlight_cut import ClipSource, LosslessClipper

_SOURCE_NAMES = (
    'mp4-gop3-b2-audio',
    'mp4-fragmented-gop3-b2-audio',
    'mp4-gop15-b0-audio',
    'mp4-hevc-gop2-b2-audio',
    'flv-default-gop3-b2-audio',
    'flv-live-gop3-b2-audio',
    'flv-indexed-gop3-b2-audio',
    'flv-live-gop2-b0-audio',
    'mp4-gop2-b3-video-only',
    'flv-live-gop2-b3-video-only',
)

_RANGES = (
    (500, 5_200),
    (1_100, 6_700),
    (3_000, 8_500),
    (3_100, 11_700),
    (8_900, 15_400),
    (29_100, 35_100),
    (31_100, 38_900),
    (40_100, 44_400),
)


def _run(command: Sequence[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        tuple(command), capture_output=True, check=True, shell=False, timeout=timeout
    )


def _generate_source(
    path: Path,
    *,
    rate: int,
    gop: int,
    b_frames: int,
    audio: bool,
    video_codec: str = 'libx264',
) -> None:
    command = [
        'ffmpeg',
        '-v',
        'error',
        '-f',
        'lavfi',
        '-i',
        'testsrc2=size=160x90:rate={}'.format(rate),
    ]
    if audio:
        command.extend(('-f', 'lavfi', '-i', 'sine=frequency=997:sample_rate=48000'))
    command.extend(
        (
            '-t',
            '45',
            '-c:v',
            video_codec,
            '-preset',
            'ultrafast',
            '-g',
            str(gop),
            '-keyint_min',
            str(gop),
            '-sc_threshold',
            '0',
            '-bf',
            str(b_frames),
            '-pix_fmt',
            'yuv420p',
        )
    )
    if video_codec == 'libx265':
        command.extend(('-x265-params', 'log-level=error'))
    if audio:
        command.extend(('-c:a', 'aac', '-b:a', '96k'))
    else:
        command.append('-an')
    command.extend(('-y', str(path)))
    _run(command)


def _remux(source: Path, output: Path, *options: str) -> None:
    _run(
        (
            'ffmpeg',
            '-v',
            'error',
            '-i',
            str(source),
            '-c',
            'copy',
            *options,
            '-y',
            str(output),
        )
    )


@pytest.fixture(scope='module')
def generated_sources(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    if os.environ.get('BLREC_RUN_HIGHLIGHT_MEDIA_TESTS') != '1':
        pytest.skip('real FFmpeg matrix is run by the dedicated CI release gate')
    missing = [name for name in ('ffmpeg', 'ffprobe') if shutil.which(name) is None]
    if missing:
        pytest.fail('required media tools are missing: {}'.format(', '.join(missing)))

    root = tmp_path_factory.mktemp('highlight-media-matrix')
    standard = root / 'standard.mp4'
    no_b_frames = root / 'no-b-frames.mp4'
    video_only = root / 'video-only.mp4'
    long_gop = root / 'long-gop.mp4'
    hevc = root / 'hevc.mp4'
    _generate_source(standard, rate=30, gop=90, b_frames=2, audio=True)
    _generate_source(no_b_frames, rate=25, gop=50, b_frames=0, audio=True)
    _generate_source(video_only, rate=60, gop=120, b_frames=3, audio=False)
    _generate_source(long_gop, rate=30, gop=450, b_frames=0, audio=True)
    _generate_source(
        hevc, rate=30, gop=60, b_frames=2, audio=True, video_codec='libx265'
    )

    sources = {
        'mp4-gop3-b2-audio': standard,
        'mp4-fragmented-gop3-b2-audio': root / 'fragmented.mp4',
        'mp4-gop15-b0-audio': long_gop,
        'mp4-hevc-gop2-b2-audio': hevc,
        'flv-default-gop3-b2-audio': root / 'default.flv',
        'flv-live-gop3-b2-audio': root / 'live.flv',
        'flv-indexed-gop3-b2-audio': root / 'indexed.flv',
        'flv-live-gop2-b0-audio': root / 'no-b-frames.flv',
        'mp4-gop2-b3-video-only': video_only,
        'flv-live-gop2-b3-video-only': root / 'video-only.flv',
        'flv-delayed-video-start': root / 'delayed-video-start.flv',
    }
    _remux(
        standard,
        sources['mp4-fragmented-gop3-b2-audio'],
        '-movflags',
        '+frag_keyframe+empty_moov',
    )
    _remux(standard, sources['flv-default-gop3-b2-audio'])
    _remux(
        standard,
        sources['flv-live-gop3-b2-audio'],
        '-flvflags',
        'no_metadata+no_duration_filesize+no_sequence_end',
    )
    _remux(
        standard,
        sources['flv-indexed-gop3-b2-audio'],
        '-flvflags',
        'add_keyframe_index',
    )
    _remux(
        no_b_frames,
        sources['flv-live-gop2-b0-audio'],
        '-flvflags',
        'no_metadata+no_duration_filesize+no_sequence_end',
    )
    _remux(
        video_only,
        sources['flv-live-gop2-b3-video-only'],
        '-flvflags',
        'no_metadata+no_duration_filesize+no_sequence_end',
    )
    _run(
        (
            'ffmpeg',
            '-v',
            'error',
            '-itsoffset',
            '1.926',
            '-f',
            'lavfi',
            '-i',
            'testsrc2=size=160x90:rate=30',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=997:sample_rate=48000',
            '-t',
            '12',
            '-map',
            '0:v:0',
            '-map',
            '1:a:0',
            '-c:v',
            'libx264',
            '-preset',
            'ultrafast',
            '-g',
            '90',
            '-keyint_min',
            '90',
            '-sc_threshold',
            '0',
            '-bf',
            '2',
            '-pix_fmt',
            'yuv420p',
            '-c:a',
            'aac',
            '-b:a',
            '96k',
            '-flvflags',
            'no_metadata+no_duration_filesize+no_sequence_end',
            '-y',
            str(sources['flv-delayed-video-start']),
        )
    )
    return sources


@pytest.mark.parametrize('source_name', _SOURCE_NAMES)
@pytest.mark.parametrize('requested_range', _RANGES)
@pytest.mark.parametrize('recording', (False, True), ids=('finalized', 'recording'))
def test_real_ffmpeg_cut_matrix_is_playable_and_keeps_its_contract(
    generated_sources: Dict[str, Path],
    source_name: str,
    requested_range: Tuple[int, int],
    recording: bool,
) -> None:
    source = generated_sources[source_name]
    requested_start_ms, requested_end_ms = requested_range
    output = source.parent / '{}-{}-{}-{}.clip.mp4'.format(
        source_name, requested_start_ms, requested_end_ms, int(recording)
    )
    clipper = LosslessClipper()
    inspection = clipper.inspect(
        (
            ClipSource(
                1,
                str(source),
                requested_start_ms,
                requested_end_ms,
                recording=recording,
            ),
        ),
        requested_start_ms=requested_start_ms,
        requested_end_ms=requested_end_ms,
        stable_end_ms=44_500,
    )

    artifact = clipper.cut(inspection, str(output))

    assert artifact.path == str(output)
    assert artifact.size_bytes > 0
    assert artifact.strategy in ('stream_copy', 'transcoded', 'transcoded_sequential')
    assert abs(artifact.duration_ms - inspection.output_duration_ms) <= 500
    if artifact.strategy != 'stream_copy':
        assert artifact.fallback_reason
    output_profile, _keyframes = clipper._probe_media(str(output))
    assert output_profile.codec_name == 'h264'
    assert output_profile.has_audio is inspection.sources[0].profile.has_audio
    _run(
        (
            'ffmpeg',
            '-v',
            'error',
            '-xerror',
            '-i',
            str(output),
            '-map',
            '0:v:0',
            '-map',
            '0:a?',
            '-f',
            'null',
            '-',
        )
    )


def _raw_frame(path: Path, seconds: float) -> bytes:
    return _run(
        (
            'ffmpeg',
            '-v',
            'error',
            '-i',
            str(path),
            '-ss',
            '{:.3f}'.format(max(0.0, seconds)),
            '-frames:v',
            '1',
            '-pix_fmt',
            'rgb24',
            '-f',
            'rawvideo',
            'pipe:1',
        )
    ).stdout


def _mean_absolute_error(first: bytes, second: bytes) -> float:
    assert first and len(first) == len(second)
    return sum(abs(left - right) for left, right in zip(first, second)) / len(first)


@pytest.mark.parametrize('source_name', _SOURCE_NAMES)
def test_real_ffmpeg_output_keeps_visual_start_and_end(
    generated_sources: Dict[str, Path], source_name: str
) -> None:
    source = generated_sources[source_name]
    output = source.parent / '{}-visual.clip.mp4'.format(source_name)
    clipper = LosslessClipper()
    inspection = clipper.inspect(
        (ClipSource(1, str(source), 1_100, 6_700),),
        requested_start_ms=1_100,
        requested_end_ms=6_700,
        stable_end_ms=44_500,
    )

    clipper.cut(inspection, str(output))

    planned_seconds = inspection.output_duration_ms / 1000.0
    source_start_seconds = inspection.sources[0].actual_start_ms / 1000.0
    source_end_seconds = inspection.sources[0].actual_end_ms / 1000.0
    assert (
        _mean_absolute_error(
            _raw_frame(output, 0.0), _raw_frame(source, source_start_seconds)
        )
        < 15.0
    )
    assert (
        _mean_absolute_error(
            _raw_frame(output, planned_seconds - 0.1),
            _raw_frame(source, source_end_seconds - 0.1),
        )
        < 15.0
    )


def test_real_ffmpeg_cut_accepts_audio_before_first_video_keyframe(
    generated_sources: Dict[str, Path]
) -> None:
    source = generated_sources['flv-delayed-video-start']
    output = source.parent / 'delayed-video-start.clip.mp4'
    clipper = LosslessClipper()

    inspection = clipper.inspect(
        (ClipSource(1, str(source), 1_100, 6_100),),
        requested_start_ms=1_100,
        requested_end_ms=6_100,
        stable_end_ms=11_500,
    )
    artifact = clipper.cut(inspection, str(output))

    assert 1_900 <= inspection.actual_start_ms <= 2_000
    assert inspection.extra_lead_ms == 0
    assert artifact.strategy in ('stream_copy', 'transcoded', 'transcoded_sequential')
    assert abs(artifact.duration_ms - inspection.output_duration_ms) <= 500
    _run(('ffmpeg', '-v', 'error', '-xerror', '-i', str(output), '-f', 'null', '-'))


def test_real_ffmpeg_legacy_multi_source_output_is_playable(
    generated_sources: Dict[str, Path]
) -> None:
    source = generated_sources['mp4-gop3-b2-audio']
    output = source.parent / 'legacy-multi-source.clip.mp4'
    clipper = LosslessClipper()
    inspection = clipper.inspect_legacy(
        (
            ClipSource(1, str(source), 3_100, 8_700),
            ClipSource(2, str(source), 1_100, 6_700),
        ),
        requested_start_ms=3_100,
        requested_end_ms=14_300,
        stable_end_ms=14_300,
    )

    artifact = clipper.cut(inspection, str(output))

    assert artifact.size_bytes > 0
    assert abs(artifact.duration_ms - inspection.output_duration_ms) <= 500
    _run(('ffmpeg', '-v', 'error', '-xerror', '-i', str(output), '-f', 'null', '-'))
