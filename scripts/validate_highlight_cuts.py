#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from blrec.bili_upload.highlight_cut import ClipSource, LosslessClipper

SUPPORTED_SUFFIXES = ('.flv', '.mp4')
_DURATION_MULTIPLIERS = (1, 2, 3, 4, 6)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Validate highlight cuts against read-only real recordings.'
    )
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--cases', type=int, default=300)
    parser.add_argument('--clip-duration-ms', type=int, default=5_000)
    parser.add_argument('--min-age-seconds', type=int, default=600)
    return parser.parse_args()


def _sources(root: Path, *, min_age_seconds: int) -> Tuple[Path, ...]:
    cutoff = time.time() - min_age_seconds
    candidates = []
    for path in root.rglob('*'):
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size <= 0 or stat.st_mtime > cutoff:
            continue
        candidates.append((stat.st_mtime_ns, str(path), path))
    candidates.sort(reverse=True)
    return tuple(item[2] for item in candidates)


def _case_start_ms(duration_ms: int, clip_duration_ms: int, round_index: int) -> int:
    latest_start_ms = duration_ms - clip_duration_ms - 250
    if round_index == 0:
        return min(1_100, latest_start_ms)
    fractions = (0.25, 0.5, 0.75, 0.9)
    fraction = fractions[(round_index - 1) % len(fractions)]
    return min(latest_start_ms, max(0, int(latest_start_ms * fraction)))


def _case_duration_ms(base_duration_ms: int, round_index: int) -> int:
    return (
        base_duration_ms
        * _DURATION_MULTIPLIERS[round_index % len(_DURATION_MULTIPLIERS)]
    )


def _decode(path: Path) -> None:
    result = subprocess.run(
        (
            'ffmpeg',
            '-v',
            'error',
            '-xerror',
            '-i',
            str(path),
            '-map',
            '0:v:0',
            '-map',
            '0:a?',
            '-f',
            'null',
            '-',
        ),
        capture_output=True,
        check=False,
        shell=False,
        timeout=300,
    )
    if result.returncode != 0:
        diagnostic = result.stderr.decode('utf8', errors='replace')[-500:]
        raise RuntimeError(
            'generated clip is not fully decodable: {}'.format(diagnostic)
        )


def _profile_sources(
    clipper: LosslessClipper, paths: Sequence[Path], clip_duration_ms: int
) -> Tuple[Tuple[Path, int], ...]:
    profiled = []
    for path in paths:
        profile, _keyframes = clipper._probe_media(str(path))
        if profile.duration_ms >= clip_duration_ms + 1_000:
            profiled.append((path, profile.duration_ms))
    return tuple(profiled)


def _sample_sources(
    sources: Sequence[Tuple[Path, int]], cases: int
) -> Tuple[Tuple[Path, int], ...]:
    positions_per_source = len(_DURATION_MULTIPLIERS)
    maximum_sources = max(1, cases // positions_per_source)
    if len(sources) <= maximum_sources:
        return tuple(sources)
    if maximum_sources == 1:
        return (sources[0],)
    last_index = len(sources) - 1
    indexes = (
        round(index * last_index / (maximum_sources - 1))
        for index in range(maximum_sources)
    )
    return tuple(sources[index] for index in indexes)


def _validate(args: argparse.Namespace) -> Dict[str, Any]:
    if args.cases <= 0 or args.clip_duration_ms <= 0 or args.min_age_seconds < 0:
        raise ValueError(
            'cases and clip duration must be positive; age cannot be negative'
        )
    source_root = args.source_root.resolve()
    output_root = args.output_root.resolve()
    if source_root == output_root:
        raise ValueError('output root must be separate from the source root')
    if not source_root.is_dir():
        raise ValueError('source root does not exist')
    output_root.mkdir(parents=True, exist_ok=True)

    started_at = time.monotonic()
    clipper = LosslessClipper()
    eligible_sources = _profile_sources(
        clipper,
        _sources(source_root, min_age_seconds=args.min_age_seconds),
        args.clip_duration_ms * max(_DURATION_MULTIPLIERS),
    )
    if not eligible_sources:
        raise RuntimeError('no eligible recording sources were found')
    sources = _sample_sources(eligible_sources, args.cases)

    strategies: Counter = Counter()
    fallback_reasons: Counter = Counter()
    failures: List[Dict[str, Any]] = []
    completed = 0
    run_prefix = 'highlight-validation-{}'.format(os.getpid())
    for case_index in range(args.cases):
        source, duration_ms = sources[case_index % len(sources)]
        round_index = case_index // len(sources)
        clip_duration_ms = _case_duration_ms(args.clip_duration_ms, round_index)
        requested_start_ms = _case_start_ms(duration_ms, clip_duration_ms, round_index)
        requested_end_ms = requested_start_ms + clip_duration_ms
        recording = False
        output = output_root / '{}-{:04d}.mp4'.format(run_prefix, case_index + 1)
        try:
            inspection = clipper.inspect(
                (
                    ClipSource(
                        case_index + 1,
                        str(source),
                        requested_start_ms,
                        requested_end_ms,
                        recording=recording,
                    ),
                ),
                requested_start_ms=requested_start_ms,
                requested_end_ms=requested_end_ms,
                stable_end_ms=duration_ms,
            )
            artifact = clipper.cut(inspection, str(output))
            _decode(output)
            strategies[artifact.strategy] += 1
            if artifact.fallback_reason:
                fallback_reasons[artifact.fallback_reason] += 1
            completed += 1
        except Exception as error:
            failures.append(
                {
                    'case': case_index + 1,
                    'source': str(source),
                    'requestedStartMs': requested_start_ms,
                    'requestedEndMs': requested_end_ms,
                    'recording': recording,
                    'error': '{}: {}'.format(type(error).__name__, error)[:1000],
                }
            )
        finally:
            try:
                output.unlink()
            except FileNotFoundError:
                pass
        if (case_index + 1) % 10 == 0:
            print(
                'validated {}/{} cases'.format(case_index + 1, args.cases),
                file=sys.stderr,
                flush=True,
            )

    return {
        'requestedCases': args.cases,
        'completedCases': completed,
        'failedCases': len(failures),
        'passRate': completed / args.cases,
        'eligibleSources': len(eligible_sources),
        'sampledSources': len(sources),
        'clipDurationsMs': [
            args.clip_duration_ms * multiplier for multiplier in _DURATION_MULTIPLIERS
        ],
        'strategies': dict(strategies),
        'fallbackReasons': dict(fallback_reasons),
        'failures': failures,
        'elapsedSeconds': round(time.monotonic() - started_at, 3),
    }


def main() -> int:
    try:
        summary = _validate(_parse_args())
    except Exception as error:
        print(
            json.dumps(
                {'fatalError': '{}: {}'.format(type(error).__name__, error)},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary['failedCases'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
