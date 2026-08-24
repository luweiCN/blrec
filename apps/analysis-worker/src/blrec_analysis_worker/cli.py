from __future__ import annotations

import argparse
import importlib
import json
import os
import socket
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from loguru import logger

from blrec.vainglory.analyzer import AnalyzedMatch, VaingloryVideoAnalyzer, VideoPart
from blrec.vainglory.glm_ocr import GlmOcrClient, GlmOcrResultReader
from blrec.vainglory.sampling import FfmpegSampler

from .model_package import ModelPackage, build_package_runtime, load_model_package
from .remote import (
    AnalysisWorkerClient,
    RemoteAnalysisWorker,
    WorkerConcurrency,
    load_worker_token,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='blrec-analysis-worker')
    commands = parser.add_subparsers(dest='command', required=True)
    scan = commands.add_parser('scan-file', help='使用独立 Worker 扫描一个本地视频文件')
    scan.add_argument('video', type=Path)
    scan.add_argument(
        '--execution-provider', choices=('auto', 'cpu', 'coreml'), default='auto'
    )
    scan.add_argument('--scan-only', action='store_true')
    scan.add_argument('--ocr-url', default=None)
    scan.add_argument('--frames-dir', type=Path, default=None)
    scan.add_argument('--debug-dir', type=Path, default=None)
    scan.add_argument('--json-output', type=Path, default=None)
    scan.add_argument(
        '--model-package',
        type=Path,
        default=(
            Path(os.environ['BLREC_VISION_MODEL_PACKAGE'])
            if os.environ.get('BLREC_VISION_MODEL_PACKAGE')
            else None
        ),
        help='已经验收并解压的版本化视觉模型包目录',
    )
    run = commands.add_parser('run', help='从 NAS 领取并处理对局分析任务')
    run.add_argument('--server', default=os.environ.get('BLREC_SERVER_URL', ''))
    run.add_argument('--token-file', type=Path, default=None)
    run.add_argument(
        '--execution-provider', choices=('auto', 'cpu', 'coreml'), default='auto'
    )
    run.add_argument('--ocr-url', default=None)
    run.add_argument(
        '--cache-dir', type=Path, default=Path('~/Library/Caches/BLRECAnalysisWorker')
    )
    run.add_argument('--debug-dir', type=Path, default=None)
    run.add_argument('--poll-seconds', type=float, default=5)
    run.add_argument('--concurrency', type=int, default=1)
    run.add_argument(
        '--worker-id',
        default=os.environ.get('BLREC_ANALYSIS_WORKER_ID', socket.gethostname()),
        help='管理后台登记的 Worker ID；默认使用当前机器名',
    )
    run.add_argument('--once', action='store_true')
    run.add_argument(
        '--model-package',
        type=Path,
        default=(
            Path(os.environ['BLREC_VISION_MODEL_PACKAGE'])
            if os.environ.get('BLREC_VISION_MODEL_PACKAGE')
            else None
        ),
        help='已经验收并解压的版本化视觉模型包目录',
    )
    return parser


def _execution_providers(name: str) -> Tuple[str, ...]:
    onnxruntime: Any = importlib.import_module('onnxruntime')
    available = set(onnxruntime.get_available_providers())
    if name == 'coreml' and 'CoreMLExecutionProvider' not in available:
        raise RuntimeError('当前 ONNX Runtime 不支持 CoreML')
    if name == 'coreml' or (name == 'auto' and 'CoreMLExecutionProvider' in available):
        return ('CoreMLExecutionProvider', 'CPUExecutionProvider')
    return ('CPUExecutionProvider',)


def _build_analyzer(
    *,
    ocr_url: str,
    providers: Sequence[str],
    model_package_path: Optional[Path],
    trusted_remote_origin: Optional[str] = None,
) -> Tuple[VaingloryVideoAnalyzer, ModelPackage]:
    if model_package_path is None:
        raise ValueError(
            '必须通过 --model-package 或 BLREC_VISION_MODEL_PACKAGE 指定已验收模型包'
        )
    package = load_model_package(model_package_path)
    runtime = build_package_runtime(package, providers=providers)
    sampler = FfmpegSampler(
        coarse_interval_seconds=max(1, package.runtime.coarse_interval_ms // 1_000),
        fine_frames_per_second=package.runtime.result_scan_fps,
        maximum_keyframe_distance_ms=package.runtime.maximum_keyframe_distance_ms,
        trusted_remote_origin=trusted_remote_origin,
    )
    logger.info(
        '已加载视觉模型包：package_id={} pipeline_version={} models={}',
        package.package_id,
        package.pipeline_version,
        ','.join(sorted(package.models)),
    )
    result_reader = None if not ocr_url else GlmOcrResultReader(GlmOcrClient(ocr_url))
    return (
        VaingloryVideoAnalyzer(
            sampler=sampler,
            result_reader=result_reader,
            hero_recognizer=runtime.hero_recognizer,
            hero_avatar_detector=runtime.hero_avatar_detector,
            recorded_player_detector=runtime.recorded_player_detector,
            result_panel_detector=runtime.result_panel_detector,
            stage_classifier=runtime.stage_classifier,
            match_mode_classifier=runtime.classifiers['match_mode'],
        ),
        package,
    )


def _match_payload(
    match: AnalyzedMatch, *, index: int, frames_dir: Optional[Path]
) -> Dict[str, Any]:
    frame_path: Optional[Path] = None
    if frames_dir is not None and match.result_frame_png:
        frames_dir.mkdir(parents=True, exist_ok=True)
        frame_path = frames_dir / 'match-{:02d}-{}.png'.format(
            index, match.result_at_ms
        )
        frame_path.write_bytes(match.result_frame_png)
    duration_seconds = match.ocr.header.duration_seconds
    return {
        'index': index,
        'result_at_ms': match.result_at_ms,
        'estimated_start_ms': (
            None
            if duration_seconds is None
            else max(0, match.result_at_ms - duration_seconds * 1_000)
        ),
        'duration_seconds': duration_seconds,
        'result_text': match.ocr.header.result_text,
        'team_size': match.layout.team_size,
        'game_mode': match.game_mode,
        'confidence': round(match.confidence, 4),
        'heroes': [hero.label for hero in match.heroes],
        'stats_eligible': match.stats_eligible,
        'stats_exclusion_reason': match.stats_exclusion_reason,
        'result_frame': None if frame_path is None else str(frame_path.resolve()),
    }


def _scan_file(arguments: argparse.Namespace) -> int:
    video = arguments.video.expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(video)
    ocr_url = (
        arguments.ocr_url
        if arguments.ocr_url is not None
        else os.environ.get('BLREC_VAINGLORY_OCR_URL', '')
    ).strip()
    run_started = time.monotonic()
    setup_started = time.monotonic()
    providers = _execution_providers(arguments.execution_provider)
    analyzer, _package = _build_analyzer(
        ocr_url=ocr_url, providers=providers, model_package_path=arguments.model_package
    )
    setup_seconds = time.monotonic() - setup_started
    part = VideoPart(id=0, index=1, path=str(video), title=video.name)
    last_percent = -5

    def report(progress: float) -> None:
        nonlocal last_percent
        percent = min(100, max(0, int(progress * 100)))
        if percent < last_percent + 5 and percent < 100:
            return
        last_percent = percent
        print('全量扫描进度：{}%'.format(percent), file=sys.stderr, flush=True)

    dense = analyzer.scan_part_cascade(
        part, progress=report, debug_dir=arguments.debug_dir
    )
    recognition_started = time.monotonic()
    matches = (
        ()
        if arguments.scan_only
        else analyzer.recognize_scanned_part(part, dense.scanned_part)
    )
    recognition_seconds = time.monotonic() - recognition_started
    total_seconds = time.monotonic() - run_started
    duration_seconds = dense.scanned_part.video_duration_ms / 1_000
    projected_minutes_per_video_hour = (
        total_seconds / duration_seconds * 60 if duration_seconds else 0
    )
    payload: Dict[str, Any] = {
        'strategy': 'timeline-v2',
        'model_package_id': dense.model_package_id,
        'execution_providers': list(providers),
        'video': str(video),
        'video_duration_ms': dense.scanned_part.video_duration_ms,
        'decoded_frames': dense.decoded_frames,
        'result_frames': dense.result_frames,
        'candidate_count': len(dense.scanned_part.candidate_times_ms),
        'candidate_times_ms': list(dense.scanned_part.candidate_times_ms),
        'timeline_sampled_frames': len(dense.timeline_points),
        'keyframe_frames': dense.keyframe_frames,
        'seek_fill_frames': dense.seek_fill_frames,
        'timeline_segments': [
            {
                'start_ms': segment.start_ms,
                'end_ms': segment.end_ms,
                'mode': segment.mode,
            }
            for segment in dense.timeline_segments
        ],
        'result_windows': [
            {
                'start_ms': window.start_ms,
                'end_ms': window.end_ms,
                'focus_ms': window.focus_ms,
                'mode': window.mode,
            }
            for window in dense.result_windows
        ],
        'training_candidate_counts': dict(
            sorted(Counter(item.task for item in dense.training_candidates).items())
        ),
        'match_count': len(matches),
        'timings_seconds': {
            'setup': round(setup_seconds, 3),
            'probe': round(dense.probe_seconds, 3),
            'decode': round(dense.decode_seconds, 3),
            'result_detection': round(dense.detection_seconds, 3),
            'dense_scan': round(dense.total_seconds, 3),
            'recognition': round(recognition_seconds, 3),
            'total': round(total_seconds, 3),
        },
        'sampled_frames_per_second': round(
            dense.decoded_frames / max(0.001, dense.total_seconds), 3
        ),
        'projected_minutes_per_video_hour': round(projected_minutes_per_video_hour, 3),
        'matches': [
            _match_payload(match, index=index, frames_dir=arguments.frames_dir)
            for index, match in enumerate(matches, 1)
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    if arguments.json_output is not None:
        output = arguments.json_output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized + '\n', encoding='utf8')
    print(serialized)
    return 0


def _run_remote_worker(arguments: argparse.Namespace) -> int:
    server_url = str(arguments.server).strip()
    if not server_url.startswith(('http://', 'https://')):
        raise ValueError('必须通过 --server 指定 NAS 的 HTTP(S) 地址')
    ocr_url = (
        arguments.ocr_url
        if arguments.ocr_url is not None
        else os.environ.get('BLREC_VAINGLORY_OCR_URL', '')
    ).strip()
    if not ocr_url:
        raise ValueError('Mac Worker 必须配置本机 OCR 服务地址')
    if not 1 <= arguments.concurrency <= 8:
        raise ValueError('并发任务数必须在 1 到 8 之间')
    worker_id = str(arguments.worker_id).strip()
    if not worker_id:
        raise ValueError('Worker ID 不能为空')
    token = load_worker_token(arguments.token_file)
    providers = _execution_providers(arguments.execution_provider)
    parsed_server = urlsplit(server_url)
    trusted_remote_origin = '{}://{}'.format(parsed_server.scheme, parsed_server.netloc)
    analyzer, package = _build_analyzer(
        ocr_url=ocr_url,
        providers=providers,
        model_package_path=arguments.model_package,
        trusted_remote_origin=trusted_remote_origin,
    )
    concurrency = WorkerConcurrency(arguments.concurrency)
    RemoteAnalysisWorker(
        lambda: AnalysisWorkerClient(
            server_url,
            token,
            worker_id=worker_id,
            model_package_id=package.package_id,
            pipeline_version=package.pipeline_version,
            concurrency=arguments.concurrency,
            concurrency_provider=concurrency.get,
        ),
        analyzer,
        cache_dir=arguments.cache_dir,
        poll_seconds=arguments.poll_seconds,
        concurrency=arguments.concurrency,
        concurrency_state=concurrency,
        worker_id=worker_id,
        debug_dir=arguments.debug_dir,
    ).run(once=arguments.once)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    logger.remove()
    logger.add(sys.stderr, level='INFO')
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == 'scan-file':
            return _scan_file(arguments)
        if arguments.command == 'run':
            return _run_remote_worker(arguments)
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        print(
            json.dumps(
                {'ok': False, 'error': '{}: {}'.format(type(error).__name__, error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
