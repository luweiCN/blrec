"""虚荣视觉标注工作台 —— FastAPI 入口。

启动:  .venv/bin/python -m labeler.server
打开:  http://127.0.0.1:8800
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import urllib.request
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, bp_review, config, db, events
from . import export as export_mod
from . import hero_review
from . import inference as inference_mod
from . import local, managed_assets, model_prefill, model_testing, result_archive
from . import stats as stats_mod
from . import (
    training,
    training_review,
    vision_jobs,
    worker_candidates,
    worker_deployment,
)
from .extract import (
    cancel_extraction,
    extract_videos_multi,
    live_next_frame,
    sync_videos,
    task_state,
)
from .nas import NasClient


class NoCacheStaticFiles(StaticFiles):
    """开发工具:静态文件不缓存,避免前端改了不生效。"""

    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        resp = super().file_response(*args, **kwargs)
        resp.headers['Cache-Control'] = 'no-store'
        return resp


@asynccontextmanager
async def lifespan(_app: FastAPI):
    candidate_index_stop = threading.Event()
    candidate_index_thread = None
    # 启动时恢复遗留状态:上次进程被中断的任务标记为可重试
    try:
        conn = db.connect(config.DB_PATH)
        training_review.migrate_legacy_training_reviews(conn)
        training_review.queue_legacy_pending_reviews(conn)
        conn.execute(
            "UPDATE extraction_jobs SET status='failed', "
            "error=COALESCE(error,'') || '; server restart' "
            "WHERE status='running'"
        )
        conn.execute(
            "UPDATE videos SET status='pending', "
            "error='抽帧被服务重启中断,请重新抽帧' "
            "WHERE status='extracting'"
        )
        conn.execute(
            "UPDATE training_runs SET status='interrupted', "
            "error='旧版本机训练被服务重启中断', finished_at=? "
            "WHERE status = 'running' AND NOT EXISTS ("
            "SELECT 1 FROM vision_jobs job "
            "WHERE job.kind = 'train_model' "
            "AND job.related_id = training_runs.id "
            "AND job.status IN ('queued', 'running'))",
            (db.now(),),
        )
        db.fail_interrupted_model_deployments(conn)
        conn.commit()
        conn.close()
        if config.CONTROL_PLANE_ONLY:
            if (
                config.CANDIDATE_LOCAL_DIR is not None
                and config.CANDIDATE_RECONCILIATION_ENABLED
            ):
                candidate_index_thread = threading.Thread(
                    target=_candidate_index_loop,
                    args=(candidate_index_stop,),
                    daemon=True,
                    name='vision-candidate-index',
                )
                candidate_index_thread.start()
    except Exception:
        db.close_connections()
        raise
    try:
        yield
    finally:
        candidate_index_stop.set()
        if candidate_index_thread is not None:
            candidate_index_thread.join(timeout=5)
        db.close_connections()


app = FastAPI(title='虚荣视觉标注工作台', version=__version__, lifespan=lifespan)

_db_lock = threading.RLock()
_sync_state: Dict[str, Any] = {'running': False, 'error': None, 'videos': 0}
_bp_collect_lock = threading.RLock()
_bp_collect_state: Dict[str, Any] = {
    'running': False,
    'model': '',
    'scanned': 0,
    'total': 0,
    'selected': 0,
    'inserted': 0,
    'failed': 0,
    'error': None,
}
_worker_candidate_sync_lock = threading.RLock()
_worker_candidate_state_last_persisted_at = 0.0
_worker_candidate_sync_state: Dict[str, Any] = {
    'running': False,
    'total': 0,
    'processed': 0,
    'inserted': 0,
    'updated': 0,
    'unchanged': 0,
    'downloaded': 0,
    'failed': 0,
    'last_error': '',
    'reviews_pulled': 0,
    'reviews_pushed': 0,
    'review_conflicts': 0,
    'push_failed': 0,
    'archive_total': 0,
    'archive_processed': 0,
    'archive_inserted': 0,
    'archive_updated': 0,
    'archive_downloaded': 0,
    'archive_failed': 0,
    'error': None,
}
_training_start_lock = threading.RLock()
_worker_deployment_lock = threading.RLock()
_training_review_cache_lock = threading.RLock()
_training_review_stats_compute_lock = threading.Lock()
_training_review_cache: Dict[str, Any] = {
    'groups': None,
    'groups_expires_at': 0.0,
    'stats': None,
    'stats_expires_at': 0.0,
    'default_queue': None,
    'default_queue_expires_at': 0.0,
}
_TRAINING_REVIEW_CACHE_SECONDS = 300.0
_worker_candidate_state_response_lock = threading.Lock()
_worker_candidate_state_response: Dict[str, Any] = {'value': None, 'expires_at': 0.0}
_WORKER_CANDIDATE_STATE_CACHE_SECONDS = 10.0


def _conn():
    return db.connect(config.DB_PATH)


@contextmanager
def _training_review_read_guard():
    if config.DATABASE_URL:
        yield
        return
    with _db_lock:
        yield


def _single_training_review_item(conn: Any, frame_id: int) -> Optional[Dict[str, Any]]:
    with _training_review_cache_lock:
        cached_groups = _training_review_cache['groups']
    return db.get_training_review_item(
        conn,
        frame_id,
        result_groups=cached_groups if isinstance(cached_groups, dict) else {},
    )


def _cached_training_review_groups(
    conn: Any, *, allow_partial_index: bool = False
) -> Dict[int, Dict[str, Any]]:
    if allow_partial_index or db.training_review_material_index_complete(conn):
        return db.training_review_result_groups(
            conn, allow_partial_index=allow_partial_index
        )
    now = time.monotonic()
    with _training_review_cache_lock:
        value = _training_review_cache['groups']
        if value is not None and now < _training_review_cache['groups_expires_at']:
            return value
    value = db.training_review_result_groups(conn)
    with _training_review_cache_lock:
        _training_review_cache['groups'] = value
        _training_review_cache['groups_expires_at'] = (
            time.monotonic() + _TRAINING_REVIEW_CACHE_SECONDS
        )
        return value


def _cached_training_review_stats(conn: Any) -> Dict[str, Any]:
    now = time.monotonic()
    with _training_review_cache_lock:
        value = _training_review_cache['stats']
        if value is not None and now < _training_review_cache['stats_expires_at']:
            return value
    with _training_review_stats_compute_lock:
        now = time.monotonic()
        with _training_review_cache_lock:
            value = _training_review_cache['stats']
            if value is not None and now < _training_review_cache['stats_expires_at']:
                return value
        groups = _cached_training_review_groups(conn)
        value = db.training_review_stats(
            conn, result_groups=groups, include_material_suggestions=False
        )
        with _training_review_cache_lock:
            _training_review_cache['stats'] = value
            _training_review_cache['stats_expires_at'] = (
                time.monotonic() + _TRAINING_REVIEW_CACHE_SECONDS
            )
            return value


def _cached_training_review_material_suggestions(conn: Any) -> List[Dict[str, Any]]:
    return db.training_review_material_suggestions(
        conn, hero_catalog=hero_review.hero_catalog()
    )


def _cached_default_training_review_queue(
    conn: Any, result_groups: Dict[int, Dict[str, Any]]
) -> tuple[int, ...]:
    if db.training_review_material_index_complete(conn):
        return tuple(
            db.training_review_frame_ids(
                conn,
                status='needs_review',
                source_scope='new',
                prefill_ready_only=True,
                result_groups=result_groups,
            )
        )
    now = time.monotonic()
    with _training_review_cache_lock:
        value = _training_review_cache['default_queue']
        if (
            value is not None
            and now < _training_review_cache['default_queue_expires_at']
        ):
            return value
    value = tuple(
        db.training_review_frame_ids(
            conn,
            status='needs_review',
            source_scope='new',
            prefill_ready_only=True,
            result_groups=result_groups,
        )
    )
    with _training_review_cache_lock:
        _training_review_cache['default_queue'] = value
        _training_review_cache['default_queue_expires_at'] = (
            time.monotonic() + _TRAINING_REVIEW_CACHE_SECONDS
        )
        return value


def _invalidate_training_review_cache() -> None:
    with _training_review_cache_lock:
        _training_review_cache['groups'] = None
        _training_review_cache['stats'] = None
        _training_review_cache['default_queue'] = None
        _training_review_cache['groups_expires_at'] = 0.0
        _training_review_cache['stats_expires_at'] = 0.0
        _training_review_cache['default_queue_expires_at'] = 0.0


def _mark_training_review_saved(frame_id: int) -> None:
    with _training_review_cache_lock:
        queue = _training_review_cache['default_queue']
        if queue is not None:
            _training_review_cache['default_queue'] = tuple(
                value for value in queue if value != int(frame_id)
            )
        _training_review_cache['stats'] = None
        _training_review_cache['stats_expires_at'] = 0.0


def _nas() -> NasClient:
    return NasClient()


def _require_vision_worker(request: Request) -> None:
    configured = config.VISION_WORKER_TOKEN
    if not configured:
        raise HTTPException(503, '尚未配置 Vision Worker token')
    authorization = request.headers.get('authorization', '')
    scheme, _, supplied = authorization.partition(' ')
    if scheme.lower() != 'bearer' or not secrets.compare_digest(
        supplied.strip(), configured
    ):
        raise HTTPException(401, 'Vision Worker token 无效')


def _require_local_heavy_operation(name: str) -> None:
    if config.CONTROL_PLANE_ONLY:
        raise HTTPException(
            409, f'{name} 已禁止在 NAS 控制面直接运行，请从 Vision Worker 任务入口执行'
        )


# ---------- 配置与任务 ----------


@app.get('/api/config')
def api_config() -> Dict[str, Any]:
    return {
        'version': __version__,
        'content_families': config.CONTENT_FAMILIES,
        'non_vainglory_types': config.NON_VAINGLORY_TYPES,
        'game_stages': config.GAME_STAGES,
        'stage_screen_types': config.STAGE_SCREEN_TYPES,
        'annotation_statuses': config.ANNOTATION_STATUSES,
        'game_modes': config.GAME_MODES,
        'match_kinds': config.MATCH_KINDS,
        'view_contexts': config.VIEW_CONTEXTS,
        'quality_flags': config.QUALITY_FLAGS,
        'black_bars': config.BLACK_BARS,
        'ocr_usable': config.OCR_USABLE,
        'result_clarity': config.RESULT_CLARITY,
        'panel_render_states': config.PANEL_RENDER_STATES,
        'hero_select_visibility': config.HERO_SELECT_VISIBILITY,
        'result_occlusion': config.RESULT_OCCLUSION,
        'occluder_types': config.OCCLUDER_TYPES,
        'box_types': config.BOX_TYPES,
        'strategies': config.STRATEGIES,
        'scoreboard_vs_result_hint': config.SCOREBOARD_VS_RESULT_HINT,
        'nas_rec_dir': config.NAS_REC_DIR,
        'control_plane_only': config.CONTROL_PLANE_ONLY,
        'database_backend': 'postgresql' if config.DATABASE_URL else 'sqlite',
        'database_schema': (config.DATABASE_SCHEMA if config.DATABASE_URL else ''),
    }


@app.get('/api/tasks')
def api_tasks() -> List[Dict[str, Any]]:
    with _db_lock:
        conn = _conn()
        try:
            return [
                dict(r)
                for r in conn.execute(
                    'SELECT * FROM annotation_tasks ORDER BY id'
                ).fetchall()
            ]
        finally:
            conn.close()


# ---------- 视频 ----------


@app.get('/api/videos/auto-pick')
def api_auto_pick(
    per_streamer: int = Query(5, ge=1, le=20),
    min_size_bytes: Optional[int] = Query(1073741824),
    strategy: Optional[str] = None,
) -> Dict[str, Any]:
    """自动挑选:每个主播取 N 个 >= 阈值的视频(按大小降序,优先完整场次)。"""
    with _db_lock:
        conn = _conn()
        try:
            videos = db.list_videos(conn, min_size_bytes=min_size_bytes)
            by_streamer: Dict[str, List[Dict[str, Any]]] = {}
            for v in videos:
                by_streamer.setdefault(v['streamer'], []).append(v)
            picks: List[Dict[str, Any]] = []
            for streamer, vs in sorted(by_streamer.items()):
                vs.sort(key=lambda x: -x['size_bytes'])
                for v in vs[:per_streamer]:
                    picks.append(v)
            total_frames = sum(
                int(v['duration_seconds'] / 5) + 1 if v['duration_seconds'] else 0
                for v in picks
            )
            return {
                'video_ids': [v['id'] for v in picks],
                'videos': len(picks),
                'streamers': len(by_streamer),
                'picks': [
                    {
                        'id': v['id'],
                        'streamer': v['streamer'],
                        'filename': v['filename'],
                        'size_bytes': v['size_bytes'],
                        'duration_seconds': v['duration_seconds'],
                    }
                    for v in picks
                ],
                'estimated_frames': total_frames,
            }
        finally:
            conn.close()


@app.get('/api/videos')
def api_videos(
    status: Optional[str] = None,
    streamer: Optional[str] = None,
    room_id: Optional[str] = None,
    bvid: Optional[str] = None,
    min_size_bytes: Optional[int] = None,
) -> List[Dict[str, Any]]:
    with _db_lock:
        conn = _conn()
        try:
            return db.list_videos(
                conn,
                status=status,
                streamer=streamer,
                room_id=room_id,
                bvid=bvid,
                min_size_bytes=min_size_bytes,
            )
        finally:
            conn.close()


@app.post('/api/sync')
def api_sync() -> Dict[str, Any]:
    _require_local_heavy_operation('NAS 录像清单同步')
    if _sync_state['running']:
        raise HTTPException(409, '同步任务已在运行')

    def _run() -> None:
        _sync_state['running'] = True
        _sync_state['error'] = None
        try:
            nas = _nas()
            with _db_lock:
                conn = _conn()
                try:
                    videos = sync_videos(conn, nas)
                finally:
                    conn.close()
            _sync_state['videos'] = len(videos)
        except Exception as exc:  # noqa: BLE001
            _sync_state['error'] = str(exc)
        finally:
            _sync_state['running'] = False

    threading.Thread(target=_run, daemon=True).start()
    return {'started': True}


@app.get('/api/sync/state')
def api_sync_state() -> Dict[str, Any]:
    return dict(_sync_state)


# ---------- 抽帧 ----------


@app.post('/api/extract')
def api_extract(body: Dict[str, Any]) -> Dict[str, Any]:
    _require_local_heavy_operation('视频抽帧')
    video_ids = [int(x) for x in body.get('video_ids', [])]
    strategy = body.get('strategy', 'existing_model_hits')
    if not video_ids:
        raise HTTPException(400, '未选择视频')
    if strategy not in config.STRATEGIES:
        raise HTTPException(400, f'未知抽帧策略: {strategy}')
    if task_state()['running']:
        raise HTTPException(409, '已有抽帧任务在运行,请等待完成或取消')
    params = body.get('params', {}) or {}

    def _run() -> None:
        try:
            nas = _nas()
            with _db_lock:
                conn = _conn()
                try:
                    extract_videos_multi(conn, nas, video_ids, strategy, params)
                finally:
                    conn.close()
        except Exception:  # noqa: BLE001
            pass  # 状态经 task_state 暴露

    threading.Thread(target=_run, daemon=True).start()
    return {'started': True, 'video_ids': video_ids, 'strategy': strategy}


@app.get('/api/extract/state')
def api_extract_state() -> Dict[str, Any]:
    return task_state()


@app.post('/api/extract/cancel')
def api_extract_cancel() -> Dict[str, Any]:
    cancel_extraction()
    return {'cancelled': True}


@app.get('/api/extraction-jobs')
def api_extraction_jobs(limit: int = 50) -> List[Dict[str, Any]]:
    with _db_lock:
        conn = _conn()
        try:
            rows = conn.execute(
                'SELECT j.*, v.streamer, v.filename FROM extraction_jobs j '
                'JOIN videos v ON v.id = j.video_id '
                'ORDER BY j.id DESC LIMIT ?',
                (limit,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d['params'] = json.loads(d['params'] or '{}')
                out.append(d)
            return out
        finally:
            conn.close()


# ---------- 帧 ----------


@app.get('/api/frames')
def api_frames(
    video_id: Optional[int] = None,
    event_id: Optional[int] = None,
    labeled: Optional[int] = None,
    status: Optional[str] = None,
    screen_type: Optional[str] = None,
    strategy: Optional[str] = None,
    representative_only: bool = False,
    limit: int = Query(200, ge=1, le=100000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            frames = db.query_frames(
                conn,
                video_id=video_id,
                event_id=event_id,
                labeled=labeled,
                status=status,
                screen_type=screen_type,
                strategy=strategy,
                representative_only=representative_only,
                limit=limit,
                offset=offset,
            )
            return {'frames': frames}
        finally:
            conn.close()


@app.get('/api/frames/{frame_id}')
def api_frame(frame_id: int) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            f = db.get_frame(conn, frame_id)
            if not f:
                raise HTTPException(404, '帧不存在')
            f['annotation'] = db.get_annotation(conn, frame_id)
            f['boxes'] = db.get_boxes(conn, frame_id)
            preds = conn.execute(
                'SELECT model_version, pred_type, confidence, bbox FROM '
                'model_predictions WHERE frame_id = ? ORDER BY id',
                (frame_id,),
            ).fetchall()
            f['predictions'] = [dict(r) for r in preds]
            return f
        finally:
            conn.close()


@app.get('/api/frames/{frame_id}/image')
def api_frame_image(frame_id: int) -> Response:
    if config.MEDIA_SERVER_URL:
        return RedirectResponse(
            f'{config.MEDIA_SERVER_URL.rstrip("/")}/api/frames/{int(frame_id)}/image'
        )
    with _db_lock:
        conn = _conn()
        try:
            row = conn.execute(
                'SELECT frame_path FROM frames WHERE id = ?', (frame_id,)
            ).fetchone()
        finally:
            conn.close()
    if not row:
        raise HTTPException(404, '帧不存在')
    return FileResponse(row['frame_path'], media_type='image/jpeg')


@app.get('/api/frames/{frame_id}/thumb')
def api_frame_thumb(frame_id: int) -> Response:
    if config.MEDIA_SERVER_URL:
        return RedirectResponse(
            f'{config.MEDIA_SERVER_URL.rstrip("/")}/api/frames/{int(frame_id)}/thumb'
        )
    with _db_lock:
        conn = _conn()
        try:
            row = conn.execute(
                'SELECT thumb_path, frame_path FROM frames WHERE id = ?', (frame_id,)
            ).fetchone()
        finally:
            conn.close()
    if not row:
        raise HTTPException(404, '帧不存在')
    path = (
        row['thumb_path']
        if row['thumb_path'] and Path(row['thumb_path']).exists()
        else row['frame_path']
    )
    return FileResponse(path, media_type='image/jpeg')


def _fetch_frame_image_bytes(frame_id: int) -> bytes:
    if not config.MEDIA_SERVER_URL:
        raise RuntimeError('尚未配置 NAS 图片服务')
    url = f'{config.MEDIA_SERVER_URL.rstrip("/")}/api/frames/{int(frame_id)}/image'
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


@app.post('/api/frames/{frame_id}/representative')
def api_set_representative(frame_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    value = 1 if body.get('value', True) else 0
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.execute(
                'UPDATE frames SET is_representative = ? WHERE id = ?',
                (value, frame_id),
            )
            conn.commit()
            if not cur.rowcount:
                raise HTTPException(404, '帧不存在')
            return {'representative': value}
        finally:
            conn.close()


# ---------- 事件 ----------


@app.post('/api/live/frame')
def api_live_frame(body: Dict[str, Any]) -> Dict[str, Any]:
    """实时抽帧:抽指定视频的下一帧(间隔 interval_ms),入库并返回帧信息。

    与上一帧内容重复时返回 {'duplicate': True}(前端可自动重试)。
    SSH 网络操作在数据库锁外执行,入库用独立连接。
    """
    _require_local_heavy_operation('远程视频抽帧')
    video_id = int(body.get('video_id', 0))
    if not video_id:
        raise HTTPException(400, '缺少 video_id')
    after_ms = int(body.get('after_ms', -5000))
    interval_ms = int(body.get('interval_ms', 5000))
    last_sha = body.get('last_sha') or None
    # 查视频(锁内,快)
    with _db_lock:
        conn = _conn()
        try:
            row = conn.execute(
                'SELECT remote_path, duration_seconds FROM videos WHERE id = ?',
                (video_id,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        raise HTTPException(404, '视频不存在')
    nas = _nas()
    try:
        # SSH 抽帧在锁外
        work_conn = db.connect(config.DB_PATH)
        try:
            result = live_next_frame(
                work_conn,
                nas,
                video_id,
                after_ms=after_ms,
                interval_ms=interval_ms,
                last_sha=last_sha,
            )
        finally:
            work_conn.close()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f'抽帧失败: {exc}')
    return result


@app.get('/api/live/videos')
def api_live_videos() -> Dict[str, Any]:
    """实时打标视频列表:全部 >1GB 视频 + 各自打标进度。"""
    with _db_lock:
        conn = _conn()
        try:
            videos = db.list_videos(conn, min_size_bytes=1073741824)
            progress = db.all_video_progress(conn)
            labeled = {
                r['video_id']: r['c']
                for r in conn.execute(
                    'SELECT video_id, COUNT(*) c FROM frames WHERE labeled = 1 '
                    'GROUP BY video_id'
                ).fetchall()
            }
            total = {
                r['video_id']: r['c']
                for r in conn.execute(
                    'SELECT video_id, COUNT(*) c FROM frames GROUP BY video_id'
                ).fetchall()
            }
            # 本地已下载的 mp4 集合(一次目录扫描)
            local_ready = set()
            if config.LOCAL_VIDEO_DIR.exists():
                local_ready = {
                    int(p.stem)
                    for p in config.LOCAL_VIDEO_DIR.glob('*.mp4')
                    if p.stat().st_size > 1024 * 1024
                }
            for v in videos:
                p = progress.get(v['id'], {})
                v['last_pts_ms'] = p.get('last_pts_ms')
                v['last_frame_id'] = p.get('last_frame_id')
                v['labeled_count'] = labeled.get(v['id'], 0)
                v['frame_count'] = total.get(v['id'], 0)
                v['local_ready'] = v['id'] in local_ready
                v['progress_pct'] = (
                    round(p['last_pts_ms'] / (v['duration_seconds'] * 1000) * 100)
                    if p.get('last_pts_ms') is not None and v['duration_seconds'] > 0
                    else None
                )
            return {'videos': videos, 'count': len(videos)}
        finally:
            conn.close()


@app.put('/api/live/videos/{video_id}/progress')
def api_save_video_progress(video_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            db.save_video_progress(
                conn,
                video_id,
                last_pts_ms=body.get('last_pts_ms'),
                last_frame_id=body.get('last_frame_id'),
            )
            return {'saved': True}
        finally:
            conn.close()


@app.get('/api/live/state')
def api_live_state() -> Dict[str, Any]:
    """读取实时打标进度(队列、当前视频、位置)。"""
    with _db_lock:
        conn = _conn()
        try:
            return db.load_live_state(conn)
        finally:
            conn.close()


@app.put('/api/live/state')
def api_save_live_state(body: Dict[str, Any]) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            db.save_live_state(
                conn,
                queue=[int(x) for x in body.get('queue', [])],
                queue_index=int(body.get('queue_index', 0)),
                video_id=body.get('video_id'),
                last_pts_ms=body.get('last_pts_ms'),
                last_frame_id=body.get('last_frame_id'),
            )
            return {'saved': True}
        finally:
            conn.close()


@app.post('/api/live/videos/{video_id}/download')
def api_download_video(video_id: int) -> Dict[str, Any]:
    """把视频下载到本地并转 mp4(后台),用于本地播放与丝滑抽帧。"""
    _require_local_heavy_operation('完整视频下载与转码')
    with _db_lock:
        conn = _conn()
        try:
            row = conn.execute(
                'SELECT remote_path FROM videos WHERE id = ?', (video_id,)
            ).fetchone()
        finally:
            conn.close()
    if not row:
        raise HTTPException(404, '视频不存在')
    if local.local_mp4_exists(video_id):
        return {'started': False, 'status': 'done'}
    nas = _nas()
    local.start_download(nas, video_id)
    return {'started': True, 'status': 'downloading'}


@app.get('/api/live/videos/{video_id}/download-state')
def api_download_state(video_id: int) -> Dict[str, Any]:
    return local.download_state(video_id)


@app.get('/api/media/{video_id}')
def api_media(video_id: int) -> FileResponse:
    """本地 mp4 流(支持 Range,浏览器可拖动进度条)。"""
    mp4 = local._mp4_path(video_id)  # noqa: SLF001
    if not mp4.exists():
        raise HTTPException(404, '视频未下载')
    return FileResponse(str(mp4), media_type='video/mp4')


@app.post('/api/live/frame-local')
def api_live_frame_local(body: Dict[str, Any]) -> Dict[str, Any]:
    """本地抽帧:在已下载的 mp4 上 seek 抽一帧入库。"""
    _require_local_heavy_operation('本地视频抽帧')
    video_id = int(body.get('video_id', 0))
    pts_ms = int(body.get('pts_ms', 0))
    interval_ms = int(body.get('interval_ms', 5000))
    if not video_id:
        raise HTTPException(400, '缺少 video_id')
    with _db_lock:
        conn = _conn()
        try:
            result = local.local_frame(conn, video_id, pts_ms, interval_ms=interval_ms)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        finally:
            conn.close()
    return result


# ---------- 3V3 / 大乱斗光栅专项 ----------


@app.get('/api/mode-gate/rounds/active')
def api_active_mode_gate_round() -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            result = db.get_active_mode_gate_round(conn)
            if not result:
                raise HTTPException(404, '还没有启用的光栅打标轮次')
            for video in result['videos']:
                video['local_ready'] = local.local_mp4_exists(video['video_id'])
                duration = float(video['duration_seconds'] or 0)
                last_pts = video.get('last_pts_ms')
                video['progress_pct'] = (
                    round(last_pts / (duration * 1000) * 100)
                    if last_pts is not None and duration > 0
                    else None
                )
            return result
        finally:
            conn.close()


@app.get('/api/mode-gate/rounds/{round_id}/frames/{frame_id}')
def api_mode_gate_annotation(round_id: str, frame_id: int) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            frame = db.get_frame(conn, frame_id)
            if not frame:
                raise HTTPException(404, '帧不存在')
            member = conn.execute(
                'SELECT expected_mode FROM mode_gate_round_videos '
                'WHERE round_id = ? AND video_id = ?',
                (round_id, frame['video_id']),
            ).fetchone()
            if not member:
                raise HTTPException(404, '该帧不属于本轮挑选的视频')
            return {
                'annotation': db.get_mode_gate_annotation(
                    conn, round_id=round_id, frame_id=frame_id
                ),
                'expected_mode': member['expected_mode'],
            }
        finally:
            conn.close()


@app.put('/api/mode-gate/rounds/{round_id}/frames/{frame_id}')
def api_save_mode_gate_annotation(
    round_id: str, frame_id: int, body: Dict[str, Any]
) -> Dict[str, Any]:
    evidence = str(body.get('evidence') or '')
    boxes: Optional[List[Dict[str, Any]]] = None
    raw_boxes = body.get('boxes')
    if raw_boxes is not None:
        if not isinstance(raw_boxes, list):
            raise HTTPException(400, 'boxes 必须是边界框数组')
        boxes = []
        try:
            for raw_box in raw_boxes:
                if not isinstance(raw_box, dict):
                    raise TypeError
                boxes.append(
                    {name: float(raw_box[name]) for name in ('x', 'y', 'w', 'h')}
                )
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, '每个边界框都必须包含数字 x/y/w/h')
    coords: Dict[str, Optional[float]] = {}
    try:
        for name in ('x', 'y', 'w', 'h'):
            value = body.get(name)
            coords[name] = None if value is None else float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, 'x/y/w/h 必须是数字')
    with _db_lock:
        conn = _conn()
        try:
            try:
                annotation = db.save_mode_gate_annotation(
                    conn,
                    round_id=round_id,
                    frame_id=frame_id,
                    evidence=evidence,
                    boxes=boxes,
                    x=coords['x'],
                    y=coords['y'],
                    w=coords['w'],
                    h=coords['h'],
                    notes=str(body.get('notes') or ''),
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            db.audit(
                conn,
                'mode_gate_label',
                frame_id=frame_id,
                detail=json.dumps(annotation, ensure_ascii=False),
            )
            return annotation
        finally:
            conn.close()


@app.delete('/api/mode-gate/rounds/{round_id}/frames/{frame_id}')
def api_delete_mode_gate_annotation(round_id: str, frame_id: int) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            db.delete_mode_gate_annotation(conn, round_id=round_id, frame_id=frame_id)
            db.audit(
                conn,
                'mode_gate_label',
                frame_id=frame_id,
                detail=json.dumps({'round_id': round_id, 'deleted': True}),
            )
            return {'deleted': True}
        finally:
            conn.close()


@app.get('/api/mode-gate/rounds/{round_id}/videos/{video_id}/frames')
def api_mode_gate_frames(round_id: str, video_id: int) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            member = conn.execute(
                'SELECT 1 FROM mode_gate_round_videos '
                'WHERE round_id = ? AND video_id = ?',
                (round_id, video_id),
            ).fetchone()
            if not member:
                raise HTTPException(404, '视频不在本轮任务中')
            return {
                'frames': db.list_mode_gate_frames(
                    conn, round_id=round_id, video_id=video_id
                )
            }
        finally:
            conn.close()


@app.get('/api/videos/{video_id}/viewport-box')
def api_video_viewport_box(video_id: int) -> Dict[str, Any]:
    """该视频最新一帧的 viewport 框(用于跨帧自动继承)。"""
    with _db_lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT b.x, b.y, b.w, b.h FROM boxes b "
                "JOIN frames f ON f.id = b.frame_id "
                "WHERE f.video_id = ? AND b.box_type = 'viewport' "
                "ORDER BY f.timestamp_ms DESC LIMIT 1",
                (video_id,),
            ).fetchone()
            return {'box': dict(row) if row else None}
        finally:
            conn.close()


@app.get('/api/videos/{video_id}/streamer-boxes')
def api_video_streamer_boxes(video_id: int) -> Dict[str, Any]:
    """该视频所属主播的默认框(跨视频记忆)。"""
    with _db_lock:
        conn = _conn()
        try:
            row = conn.execute(
                'SELECT v.streamer FROM videos v WHERE v.id = ?', (video_id,)
            ).fetchone()
            if not row or not row['streamer']:
                return {'streamer': None, 'boxes': {}}
            boxes = {
                r['box_type']: dict(r)
                for r in conn.execute(
                    'SELECT box_type, x, y, w, h FROM streamer_boxes '
                    'WHERE streamer = ?',
                    (row['streamer'],),
                ).fetchall()
            }
            return {'streamer': row['streamer'], 'boxes': boxes}
        finally:
            conn.close()


@app.delete('/api/videos/{video_id}/streamer-box/{box_type}')
def api_delete_streamer_box(video_id: int, box_type: str) -> Dict[str, Any]:
    """删除该视频所属主播的某类默认框(清框时同步,避免下帧又带出)。"""
    with _db_lock:
        conn = _conn()
        try:
            row = conn.execute(
                'SELECT streamer FROM videos WHERE id = ?', (video_id,)
            ).fetchone()
            if row and row['streamer']:
                conn.execute(
                    'DELETE FROM streamer_boxes WHERE streamer = ? AND box_type = ?',
                    (row['streamer'], box_type),
                )
                conn.commit()
            return {'deleted': True}
        finally:
            conn.close()


@app.post('/api/videos/{video_id}/backfill-boxes')
def api_backfill_boxes(video_id: int) -> Dict[str, Any]:
    """用主播默认框补齐已标帧缺失的框(商店/积分板/结算/游戏窗口)。"""
    with _db_lock:
        conn = _conn()
        try:
            v = conn.execute(
                'SELECT streamer FROM videos WHERE id = ?', (video_id,)
            ).fetchone()
            if not v or not v['streamer']:
                return {'filled': 0, 'note': '视频不存在'}
            defaults = {
                r['box_type']: r
                for r in conn.execute(
                    'SELECT box_type, x, y, w, h FROM streamer_boxes '
                    'WHERE streamer = ?',
                    (v['streamer'],),
                ).fetchall()
            }
            if not defaults:
                return {'filled': 0, 'note': '该主播还没有默认框,先画一次'}
            # screen_type → 需要的框类型(未列出的类型不需要面板框)
            need = {
                'ingame_shop': 'shop_panel',
                'equipment_select': 'equipment_panel',
                'talent_select': 'talent_panel',
                'scoreboard': 'scoreboard_panel',
                'death_scoreboard': 'scoreboard_panel',
                'result_page': 'result_panel',
            }
            rows = conn.execute(
                'SELECT a.frame_id, a.screen_type, a.content_family '
                'FROM annotations a JOIN frames f ON f.id = a.frame_id '
                "WHERE f.video_id = ? AND a.annotation_status = 'complete' "
                'AND a.screen_type IS NOT NULL',
                (video_id,),
            ).fetchall()
            filled = 0
            for r in rows:
                bt = need.get(r['screen_type'])
                if bt and bt in defaults:
                    exists = conn.execute(
                        'SELECT 1 FROM boxes WHERE frame_id = ? AND box_type = ?',
                        (r['frame_id'], bt),
                    ).fetchone()
                    if not exists:
                        d = defaults[bt]
                        conn.execute(
                            'INSERT INTO boxes (frame_id, box_type, x, y, w, h) '
                            'VALUES (?, ?, ?, ?, ?, ?)',
                            (r['frame_id'], bt, d['x'], d['y'], d['w'], d['h']),
                        )
                        filled += 1
                # 游戏窗口:虚荣画面都可补(非虚荣画面没有游戏窗口)
                if r['content_family'] == 'vainglory' and 'viewport' in defaults:
                    exists = conn.execute(
                        'SELECT 1 FROM boxes WHERE frame_id = ? AND box_type = ?',
                        (r['frame_id'], 'viewport'),
                    ).fetchone()
                    if not exists:
                        d = defaults['viewport']
                        conn.execute(
                            'INSERT INTO boxes (frame_id, box_type, x, y, w, h) '
                            'VALUES (?, ?, ?, ?, ?, ?)',
                            (r['frame_id'], 'viewport', d['x'], d['y'], d['w'], d['h']),
                        )
                        filled += 1
            conn.commit()
            return {'filled': filled}
        finally:
            conn.close()


@app.delete('/api/frames/{frame_id}/annotation')
def api_clear_annotation(frame_id: int) -> Dict[str, Any]:
    """清除当前帧的标注(回到未标注状态):删标注、删所有框、脱离事件。"""
    with _db_lock:
        conn = _conn()
        try:
            conn.execute('DELETE FROM annotations WHERE frame_id = ?', (frame_id,))
            conn.execute('DELETE FROM boxes WHERE frame_id = ?', (frame_id,))
            # 脱离所属事件;若事件因此没有其他帧,删除该事件
            row = conn.execute(
                'SELECT event_id FROM frames WHERE id = ?', (frame_id,)
            ).fetchone()
            if row and row['event_id']:
                conn.execute(
                    'UPDATE frames SET event_id = NULL WHERE id = ?', (frame_id,)
                )
                other = conn.execute(
                    'SELECT COUNT(*) c FROM frames WHERE event_id = ?',
                    (row['event_id'],),
                ).fetchone()['c']
                if other == 0:
                    conn.execute('DELETE FROM events WHERE id = ?', (row['event_id'],))
            conn.execute('UPDATE frames SET labeled = 0 WHERE id = ?', (frame_id,))
            conn.commit()
            return {'cleared': True}
        finally:
            conn.close()


# ---------- BP 主动学习复核 ----------


def _set_worker_candidate_sync_state(
    *, force_persist: bool = False, **values: Any
) -> None:
    global _worker_candidate_state_last_persisted_at
    with _worker_candidate_sync_lock:
        _worker_candidate_sync_state.update(values)
        snapshot = dict(_worker_candidate_sync_state)
        current = time.monotonic()
        should_persist = (
            config.CONTROL_PLANE_ONLY
            and config.CANDIDATE_LOCAL_DIR is not None
            and (
                force_persist
                or current - _worker_candidate_state_last_persisted_at >= 2.0
            )
        )
        if should_persist:
            _worker_candidate_state_last_persisted_at = current
    if not should_persist:
        return
    conn = None
    try:
        conn = _conn()
        db.save_service_runtime_state(conn, 'candidate_index', snapshot)
    except Exception:  # noqa: BLE001 - 索引主流程不因状态展示失败而中断
        pass
    finally:
        if conn is not None:
            conn.close()


def _sync_worker_candidate_queue(*, maximum: int) -> None:
    _set_worker_candidate_sync_state(
        running=True,
        total=0,
        processed=0,
        inserted=0,
        updated=0,
        unchanged=0,
        downloaded=0,
        failed=0,
        last_error='',
        reviews_pulled=0,
        reviews_pushed=0,
        review_conflicts=0,
        push_failed=0,
        error=None,
        archive_total=0,
        archive_processed=0,
        archive_inserted=0,
        archive_updated=0,
        archive_downloaded=0,
        archive_failed=0,
        archive_last_error='',
        archive_box_suggested=0,
    )
    conn = None
    try:
        nas = _nas()
        items = nas.list_training_candidates()
        reviews = nas.list_training_candidate_reviews()
        archive_items = (
            nas.list_result_frame_candidates() if config.SYNC_RESULT_ARCHIVE else []
        )
        _set_worker_candidate_sync_state(
            total=min(len(items), maximum),
            archive_total=min(len(archive_items), maximum),
        )
        conn = _conn()
        result = worker_candidates.sync_worker_candidates(
            conn,
            nas,
            items,
            maximum=maximum,
            progress=lambda values: _set_worker_candidate_sync_state(**values),
        )
        legacy_pull = worker_candidates.pull_worker_candidate_reviews(conn, reviews)
        unified_pull = worker_candidates.pull_training_review_reviews(conn, reviews)
        legacy_push = worker_candidates.push_worker_candidate_reviews(conn, nas)
        unified_push = worker_candidates.push_training_review_reviews(conn, nas)
        result.update(
            {
                'reviews_pulled': (
                    legacy_pull['reviews_pulled'] + unified_pull['reviews_pulled']
                ),
                'review_conflicts': (
                    legacy_pull['review_conflicts'] + unified_pull['review_conflicts']
                ),
                'reviews_ignored': (
                    legacy_pull['reviews_ignored'] + unified_pull['reviews_ignored']
                ),
                'reviews_pushed': (
                    legacy_push['reviews_pushed'] + unified_push['reviews_pushed']
                ),
                'push_failed': legacy_push['push_failed'] + unified_push['push_failed'],
            }
        )
        archive = result_archive.sync_result_archive(
            conn,
            nas,
            archive_items,
            maximum=maximum,
            box_suggester=result_archive.detect_result_box,
            progress=lambda values: _set_worker_candidate_sync_state(
                archive_total=values['total'],
                archive_processed=values['processed'],
                archive_inserted=values['inserted'],
                archive_updated=values['updated'],
                archive_downloaded=values['downloaded'],
                archive_failed=values['failed'],
                archive_last_error=values['last_error'],
                archive_box_suggested=values['box_suggested'],
            ),
        )
        result.update(
            {
                'archive_total': archive['total'],
                'archive_processed': archive['processed'],
                'archive_inserted': archive['inserted'],
                'archive_updated': archive['updated'],
                'archive_downloaded': archive['downloaded'],
                'archive_failed': archive['failed'],
                'archive_last_error': archive['last_error'],
                'archive_box_suggested': archive['box_suggested'],
            }
        )
        if any(
            int(result.get(key) or 0)
            for key in (
                'inserted',
                'updated',
                'reviews_pulled',
                'archive_inserted',
                'archive_updated',
            )
        ):
            _invalidate_training_review_cache()
        _set_worker_candidate_sync_state(
            **result, running=False, last_completed_at=db.now(), force_persist=True
        )
    except Exception as exc:  # noqa: BLE001
        _set_worker_candidate_sync_state(
            running=False, error=str(exc), force_persist=True
        )
    finally:
        if conn is not None:
            conn.close()


def _begin_candidate_index() -> bool:
    with _worker_candidate_sync_lock:
        if _worker_candidate_sync_state['running']:
            return False
        _worker_candidate_sync_state['running'] = True
        _worker_candidate_sync_state['error'] = None
        return True


def _candidate_index_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        if _begin_candidate_index():
            _sync_worker_candidate_queue(maximum=20_000)
        stop.wait(config.CANDIDATE_INDEX_INTERVAL_SECONDS)


@app.post('/api/bp-review/sync-worker')
def api_sync_worker_candidates(body: Dict[str, Any]) -> Dict[str, Any]:
    maximum = int(body.get('maximum', 10_000))
    if not 1 <= maximum <= 20_000:
        raise HTTPException(400, 'maximum 必须在 1 到 20000 之间')
    if not _begin_candidate_index():
        raise HTTPException(409, 'Worker 候选素材正在建立索引')
    threading.Thread(
        target=_sync_worker_candidate_queue, kwargs={'maximum': maximum}, daemon=True
    ).start()
    return {'started': True}


@app.post('/api/worker-candidates/sync')
def api_sync_all_worker_candidates(body: Dict[str, Any]) -> Dict[str, Any]:
    """同步 NAS 上全部专项候选；保留旧 BP 路由兼容已有页面。"""
    return api_sync_worker_candidates(body)


@app.get('/api/worker-candidates/state')
def api_worker_candidate_state() -> Dict[str, Any]:
    now = time.monotonic()
    with _worker_candidate_state_response_lock:
        cached = _worker_candidate_state_response['value']
        if cached is not None and now < _worker_candidate_state_response['expires_at']:
            return cached
        with _worker_candidate_sync_lock:
            sync = dict(_worker_candidate_sync_state)
        with _training_review_cache_lock:
            cached_review = _training_review_cache['stats']
            review = dict(cached_review) if isinstance(cached_review, dict) else {}
        if config.CANDIDATE_LOCAL_DIR is None:
            with _training_review_read_guard():
                conn = _conn()
                try:
                    published = db.load_service_runtime_state(conn, 'candidate_index')
                    if published:
                        sync = published
                finally:
                    conn.close()
        result = {'sync': sync, 'review': review}
        _worker_candidate_state_response.update(
            {
                'value': result,
                'expires_at': (
                    time.monotonic() + _WORKER_CANDIDATE_STATE_CACHE_SECONDS
                ),
            }
        )
        return result


@app.get('/api/training-review/items')
def api_training_review_items(
    status: str = 'needs_review',
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    source_scope: str = 'all',
    streamer: str = '',
    hero_screen_type: str = '',
    source_type: str = '',
    scene: str = '',
    match_mode: str = '',
    hero: Optional[List[str]] = Query(None),
    confidence: str = '',
    include_stats: bool = True,
) -> Dict[str, Any]:
    hero_values = hero if isinstance(hero, list) else []
    with _training_review_read_guard():
        conn = _conn()
        try:
            try:
                default_queue = (
                    status == 'needs_review'
                    and source_scope == 'new'
                    and not any(
                        (
                            streamer,
                            hero_screen_type,
                            source_type,
                            scene,
                            match_mode,
                            hero_values,
                            confidence,
                        )
                    )
                )
                if default_queue:
                    result_groups = _cached_training_review_groups(
                        conn, allow_partial_index=True
                    )
                    frame_ids = _cached_default_training_review_queue(
                        conn, result_groups
                    )
                    items = db.get_training_review_items(
                        conn,
                        frame_ids[offset : offset + limit],
                        result_groups=result_groups,
                        pending_review_queue=True,
                    )
                    filtered_total = len(frame_ids)
                else:
                    result_groups = _cached_training_review_groups(
                        conn, allow_partial_index=True
                    )
                    items, filtered_total = db.training_review_page(
                        conn,
                        status=status,
                        limit=limit,
                        offset=offset,
                        source_scope=source_scope,
                        streamer=streamer,
                        hero_screen_type=hero_screen_type,
                        source_type=source_type,
                        scene=scene,
                        match_mode=match_mode,
                        hero=hero_values,
                        confidence=confidence,
                        prefill_ready_only=True,
                        result_groups=result_groups,
                    )
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            stats = (
                _training_review_stats_response(
                    conn,
                    source_scope=source_scope,
                    status=status,
                    streamer=streamer,
                    hero_screen_type=hero_screen_type,
                )
                if include_stats
                else {}
            )
            return {'items': items, 'stats': stats, 'filtered_total': filtered_total}
        finally:
            conn.close()


def _training_review_stats_response(
    conn: Any,
    *,
    source_scope: str,
    status: str,
    streamer: str = '',
    hero_screen_type: str = '',
) -> Dict[str, Any]:
    stats = dict(_cached_training_review_stats(conn))
    if source_scope == 'legacy' or status == 'legacy_hero':
        stats['legacy_hero'] = db.legacy_hero_review_stats(conn)
    if status == 'legacy_hero':
        stats['legacy_hero_filtered'] = db.legacy_hero_review_stats(
            conn,
            streamer=streamer,
            screen_type=hero_screen_type,
            prefill_ready_only=True,
        )
    return stats


@app.get('/api/training-review/stats')
def api_training_review_stats(
    source_scope: str = 'all',
    status: str = 'needs_review',
    streamer: str = '',
    hero_screen_type: str = '',
) -> Dict[str, Any]:
    conn = _conn()
    try:
        return {
            'stats': _training_review_stats_response(
                conn,
                source_scope=source_scope,
                status=status,
                streamer=streamer,
                hero_screen_type=hero_screen_type,
            )
        }
    finally:
        conn.close()


@app.get('/api/training-review/queue-summary')
def api_training_review_queue_summary(source_scope: str = 'new') -> Dict[str, Any]:
    with _training_review_read_guard():
        conn = _conn()
        try:
            try:
                return {
                    'summary': db.training_review_queue_summary(
                        conn, source_scope=source_scope
                    )
                }
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        finally:
            conn.close()


@app.get('/api/training-review/material-suggestions')
def api_training_review_material_suggestions() -> Dict[str, Any]:
    conn = _conn()
    try:
        return {
            'material_suggestions': _cached_training_review_material_suggestions(conn)
        }
    finally:
        conn.close()


@app.get('/api/training-review/filter-options')
def api_training_review_filter_options(source_scope: str = 'all') -> Dict[str, Any]:
    with _training_review_read_guard():
        conn = _conn()
        try:
            return db.training_review_filter_options(conn, source_scope=source_scope)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            conn.close()


@app.post('/api/training-review/items/{frame_id}/prefill')
def api_training_review_prefill(frame_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    if config.CONTROL_PLANE_ONLY:
        with _db_lock:
            conn = _conn()
            try:
                item = _single_training_review_item(conn, frame_id)
                if item is None:
                    raise HTTPException(404, '训练复核图片不存在')
                models = model_prefill.latest_model_specs(
                    conn, (*model_prefill.CORE_PREFILL_TASKS, 'hero_avatar_detector')
                )
                model_runs = {
                    task_id: value['run_id'] for task_id, value in models.items()
                }
                previous = next(
                    (
                        source
                        for source in item.get('sources') or []
                        if source.get('source_type') == 'new_model_prefill'
                        and (source.get('metadata') or {}).get('model_runs')
                        == model_runs
                        and not (source.get('metadata') or {}).get('errors')
                    ),
                    None,
                )
                if previous is not None and not bool(body.get('force')):
                    return {
                        'applied': False,
                        'cached': True,
                        'models': model_runs,
                        'item': item,
                    }
                if not models:
                    return {
                        'applied': False,
                        'cached': False,
                        'models': {},
                        'item': item,
                    }
                job = _queue_model_prefill(
                    conn,
                    frame_id=frame_id,
                    operation='core',
                    models=models,
                    force=bool(body.get('force')),
                )
                return {
                    'applied': False,
                    'cached': False,
                    'queued': True,
                    'models': model_runs,
                    'job': job,
                    'item': item,
                }
            finally:
                conn.close()
    with _db_lock:
        conn = _conn()
        try:
            try:
                return model_prefill.prefill_training_review_item(
                    conn, frame_id, force=bool(body.get('force')), result_groups={}
                )
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
            except FileNotFoundError as exc:
                raise HTTPException(422, str(exc)) from exc
        finally:
            conn.close()


def _queue_model_prefill(
    conn: Any,
    *,
    frame_id: int,
    operation: str,
    models: Dict[str, Any],
    screen_type: str = '',
    team_size: Optional[int] = None,
    slots: Optional[List[Dict[str, Any]]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        'operation': operation,
        'frame_id': int(frame_id),
        'models': models,
    }
    if screen_type:
        payload['screen_type'] = screen_type
    if team_size is not None:
        payload['team_size'] = int(team_size)
    if slots is not None:
        payload['slots'] = slots
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    ).hexdigest()[:16]
    related_id = f'{operation}:{int(frame_id)}:{fingerprint}'
    if force:
        related_id += ':' + secrets.token_hex(4)
    return vision_jobs.create_job(
        conn, kind='model_prefill', related_id=related_id, priority=80, payload=payload
    )


def _queue_next_autonomous_model_prefill(conn: Any) -> Optional[Dict[str, Any]]:
    """在 Worker 领取边界按需生成一张预打标任务。"""
    candidate = db.next_training_review_prefill_candidate(conn)
    if candidate is None:
        return None
    frame_id = int(candidate['frame_id'])
    stage = str(candidate['prefill_stage'])
    if stage == 'core':
        task_ids = (*model_prefill.CORE_PREFILL_TASKS, 'hero_avatar_detector')
        operation = 'core'
        screen_type = ''
        team_size = None
    elif stage == 'hero':
        task_ids = model_prefill.HERO_PREFILL_TASKS
        operation = 'hero_lineup'
        screen_type = str(candidate['prefill_screen_type'] or '')
        team_size = int(candidate['prefill_team_size'] or 0)
    else:
        db.update_training_review_prefill_state(
            conn, frame_id=frame_id, status='ready', stage='complete'
        )
        return None
    models = model_prefill.latest_model_specs(conn, task_ids)
    if any(task_id not in models for task_id in task_ids):
        return None
    job = _queue_model_prefill(
        conn,
        frame_id=frame_id,
        operation=operation,
        models=models,
        screen_type=screen_type,
        team_size=team_size,
    )
    db.update_training_review_prefill_state(
        conn,
        frame_id=frame_id,
        status='queued',
        stage=stage,
        screen_type=screen_type,
        team_size=team_size,
        increment_attempt=True,
    )
    return job


def _update_autonomous_prefill_after_result(
    conn: Any, leased: Dict[str, Any], result: Dict[str, Any]
) -> None:
    payload = leased.get('payload') or {}
    frame_id = int(payload.get('frame_id') or result.get('frame_id') or 0)
    operation = str(payload.get('operation') or result.get('operation') or 'core')
    if frame_id <= 0:
        return
    if operation != 'core':
        db.update_training_review_prefill_state(
            conn,
            frame_id=frame_id,
            status='ready',
            stage='complete',
            screen_type=str(payload.get('screen_type') or ''),
            team_size=(int(payload['team_size']) if payload.get('team_size') else None),
        )
        return
    errors = result.get('errors') if isinstance(result.get('errors'), dict) else {}
    if errors:
        raise RuntimeError(
            '核心模型预打标失败：'
            + '；'.join(f'{task}: {error}' for task, error in errors.items())
        )
    suggestions = (
        result.get('suggestions') if isinstance(result.get('suggestions'), dict) else {}
    )
    select = suggestions.get('hero_select')
    select_label = str(select.get('label') or '') if isinstance(select, dict) else ''
    context = result.get('hero_context_suggestion')
    context = context if isinstance(context, dict) else {}
    screen_type = str(context.get('screen_type') or '')
    team_size = int(context.get('team_size') or 0)
    needs_hero = (
        not select_label.startswith('select_')
        and screen_type in {'gameplay_hud', 'scoreboard', 'result_page'}
        and team_size in {3, 5}
    )
    if needs_hero:
        db.update_training_review_prefill_state(
            conn,
            frame_id=frame_id,
            status='pending',
            stage='hero',
            screen_type=screen_type,
            team_size=team_size,
            reset_attempts=True,
        )
    else:
        db.update_training_review_prefill_state(
            conn, frame_id=frame_id, status='ready', stage='complete'
        )


def _queue_model_validation(
    conn: Any,
    *,
    run_id: str,
    split: str,
    conf_thr: float,
    iou_threshold: float,
    sample_id: str = '',
) -> Dict[str, Any]:
    payload = {
        'run_id': run_id,
        'split': split,
        'conf_thr': max(0.0, min(1.0, float(conf_thr))),
        'iou_threshold': max(0.0, min(1.0, float(iou_threshold))),
        'sample_id': sample_id,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    ).hexdigest()[:16]
    return vision_jobs.create_job(
        conn,
        kind='validate_model',
        related_id=f'validate:{run_id}:{fingerprint}',
        priority=90 if sample_id else 70,
        payload=payload,
    )


def _hero_lineup_payload(
    lineup: Dict[str, Any], *, item: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    frame_id = int(lineup['frame_id'])
    slots = []
    for slot in lineup.get('slots') or []:
        value = dict(slot)
        value['crop_url'] = (
            f'/api/training-review/items/{frame_id}/heroes/'
            f"{value['side']}/{value['slot']}/crop"
        )
        slots.append(value)
    payload = {'applicable': True, **lineup, 'slots': slots}
    if item is not None:
        for source in item.get('sources') or []:
            if source.get('source_type') != 'new_model_hero_prefill':
                continue
            metadata = source.get('metadata') or {}
            if metadata.get('screen_type') == lineup.get('screen_type') and int(
                metadata.get('team_size') or 0
            ) == int(lineup.get('team_size') or 0):
                payload['player_suggestion'] = metadata.get('player_suggestion')
                payload['model_runs'] = metadata.get('model_runs') or {}
                break
    return payload


def _save_new_model_hero_prefill_source(
    conn: Any,
    *,
    frame_id: int,
    item: Dict[str, Any],
    screen_type: str,
    team_size: int,
    result: Dict[str, Any],
) -> None:
    db.add_training_review_source(
        conn,
        frame_id=frame_id,
        source_type='new_model_hero_prefill',
        source_id=f'frame:{frame_id}',
        metadata={
            'screen_type': screen_type,
            'team_size': team_size,
            'complete': bool(result.get('complete')),
            'reason': str(result.get('reason') or '')[:500],
            'model_runs': result.get('model_runs') or {},
            'player_suggestion': result.get('player_suggestion'),
            'detected': int(result.get('detected') or 0),
        },
        image_path=str(item['frame_path']),
    )


def _remote_training_review_hero_lineup(
    frame_id: int,
    *,
    screen_type: Optional[str],
    team_size: Optional[int],
    refresh: bool,
) -> Dict[str, Any]:
    """NAS 控制面只保存布局并排队，头像推理全部交给 Vision Worker。"""
    with _db_lock:
        conn = _conn()
        try:
            item = _single_training_review_item(conn, frame_id)
            if item is None:
                raise HTTPException(404, '训练复核图片不存在')
            existing = db.get_training_review_hero_lineup(conn, frame_id)
            try:
                context = hero_review.infer_lineup_context(
                    item, screen_type_hint=screen_type, team_size_hint=team_size
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if context is None:
                if existing is not None and screen_type is None:
                    return _hero_lineup_payload(existing, item=item)
                return {'applicable': False, 'reason': '尚未选择英雄头像所在画面'}
            inferred_screen, inferred_size = context
            if inferred_size is None:
                return {
                    'applicable': True,
                    'screen_type': inferred_screen,
                    'needs_team_size': True,
                    'slots': [],
                }
            same_existing = bool(
                existing
                and existing['screen_type'] == inferred_screen
                and int(existing['team_size']) == inferred_size
            )
            if same_existing and existing and existing['review_status'] == 'confirmed':
                return _hero_lineup_payload(existing, item=item)
            if (
                same_existing
                and existing
                and not refresh
                and existing['review_status'] == 'pending'
                and existing['suggestion_method'] == 'new-model-incomplete-worker-v1'
            ):
                return _hero_lineup_payload(existing, item=item)

            source_slots: List[Dict[str, Any]] = []
            if same_existing and existing and existing.get('slots'):
                source_slots = [
                    {
                        'side': value['side'],
                        'slot': int(value['slot']),
                        'crop': dict(value['crop']),
                    }
                    for value in existing['slots']
                ]
            if not source_slots:
                width = int(item['width'] or 0)
                height = int(item['height'] or 0)
                template = None
                if width > 0 and height > 0:
                    template = db.get_training_review_hero_template(
                        conn,
                        streamer=str(item['streamer'] or ''),
                        screen_type=inferred_screen,
                        team_size=inferred_size,
                        layout_key=db.hero_layout_key(width, height),
                    )
                if template is not None:
                    source_slots = [
                        {
                            'side': value['side'],
                            'slot': int(value['slot']),
                            'crop': dict(value['crop']),
                        }
                        for value in template['slots']
                    ]
                    existing = db.replace_training_review_hero_layout(
                        conn,
                        frame_id=frame_id,
                        screen_type=inferred_screen,
                        team_size=inferred_size,
                        method='layout-template+worker-pending-v1',
                        slots=source_slots,
                    )
                    same_existing = True

            operation = 'hero_slots' if source_slots else 'hero_lineup'
            task_ids = (
                ('hero_identity', 'player_position')
                if operation == 'hero_slots'
                else model_prefill.HERO_PREFILL_TASKS
            )
            models = model_prefill.latest_model_specs(conn, task_ids)
            if not models:
                if same_existing and existing is not None:
                    return _hero_lineup_payload(existing, item=item)
                return {
                    'applicable': True,
                    'screen_type': inferred_screen,
                    'team_size': inferred_size,
                    'review_status': 'pending',
                    'suggestion_method': 'manual-circle-v1',
                    'template_found': False,
                    'slots': [],
                }
            already_prefilled = bool(
                same_existing
                and existing
                and existing.get('slots')
                and all(
                    str(value.get('suggested_label') or '')
                    for value in existing['slots']
                )
            )
            if already_prefilled and not refresh:
                return _hero_lineup_payload(existing, item=item)
            job = _queue_model_prefill(
                conn,
                frame_id=frame_id,
                operation=operation,
                models=models,
                screen_type=inferred_screen,
                team_size=inferred_size,
                slots=source_slots if source_slots else None,
                force=refresh,
            )
            if same_existing and existing is not None:
                payload = _hero_lineup_payload(existing, item=item)
            else:
                payload = {
                    'applicable': True,
                    'screen_type': inferred_screen,
                    'team_size': inferred_size,
                    'review_status': 'pending',
                    'suggestion_method': 'worker-pending-v1',
                    'template_found': False,
                    'slots': [],
                }
            payload['prefill_job'] = job
            return payload
        finally:
            conn.close()


@app.get('/api/training-review/heroes')
def api_training_review_heroes() -> Dict[str, Any]:
    try:
        heroes = hero_review.hero_catalog()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {
        'heroes': [
            {
                **hero,
                'image_url': '/api/training-review/heroes/{}/image'.format(
                    hero['label']
                ),
            }
            for hero in heroes
        ]
    }


@app.get('/api/training-review/heroes/{label}/image')
def api_training_review_hero_image(label: str) -> Response:
    try:
        content = hero_review.hero_image_bytes(label)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    if content is None:
        raise HTTPException(404, '英雄头像不存在')
    return Response(
        content=content,
        media_type='image/jpeg',
        headers={'Cache-Control': 'private, max-age=86400'},
    )


@app.get('/api/training-review/items/{frame_id}/hero-lineup')
def api_training_review_hero_lineup(
    frame_id: int,
    screen_type: Optional[str] = None,
    team_size: Optional[int] = Query(None),
    refresh: bool = False,
) -> Dict[str, Any]:
    if config.CONTROL_PLANE_ONLY:
        return _remote_training_review_hero_lineup(
            frame_id, screen_type=screen_type, team_size=team_size, refresh=refresh
        )
    with _db_lock:
        conn = _conn()
        try:
            item = _single_training_review_item(conn, frame_id)
            if item is None:
                raise HTTPException(404, '训练复核图片不存在')
            existing = db.get_training_review_hero_lineup(conn, frame_id)
        finally:
            conn.close()
    try:
        context = hero_review.infer_lineup_context(
            item, screen_type_hint=screen_type, team_size_hint=team_size
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if context is None:
        if existing is not None and screen_type is None:
            return _hero_lineup_payload(existing, item=item)
        return {'applicable': False, 'reason': '尚未选择英雄头像所在画面'}
    inferred_screen, inferred_size = context
    same_context_existing = False
    if existing is not None:
        same_screen = existing['screen_type'] == inferred_screen
        same_size = inferred_size is None or int(existing['team_size']) == inferred_size
        same_context_existing = same_screen and same_size
        refreshes_automatic_layout = (
            inferred_size is not None
            and existing['review_status'] == 'pending'
            and str(existing['suggestion_method']).startswith('layout-template+')
        )
        if same_context_existing and not refresh and not refreshes_automatic_layout:
            return _hero_lineup_payload(existing, item=item)
    if inferred_size is None:
        return {
            'applicable': True,
            'screen_type': inferred_screen,
            'needs_team_size': True,
            'slots': [],
        }
    if existing is None or existing['review_status'] != 'confirmed':
        try:
            with _db_lock:
                model_conn = _conn()
                try:
                    model_result = model_prefill.prefill_hero_lineup(
                        model_conn,
                        Path(str(item['frame_path'])),
                        screen_type=inferred_screen,
                        team_size=inferred_size,
                    )
                finally:
                    model_conn.close()
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            model_result = {'complete': False}
        if model_result.get('complete'):
            with _db_lock:
                conn = _conn()
                try:
                    lineup = db.replace_training_review_hero_suggestions(
                        conn,
                        frame_id=frame_id,
                        screen_type=inferred_screen,
                        team_size=inferred_size,
                        method='new-model-cascade-v1',
                        slots=model_result['slots'],
                    )
                    _save_new_model_hero_prefill_source(
                        conn,
                        frame_id=frame_id,
                        item=item,
                        screen_type=inferred_screen,
                        team_size=inferred_size,
                        result=model_result,
                    )
                    item = _single_training_review_item(conn, frame_id) or item
                finally:
                    conn.close()
            return _hero_lineup_payload(lineup, item=item)
    with _db_lock:
        conn = _conn()
        try:
            template = db.get_training_review_hero_template(
                conn,
                streamer=str(item['streamer'] or ''),
                screen_type=inferred_screen,
                team_size=inferred_size,
                layout_key=db.hero_layout_key(int(item['width']), int(item['height'])),
            )
        finally:
            conn.close()
    if template is None:
        if same_context_existing and existing is not None:
            return _hero_lineup_payload(existing, item=item)
        return {
            'applicable': True,
            'screen_type': inferred_screen,
            'team_size': inferred_size,
            'review_status': 'pending',
            'suggestion_method': 'manual-circle-v1',
            'template_found': False,
            'slots': [],
        }
    if (
        same_context_existing
        and existing is not None
        and not refresh
        and str(template['updated_at']) <= str(existing['updated_at'])
    ):
        return _hero_lineup_payload(existing, item=item)
    try:
        with _db_lock:
            conn = _conn()
            try:
                model_result = model_prefill.prefill_hero_slots(
                    conn,
                    Path(str(item['frame_path'])),
                    template['slots'],
                    screen_type=inferred_screen,
                    team_size=inferred_size,
                )
            finally:
                conn.close()
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(422, f'英雄预填失败：{exc}') from exc
    slots = model_result['slots']
    with _db_lock:
        conn = _conn()
        try:
            lineup = db.replace_training_review_hero_layout(
                conn,
                frame_id=frame_id,
                screen_type=inferred_screen,
                team_size=inferred_size,
                method=(
                    'layout-template+hero-identity-v1'
                    if model_result.get('complete')
                    else 'layout-template+manual-v1'
                ),
                slots=slots,
            )
            if model_result.get('complete'):
                _save_new_model_hero_prefill_source(
                    conn,
                    frame_id=frame_id,
                    item=item,
                    screen_type=inferred_screen,
                    team_size=inferred_size,
                    result=model_result,
                )
                item = _single_training_review_item(conn, frame_id) or item
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            conn.close()
    lineup['template_found'] = True
    return _hero_lineup_payload(lineup, item=item)


def _same_hero_crop(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return all(
        abs(float(left[name]) - float(right[name])) < 0.000001
        for name in ('x', 'y', 'w', 'h')
    )


@app.put('/api/training-review/items/{frame_id}/hero-layout')
def api_save_training_review_hero_layout(
    frame_id: int, body: Dict[str, Any]
) -> Dict[str, Any]:
    screen_type = str(body.get('screen_type') or '')
    try:
        team_size = int(body.get('team_size'))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, '英雄阵容人数必须是 3 或 5') from exc
    raw_slots = body.get('slots')
    if not isinstance(raw_slots, list):
        raise HTTPException(400, '英雄头像框必须是列表')
    recognize = bool(body.get('recognize'))
    save_template = bool(body.get('save_template'))
    try:
        image_width = int(body.get('image_width') or 0)
        image_height = int(body.get('image_height') or 0)
    except (TypeError, ValueError):
        image_width = 0
        image_height = 0
    if image_width <= 0 or image_height <= 0:
        image_width = 0
        image_height = 0
    with _db_lock:
        conn = _conn()
        try:
            item = _single_training_review_item(conn, frame_id)
            existing = db.get_training_review_hero_lineup(conn, frame_id)
            if (
                item is not None
                and (int(item['width'] or 0) <= 0 or int(item['height'] or 0) <= 0)
                and image_width > 0
                and image_height > 0
            ):
                db.update_frame_dimensions(conn, frame_id, image_width, image_height)
                item['width'] = image_width
                item['height'] = image_height
        finally:
            conn.close()
    if item is None:
        raise HTTPException(404, '训练复核图片不存在')
    template_streamer = str(item['streamer'] or '').strip()
    slots = [
        {
            'side': value.get('side'),
            'slot': value.get('slot'),
            'crop': value.get('crop'),
        }
        for value in raw_slots
        if isinstance(value, dict)
    ]
    model_result: Optional[Dict[str, Any]] = None
    queued_job: Optional[Dict[str, Any]] = None
    template_saved = False
    try:
        if recognize and not config.CONTROL_PLANE_ONLY:
            with _db_lock:
                conn = _conn()
                try:
                    model_result = model_prefill.prefill_hero_slots(
                        conn,
                        Path(str(item['frame_path'])),
                        slots,
                        screen_type=screen_type,
                        team_size=team_size,
                    )
                finally:
                    conn.close()
            slots = model_result['slots']
        else:
            existing_slots = {
                (slot['side'], int(slot['slot'])): slot
                for slot in (existing or {}).get('slots', [])
            }
            for slot in slots:
                previous = existing_slots.get((str(slot['side']), int(slot['slot'])))
                if previous and _same_hero_crop(previous['crop'], slot['crop'] or {}):
                    slot['suggested_label'] = previous['suggested_label']
                    slot['suggestion_confidence'] = previous['suggestion_confidence']
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(422, f'英雄头像识别失败：{exc}') from exc
    with _db_lock:
        conn = _conn()
        try:
            try:
                lineup = db.replace_training_review_hero_layout(
                    conn,
                    frame_id=frame_id,
                    screen_type=screen_type,
                    team_size=team_size,
                    method=(
                        'manual-circle+hero-identity-v1'
                        if recognize and model_result and model_result.get('complete')
                        else (
                            'manual-circle+worker-pending-v1'
                            if recognize and config.CONTROL_PLANE_ONLY
                            else 'manual-circle-v1'
                        )
                    ),
                    slots=slots,
                    refresh_material_index=False,
                )
                if model_result and model_result.get('complete'):
                    _save_new_model_hero_prefill_source(
                        conn,
                        frame_id=frame_id,
                        item=item,
                        screen_type=screen_type,
                        team_size=team_size,
                        result=model_result,
                    )
                    item = _single_training_review_item(conn, frame_id) or item
                if (
                    save_template
                    and template_streamer
                    and int(item['width'] or 0) > 0
                    and int(item['height'] or 0) > 0
                ):
                    db.save_training_review_hero_template(
                        conn,
                        streamer=template_streamer,
                        screen_type=screen_type,
                        team_size=team_size,
                        layout_key=db.hero_layout_key(
                            int(item['width']), int(item['height'])
                        ),
                        slots=slots,
                    )
                    template_saved = True
                if recognize and config.CONTROL_PLANE_ONLY and slots:
                    models = model_prefill.latest_model_specs(
                        conn, ('hero_identity', 'player_position')
                    )
                    if models:
                        queued_job = _queue_model_prefill(
                            conn,
                            frame_id=frame_id,
                            operation='hero_slots',
                            models=models,
                            screen_type=screen_type,
                            team_size=team_size,
                            slots=[
                                {
                                    'side': value['side'],
                                    'slot': int(value['slot']),
                                    'crop': dict(value['crop']),
                                }
                                for value in slots
                            ],
                        )
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        finally:
            conn.close()
    lineup['template_saved'] = template_saved
    payload = _hero_lineup_payload(lineup, item=item)
    if queued_job is not None:
        payload['prefill_job'] = queued_job
    return payload


@app.get('/api/training-review/items/{frame_id}/heroes/{side}/{slot}/crop')
def api_training_review_hero_crop(frame_id: int, side: str, slot: int) -> Response:
    with _db_lock:
        conn = _conn()
        try:
            item = _single_training_review_item(conn, frame_id)
            lineup = db.get_training_review_hero_lineup(conn, frame_id)
        finally:
            conn.close()
    if item is None or lineup is None:
        raise HTTPException(404, '英雄阵容不存在')
    selected = next(
        (
            value
            for value in lineup['slots']
            if value['side'] == side and int(value['slot']) == slot
        ),
        None,
    )
    if selected is None:
        raise HTTPException(404, '英雄位置不存在')
    try:
        if config.MEDIA_SERVER_URL:
            content = hero_review.crop_image_content(
                _fetch_frame_image_bytes(frame_id), selected['crop']
            )
        else:
            content = hero_review.crop_image_bytes(
                Path(str(item['frame_path'])), selected['crop']
            )
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f'英雄头像裁剪失败：{exc}') from exc
    return Response(
        content=content,
        media_type='image/jpeg',
        headers={'Cache-Control': 'private, max-age=3600'},
    )


@app.put('/api/training-review/items/{frame_id}/hero-lineup')
def api_save_training_review_hero_lineup(
    frame_id: int, body: Dict[str, Any]
) -> Dict[str, Any]:
    labels = body.get('heroes')
    if not isinstance(labels, list):
        raise HTTPException(400, '英雄阵容必须是列表')
    try:
        allowed = hero_review.allowed_hero_labels()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    with _db_lock:
        conn = _conn()
        try:
            try:
                lineup = db.save_training_review_hero_lineup(
                    conn,
                    frame_id=frame_id,
                    labels=labels,
                    allowed_labels=allowed,
                    player_status=body.get('player_status'),
                    player_side=body.get('player_side'),
                    player_slot=body.get('player_slot'),
                )
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        finally:
            conn.close()
    return _hero_lineup_payload(lineup)


def _training_review_box(value: Any) -> Optional[Dict[str, float]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HTTPException(400, '结算框必须是对象')
    try:
        box = {name: float(value[name]) for name in ('x', 'y', 'w', 'h')}
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, '结算框需要 x/y/w/h 数字') from exc
    if not (
        0 <= box['x'] <= 1
        and 0 <= box['y'] <= 1
        and 0 < box['w'] <= 1
        and 0 < box['h'] <= 1
        and box['x'] + box['w'] <= 1.001
        and box['y'] + box['h'] <= 1.001
    ):
        raise HTTPException(400, '结算框坐标必须归一化到 [0,1]')
    return box


@app.put('/api/training-review/items/{frame_id}')
def api_save_training_review_item(
    frame_id: int, body: Dict[str, Any]
) -> Dict[str, Any]:
    status = str(body.get('review_status') or 'confirmed')
    result_label = body.get('result_panel_label')
    result_box = _training_review_box(body.get('result_box'))
    hero_lineup_body = body.get('hero_lineup')
    if hero_lineup_body is not None and not isinstance(hero_lineup_body, dict):
        raise HTTPException(400, '英雄阵容必须是对象')
    allowed_heroes: Optional[set[str]] = None
    if hero_lineup_body is not None:
        labels = hero_lineup_body.get('heroes')
        if not isinstance(labels, list):
            raise HTTPException(400, '英雄阵容必须是列表')
        try:
            allowed_heroes = hero_review.allowed_hero_labels()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
    with _db_lock:
        conn = _conn()
        try:
            if (
                conn.execute(
                    'SELECT 1 FROM training_review_items WHERE frame_id = ?',
                    (int(frame_id),),
                ).fetchone()
                is None
            ):
                raise HTTPException(404, '训练复核图片不存在')
            if result_label == 'result_panel':
                if result_box is None and 'result_panel' not in db.get_boxes(
                    conn, frame_id
                ):
                    raise HTTPException(400, '结算正样本必须框出完整结算面板')
                if result_box is not None:
                    db.save_box(
                        conn,
                        frame_id,
                        'result_panel',
                        result_box['x'],
                        result_box['y'],
                        result_box['w'],
                        result_box['h'],
                        commit=False,
                    )
            try:
                saved_lineup = None
                if hero_lineup_body is not None:
                    saved_lineup = db.save_training_review_hero_lineup(
                        conn,
                        frame_id=frame_id,
                        labels=hero_lineup_body['heroes'],
                        allowed_labels=allowed_heroes or set(),
                        player_status=hero_lineup_body.get('player_status'),
                        player_side=hero_lineup_body.get('player_side'),
                        player_slot=hero_lineup_body.get('player_slot'),
                        refresh_material_index=False,
                        commit=False,
                    )
                saved = db.save_training_review(
                    conn,
                    frame_id=frame_id,
                    match_flow_label=body.get('match_flow_label'),
                    match_mode_label=body.get('match_mode_label'),
                    hero_select_label=body.get('hero_select_label'),
                    hero_select_variant=body.get('hero_select_variant'),
                    hero_select_visibility=body.get('hero_select_visibility'),
                    result_panel_label=result_label,
                    hero_layout_label=body.get('hero_layout_label'),
                    panel_render_state=str(body.get('panel_render_state') or 'clear'),
                    ocr_usable=str(body.get('ocr_usable') or 'yes'),
                    result_occlusion=str(body.get('result_occlusion') or 'none'),
                    occluder_types=body.get('occluder_types') or [],
                    status=status,
                    notes=str(body.get('notes') or ''),
                    result_groups={},
                    hydrate=False,
                    commit=True,
                )
                if saved_lineup is not None:
                    saved['hero_lineup'] = _hero_lineup_payload(saved_lineup)
                _mark_training_review_saved(frame_id)
                return saved
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        finally:
            conn.close()


@app.get('/api/worker-candidates/items')
def api_worker_candidate_items(
    task: str = '',
    status: str = 'pending',
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            try:
                items = db.list_worker_candidates(
                    conn, task=task, status=status, limit=limit, offset=offset
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            return {'items': items, 'stats': db.worker_candidate_stats(conn)}
        finally:
            conn.close()


@app.put('/api/worker-candidates/items/{candidate_id}')
def api_review_worker_candidate(
    candidate_id: int, body: Dict[str, Any]
) -> Dict[str, Any]:
    raw_label = body.get('label')
    label = None if raw_label in (None, '', 'skip') else str(raw_label)
    visual_condition = str(body.get('visual_condition') or 'clear')
    boxes = body.get('boxes') or []
    notes = str(body.get('notes') or '')
    with _db_lock:
        conn = _conn()
        try:
            item = db.get_worker_candidate(conn, candidate_id)
            if item is None:
                raise HTTPException(404, 'worker 候选不存在')
            try:
                if item['task'] == 'bp_review':
                    db.review_bp_item(
                        conn,
                        frame_id=int(item['frame_id']),
                        label=label,
                        visual_condition=visual_condition,
                    )
                elif item['task'] == 'key_screen_review':
                    db.review_key_screen_item(
                        conn,
                        frame_id=int(item['frame_id']),
                        label=label,
                        visual_condition=visual_condition,
                    )
                return db.review_worker_candidate(
                    conn,
                    candidate_id=candidate_id,
                    label=label,
                    visual_condition=visual_condition,
                    boxes=boxes,
                    notes=notes,
                )
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        finally:
            conn.close()


def _set_bp_collect_state(**values: Any) -> None:
    with _bp_collect_lock:
        _bp_collect_state.update(values)


def _collect_bp_candidates(
    *,
    model_name: str,
    maximum_scan: int,
    maximum_items: int,
    maximum_per_video: int,
    video_ids: List[int],
) -> None:
    _set_bp_collect_state(
        running=True,
        model=model_name,
        scanned=0,
        total=0,
        selected=0,
        inserted=0,
        failed=0,
        error=None,
    )
    try:
        with _db_lock:
            conn = _conn()
            try:
                where = ["f.frame_path != ''", 'f.labeled = 0']
                args: List[Any] = []
                if video_ids:
                    placeholders = ','.join('?' for _ in video_ids)
                    where.append(f'f.video_id IN ({placeholders})')
                    args.extend(video_ids)
                rows = [
                    dict(row)
                    for row in conn.execute(
                        'SELECT f.id AS frame_id, f.video_id, f.timestamp_ms, '
                        'f.frame_path, f.phash, v.streamer, v.filename '
                        'FROM frames f JOIN videos v ON v.id = f.video_id '
                        'WHERE '
                        + ' AND '.join(where)
                        + ' ORDER BY f.video_id, f.timestamp_ms',
                        args,
                    ).fetchall()
                ]
            finally:
                conn.close()
        frames = bp_review.balanced_frame_rows(rows, maximum=maximum_scan)
        _set_bp_collect_state(total=len(frames))
        observations = []
        failed = 0
        for index, frame in enumerate(frames, start=1):
            path = Path(frame['frame_path'])
            try:
                if not path.is_file():
                    raise FileNotFoundError(path)
                prediction = inference_mod.run_model(model_name, path)
                observations.append(
                    bp_review.observation_from_prediction(frame, prediction)
                )
            except Exception:  # noqa: BLE001
                failed += 1
            _set_bp_collect_state(scanned=index, failed=failed)
        candidates = bp_review.select_candidates(
            observations, maximum=maximum_items, maximum_per_video=maximum_per_video
        )
        inserted = 0
        with _db_lock:
            conn = _conn()
            try:
                for candidate in candidates:
                    inserted += int(
                        db.upsert_bp_review_item(
                            conn,
                            frame_id=int(candidate['frame_id']),
                            model_version=model_name,
                            suggested_label=candidate['suggested_label'],
                            suggestion_confidence=candidate['suggestion_confidence'],
                            stage_class=candidate['stage_class'],
                            stage_confidence=candidate['stage_confidence'],
                            pre_match_confidence=candidate['pre_match_confidence'],
                            mode_class=candidate['mode_class'],
                            mode_confidence=candidate['mode_confidence'],
                            mode_margin=candidate['mode_margin'],
                            selection_reason=candidate['selection_reason'],
                            priority=candidate['priority'],
                            raw_prediction=candidate['raw_prediction'],
                        )
                    )
            finally:
                conn.close()
        _set_bp_collect_state(
            selected=len(candidates), inserted=inserted, running=False
        )
    except Exception as exc:  # noqa: BLE001
        _set_bp_collect_state(running=False, error=str(exc))


@app.post('/api/bp-review/collect')
def api_collect_bp_review(body: Dict[str, Any]) -> Dict[str, Any]:
    _require_local_heavy_operation('旧 BP 候选批量推理')
    with _bp_collect_lock:
        if _bp_collect_state['running']:
            raise HTTPException(409, 'BP 候选收集任务已在运行')
    model_name = str(body.get('model_name') or 'multi-v2')
    maximum_scan = int(body.get('maximum_scan', 3000))
    maximum_items = int(body.get('maximum_items', 300))
    maximum_per_video = int(body.get('maximum_per_video', 24))
    video_ids = [int(value) for value in body.get('video_ids', [])]
    if not 100 <= maximum_scan <= 50_000:
        raise HTTPException(400, 'maximum_scan 必须在 100 到 50000 之间')
    if not 10 <= maximum_items <= 2_000:
        raise HTTPException(400, 'maximum_items 必须在 10 到 2000 之间')
    if not 3 <= maximum_per_video <= 100:
        raise HTTPException(400, 'maximum_per_video 必须在 3 到 100 之间')
    model = next(
        (
            item
            for item in inference_mod.list_models()
            if item['name'] == model_name and item['task'] == 'multi'
        ),
        None,
    )
    if model is None:
        raise HTTPException(400, f'模型 {model_name} 不是可用的多头分类模型')
    threading.Thread(
        target=_collect_bp_candidates,
        kwargs={
            'model_name': model_name,
            'maximum_scan': maximum_scan,
            'maximum_items': maximum_items,
            'maximum_per_video': maximum_per_video,
            'video_ids': video_ids,
        },
        daemon=True,
    ).start()
    return {'started': True, 'model': model_name}


@app.get('/api/bp-review/state')
def api_bp_review_state() -> Dict[str, Any]:
    with _bp_collect_lock:
        state = dict(_bp_collect_state)
    with _worker_candidate_sync_lock:
        state['worker_sync'] = dict(_worker_candidate_sync_state)
    with _db_lock:
        conn = _conn()
        try:
            state['review'] = db.bp_review_stats(conn)
        finally:
            conn.close()
    return state


@app.get('/api/bp-review/items')
def api_bp_review_items(
    status: str = 'pending',
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            try:
                items = db.list_bp_review_items(
                    conn, status=status, limit=limit, offset=offset
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            return {'items': items, 'stats': db.bp_review_stats(conn)}
        finally:
            conn.close()


@app.put('/api/bp-review/items/{frame_id}')
def api_review_bp_item(frame_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    value = body.get('label')
    label = None if value in (None, '', 'skip') else str(value)
    visual_condition = str(body.get('visual_condition') or 'clear')
    with _db_lock:
        conn = _conn()
        try:
            try:
                result = db.review_bp_item(
                    conn,
                    frame_id=frame_id,
                    label=label,
                    visual_condition=visual_condition,
                )
                db.mark_worker_candidate_review_for_frame(
                    conn,
                    frame_id=frame_id,
                    task='bp_review',
                    label=label,
                    visual_condition=visual_condition,
                )
                training_review.mirror_confirmed_bp_review(conn, frame_id)
                return result
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        finally:
            conn.close()


# ---------- 结算页 / 计分板主动学习复核 ----------


@app.get('/api/key-screen-review/state')
def api_key_screen_review_state() -> Dict[str, Any]:
    with _worker_candidate_sync_lock:
        state = {'worker_sync': dict(_worker_candidate_sync_state)}
    with _db_lock:
        conn = _conn()
        try:
            state['review'] = db.key_screen_review_stats(conn)
        finally:
            conn.close()
    return state


@app.get('/api/key-screen-review/items')
def api_key_screen_review_items(
    status: str = 'pending',
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            try:
                items = db.list_key_screen_review_items(
                    conn, status=status, limit=limit, offset=offset
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            return {'items': items, 'stats': db.key_screen_review_stats(conn)}
        finally:
            conn.close()


@app.put('/api/key-screen-review/items/{frame_id}')
def api_review_key_screen_item(frame_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    value = body.get('label')
    label = None if value in (None, '', 'skip') else str(value)
    visual_condition = str(body.get('visual_condition') or 'clear')
    with _db_lock:
        conn = _conn()
        try:
            try:
                result = db.review_key_screen_item(
                    conn,
                    frame_id=frame_id,
                    label=label,
                    visual_condition=visual_condition,
                )
                db.mark_worker_candidate_review_for_frame(
                    conn,
                    frame_id=frame_id,
                    task='key_screen_review',
                    label=label,
                    visual_condition=visual_condition,
                )
                training_review.mirror_confirmed_key_screen_review(conn, frame_id)
                return result
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        finally:
            conn.close()


@app.get('/api/models')
def api_models() -> List[Dict[str, Any]]:
    """可用模型列表(扫描 models/*.onnx)。"""
    return inference_mod.list_models()


@app.post('/api/models/{model_name}/test')
def api_model_test(model_name: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """对指定帧跑模型推理,返回原始输出 + 格式化结果。"""
    _require_local_heavy_operation('旧模型单帧推理')
    frame_id = body.get('frame_id')
    if not frame_id:
        raise HTTPException(400, '需要 frame_id')
    with _db_lock:
        conn = _conn()
        try:
            f = db.get_frame(conn, int(frame_id))
            if not f:
                raise HTTPException(404, '帧不存在')
            frame_path = Path(f['frame_path'])
        finally:
            conn.close()
    if not frame_path.exists():
        raise HTTPException(404, f'帧文件不存在: {frame_path}')
    try:
        result = inference_mod.run_model(
            model_name, frame_path, conf_thr=body.get('conf_thr', 0.25)
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # 推理异常(模型结构不匹配等)
        raise HTTPException(500, f'推理失败: {e}')
    result['frame_id'] = int(frame_id)
    return result


@app.get('/api/model-tests/runs')
def api_model_test_runs() -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            return {'runs': model_testing.list_testable_runs(conn)}
        finally:
            conn.close()


@app.get('/api/model-tests/runs/{run_id}/samples')
def api_model_test_samples(
    run_id: str, split: str = 'test', limit: int = Query(500, ge=1, le=2000)
) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            try:
                return model_testing.list_run_samples(
                    conn, run_id, split=split, limit=limit
                )
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(400, str(exc))
        finally:
            conn.close()


@app.post('/api/model-tests/runs/{run_id}/predict')
def api_model_test_run_predict(run_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    sample_id = str(body.get('sample_id') or '')
    split = str(body.get('split') or '')
    if not sample_id or split not in {
        'train',
        'val',
        'test',
        'scoreboard_challenge',
        'post_run_challenge',
    }:
        raise HTTPException(400, '需要有效的 sample_id 和 split')
    if config.CONTROL_PLANE_ONLY:
        with _db_lock:
            conn = _conn()
            try:
                try:
                    model_testing.worker_evaluation_plan(conn, run_id, split=split)
                    job = _queue_model_validation(
                        conn,
                        run_id=run_id,
                        split=split,
                        sample_id=sample_id,
                        conf_thr=float(body.get('conf_thr', 0.25)),
                        iou_threshold=float(body.get('iou_threshold', 0.5)),
                    )
                except KeyError as exc:
                    raise HTTPException(404, str(exc)) from exc
                except (FileNotFoundError, ValueError) as exc:
                    raise HTTPException(400, str(exc)) from exc
                return {'queued': True, 'job': job}
            finally:
                conn.close()
    with _db_lock:
        conn = _conn()
        try:
            try:
                return model_testing.predict_run_sample(
                    conn,
                    run_id,
                    sample_id=sample_id,
                    split=split,
                    conf_thr=float(body.get('conf_thr', 0.25)),
                )
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(400, str(exc))
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(500, f'推理失败: {exc}')
        finally:
            conn.close()


@app.post('/api/model-tests/runs/{run_id}/batch')
def api_model_test_run_batch(run_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    split = str(body.get('split') or '')
    if split not in {
        'train',
        'val',
        'test',
        'scoreboard_challenge',
        'post_run_challenge',
    }:
        raise HTTPException(400, '需要有效的 split')
    if config.CONTROL_PLANE_ONLY:
        with _db_lock:
            conn = _conn()
            try:
                try:
                    model_testing.worker_evaluation_plan(conn, run_id, split=split)
                    job = _queue_model_validation(
                        conn,
                        run_id=run_id,
                        split=split,
                        conf_thr=float(body.get('conf_thr', 0.25)),
                        iou_threshold=float(body.get('iou_threshold', 0.5)),
                    )
                except KeyError as exc:
                    raise HTTPException(404, str(exc)) from exc
                except (FileNotFoundError, ValueError) as exc:
                    raise HTTPException(400, str(exc)) from exc
                return {'queued': True, 'job': job}
            finally:
                conn.close()
    with _db_lock:
        conn = _conn()
        try:
            try:
                return model_testing.evaluate_run_samples(
                    conn,
                    run_id,
                    split=split,
                    conf_thr=float(body.get('conf_thr', 0.25)),
                    iou_threshold=float(body.get('iou_threshold', 0.5)),
                )
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(400, str(exc))
        finally:
            conn.close()


@app.get('/api/model-tests/runs/{run_id}/samples/{sample_id}/image')
def api_model_test_sample_image(run_id: str, sample_id: str, split: str) -> Response:
    with _db_lock:
        conn = _conn()
        try:
            try:
                if config.MEDIA_SERVER_URL:
                    reference = model_testing.run_sample_image_reference(
                        conn, run_id, sample_id=sample_id, split=split
                    )
                    path = None
                else:
                    path = model_testing.run_sample_image_path(
                        conn, run_id, sample_id=sample_id, split=split
                    )
                    reference = None
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(400, str(exc))
        finally:
            conn.close()
    if reference is not None:
        content = _fetch_frame_image_bytes(int(reference['frame_id']))
        crop = reference.get('crop')
        if isinstance(crop, dict):
            content = hero_review.crop_image_content(content, crop)
        return Response(content=content, media_type='image/jpeg')
    assert path is not None
    return FileResponse(path, media_type='image/jpeg')


@app.put('/api/model-tests/runs/{run_id}/validation')
def api_validate_model_run(run_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            try:
                return model_testing.validate_run(
                    conn,
                    run_id,
                    status=str(body.get('status') or ''),
                    notes=str(body.get('notes') or ''),
                )
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        finally:
            conn.close()


@app.get('/api/model-packages')
def api_model_packages() -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            return {'packages': db.list_model_packages(conn)}
        finally:
            conn.close()


@app.post('/api/model-packages')
def api_build_model_package(body: Dict[str, Any]) -> Dict[str, Any]:
    run_ids = [str(value) for value in body.get('run_ids') or []]
    if config.CONTROL_PLANE_ONLY:
        with _db_lock:
            conn = _conn()
            try:
                try:
                    plan = model_testing.prepare_model_package(
                        conn, run_ids, package_id=str(body.get('package_id') or '')
                    )
                    job = vision_jobs.create_job(
                        conn,
                        kind='package_models',
                        related_id=str(plan['package_id']),
                        priority=60,
                        payload=plan,
                    )
                    return {
                        'id': plan['package_id'],
                        'status': plan['status'],
                        'missing_tasks': plan['missing_tasks'],
                        'evaluation_gaps': plan['evaluation_gaps'],
                        'queued': True,
                        'job': job,
                    }
                except KeyError as exc:
                    raise HTTPException(404, str(exc)) from exc
                except (FileNotFoundError, ValueError) as exc:
                    raise HTTPException(400, str(exc)) from exc
            finally:
                conn.close()
    with _db_lock:
        conn = _conn()
        try:
            try:
                return model_testing.build_model_package(
                    conn, run_ids, package_id=str(body.get('package_id') or '')
                )
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(400, str(exc))
        finally:
            conn.close()


@app.get('/api/model-packages/{package_id}/archive')
def api_model_package_archive(package_id: str) -> FileResponse:
    with _db_lock:
        conn = _conn()
        try:
            try:
                archive = model_testing.model_package_archive(conn, package_id)
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except (FileNotFoundError, ValueError) as exc:
                if not config.MEDIA_SERVER_URL:
                    raise HTTPException(400, str(exc))
                try:
                    archive = managed_assets.resolve_model_package_archive(package_id)
                except (FileNotFoundError, RuntimeError, ValueError) as error:
                    raise HTTPException(400, str(error)) from error
        finally:
            conn.close()
    return FileResponse(archive, media_type='application/zip', filename=archive.name)


def _deploy_model_package_to_worker(deployment_id: int, package_id: str) -> None:
    with _worker_deployment_lock:
        try:
            target = worker_deployment.configured_target()
            with _db_lock:
                conn = _conn()
                try:
                    db.update_model_deployment(
                        conn, deployment_id=deployment_id, status='running'
                    )
                    try:
                        archive = model_testing.model_package_archive(conn, package_id)
                    except (FileNotFoundError, ValueError):
                        archive = managed_assets.resolve_model_package_archive(
                            package_id
                        )
                finally:
                    conn.close()
            result = worker_deployment.WorkerDeploymentClient(target).deploy(
                archive, package_id
            )
            with _db_lock:
                conn = _conn()
                try:
                    db.update_model_deployment(
                        conn,
                        deployment_id=deployment_id,
                        status='succeeded',
                        previous_package_id=str(
                            result.get('previous_package_id') or ''
                        ),
                        worker_package_id=str(result.get('package_id') or ''),
                        error='',
                        detail=result,
                    )
                finally:
                    conn.close()
        except Exception as error:  # noqa: BLE001
            with _db_lock:
                conn = _conn()
                try:
                    current = db.get_model_deployment(conn, deployment_id)
                    if current is not None and current['status'] in {
                        'queued',
                        'running',
                    }:
                        db.update_model_deployment(
                            conn,
                            deployment_id=deployment_id,
                            status='failed',
                            error='{}: {}'.format(type(error).__name__, error),
                        )
                finally:
                    conn.close()


@app.get('/api/model-deployments')
def api_model_deployments(probe: bool = Query(False)) -> Dict[str, Any]:
    target_payload: Dict[str, Any] = {'configured': False}
    live: Optional[Dict[str, Any]] = None
    probe_error = ''
    try:
        target = worker_deployment.configured_target()
        target_payload = {
            'configured': True,
            'display_name': target.display_name,
            'model_root': target.model_root,
            'launchd_label': target.launchd_label,
        }
        if probe:
            try:
                live = worker_deployment.WorkerDeploymentClient(target).status()
            except (OSError, RuntimeError, ValueError) as error:
                probe_error = str(error)
    except ValueError as error:
        probe_error = str(error)
    with _db_lock:
        conn = _conn()
        try:
            deployments = db.list_model_deployments(conn, limit=30)
        finally:
            conn.close()
    return {
        'target': target_payload,
        'live': live,
        'probe_error': probe_error,
        'deployments': deployments,
    }


@app.post('/api/model-packages/{package_id}/deploy-worker')
def api_deploy_model_package_to_worker(package_id: str) -> Dict[str, Any]:
    try:
        target = worker_deployment.configured_target()
    except ValueError as error:
        raise HTTPException(400, str(error))
    with _db_lock:
        conn = _conn()
        try:
            try:
                deployment = db.create_model_deployment(
                    conn, package_id=package_id, target='analysis-worker'
                )
            except KeyError as error:
                raise HTTPException(404, str(error))
            except ValueError as error:
                raise HTTPException(409, str(error))
        finally:
            conn.close()
    threading.Thread(
        target=_deploy_model_package_to_worker,
        args=(int(deployment['id']), package_id),
        name='worker-model-deployment',
        daemon=True,
    ).start()
    return {'deployment': deployment, 'target': target.display_name}


@app.get('/api/events')
def api_events(video_id: Optional[int] = None) -> List[Dict[str, Any]]:
    with _db_lock:
        conn = _conn()
        try:
            return (
                db.event_stats(conn)
                if not video_id
                else [e for e in db.event_stats(conn) if e['video_id'] == video_id]
            )
        finally:
            conn.close()


@app.get('/api/events/{event_id}')
def api_event(event_id: int) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            ev = conn.execute(
                'SELECT e.*, v.streamer, v.remote_path FROM events e '
                'JOIN videos v ON v.id = e.video_id WHERE e.id = ?',
                (event_id,),
            ).fetchone()
            if not ev:
                raise HTTPException(404, '事件不存在')
            frames = db.query_frames(conn, event_id=event_id, limit=1000)
            return {'event': dict(ev), 'frames': frames}
        finally:
            conn.close()


@app.post('/api/events/auto-group')
def api_auto_group(body: Dict[str, Any]) -> Dict[str, Any]:
    video_id = body.get('video_id')
    with _db_lock:
        conn = _conn()
        try:
            if video_id:
                created = events.auto_group(conn, int(video_id))
            else:
                created = []
                for vid, evs in events.group_all_unassigned(conn).items():
                    created.extend(evs)
            return {'events': len(created)}
        finally:
            conn.close()


@app.post('/api/events/{event_id}/merge')
def api_merge_events(event_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    with_ids = [int(x) for x in body.get('with_ids', [])]
    with _db_lock:
        conn = _conn()
        try:
            keep = db.merge_events(conn, [event_id] + with_ids)
            db.audit(
                conn,
                'event_merge',
                event_id=keep,
                detail=f'merged {[event_id] + with_ids}',
            )
            return {'event_id': keep}
        finally:
            conn.close()


@app.post('/api/events/{event_id}/split')
def api_split_event(event_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    at_ms = int(body.get('at_ms', 0))
    with _db_lock:
        conn = _conn()
        try:
            new_id = db.split_event(conn, event_id, at_ms)
            db.audit(
                conn,
                'event_split',
                event_id=event_id,
                detail=f'split at {at_ms} -> new {new_id}',
            )
            return {'event_id': event_id, 'new_event_id': new_id}
        finally:
            conn.close()


# ---------- 标注(条件式) ----------


def _validate_annotation(body: Dict[str, Any]) -> Dict[str, Any]:
    """按分层规则清洗并校验标注;矛盾字段自动清空。"""
    cf = body.get('content_family')
    if cf not in config.CONTENT_FAMILIES:
        raise HTTPException(
            400, f'content_family 必须是 {list(config.CONTENT_FAMILIES)}'
        )
    data = {
        'content_family': cf,
        'non_vainglory_type': None,
        'game_context': None,
        'screen_type': None,
        'game_mode': None,
        'match_kind': body.get('match_kind') or 'unknown',
        'view_context': body.get('view_context') or 'unknown',
        'quality_flags': [
            f
            for f in body.get('quality_flags', [])
            if f in {x[0] for x in config.QUALITY_FLAGS}
        ],
        'black_bars': body.get('black_bars') or 'none',
        'ocr_usable': None,
        'notes': str(body.get('notes', ''))[:1000],
    }
    if cf == 'not_vainglory':
        nvt = body.get('non_vainglory_type')
        if nvt is not None and nvt not in config.NON_VAINGLORY_TYPES:
            raise HTTPException(400, f'未知非虚荣类型: {nvt}')
        data['non_vainglory_type'] = nvt
        # 非虚荣时清空游戏内字段
        data['game_mode'] = None
        return data
    if cf == 'uncertain':
        return data
    # vainglory:对局阶段未选时允许保存草稿(界面继续引导),仅校验已填值
    gc = body.get('game_context')
    if gc is not None and gc not in config.GAME_STAGES:
        raise HTTPException(400, f'game_context 必须是 {list(config.GAME_STAGES)}')
    data['game_context'] = gc
    if gc is None:
        return data
    valid = config.STAGE_SCREEN_TYPES.get(gc, {})
    st = body.get('screen_type')
    if st is not None and not valid:
        raise HTTPException(400, f'阶段 {gc} 没有具体界面')
    if st is not None and st not in valid:
        raise HTTPException(400, f'screen_type {st} 不属于 {gc}')
    data['screen_type'] = st
    # 积分板不允许带结算框(前端也会清,后端兜底)
    if st in config.SCOREBOARD_HARD_NEGATIVE:
        data['ocr_usable'] = None
    gm = body.get('game_mode')
    if gm is not None and gm not in config.GAME_MODES:
        raise HTTPException(400, f'game_mode 必须是 {list(config.GAME_MODES)}')
    data['game_mode'] = gm
    if st == 'result_page':
        ocr = body.get('ocr_usable')
        if ocr is not None and ocr not in config.OCR_USABLE:
            raise HTTPException(400, f'ocr_usable 必须是 {list(config.OCR_USABLE)}')
        # 默认值:可 OCR、清晰、无遮挡;只在异常时特意标注
        data['ocr_usable'] = ocr or 'yes'
        # 结算框评估字段
        rc = body.get('result_clarity')
        if rc is not None and rc not in config.RESULT_CLARITY:
            raise HTTPException(
                400, f'result_clarity 必须是 {list(config.RESULT_CLARITY)}'
            )
        data['result_clarity'] = rc or 'clear'
        ro = body.get('result_occlusion')
        if ro is not None and ro not in config.RESULT_OCCLUSION:
            raise HTTPException(
                400, f'result_occlusion 必须是 {list(config.RESULT_OCCLUSION)}'
            )
        data['result_occlusion'] = ro or 'none'
        occl = [
            o
            for o in body.get('occluder_types', [])
            if o in {x[0] for x in config.OCCLUDER_TYPES}
        ]
        data['occluder_types'] = occl if ro != 'none' else []
    else:
        data['ocr_usable'] = None
        data['result_clarity'] = None
        data['result_occlusion'] = None
        data['occluder_types'] = []
    return data


@app.put('/api/frames/{frame_id}/annotation')
def api_save_annotation(frame_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    data = _validate_annotation(body)
    data['talent_mode'] = body.get('talent_mode')
    status = body.get('annotation_status', 'draft')
    if status not in config.ANNOTATION_STATUSES:
        status = 'draft'
    with _db_lock:
        conn = _conn()
        try:
            if not db.get_frame(conn, frame_id):
                raise HTTPException(404, '帧不存在')
            # 兜底清理:界面类型对应唯一面板框;清掉不匹配的
            expected = {
                'ingame_shop': 'shop_panel',
                'equipment_select': 'equipment_panel',
                'talent_select': 'talent_panel',
                'scoreboard': 'scoreboard_panel',
                'death_scoreboard': 'scoreboard_panel',
                'result_page': 'result_panel',
            }.get(data['screen_type'])
            for bt in (
                'shop_panel',
                'scoreboard_panel',
                'result_panel',
                'equipment_panel',
                'talent_panel',
            ):
                if bt != expected:
                    db.delete_box(conn, frame_id, bt)
            # 兜底补框:面板类型但没有对应框时,自动带出主播历史框
            # (无论前端时序如何,面板标注永远不会无框)
            if expected and expected not in db.get_boxes(conn, frame_id):
                srow = conn.execute(
                    'SELECT v.streamer FROM frames f JOIN videos v '
                    'ON v.id = f.video_id WHERE f.id = ?',
                    (frame_id,),
                ).fetchone()
                if srow and srow['streamer']:
                    sb = conn.execute(
                        'SELECT x, y, w, h FROM streamer_boxes '
                        'WHERE streamer = ? AND box_type = ?',
                        (srow['streamer'], expected),
                    ).fetchone()
                    if sb:
                        db.save_box(
                            conn, frame_id, expected, sb['x'], sb['y'], sb['w'], sb['h']
                        )
            if data['content_family'] != 'vainglory':
                # 非虚荣画面没有游戏窗口,也不该有任何面板框
                for bt in (
                    'viewport',
                    'result_panel',
                    'scoreboard_panel',
                    'shop_panel',
                    'equipment_panel',
                    'talent_panel',
                ):
                    db.delete_box(conn, frame_id, bt)
            # 撤销快照:记录旧值
            old = db.get_annotation(conn, frame_id)
            db.audit(
                conn,
                'label',
                frame_id=frame_id,
                detail=json.dumps(old or {}, ensure_ascii=False),
            )
            db.save_annotation(conn, frame_id, data, status=status)
            return db.get_annotation(conn, frame_id)
        finally:
            conn.close()


@app.put('/api/frames/{frame_id}/box')
def api_save_box(frame_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    box_type = body.get('box_type')
    if box_type not in config.BOX_TYPES:
        raise HTTPException(400, f'box_type 必须是 {list(config.BOX_TYPES)}')
    try:
        x, y, w, h = (float(body[k]) for k in ('x', 'y', 'w', 'h'))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, '需要 x/y/w/h 数字')
    if not (
        0 <= x <= 1
        and 0 <= y <= 1
        and 0 < w <= 1
        and 0 < h <= 1
        and x + w <= 1.001
        and y + h <= 1.001
    ):
        raise HTTPException(400, '框坐标必须归一化到 [0,1]')
    with _db_lock:
        conn = _conn()
        try:
            f = db.get_frame(conn, frame_id)
            if not f:
                raise HTTPException(404, '帧不存在')
            # 非虚荣画面没有游戏窗口/面板框,禁止画框
            ann = db.get_annotation(conn, frame_id)
            if ann and ann.get('content_family') == 'not_vainglory':
                raise HTTPException(400, '非虚荣画面没有游戏窗口/面板框,不能画框')
            # 面板框必须与当前标注的界面类型匹配(防预选带框/旧保存竞态写回不匹配的框)
            # 未标注或尚未选择界面类型时允许先画框(先画框再选类型的流程)
            panel_required = {
                'shop_panel': {'ingame_shop'},
                'equipment_panel': {'equipment_select'},
                'talent_panel': {'talent_select'},
                'scoreboard_panel': {'scoreboard', 'death_scoreboard'},
                'result_panel': {'result_page'},
            }
            if box_type in panel_required:
                st = (ann or {}).get('screen_type')
                if st and st not in panel_required[box_type]:
                    raise HTTPException(
                        400,
                        f'{box_type} 与当前界面类型 {st} 不匹配,'
                        '请先切换界面类型再画框',
                    )
            old = db.get_boxes(conn, frame_id).get(box_type)
            db.audit(
                conn,
                'box',
                frame_id=frame_id,
                detail=json.dumps({'box_type': box_type, 'old': old}),
            )
            db.save_box(conn, frame_id, box_type, x, y, w, h)
            return db.get_boxes(conn, frame_id)
        finally:
            conn.close()


@app.delete('/api/frames/{frame_id}/box')
def api_delete_box(frame_id: int, box_type: str) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            db.delete_box(conn, frame_id, box_type)
            db.audit(conn, 'box', frame_id=frame_id, detail=f'delete {box_type}')
            return {'deleted': box_type}
        finally:
            conn.close()


@app.post('/api/frames/{frame_id}/propagate')
def api_propagate(frame_id: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """把帧的部分标注字段批量传播到同事件帧。"""
    fields = [
        f
        for f in body.get('fields', [])
        if f
        in (
            'game_mode',
            'view_context',
            'match_kind',
            'quality_flags',
            'black_bars',
            'viewport_bbox',
        )
    ]
    if not fields:
        raise HTTPException(400, '没有可传播字段')
    with _db_lock:
        conn = _conn()
        try:
            f = db.get_frame(conn, frame_id)
            if not f:
                raise HTTPException(404, '帧不存在')
            event_id = f['event_id']
            if not event_id:
                raise HTTPException(400, '该帧不属于任何事件,无法传播')
            src = db.get_annotation(conn, frame_id) or {}
            boxes = db.get_boxes(conn, frame_id)
            targets = [
                r['id']
                for r in conn.execute(
                    'SELECT id FROM frames WHERE event_id = ? AND id != ?',
                    (event_id, frame_id),
                ).fetchall()
            ]
            count = 0
            for tid in targets:
                cur = db.get_annotation(conn, tid) or {}
                changed = False
                for fld in fields:
                    if fld == 'viewport_bbox':
                        if 'viewport' in boxes:
                            b = boxes['viewport']
                            db.save_box(
                                conn, tid, 'viewport', b['x'], b['y'], b['w'], b['h']
                            )
                            changed = True
                        continue
                    if fld == 'quality_flags':
                        cur['quality_flags'] = list(src.get('quality_flags', []))
                    else:
                        cur[fld] = src.get(fld)
                    changed = True
                if changed:
                    db.save_annotation(conn, tid, cur)
                    count += 1
            db.audit(
                conn,
                'propagate',
                frame_id=frame_id,
                detail=f'fields={fields} targets={count}',
            )
            return {'propagated': count, 'fields': fields}
        finally:
            conn.close()


@app.post('/api/undo')
def api_undo() -> Dict[str, Any]:
    """撤销最近一次标注/框修改(audit_log 快照恢复)。"""
    with _db_lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT * FROM audit_log WHERE action IN ('label','box') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return {'undone': False, 'reason': '没有可撤销操作'}
            if row['action'] == 'label' and row['frame_id']:
                old = json.loads(row['detail'] or 'null') or {}
                if old:
                    db.save_annotation(conn, row['frame_id'], old)
            elif row['action'] == 'box' and row['frame_id']:
                detail = json.loads(row['detail'] or '{}')
                if detail.get('old') is None:
                    db.delete_box(conn, row['frame_id'], detail['box_type'])
                else:
                    b = detail['old']
                    db.save_box(
                        conn,
                        row['frame_id'],
                        detail['box_type'],
                        b['x'],
                        b['y'],
                        b['w'],
                        b['h'],
                    )
            conn.execute('DELETE FROM audit_log WHERE id = ?', (row['id'],))
            conn.commit()
            return {
                'undone': True,
                'action': row['action'],
                'frame_id': row['frame_id'],
            }
        finally:
            conn.close()


@app.get('/api/audit')
def api_audit(limit: int = 50) -> List[Dict[str, Any]]:
    with _db_lock:
        conn = _conn()
        try:
            return db.audit_recent(conn, limit=limit)
        finally:
            conn.close()


# ---------- 同局配对 ----------


@app.get('/api/pairs')
def api_pairs(limit: int = 100) -> List[Dict[str, Any]]:
    with _db_lock:
        conn = _conn()
        try:
            rows = conn.execute(
                'SELECT * FROM pair_annotations ORDER BY id DESC LIMIT ?', (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


@app.post('/api/pairs')
def api_save_pair(body: Dict[str, Any]) -> Dict[str, Any]:
    a = int(body.get('frame_a_id', 0))
    b = int(body.get('frame_b_id', 0))
    label = body.get('label')
    if label not in ('same_match', 'different_match', 'uncertain'):
        raise HTTPException(400, 'label 必须是 same_match/different_match/uncertain')
    if a == b:
        raise HTTPException(400, '不能配对同一帧')
    with _db_lock:
        conn = _conn()
        try:
            db.save_pair(conn, a, b, label)
            db.audit(conn, 'pair', detail=f'{a} x {b} = {label}')
            return {'saved': True}
        finally:
            conn.close()


# ---------- 导出与版本 ----------


@app.post('/api/export')
def api_export(body: Dict[str, Any]) -> Dict[str, Any]:
    task_id = body.get('task_id', 'result_detector')
    if config.CONTROL_PLANE_ONLY:
        definition = training.TRAINING_TASKS.get(str(task_id))
        if definition is None or not definition.get('active', True):
            _require_local_heavy_operation('旧数据集物化导出')
        with _db_lock:
            conn = _conn()
            try:
                return training.export_snapshot(conn, str(task_id), materialize=False)
            except RuntimeError as exc:
                raise HTTPException(400, str(exc)) from exc
            finally:
                conn.close()
    with _db_lock:
        conn = _conn()
        try:
            if task_id == 'result_detector':
                return export_mod.export_result_detector(
                    conn, include_negatives=body.get('include_negatives', True)
                )
            if task_id == 'bp_review':
                return export_mod.export_bp_classifier(conn)
            if task_id == 'key_screen_review':
                return export_mod.export_key_screen_classifier(conn)
            if task_id == 'mode_gate':
                return export_mod.export_mode_gate_detector(conn)
            return export_mod.export_generic(conn, task_id)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        finally:
            conn.close()


@app.get('/api/datasets')
def api_datasets() -> List[Dict[str, Any]]:
    with _db_lock:
        conn = _conn()
        try:
            return db.list_dataset_versions(conn)
        finally:
            conn.close()


# ---------- 训练与本机模型版本 ----------


@app.get('/api/vision-workers')
def api_vision_workers() -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            return {
                'workers': vision_jobs.list_workers(conn),
                'jobs': vision_jobs.list_jobs(
                    conn, limit=config.VISION_WORKER_JOB_LIMIT
                ),
            }
        finally:
            conn.close()


@app.patch('/api/vision-workers/{worker_id}')
def api_update_vision_worker(worker_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    if 'enabled' not in body:
        raise HTTPException(400, '缺少 enabled')
    with _db_lock:
        conn = _conn()
        try:
            try:
                return vision_jobs.set_worker_enabled(
                    conn, worker_id=worker_id, enabled=bool(body['enabled'])
                )
            except KeyError:
                raise HTTPException(404, 'Vision Worker 不存在')
        finally:
            conn.close()


@app.get('/api/vision-jobs/{job_id}')
def api_vision_job(job_id: str) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            job = vision_jobs.get_job(conn, job_id)
            if job is None:
                raise HTTPException(404, 'Vision Worker 任务不存在')
            return {'job': job}
        finally:
            conn.close()


@app.post('/api/vision-workers/register')
def api_register_vision_worker(
    request: Request, body: Dict[str, Any]
) -> Dict[str, Any]:
    _require_vision_worker(request)
    with _db_lock:
        conn = _conn()
        try:
            try:
                return vision_jobs.register_worker(
                    conn,
                    worker_id=str(body.get('worker_id') or ''),
                    display_name=str(body.get('display_name') or ''),
                    capabilities=body.get('capabilities') or [],
                    version=str(body.get('version') or ''),
                    platform=str(body.get('platform') or ''),
                    detail=(
                        body.get('detail')
                        if isinstance(body.get('detail'), dict)
                        else {}
                    ),
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        finally:
            conn.close()


@app.post('/api/vision-workers/claim')
def api_claim_vision_job(request: Request, body: Dict[str, Any]) -> Dict[str, Any]:
    _require_vision_worker(request)
    worker_id = str(body.get('worker_id') or '')
    capabilities = body.get('capabilities') or []
    with _db_lock:
        conn = _conn()
        try:
            try:
                job = vision_jobs.claim_job(
                    conn,
                    worker_id=worker_id,
                    capabilities=capabilities,
                    lease_seconds=config.VISION_WORKER_LEASE_SECONDS,
                )
                worker = vision_jobs.get_worker(conn, worker_id)
                if (
                    job is None
                    and worker is not None
                    and bool(worker['enabled'])
                    and 'model_prefill' in capabilities
                    and _queue_next_autonomous_model_prefill(conn) is not None
                ):
                    job = vision_jobs.claim_job(
                        conn,
                        worker_id=worker_id,
                        capabilities=capabilities,
                        lease_seconds=config.VISION_WORKER_LEASE_SECONDS,
                    )
                if job is not None and job['kind'] == 'model_prefill':
                    payload = job.get('payload') or {}
                    operation = str(payload.get('operation') or 'core')
                    screen_type = str(payload.get('screen_type') or '')
                    team_size = (
                        int(payload['team_size']) if payload.get('team_size') else None
                    )
                    db.update_training_review_prefill_state(
                        conn,
                        frame_id=int(payload['frame_id']),
                        status='running',
                        stage='core' if operation == 'core' else 'hero',
                        screen_type=screen_type,
                        team_size=team_size,
                    )
            except KeyError:
                raise HTTPException(409, '请先注册 Vision Worker')
            return {'job': job}
        finally:
            conn.close()


@app.post('/api/vision-workers/jobs/{job_id}/heartbeat')
def api_heartbeat_vision_job(
    job_id: str, request: Request, body: Dict[str, Any]
) -> Dict[str, Any]:
    _require_vision_worker(request)
    worker_id = str(body.get('worker_id') or '')
    lease_token = str(body.get('lease_token') or '')
    with _db_lock:
        conn = _conn()
        try:
            try:
                job = vision_jobs.update_job_lease(
                    conn,
                    job_id=job_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    lease_seconds=config.VISION_WORKER_LEASE_SECONDS,
                    progress=(
                        None
                        if body.get('progress') is None
                        else float(body['progress'])
                    ),
                    stage=(None if body.get('stage') is None else str(body['stage'])),
                    detail=(
                        None if body.get('detail') is None else str(body['detail'])
                    ),
                )
            except PermissionError as exc:
                raise HTTPException(409, str(exc))
            if job['kind'] == 'train_model':
                updates: Dict[str, Any] = {
                    'status': 'running',
                    'progress': float(job['progress']),
                    'error': '',
                }
                if body.get('current_epoch') is not None:
                    updates['current_epoch'] = int(body['current_epoch'])
                if isinstance(body.get('metrics'), dict):
                    updates['metrics'] = body['metrics']
                run = db.get_training_run(conn, job['related_id'])
                if run is not None:
                    if not run.get('started_at'):
                        updates['started_at'] = db.now()
                    db.update_training_run(conn, job['related_id'], **updates)
            return {'job': job, 'cancel_requested': job['cancel_requested']}
        finally:
            conn.close()


@app.get('/api/vision-workers/datasets/{version_id}/manifest')
def api_vision_worker_dataset_manifest(
    version_id: str, request: Request
) -> FileResponse:
    _require_vision_worker(request)
    with _db_lock:
        conn = _conn()
        try:
            row = conn.execute(
                'SELECT manifest_path FROM dataset_versions WHERE id = ?', (version_id,)
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        raise HTTPException(404, '数据集版本不存在')
    try:
        manifest = managed_assets.resolve_dataset_manifest(
            version_id, Path(str(row['manifest_path']))
        )
    except (FileNotFoundError, RuntimeError, ValueError):
        raise HTTPException(404, '数据集清单不存在')
    return FileResponse(manifest, media_type='application/x-ndjson')


@app.get('/api/vision-workers/frames/{frame_id}/image')
def api_vision_worker_frame_image(frame_id: int, request: Request) -> FileResponse:
    _require_vision_worker(request)
    return api_frame_image(frame_id)


@app.get('/api/vision-workers/model-runs/{run_id}/artifact')
def api_vision_worker_model_artifact(run_id: str, request: Request) -> FileResponse:
    _require_vision_worker(request)
    with _db_lock:
        conn = _conn()
        try:
            run = db.get_training_run(conn, run_id)
        finally:
            conn.close()
    if run is None or run['status'] != 'succeeded':
        raise HTTPException(404, '训练产物不存在')
    try:
        artifact, _ = managed_assets.resolve_model_run(
            run_id, Path(str(run['artifact_path']))
        )
    except (FileNotFoundError, RuntimeError, ValueError):
        raise HTTPException(404, '训练模型文件不存在')
    return FileResponse(
        artifact, media_type='application/octet-stream', filename=f'{run_id}.onnx'
    )


@app.get('/api/vision-workers/model-runs/{run_id}/metadata')
def api_vision_worker_model_metadata(run_id: str, request: Request) -> FileResponse:
    _require_vision_worker(request)
    with _db_lock:
        conn = _conn()
        try:
            run = db.get_training_run(conn, run_id)
        finally:
            conn.close()
    if run is None or run['status'] != 'succeeded':
        raise HTTPException(404, '训练产物不存在')
    try:
        _, metadata = managed_assets.resolve_model_run(
            run_id, Path(str(run['artifact_path']))
        )
    except (FileNotFoundError, RuntimeError, ValueError):
        raise HTTPException(404, '训练模型元数据不存在')
    return FileResponse(
        metadata, media_type='application/json', filename=f'{run_id}.json'
    )


@app.get('/api/vision-workers/model-tests/{run_id}/plan')
def api_vision_worker_model_test_plan(
    run_id: str, request: Request, split: str = 'test', sample_id: str = ''
) -> Dict[str, Any]:
    _require_vision_worker(request)
    with _db_lock:
        conn = _conn()
        try:
            try:
                plan = model_testing.worker_evaluation_plan(conn, run_id, split=split)
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(400, str(exc)) from exc
        finally:
            conn.close()
    if sample_id:
        plan['samples'] = [
            value
            for value in plan['samples']
            if str(value.get('sample_id') or '') == sample_id
        ]
        if not plan['samples']:
            raise HTTPException(404, '测试样本不存在')
        plan['total'] = 1
    return plan


@app.put('/api/vision-workers/jobs/{job_id}/artifacts/{filename}')
async def api_upload_vision_job_artifact(
    job_id: str,
    filename: str,
    request: Request,
    worker_id: str = Query(..., min_length=1, max_length=120),
    lease_token: str = Query(..., min_length=1, max_length=200),
) -> Dict[str, Any]:
    _require_vision_worker(request)
    if filename not in {'model.onnx', 'model.json', 'train.log', 'package.zip'}:
        raise HTTPException(400, '不支持的训练产物文件名')
    with _db_lock:
        conn = _conn()
        try:
            try:
                job = vision_jobs.validate_lease(
                    conn, job_id=job_id, worker_id=worker_id, lease_token=lease_token
                )
            except PermissionError as exc:
                raise HTTPException(409, str(exc))
        finally:
            conn.close()
    if job['kind'] == 'train_model' and filename in {
        'model.onnx',
        'model.json',
        'train.log',
    }:
        destination_dir = config.WORK_DIR / 'training-runs' / job['related_id']
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / filename
    elif job['kind'] == 'package_models' and filename == 'package.zip':
        destination_dir = config.WORK_DIR / 'model-packages'
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{job['related_id']}.zip"
    else:
        raise HTTPException(400, '该任务不接受这个产物')
    temporary = destination.with_name(f'.{filename}.{job_id}.upload')
    size = 0
    try:
        with temporary.open('wb') as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                output.write(chunk)
                size += len(chunk)
            output.flush()
        if size <= 0:
            raise HTTPException(400, '上传产物为空')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {'saved': True, 'filename': filename, 'size_bytes': size}


def _apply_remote_model_prefill(
    conn: Any, leased: Dict[str, Any], result: Dict[str, Any]
) -> Dict[str, Any]:
    payload = leased.get('payload') or {}
    frame_id = int(payload.get('frame_id') or result.get('frame_id') or 0)
    operation = str(payload.get('operation') or result.get('operation') or 'core')
    if frame_id <= 0:
        raise ValueError('预填任务缺少 frame_id')
    try:
        image_width = int(result.get('image_width') or 0)
        image_height = int(result.get('image_height') or 0)
    except (TypeError, ValueError):
        image_width = 0
        image_height = 0
    if image_width > 0 and image_height > 0:
        db.update_frame_dimensions(
            conn, frame_id, image_width, image_height, commit=False
        )
    if operation == 'core':
        errors = result.get('errors') if isinstance(result.get('errors'), dict) else {}
        if errors:
            raise RuntimeError(
                '核心模型预打标失败：'
                + '；'.join(f'{task}: {error}' for task, error in errors.items())
            )
        item = model_prefill.apply_core_prefill(
            conn, frame_id, result, result_groups={}
        )
        return {'applied': True, 'frame_id': frame_id, 'item': item}
    item = _single_training_review_item(conn, frame_id)
    if item is None:
        raise KeyError(f'训练复核图片不存在: {frame_id}')
    existing = db.get_training_review_hero_lineup(conn, frame_id)
    if existing is not None and existing['review_status'] == 'confirmed':
        return {'applied': False, 'frame_id': frame_id, 'reason': '人工已确认'}
    screen_type = str(payload.get('screen_type') or result.get('screen_type') or '')
    team_size = int(payload.get('team_size') or result.get('team_size') or 0)
    result_slots = result.get('slots') if isinstance(result.get('slots'), list) else []
    if not result.get('complete') or len(result_slots) != team_size * 2:
        lineup = db.replace_training_review_hero_layout(
            conn,
            frame_id=frame_id,
            screen_type=screen_type,
            team_size=team_size,
            method='new-model-incomplete-worker-v1',
            slots=result_slots,
        )
        _save_new_model_hero_prefill_source(
            conn,
            frame_id=frame_id,
            item=item,
            screen_type=screen_type,
            team_size=team_size,
            result=result,
        )
        return {
            'applied': True,
            'frame_id': frame_id,
            'complete': False,
            'reason': str(result.get('reason') or '模型没有找全头像'),
            'lineup': lineup,
        }
    if operation == 'hero_slots':
        expected_slots = (
            payload.get('slots') if isinstance(payload.get('slots'), list) else []
        )
        existing_by_key = {
            (str(value['side']), int(value['slot'])): value
            for value in (existing or {}).get('slots', [])
        }
        unchanged = len(expected_slots) == len(existing_by_key) and all(
            (key := (str(value.get('side')), int(value.get('slot') or 0)))
            in existing_by_key
            and _same_hero_crop(
                dict(value.get('crop') or {}), existing_by_key[key]['crop']
            )
            for value in expected_slots
        )
        if not unchanged:
            return {
                'applied': False,
                'frame_id': frame_id,
                'reason': '等待期间头像框已被人工修改',
            }
        lineup = db.replace_training_review_hero_layout(
            conn,
            frame_id=frame_id,
            screen_type=screen_type,
            team_size=team_size,
            method='manual-circle+hero-identity-worker-v1',
            slots=result_slots,
        )
    elif operation == 'hero_lineup':
        if (
            existing is not None
            and existing.get('slots')
            and not str(existing.get('suggestion_method') or '').startswith(
                'worker-pending'
            )
        ):
            return {
                'applied': False,
                'frame_id': frame_id,
                'reason': '等待期间已人工绘制头像框',
            }
        lineup = db.replace_training_review_hero_suggestions(
            conn,
            frame_id=frame_id,
            screen_type=screen_type,
            team_size=team_size,
            method='new-model-cascade-worker-v1',
            slots=result_slots,
        )
    else:
        raise ValueError(f'未知预填操作: {operation}')
    _save_new_model_hero_prefill_source(
        conn,
        frame_id=frame_id,
        item=item,
        screen_type=screen_type,
        team_size=team_size,
        result=result,
    )
    return {'applied': True, 'frame_id': frame_id, 'lineup': lineup}


@app.post('/api/vision-workers/jobs/{job_id}/complete')
def api_complete_vision_job(
    job_id: str, request: Request, body: Dict[str, Any]
) -> Dict[str, Any]:
    _require_vision_worker(request)
    worker_id = str(body.get('worker_id') or '')
    lease_token = str(body.get('lease_token') or '')
    with _db_lock:
        conn = _conn()
        try:
            try:
                leased = vision_jobs.validate_lease(
                    conn, job_id=job_id, worker_id=worker_id, lease_token=lease_token
                )
            except PermissionError as exc:
                raise HTTPException(409, str(exc))
            result = body.get('result') if isinstance(body.get('result'), dict) else {}
            if leased['kind'] == 'train_model':
                artifact = (
                    config.WORK_DIR
                    / 'training-runs'
                    / leased['related_id']
                    / 'model.onnx'
                )
                if not artifact.is_file() or artifact.stat().st_size <= 0:
                    raise HTTPException(409, '训练模型尚未上传或文件为空')
                db.update_training_run(
                    conn,
                    leased['related_id'],
                    status='succeeded',
                    current_epoch=int(result.get('epochs') or 0),
                    progress=1.0,
                    metrics=(
                        result.get('metrics')
                        if isinstance(result.get('metrics'), dict)
                        else {}
                    ),
                    artifact_path=str(artifact),
                    finished_at=db.now(),
                )
            elif leased['kind'] == 'model_prefill':
                application = _apply_remote_model_prefill(conn, leased, result)
                _update_autonomous_prefill_after_result(conn, leased, result)
                result = {**result, 'application': application}
                _invalidate_training_review_cache()
            elif leased['kind'] == 'package_models':
                package_id = str(leased['related_id'])
                archive = config.WORK_DIR / 'model-packages' / f'{package_id}.zip'
                if not archive.is_file() or archive.stat().st_size <= 0:
                    raise HTTPException(409, '模型包尚未上传或文件为空')
                if str(result.get('package_id') or '') != package_id:
                    raise HTTPException(409, 'Worker 返回的模型包 ID 不一致')
                db.create_model_package(
                    conn,
                    package_id=package_id,
                    status=str(result.get('status') or 'incomplete'),
                    path=str(archive),
                    manifest=(
                        result.get('manifest')
                        if isinstance(result.get('manifest'), dict)
                        else {}
                    ),
                )
            job = vision_jobs.finish_job(
                conn,
                job_id=job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                succeeded=True,
                result=result,
            )
            return {'job': job}
        finally:
            conn.close()


@app.post('/api/vision-workers/jobs/{job_id}/fail')
def api_fail_vision_job(
    job_id: str, request: Request, body: Dict[str, Any]
) -> Dict[str, Any]:
    _require_vision_worker(request)
    worker_id = str(body.get('worker_id') or '')
    lease_token = str(body.get('lease_token') or '')
    error = str(body.get('error') or 'Vision Worker 任务失败')[:2_000]
    with _db_lock:
        conn = _conn()
        try:
            try:
                leased = vision_jobs.validate_lease(
                    conn, job_id=job_id, worker_id=worker_id, lease_token=lease_token
                )
            except PermissionError as exc:
                raise HTTPException(409, str(exc))
            if leased['kind'] == 'train_model':
                db.update_training_run(
                    conn,
                    leased['related_id'],
                    status='failed',
                    error=error,
                    finished_at=db.now(),
                )
            elif leased['kind'] == 'model_prefill':
                payload = leased.get('payload') or {}
                operation = str(payload.get('operation') or 'core')
                db.update_training_review_prefill_state(
                    conn,
                    frame_id=int(payload['frame_id']),
                    status='failed',
                    stage='core' if operation == 'core' else 'hero',
                    screen_type=str(payload.get('screen_type') or ''),
                    team_size=(
                        int(payload['team_size']) if payload.get('team_size') else None
                    ),
                    error=error,
                )
                _invalidate_training_review_cache()
            job = vision_jobs.finish_job(
                conn,
                job_id=job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                succeeded=False,
                error=error,
            )
            return {'job': job}
        finally:
            conn.close()


@app.post('/api/vision-jobs/{job_id}/cancel')
def api_cancel_vision_job(job_id: str) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            try:
                job = vision_jobs.request_cancel(conn, job_id)
            except KeyError:
                raise HTTPException(404, 'Vision Worker 任务不存在')
            if job['kind'] == 'train_model' and job['status'] == 'cancelled':
                run = db.get_training_run(conn, job['related_id'])
                if run is not None:
                    db.update_training_run(
                        conn,
                        job['related_id'],
                        status='cancelled',
                        error='用户取消',
                        finished_at=db.now(),
                    )
            return {'job': job}
        finally:
            conn.close()


@app.get('/api/training/tasks')
def api_training_tasks() -> List[Dict[str, Any]]:
    with _db_lock:
        conn = _conn()
        try:
            return training.task_summaries(conn)
        finally:
            conn.close()


@app.get('/api/training/runs')
def api_training_runs(limit: int = Query(100, ge=1, le=1000)) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            active = conn.execute(
                "SELECT id FROM training_runs WHERE status IN ('running', 'queued') "
                "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, "
                'created_at, id LIMIT 1'
            ).fetchone()
            return {
                'active_run_id': str(active['id']) if active is not None else None,
                'runs': db.list_training_runs(conn, limit=limit),
            }
        finally:
            conn.close()


@app.post('/api/training/start')
def api_start_training(body: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str(body.get('task_id') or '')
    definition = training.TRAINING_TASKS.get(task_id)
    if definition is None or not definition.get('active', True):
        raise HTTPException(400, f'未知训练任务: {task_id}')
    epochs = int(body.get('epochs') or definition['epochs'])
    if not 1 <= epochs <= 500:
        raise HTTPException(400, 'epochs 必须在 1 到 500 之间')
    with _training_start_lock:
        with _db_lock:
            conn = _conn()
            try:
                summary = next(
                    item
                    for item in training.task_summaries(conn)
                    if item['id'] == task_id
                )
                if not summary['ready']:
                    raise HTTPException(
                        400,
                        '当前数据还不能训练：' + '；'.join(summary['blocking_reasons']),
                    )
                snapshot = training.export_snapshot(conn, task_id, materialize=False)
                run_id = training.new_run_id(task_id)
                log_path = config.WORK_DIR / 'training-runs' / run_id / 'train.log'
                db.create_training_run(
                    conn,
                    run_id=run_id,
                    task_id=task_id,
                    dataset_version_id=snapshot['version'],
                    epochs=epochs,
                    config_json={
                        'kind': definition['kind'],
                        'imgsz': definition['imgsz'],
                        'input_width': definition.get('input_width'),
                        'input_height': definition.get('input_height'),
                        'base_model': definition['base_model'],
                    },
                    log_path=str(log_path),
                )
                job = vision_jobs.create_job(
                    conn,
                    kind='train_model',
                    related_id=run_id,
                    priority=100,
                    payload={
                        'run_id': run_id,
                        'task_id': task_id,
                        'dataset_version_id': snapshot['version'],
                        'epochs': epochs,
                        'kind': definition['kind'],
                        'imgsz': definition['imgsz'],
                        'input_width': definition.get('input_width'),
                        'input_height': definition.get('input_height'),
                        'base_model': definition['base_model'],
                    },
                )
                run = db.get_training_run(conn, run_id)
            except RuntimeError as exc:
                raise HTTPException(400, str(exc))
            finally:
                conn.close()
    assert run is not None
    run['vision_job'] = job
    return run


@app.post('/api/training/runs/{run_id}/cancel')
def api_cancel_training(run_id: str) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT id FROM vision_jobs WHERE kind='train_model' "
                "AND related_id=? AND status IN ('queued', 'running') "
                'ORDER BY created_at DESC LIMIT 1',
                (run_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(409, '训练任务未在排队或运行')
            job = vision_jobs.request_cancel(conn, str(row['id']))
            if job['status'] == 'cancelled':
                db.update_training_run(
                    conn,
                    run_id,
                    status='cancelled',
                    error='用户取消',
                    finished_at=db.now(),
                )
            return {'cancel_requested': True, 'run_id': run_id, 'job': job}
        finally:
            conn.close()


@app.post('/api/training/runs/{run_id}/resume')
def api_resume_training(run_id: str) -> Dict[str, Any]:
    with _training_start_lock:
        with _db_lock:
            conn = _conn()
            try:
                run = db.get_training_run(conn, run_id)
                if run is None:
                    raise HTTPException(404, '训练记录不存在')
                if run['status'] != 'interrupted':
                    raise HTTPException(409, '只有已中断的训练才能恢复')
                payload = {
                    'run_id': run_id,
                    'task_id': run['task_id'],
                    'dataset_version_id': run['dataset_version_id'],
                    'epochs': run['epochs'],
                    **run['config_json'],
                    'resume': True,
                }
                db.update_training_run(
                    conn, run_id, status='queued', error='', finished_at=None
                )
                job = vision_jobs.create_job(
                    conn,
                    kind='train_model',
                    related_id=run_id,
                    priority=110,
                    payload=payload,
                )
            finally:
                conn.close()
    return {'run_id': run_id, 'status': 'queued', 'vision_job': job}


@app.get('/api/training/runs/{run_id}/log')
def api_training_log(
    run_id: str, tail: int = Query(200, ge=1, le=5000)
) -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            run = db.get_training_run(conn, run_id)
        finally:
            conn.close()
    if run is None:
        raise HTTPException(404, '训练记录不存在')
    path = Path(run['log_path'])
    if not path.is_file():
        return {'run_id': run_id, 'log': ''}
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    return {'run_id': run_id, 'log': '\n'.join(lines[-tail:])}


@app.post('/api/training/runs/{run_id}/publish-local')
def api_publish_local_model(run_id: str) -> Dict[str, str]:
    with _db_lock:
        conn = _conn()
        try:
            try:
                result = training.publish_local_model(conn, run_id)
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(400, str(exc))
        finally:
            conn.close()
    inference_mod.clear_model_cache()
    return result


# ---------- 统计 ----------


@app.get('/api/stats')
def api_stats() -> Dict[str, Any]:
    with _db_lock:
        conn = _conn()
        try:
            return stats_mod.stats(conn)
        finally:
            conn.close()


# ---------- 静态前端 ----------

_static_dir = Path(__file__).resolve().parent / 'static'
app.mount('/', NoCacheStaticFiles(directory=str(_static_dir), html=True), name='static')


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT, log_level='info')


if __name__ == '__main__':
    main()
