"""NAS(群晖 192.168.50.24)访问封装 —— 虚荣视觉标注工作台。

- 凭据只从环境变量 SYNO_ADMIN_USERNAME / SYNO_ADMIN_PASSWORD 读取,绝不写入
  仓库或命令行(见仓库 AGENTS.md)。
- ssh 认证密码经 SSH_ASKPASS(无 tty、无回显)提供;若远程命令需要 sudo
  (docker exec),sudo 密码经 ssh 的 stdin 管道传给 `sudo -S`,同样不进命令行。
- 抽帧在 blrec-next 容器内执行(宿主 ffmpeg 为精简版,无 h264 解码器)。
- 帧以 JPEG 流经 ssh stdout 管道拉回本机,时间戳经 ffmpeg `showinfo` 滤镜
  从 stderr 解析(真实 PTS,毫秒),按 JPEG SOI/EOI 切分。
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import subprocess
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .config import (
    NAS_HOST,
    NAS_REC_DIR,
    NAS_TRAINING_CANDIDATE_DIR,
    VIDEO_EXTS,
)

DOCKER_EXEC = 'sudo -S /usr/local/bin/docker exec blrec-next'
CONTAINER_REC = '/rec'

_ASKPASS_SCRIPT = '''#!/bin/sh
# 仅供 ssh 通过 SSH_ASKPASS 调用;凭据来自环境变量,不回显、不落盘
test -n "$SYNO_ADMIN_PASSWORD" && echo "$SYNO_ADMIN_PASSWORD"
'''

_PTS_RE = re.compile(r'pts_time:([0-9.]+)')


def _require_env(name: str) -> str:
    value = os.environ.get(name, '')
    if not value:
        raise RuntimeError(
            f'缺少环境变量 {name}:NAS 访问需要先设置 SYNO_ADMIN_USERNAME 和 '
            'SYNO_ADMIN_PASSWORD(见 README)'
        )
    return value


def ensure_askpass_script() -> Path:
    path = Path(__file__).resolve().parent.parent / '.ssh_askpass.sh'
    if not path.exists():
        path.write_text(_ASKPASS_SCRIPT)
        path.chmod(0o700)
    return path


def _jpeg_stream(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """把字节流按 JPEG 帧切分(SOI FFD8 … EOI FFD9)。"""
    buf = bytearray()
    start = -1
    for chunk in chunks:
        buf.extend(chunk)
        while True:
            if start < 0:
                start = buf.find(b'\xff\xd8')
                if start < 0:
                    if len(buf) > 1 << 20:
                        del buf[: len(buf) - 1]
                    break
                del buf[:start]
                start = 0
            end = buf.find(b'\xff\xd9', 2)
            if end < 0:
                if len(buf) > 1 << 24:
                    del buf[: len(buf) - 1]
                    start = -1
                break
            frame = bytes(buf[: end + 2])
            del buf[: end + 2]
            start = -1
            yield frame


class NasClient:
    def __init__(self) -> None:
        self._user = _require_env('SYNO_ADMIN_USERNAME')
        self._password = _require_env('SYNO_ADMIN_PASSWORD')
        self._askpass = ensure_askpass_script()

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env.update({
            'SSH_ASKPASS': str(self._askpass),
            'SSH_ASKPASS_REQUIRE': 'force',
            'DISPLAY': ':0',
        })
        return env

    def _ssh_cmd(self, remote_cmd: str) -> List[str]:
        return [
            'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10',
            f'{self._user}@{NAS_HOST}', remote_cmd,
        ]

    def run(self, remote_cmd: str, *, timeout: int = 60,
            check: bool = True, sudo: bool = False) -> bytes:
        stdin_data = (self._password + '\n').encode() if sudo else None
        kwargs: Dict[str, Any] = {
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
            'env': self._env(),
            'start_new_session': True,
            'timeout': timeout,
        }
        if sudo:
            kwargs['input'] = stdin_data
        else:
            kwargs['stdin'] = subprocess.DEVNULL
        proc = subprocess.run(self._ssh_cmd(remote_cmd), **kwargs)
        if check and proc.returncode != 0:
            raise RuntimeError(
                f'NAS 命令失败(exit={proc.returncode}):\n'
                f'cmd: {remote_cmd}\nstderr: {proc.stderr.decode(errors="replace")}'
            )
        return proc.stdout

    def stream(self, remote_cmd: str, *, timeout: int = 3600,
               sudo: bool = False) -> Iterator[bytes]:
        """流式执行远程命令,逐块产出 stdout。"""
        stdin_data = (self._password + '\n').encode() if sudo else None
        proc = subprocess.Popen(
            self._ssh_cmd(remote_cmd),
            stdin=subprocess.PIPE if sudo else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._env(), start_new_session=True,
        )
        if sudo:
            assert proc.stdin is not None
            proc.stdin.write(stdin_data)
            proc.stdin.close()
        assert proc.stdout is not None
        try:
            while True:
                chunk = proc.stdout.read(1 << 16)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            proc.wait(timeout=timeout)

    # ---------- 视频清单 ----------

    def list_videos(self) -> List[Dict[str, Any]]:
        """列出 NAS rec 目录下所有视频。"""
        exts = ' -o '.join(f"-iname '*{e}'" for e in sorted(VIDEO_EXTS))
        cmd = (
            f'find {shlex.quote(NAS_REC_DIR)} -type f \\( {exts} \\) 2>/dev/null '
            '| while IFS= read -r f; do '
            's=$(stat -c \'%s\' "$f" 2>/dev/null || stat -f \'%z\' "$f" 2>/dev/null); '
            'printf \'%s\\t%s\\n\' "$f" "$s"; done'
        )
        out = self.run(cmd, timeout=300).decode(errors='replace')
        videos: List[Dict[str, Any]] = []
        for line in out.splitlines():
            line = line.rstrip('\n')
            if not line:
                continue
            path, _, size = line.rpartition('\t')
            if not path:
                continue
            rel = path[len(NAS_REC_DIR):].lstrip('/')
            parts = rel.split('/')
            streamer = parts[0] if parts else ''
            room_id = streamer.split(' ')[0] if streamer else ''
            filename = parts[-1] if parts else rel
            videos.append({
                'path': path,
                'remote_path': rel,
                'streamer': streamer,
                'room_id': room_id,
                'filename': filename,
                'size_bytes': int(size) if size.isdigit() else 0,
            })
        return videos

    # ---------- Worker 训练候选 ----------

    def list_training_candidates(self) -> List[Dict[str, Any]]:
        """读取 worker 落在 NAS 上的候选帧说明，不下载图片。"""
        root = shlex.quote(NAS_TRAINING_CANDIDATE_DIR)
        cmd = (
            f'if [ -d {root} ]; then '
            f'find {root} -type f -name \'*.json\' 2>/dev/null | sort | '
            'while IFS= read -r f; do cat "$f"; printf \'\\n\'; done; fi'
        )
        output = self.run(cmd, timeout=300).decode(errors='replace')
        items: List[Dict[str, Any]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError('NAS worker 候选说明不是 JSON 对象')
            self._candidate_relative_path(value.get('image_path'))
            items.append(value)
        return items

    def read_training_candidate(self, relative_path: str) -> bytes:
        """下载一张 worker 候选图；路径只能位于候选目录内部。"""
        safe_path = self._candidate_relative_path(relative_path)
        absolute = '{}/{}'.format(
            NAS_TRAINING_CANDIDATE_DIR.rstrip('/'), safe_path
        )
        return self.run(f'cat {shlex.quote(absolute)}', timeout=60)

    @staticmethod
    def _candidate_relative_path(value: object) -> str:
        path = PurePosixPath(str(value or ''))
        if (
            not str(value or '')
            or path.is_absolute()
            or '..' in path.parts
            or path.suffix.lower() not in ('.jpg', '.jpeg')
        ):
            raise ValueError('worker 候选图片路径无效')
        return path.as_posix()

    def ffprobe_duration(self, remote_path: str, *, timeout: int = 120) -> float:
        """容器内 ffprobe 视频时长(秒)。"""
        cmd = (
            f'{DOCKER_EXEC} ffprobe -v error -show_entries format=duration '
            f'-of csv=p=0 {shlex.quote(self._container_path(remote_path))}'
        )
        out = self.run(cmd, timeout=timeout, sudo=True).decode(errors='replace').strip()
        try:
            return float(out)
        except ValueError:
            return 0.0

    def download(self, remote_path: str, local_path: Path,
                 total_bytes: int = 0,
                 progress_cb: Optional[Callable[[int, int], None]] = None,
                 *, timeout: int = 7200) -> Path:
        """把 NAS 视频流式下载到本地(ssh cat 管道,不进内存)。"""
        abs_path = f'{NAS_REC_DIR}/{remote_path}'
        cmd = f'cat {shlex.quote(abs_path)}'
        local_path.parent.mkdir(parents=True, exist_ok=True)
        got = 0
        with local_path.open('wb') as fh:
            for chunk in self.stream(cmd, timeout=timeout):
                fh.write(chunk)
                got += len(chunk)
                if progress_cb:
                    progress_cb(got, total_bytes)
        return local_path

    def _container_path(self, remote_path: str) -> str:
        return f'{CONTAINER_REC}/{remote_path}'

    # ---------- 抽帧通道(全部带真实 PTS) ----------

    def _frames_pts(self, cmd: str) -> Iterator[Tuple[int, bytes]]:
        """执行 ffmpeg 命令,产出 (pts_ms, jpeg)。showinfo 经 stderr 解析。"""
        stdin_data = (self._password + '\n').encode()
        proc = subprocess.Popen(
            self._ssh_cmd(cmd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=self._env(), start_new_session=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(stdin_data)
        proc.stdin.close()
        pts_q: 'queue.Queue[Optional[float]]' = queue.Queue()

        def _read_stderr() -> None:
            assert proc.stderr is not None
            for raw in proc.stderr:
                m = _PTS_RE.search(raw.decode(errors='replace'))
                if m:
                    pts_q.put(float(m.group(1)))
            pts_q.put(None)

        t = threading.Thread(target=_read_stderr, daemon=True)
        t.start()
        assert proc.stdout is not None
        try:
            frames = _jpeg_stream(iter(lambda: proc.stdout.read(1 << 16), b''))
            for jpeg in frames:
                pts = pts_q.get(timeout=30)
                if pts is None:
                    break
                yield int(round(pts * 1000)), jpeg
        finally:
            proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            t.join(timeout=5)
            proc.wait(timeout=3600)

    def coarse_frames(self, remote_path: str, *, sample_seconds: int = 2,
                      width: int = 640) -> Iterator[Tuple[int, bytes]]:
        """粗扫:小图、低帧率,用于模型预打分与候选定位。"""
        vf = f'fps=1/{sample_seconds},scale={width}:-2,showinfo'
        cmd = (
            f'{DOCKER_EXEC} ffmpeg -hide_banner '
            f'-i {shlex.quote(self._container_path(remote_path))} '
            f'-vf {shlex.quote(vf)} -q:v 6 -f image2pipe -c:v mjpeg -'
        )
        return self._frames_pts(cmd)

    def dense_frames(self, remote_path: str, *, start_ms: int = 0,
                     end_ms: Optional[int] = None, fps: int = 4,
                     width: Optional[int] = None) -> Iterator[Tuple[int, bytes]]:
        """密集抽帧:指定区间(默认全片)按 fps 抽原始分辨率帧。"""
        scale = f',scale={width}:-2' if width else ''
        vf = f'fps={fps}{scale},showinfo'
        cmd = (
            f'{DOCKER_EXEC} ffmpeg -hide_banner '
            f'-ss {start_ms / 1000:.3f} '
            f'-i {shlex.quote(self._container_path(remote_path))} '
            f'-vf {shlex.quote(vf)} -q:v {5} -f image2pipe -c:v mjpeg -'
        )
        if end_ms is not None:
            cmd += f' -t {(end_ms - start_ms) / 1000:.3f}'
        return self._frames_pts(cmd)

    def point_frames(self, remote_path: str, *,
                     times_ms: Sequence[int],
                     width: Optional[int] = None) -> Iterator[Tuple[int, bytes]]:
        """多点单帧:对每个时间点抽 1 帧原始分辨率(seek 定位)。

        注意:不挂 fps filter 时 showinfo 的 pts_time 不可靠(time_base 0/0),
        帧时间戳直接取请求时间(seek 后帧的真实位置在 [t-关键帧间隔, t] 内)。
        """
        scale = f',scale={width}:-2' if width else ''
        parts = []
        for t in times_ms:
            parts.append(
                f"{DOCKER_EXEC} ffmpeg -hide_banner -ss {t / 1000:.3f} "
                f"-i {shlex.quote(self._container_path(remote_path))} "
                f"-vf 'null{scale}' -frames:v 1 -q:v 5 "
                f"-f image2pipe -c:v mjpeg -"
            )
        cmd = '; '.join(parts)
        frames = _jpeg_stream(self.stream(cmd, sudo=True))
        for t, jpeg in zip(times_ms, frames):
            yield t, jpeg
