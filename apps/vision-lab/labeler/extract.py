"""抽帧引擎:同步 NAS 清单 → 按配方抽帧(真实 PTS 原始分辨率)→ 哈希去重 → 预打分。"""

from __future__ import annotations

import hashlib
import io
import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from PIL import Image

from . import config, db
from .nas import NasClient

_state: Dict[str, Any] = {
    'running': False,
    'cancel': threading.Event(),
    'progress': {},
    'summary': None,  # 任务结束后的汇总 {videos, added, failed, cancelled}
}


# ---------- 感知哈希(dHash,64bit) ----------

def phash_image(image: Image.Image) -> str:
    """dHash:9x8 灰度差分,64 位十六进制字符串。"""
    gray = image.convert('L').resize((9, 8), Image.BILINEAR)
    px = list(gray.getdata())
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (1 if px[y * 9 + x] >= px[y * 9 + x + 1] else 0)
    return f'{bits:016x}'


def phash_distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count('1')


# ---------- 模型预打分 ----------

class ModelScorer:
    """复用仓库内 result-panel.onnx(结算面板检测)做预打分。"""

    def __init__(self) -> None:
        if not config.MODEL_PATH.is_file():
            self._session = None
            return
        import onnxruntime
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(config.MODEL_PATH), sess_options=options)
        self._input_name = self._session.get_inputs()[0].name
        self.version = 'result-panel.onnx'

    @property
    def available(self) -> bool:
        return self._session is not None

    def score(self, image: Image.Image) -> Optional[Dict[str, Any]]:
        """返回 {confidence, bbox(x,y,w,h 归一化)} 或 None。"""
        if self._session is None:
            return None
        import numpy as np
        size = config.MODEL_INPUT_SIZE
        img = image.convert('RGB')
        w, h = img.size
        scale = min(size / w, size / h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        resized = img.resize((nw, nh), Image.BILINEAR)
        canvas = Image.new('RGB', (size, size), (114, 114, 114))
        left, top = (size - nw) // 2, (size - nh) // 2
        canvas.paste(resized, (left, top))
        tensor = np.asarray(canvas).transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        output = self._session.run(None, {self._input_name: tensor})[0]
        pred = np.squeeze(output)
        if pred.ndim != 2 or pred.shape[1] < 5:
            return None
        if pred.shape[0] <= 16 and pred.shape[1] > pred.shape[0]:
            pred = pred.transpose()
        scores = pred[:, 4] if pred.shape[1] == 5 else np.max(pred[:, 4:], axis=1)
        idx = int(np.argmax(scores))
        conf = float(scores[idx])
        if conf < config.MODEL_CONF_THRESHOLD:
            return None
        cx, cy, bw, bh = (float(v) for v in pred[idx, :4])
        x = (cx - bw / 2 - left) / scale / w
        y = (cy - bh / 2 - top) / scale / h
        box_w = bw / scale / w
        box_h = bh / scale / h
        return {'confidence': round(conf, 4),
                'bbox': {'x': round(max(0, x), 4), 'y': round(max(0, y), 4),
                         'w': round(min(1, box_w), 4), 'h': round(min(1, box_h), 4)}}


# ---------- 任务状态 ----------

def task_state() -> Dict[str, Any]:
    return {'running': _state['running'], 'progress': _state['progress'],
            'summary': _state['summary']}


def cancel_extraction() -> None:
    _state['cancel'].set()


def sync_videos(conn: Any, nas: NasClient) -> List[Dict[str, Any]]:
    """把 NAS 视频清单同步进本地库。"""
    for v in nas.list_videos():
        db.upsert_video(
            conn, remote_path=v['remote_path'], streamer=v['streamer'],
            room_id=v['room_id'], filename=v['filename'],
            duration_seconds=0.0, size_bytes=v['size_bytes'],
        )
    return db.list_videos(conn)


# ---------- 帧入库 ----------

def _store_frame(conn: Any, video_id: int, pts_ms: int, jpeg: bytes, *,
                 strategy: str, model_result: Optional[Dict[str, Any]],
                 model_version: str = '') -> Optional[int]:
    """存原始分辨率帧 + 缩略图,返回帧 id(内容重复返回 None)。"""
    frame_dir = config.FRAME_DIR
    thumb_dir = config.THUMB_DIR
    frame_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(jpeg).hexdigest()
    frame_path = frame_dir / f'{sha}.jpg'
    if not frame_path.exists():
        frame_path.write_bytes(jpeg)
    img = Image.open(frame_path)
    w, h = img.size
    thumb_path = thumb_dir / f'{sha}.jpg'
    if not thumb_path.exists():
        t = img.copy()
        t.thumbnail((config.THUMB_WIDTH, config.THUMB_WIDTH))
        t.convert('RGB').save(thumb_path, quality=80)
    ph = phash_image(img)
    entry = {
        'timestamp_ms': pts_ms, 'width': w, 'height': h, 'sha256': sha,
        'phash': ph, 'frame_path': str(frame_path), 'thumb_path': str(thumb_path),
        'strategy': strategy,
        'model_source': model_version if model_result else '',
        'model_confidence': model_result['confidence'] if model_result else None,
    }
    ids = db.add_frames(conn, video_id, [entry])
    if ids and model_result and model_version:
        db.add_prediction(conn, ids[0], model_version=model_version,
                          pred_type='result_panel',
                          confidence=model_result['confidence'],
                          bbox=model_result.get('bbox'))
    return ids[0] if ids else None


def live_next_frame(conn: Any, nas: NasClient, video_id: int, *,
                    after_ms: int = -5000, interval_ms: int = 5000,
                    last_sha: Optional[str] = None) -> Dict[str, Any]:
    """实时抽帧:抽视频 after_ms 之后的下一帧(间隔 interval_ms),入库并返回。

    与上一帧内容重复(sha256 相同)时自动跳帧(最多 100 次)。
    返回 {'done': True} 表示视频已抽完;{'duplicate': True} 表示需要前端重试。
    """
    v = next((x for x in db.list_videos(conn) if x['id'] == video_id), None)
    if v is None:
        raise KeyError(f'video {video_id} 不存在')
    duration_ms = int(v['duration_seconds'] * 1000) if v['duration_seconds'] else 0
    t = after_ms + interval_ms
    for _ in range(100):
        if duration_ms and t >= duration_ms:
            return {'done': True, 'duration_ms': duration_ms}
        frames = nas.point_frames(v['remote_path'], times_ms=[t])
        item = next(iter(frames), None)
        if item is None:
            return {'done': True, 'duration_ms': duration_ms}
        pts_ms, jpeg = item
        sha = hashlib.sha256(jpeg).hexdigest()
        if last_sha and sha == last_sha:
            t += interval_ms
            continue
        fid = _store_frame(conn, video_id, pts_ms or t, jpeg, strategy='live',
                           model_result=None, model_version='')
        return {
            'frame_id': fid, 'pts_ms': pts_ms or t, 'sha256': sha,
            'next_after_ms': t, 'duration_ms': duration_ms,
            'width': v['width'] if 'width' in v else None,
        }
    return {'done': True, 'duration_ms': duration_ms}


def _consume(conn: Any, gen: Iterator[Tuple[int, bytes]], video_id: int, strategy: str,
             progress: Dict[str, Any],
             progress_cb: Optional[Callable[[Dict[str, Any]], None]],
             score_each: bool = False) -> int:
    """消费帧流入库;score_each=True 时逐帧用模型预打分。"""
    added = 0
    scorer = ModelScorer() if score_each else None
    for pts_ms, jpeg in gen:
        if _state['cancel'].is_set():
            raise InterruptedError('任务已取消')
        model_result = None
        if scorer is not None and scorer.available:
            model_result = scorer.score(Image.open(io.BytesIO(jpeg)))
        if _store_frame(conn, video_id, pts_ms, jpeg, strategy=strategy,
                        model_result=model_result,
                        model_version=scorer.version if scorer else ''):
            added += 1
        progress['current'] += 1
        if progress['current'] % 10 == 0 and progress_cb:
            progress_cb(dict(progress))
    return added


# ---------- 抽帧任务 ----------

def run_extraction(
    conn: Any, nas: NasClient, video_id: int, strategy: str,
    params: Dict[str, Any],
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """执行单个视频的抽帧任务,返回统计。"""
    v = next((x for x in db.list_videos(conn) if x['id'] == video_id), None)
    if v is None:
        raise KeyError(f'video {video_id} 不存在')
    progress = {'video_id': video_id, 'streamer': v['streamer'],
                'filename': v['filename'], 'current': 0, 'status': 'extracting'}
    _state['progress'] = {video_id: progress}
    if progress_cb:
        progress_cb(dict(progress))
    db.set_video_status(conn, video_id, 'extracting')
    job_id = _create_job(conn, video_id, strategy, params)
    scorer = ModelScorer()
    result = {'added': 0, 'strategy': strategy}
    try:
        duration = nas.ffprobe_duration(v['remote_path'])
        db.upsert_video(conn, remote_path=v['remote_path'], streamer=v['streamer'],
                        room_id=v['room_id'], filename=v['filename'],
                        duration_seconds=duration, size_bytes=v['size_bytes'])
        if strategy in ('existing_model_hits', 'dense_around_candidate'):
            gen = _dense_around_hits(conn, nas, v, params, scorer, progress,
                                     progress_cb)
        elif strategy == 'uniform_every_n_seconds':
            gen = _uniform_every_n(nas, v, params)
        elif strategy == 'uniform_random':
            gen = _uniform_random(nas, v, params, progress, progress_cb)
        elif strategy == 'manual_timestamps':
            gen = _manual_timestamps(nas, v, params)
        elif strategy == 'dense_interval':
            gen = _dense_interval(nas, v, params)
        elif strategy == 'transition_windows':
            gen = _transition_windows(nas, v, params, progress, progress_cb)
        else:
            raise ValueError(f'未知抽帧策略: {strategy}')
        result['added'] = _consume(conn, gen, video_id, strategy, progress, progress_cb)
        db.set_video_status(conn, video_id, 'done')
        progress['status'] = 'done'
        _state['progress'] = {video_id: progress}
        if progress_cb:
            progress_cb(dict(progress))
        conn.execute("UPDATE extraction_jobs SET status='done' WHERE id=?", (job_id,))
        conn.commit()
    except InterruptedError:
        db.set_video_status(conn, video_id, 'pending')
        progress['status'] = 'cancelled'
        if progress_cb:
            progress_cb(dict(progress))
        result['status'] = 'cancelled'
    except Exception as exc:  # noqa: BLE001
        db.set_video_status(conn, video_id, 'failed', error=str(exc))
        conn.execute("UPDATE extraction_jobs SET status='failed', error=? WHERE id=?",
                     (str(exc), job_id))
        conn.commit()
        progress['status'] = 'failed'
        progress['error'] = str(exc)
        if progress_cb:
            progress_cb(dict(progress))
        result['status'] = 'failed'
        result['error'] = str(exc)
    result['video_id'] = video_id
    return result


def extract_videos_multi(
    conn: Any, nas: NasClient, video_ids: List[int], strategy: str,
    params: Dict[str, Any], *,
    max_workers: int = 3,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """批量抽帧任务入口:维护全局任务状态(running/progress/summary)。

    多路并行(默认 3 线程)加速;每路使用独立数据库连接;单视频失败不影响其余。
    """
    if _state['running']:
        raise RuntimeError('已有抽帧任务在运行')
    _state['running'] = True
    _state['cancel'].clear()
    _state['progress'] = {}
    _state['summary'] = None
    summary = {'videos': len(video_ids), 'added': 0, 'failed': [],
               'cancelled': False}
    lock = threading.Lock()

    def _worker(vid: int) -> None:
        if _state['cancel'].is_set():
            with lock:
                summary['cancelled'] = True
            return
        work_conn = db.connect(config.DB_PATH)
        try:
            result = run_extraction(work_conn, nas, vid, strategy, params,
                                    progress_cb=progress_cb)
            with lock:
                summary['added'] += result.get('added', 0)
                if result.get('status') == 'failed':
                    summary['failed'].append(
                        {'video_id': vid, 'error': result.get('error', '未知错误')})
                if result.get('status') == 'cancelled':
                    summary['cancelled'] = True
        except Exception as exc:  # noqa: BLE001
            with lock:
                summary['failed'].append({'video_id': vid, 'error': str(exc)})
        finally:
            work_conn.close()

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_worker, video_ids))
    finally:
        _state['running'] = False
        _state['progress'] = {}
        _state['summary'] = summary
    return summary


def _create_job(conn: Any, video_id: int, strategy: str,
                params: Dict[str, Any]) -> int:
    cur = conn.execute(
        'INSERT INTO extraction_jobs (video_id, strategy, params, status, created_at) '
        'VALUES (?, ?, ?, ?, ?)',
        (video_id, strategy, json.dumps(params, ensure_ascii=False), 'running',
         db.now()),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------- 配方实现 ----------

def _coarse_hits(nas: NasClient, v: Dict[str, Any], scorer: ModelScorer,
                 progress: Dict[str, Any],
                 progress_cb: Optional[Callable[[Dict[str, Any]], None]]
                 ) -> Tuple[List[int], List[Tuple[int, bytes]]]:
    """粗扫全片:返回 (模型命中时间点列表, 全部粗扫帧列表)。"""
    hits: List[int] = []
    frames: List[Tuple[int, bytes]] = []
    for pts_ms, jpeg in nas.coarse_frames(v['remote_path'],
                                          sample_seconds=config.COARSE_SAMPLE_SECONDS):
        if _state['cancel'].is_set():
            raise InterruptedError('任务已取消')
        frames.append((pts_ms, jpeg))
        progress['scan'] = progress.get('scan', 0) + 1
        if scorer.available:
            r = scorer.score(Image.open(io.BytesIO(jpeg)))
            if r and r['confidence'] >= config.MODEL_CONF_THRESHOLD:
                hits.append(pts_ms)
    return hits, frames


def _dense_around_hits(conn: Any, nas: NasClient, v: Dict[str, Any],
                       params: Dict[str, Any], scorer: ModelScorer,
                       progress: Dict[str, Any],
                       progress_cb: Optional[Callable[[Dict[str, Any]], None]]
                       ) -> Iterator[Tuple[int, bytes]]:
    """existing_model_hits / dense_around_candidate:命中(或给定候选)前后窗口密集抽帧。"""
    if params.get('candidates'):
        candidates = [int(t) for t in params['candidates']]
    else:
        hits, coarse = _coarse_hits(nas, v, scorer, progress, progress_cb)
        # 粗扫命中帧(小图)入库,作为候选索引
        for pts_ms, jpeg in coarse:
            if pts_ms in hits:
                _store_frame(conn, v['id'], pts_ms, jpeg,
                             strategy='existing_model_hits',
                             model_result=None)
        candidates = hits
    window_ms = int(params.get('window_seconds', config.DENSE_WINDOW_SECONDS) * 1000)
    fps = int(params.get('fps', config.DENSE_FPS))
    for t in candidates:
        if _state['cancel'].is_set():
            raise InterruptedError('任务已取消')
        start = max(0, t - window_ms)
        yield from nas.dense_frames(v['remote_path'], start_ms=start,
                                    end_ms=t + window_ms, fps=fps)


def _uniform_every_n(nas: NasClient, v: Dict[str, Any],
                     params: Dict[str, Any]) -> Iterator[Tuple[int, bytes]]:
    """uniform_every_n_seconds:全片每 N 秒抽 1 帧原始分辨率(均匀负样本)。"""
    interval = float(params.get('interval_seconds', 5))
    if interval <= 0:
        raise ValueError('interval_seconds 必须为正数')
    yield from nas.dense_frames(v['remote_path'], start_ms=0, end_ms=None,
                                fps=1.0 / interval)


def _uniform_random(nas: NasClient, v: Dict[str, Any], params: Dict[str, Any],
                    progress: Dict[str, Any],
                    progress_cb: Optional[Callable[[Dict[str, Any]], None]]
                    ) -> Iterator[Tuple[int, bytes]]:
    """uniform_random:粗扫获取时间点,随机选 K 个,每点抽 1 帧原图。"""
    count = int(params.get('count', 200))
    rng = random.Random(int(params.get('seed', 42)))
    pts_all: List[int] = []
    for pts_ms, _jpeg in nas.coarse_frames(v['remote_path'], sample_seconds=2):
        pts_all.append(pts_ms)
        progress['scan'] = progress.get('scan', 0) + 1
    if not pts_all:
        return
    picks = rng.sample(pts_all, min(count, len(pts_all)))
    yield from nas.point_frames(v['remote_path'], times_ms=picks)


def _manual_timestamps(nas: NasClient, v: Dict[str, Any],
                       params: Dict[str, Any]) -> Iterator[Tuple[int, bytes]]:
    """manual_timestamps:手动时间点(毫秒),每点抽 1 帧原图。"""
    times = [int(t) for t in params.get('timestamps', [])]
    yield from nas.point_frames(v['remote_path'], times_ms=times)


def _dense_interval(nas: NasClient, v: Dict[str, Any],
                    params: Dict[str, Any]) -> Iterator[Tuple[int, bytes]]:
    """dense_interval:指定起止区间密集抽帧。"""
    start = int(params.get('start_ms', 0))
    end = int(params.get('end_ms', 0))
    fps = int(params.get('fps', config.DENSE_FPS))
    yield from nas.dense_frames(v['remote_path'], start_ms=start, end_ms=end,
                                fps=fps)


def _transition_windows(nas: NasClient, v: Dict[str, Any], params: Dict[str, Any],
                        progress: Dict[str, Any],
                        progress_cb: Optional[Callable[[Dict[str, Any]], None]]
                        ) -> Iterator[Tuple[int, bytes]]:
    """transition_windows:粗扫帧感知哈希突变点,前后窗口密集抽帧。"""
    prev: Optional[Tuple[int, str]] = None
    transitions: List[int] = []
    threshold = int(params.get('threshold', 24))
    for pts_ms, jpeg in nas.coarse_frames(v['remote_path'],
                                          sample_seconds=config.COARSE_SAMPLE_SECONDS):
        ph = phash_image(Image.open(io.BytesIO(jpeg)))
        if prev is not None and phash_distance(prev[1], ph) >= threshold:
            transitions.append((prev[0] + pts_ms) // 2)
        prev = (pts_ms, ph)
    window_ms = int(params.get('window_seconds', 3) * 1000)
    fps = int(params.get('fps', config.DENSE_FPS))
    for t in transitions:
        if _state['cancel'].is_set():
            raise InterruptedError('任务已取消')
        start = max(0, t - window_ms)
        yield from nas.dense_frames(v['remote_path'], start_ms=start,
                                    end_ms=t + window_ms, fps=fps)
