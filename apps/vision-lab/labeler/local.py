"""本地视频管理:下载 NAS 视频 → 转 mp4(seek 精确、可流式播放)→ 本地抽帧。

进入实时打标时把整个视频拉到本地,之后播放/拖动进度条/取帧全部本地操作,
不依赖 NAS 网络往返,操作丝滑。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import config, db
from .extract import _store_frame
from .nas import NasClient

_downloads: Dict[int, Dict[str, Any]] = {}
_lock = threading.Lock()


def _mp4_path(video_id: int) -> Path:
    return config.LOCAL_VIDEO_DIR / f'{video_id}.mp4'


def _flv_path(video_id: int) -> Path:
    return config.LOCAL_VIDEO_DIR / f'{video_id}.tmp.flv'


def local_mp4_exists(video_id: int) -> bool:
    p = _mp4_path(video_id)
    return p.exists() and p.stat().st_size > 1024 * 1024


def download_state(video_id: int) -> Dict[str, Any]:
    with _lock:
        st = dict(_downloads.get(video_id, {}))
    if not st:
        if local_mp4_exists(video_id):
            return {'status': 'done', 'progress': 100, 'error': None}
        return {'status': 'none', 'progress': 0, 'error': None}
    return st


def start_download(nas: NasClient, video_id: int,
                   progress_cb: Optional[Callable[[int, int], None]] = None,
                   ) -> None:
    """后台线程:下载 flv → 转 mp4 → 清理临时文件。线程自建数据库连接。"""
    with _lock:
        if _downloads.get(video_id, {}).get('status') in ('downloading',
                                                          'converting'):
            return
        _downloads[video_id] = {'status': 'downloading', 'progress': 0,
                                'error': None}

    def _run() -> None:
        conn = db.connect(config.DB_PATH)
        try:
            v = next((x for x in db.list_videos(conn) if x['id'] == video_id),
                     None)
            if v is None:
                raise KeyError(f'video {video_id} 不存在')
            flv = _flv_path(video_id)
            if flv.exists():
                flv.unlink()

            def _p(got: int, total: int) -> None:
                pct = int(got / total * 100) if total else 0
                with _lock:
                    _downloads[video_id] = {
                        'status': 'downloading', 'progress': min(pct, 99),
                        'error': None}
                if progress_cb:
                    progress_cb(got, total)

            nas.download(v['remote_path'], flv,
                         total_bytes=v['size_bytes'], progress_cb=_p)
            # 转 mp4(copy 不重编码,快;moov 前置保证播放器可拖动)
            mp4 = _mp4_path(video_id)
            with _lock:
                _downloads[video_id] = {'status': 'converting',
                                        'progress': 99, 'error': None}
            proc = subprocess.run(
                ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                 '-i', str(flv), '-c', 'copy', '-movflags', '+faststart',
                 str(mp4)],
                capture_output=True, text=True, timeout=7200,
            )
            if proc.returncode != 0:
                raise RuntimeError(f'转码失败: {proc.stderr[:300]}')
            flv.unlink(missing_ok=True)
            # 更新时长
            try:
                out = subprocess.run(
                    ['ffprobe', '-v', 'error', '-show_entries',
                     'format=duration', '-of', 'csv=p=0', str(mp4)],
                    capture_output=True, text=True, timeout=60,
                )
                dur = float(out.stdout.strip())
                if dur > 0:
                    db.upsert_video(conn, remote_path=v['remote_path'],
                                    streamer=v['streamer'], room_id=v['room_id'],
                                    filename=v['filename'],
                                    duration_seconds=dur,
                                    size_bytes=v['size_bytes'])
            except Exception:  # noqa: BLE001
                pass
            with _lock:
                _downloads[video_id] = {'status': 'done', 'progress': 100,
                                        'error': None}
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _downloads[video_id] = {'status': 'failed', 'progress': 0,
                                        'error': str(exc)}
            for tmp in (_flv_path(video_id), _mp4_path(video_id)):
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            conn.close()

    threading.Thread(target=_run, daemon=True).start()


def local_frame(conn: Any, video_id: int, pts_ms: int,
                interval_ms: int = 5000) -> Dict[str, Any]:
    """本地抽帧:在本地 mp4 上 seek 抽一帧,入库返回。

    与上一帧内容重复(sha256)时自动跳过。返回 {'done'} 表示超出范围/失败。
    """
    mp4 = _mp4_path(video_id)
    if not mp4.exists():
        raise RuntimeError('视频尚未下载到本地')
    proc = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', str(mp4)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        duration_ms = int(float(proc.stdout.strip()) * 1000)
    except ValueError:
        duration_ms = 0
    for _ in range(50):
        if duration_ms and pts_ms >= duration_ms:
            return {'done': True, 'duration_ms': duration_ms}
        # 该位置附近已有帧(批量/之前抽过)→ 直接返回,不重复抽
        # 窗口 30ms≈1 帧:微调跳转(±0.1s/±0.2s/±0.25s)必须能抽到新帧;
        # 完全相同的画面由 sha256 去重兜底,不会重复存储
        row = conn.execute(
            'SELECT id, timestamp_ms FROM frames WHERE video_id = ? '
            'AND ABS(timestamp_ms - ?) <= 30 ORDER BY '
            'ABS(timestamp_ms - ?) LIMIT 1',
            (video_id, pts_ms, pts_ms)).fetchone()
        if row:
            return {'frame_id': row['id'], 'pts_ms': row['timestamp_ms'],
                    'duration_ms': duration_ms, 'existing': True}
        p = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error',
             '-ss', f'{pts_ms / 1000:.3f}', '-i', str(mp4),
             '-frames:v', '1', '-q:v', '5',
             '-f', 'image2pipe', '-c:v', 'mjpeg', '-'],
            capture_output=True, timeout=120,
        )
        if not p.stdout:
            return {'done': True, 'duration_ms': duration_ms}
        fid = _store_frame(conn, video_id, pts_ms, p.stdout,
                           strategy='local', model_result=None)
        if fid:
            return {'frame_id': fid, 'pts_ms': pts_ms,
                    'duration_ms': duration_ms}
        pts_ms += interval_ms  # 重复帧跳过
    return {'done': True, 'duration_ms': duration_ms}
