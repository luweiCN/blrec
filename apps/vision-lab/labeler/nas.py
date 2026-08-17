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

import base64
import json
import os
import queue
import re
import shlex
import struct
import subprocess
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple
from uuid import uuid4

from .config import (
    CANDIDATE_LOCAL_DIR,
    NAS_HOST,
    NAS_REC_DIR,
    NAS_RESULT_FRAME_DIR,
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
    def __init__(self, *, candidate_local_root: Optional[Path] = None) -> None:
        self._user = os.environ.get('SYNO_ADMIN_USERNAME', '')
        self._password = os.environ.get('SYNO_ADMIN_PASSWORD', '')
        self._askpass: Optional[Path] = None
        self._candidate_local_root = (
            candidate_local_root.resolve()
            if candidate_local_root is not None
            else (
                None if CANDIDATE_LOCAL_DIR is None else CANDIDATE_LOCAL_DIR.resolve()
            )
        )
        self._result_frame_root = NAS_RESULT_FRAME_DIR
        self._candidate_json_cache: Optional[
            Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]
        ] = None

    def _require_remote_credentials(self) -> None:
        if not self._user:
            self._user = _require_env('SYNO_ADMIN_USERNAME')
        if not self._password:
            self._password = _require_env('SYNO_ADMIN_PASSWORD')
        if self._askpass is None:
            self._askpass = ensure_askpass_script()

    def _env(self) -> Dict[str, str]:
        self._require_remote_credentials()
        assert self._askpass is not None
        env = dict(os.environ)
        env.update(
            {
                'SSH_ASKPASS': str(self._askpass),
                'SSH_ASKPASS_REQUIRE': 'force',
                'DISPLAY': ':0',
            }
        )
        return env

    def _ssh_cmd(self, remote_cmd: str) -> List[str]:
        self._require_remote_credentials()
        return [
            'ssh',
            '-o',
            'StrictHostKeyChecking=no',
            '-o',
            'ConnectTimeout=10',
            f'{self._user}@{NAS_HOST}',
            remote_cmd,
        ]

    def run(
        self,
        remote_cmd: str,
        *,
        timeout: int = 60,
        check: bool = True,
        sudo: bool = False,
    ) -> bytes:
        self._require_remote_credentials()
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

    def run_with_input(
        self, remote_cmd: str, content: bytes, *, timeout: int = 60, sudo: bool = False
    ) -> bytes:
        """把内容经 stdin 送入远程命令；用于原子写入小型复核 JSON。"""
        self._require_remote_credentials()
        stdin_data = (self._password + '\n').encode() + content if sudo else content
        proc = subprocess.run(
            self._ssh_cmd(remote_cmd),
            input=stdin_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(),
            start_new_session=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f'NAS 命令失败(exit={proc.returncode}):\n'
                f'cmd: {remote_cmd}\n'
                f'stderr: {proc.stderr.decode(errors="replace")}'
            )
        return proc.stdout

    def stream(
        self, remote_cmd: str, *, timeout: int = 3600, sudo: bool = False
    ) -> Iterator[bytes]:
        """流式执行远程命令,逐块产出 stdout。"""
        self._require_remote_credentials()
        stdin_data = (self._password + '\n').encode() if sudo else None
        proc = subprocess.Popen(
            self._ssh_cmd(remote_cmd),
            stdin=subprocess.PIPE if sudo else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(),
            start_new_session=True,
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
            rel = path[len(NAS_REC_DIR) :].lstrip('/')
            parts = rel.split('/')
            streamer = parts[0] if parts else ''
            room_id = streamer.split(' ')[0] if streamer else ''
            filename = parts[-1] if parts else rel
            videos.append(
                {
                    'path': path,
                    'remote_path': rel,
                    'streamer': streamer,
                    'room_id': room_id,
                    'filename': filename,
                    'size_bytes': int(size) if size.isdigit() else 0,
                }
            )
        return videos

    # ---------- Worker 训练候选 ----------

    def list_training_candidates(self) -> List[Dict[str, Any]]:
        """读取 worker 落在 NAS 上的候选帧说明，不下载图片。"""
        if self._candidate_local_root is not None:
            return self._list_local_candidate_json(review=False)
        root = shlex.quote(NAS_TRAINING_CANDIDATE_DIR)
        shell = (
            f'if [ -d {root} ]; then '
            f'find {root} -type f -name \'*.json\' '
            "! -name '*.review.json' 2>/dev/null | sort | "
            'while IFS= read -r f; do cat "$f"; printf \'\\n\'; done; fi'
        )
        command = 'sudo -S sh -c {}'.format(shlex.quote(shell))
        output = self.run(command, timeout=300, sudo=True).decode(errors='replace')
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

    def list_training_candidate_reviews(self) -> List[Dict[str, Any]]:
        """读取其他 Vision Lab 已回传的人工复核，不下载候选图片。"""
        if self._candidate_local_root is not None:
            return self._list_local_candidate_json(review=True)
        root = shlex.quote(NAS_TRAINING_CANDIDATE_DIR)
        shell = (
            f'if [ -d {root} ]; then '
            f'find {root} -type f -name \'*.review.json\' 2>/dev/null | sort | '
            'while IFS= read -r f; do cat "$f"; printf \'\\n\'; done; fi'
        )
        command = 'sudo -S sh -c {}'.format(shlex.quote(shell))
        output = self.run(command, timeout=300, sudo=True).decode(errors='replace')
        reviews: List[Dict[str, Any]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError('NAS worker 人工复核不是 JSON 对象')
            self._candidate_relative_path(value.get('image_path'))
            reviews.append(value)
        return reviews

    def read_training_candidate(self, relative_path: str) -> bytes:
        """下载一张 worker 候选图；路径只能位于候选目录内部。"""
        local_path = self.training_candidate_local_path(relative_path)
        if local_path is not None:
            return local_path.read_bytes()
        safe_path = self._candidate_relative_path(relative_path)
        absolute = '{}/{}'.format(NAS_TRAINING_CANDIDATE_DIR.rstrip('/'), safe_path)
        return self.run(f'sudo -S cat {shlex.quote(absolute)}', timeout=60, sudo=True)

    def write_training_candidate_review(
        self, relative_image_path: str, review: Dict[str, Any]
    ) -> None:
        """在候选图旁原子写入 `.review.json`；不覆盖候选图或模型预标。"""
        if self._candidate_local_root is not None:
            self._write_local_candidate_review(relative_image_path, review)
            return
        safe_image = PurePosixPath(self._candidate_relative_path(relative_image_path))
        relative_review = safe_image.with_suffix('.review.json')
        absolute = PurePosixPath(NAS_TRAINING_CANDIDATE_DIR) / relative_review
        parent = absolute.parent
        temporary = parent / ('.review-{}-{}.tmp'.format(absolute.stem, uuid4().hex))
        shell = (
            'umask 077; mkdir -p {parent}; cat > {temporary}; '
            'chmod 600 {temporary}; mv -f {temporary} {destination}'
        ).format(
            parent=shlex.quote(parent.as_posix()),
            temporary=shlex.quote(temporary.as_posix()),
            destination=shlex.quote(absolute.as_posix()),
        )
        command = 'sudo -S sh -c {}'.format(shlex.quote(shell))
        payload = json.dumps(
            review, ensure_ascii=False, separators=(',', ':'), sort_keys=True
        ).encode('utf-8')
        self.run_with_input(command, payload, timeout=60, sudo=True)

    def training_candidate_local_path(self, relative_path: str) -> Optional[Path]:
        """返回容器已挂载候选图的安全路径；远程 SSH 模式返回 ``None``。"""
        if self._candidate_local_root is None:
            return None
        safe_path = self._candidate_relative_path(relative_path)
        resolved = (self._candidate_local_root / safe_path).resolve()
        try:
            resolved.relative_to(self._candidate_local_root)
        except ValueError as error:
            raise ValueError('worker 候选图片路径越出候选目录') from error
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return resolved

    def _list_local_candidate_json(self, *, review: bool) -> List[Dict[str, Any]]:
        candidates, reviews = self._load_local_candidate_json()
        return list(reviews if review else candidates)

    def _load_local_candidate_json(
        self,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        if self._candidate_json_cache is None:
            self._candidate_json_cache = self._scan_local_candidate_json()
        return self._candidate_json_cache

    def _scan_local_candidate_json(
        self,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        root = self._candidate_local_root
        assert root is not None
        if not root.is_dir():
            return [], []
        suffix = '.review.json'
        paths = sorted(root.rglob('*.json'))
        candidates: List[Dict[str, Any]] = []
        reviews: List[Dict[str, Any]] = []
        for path in paths:
            is_review = path.name.endswith(suffix)
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError('NAS worker 候选说明不是 JSON 对象')
            self._candidate_relative_path(value.get('image_path'))
            (reviews if is_review else candidates).append(value)
        return candidates, reviews

    def _write_local_candidate_review(
        self, relative_image_path: str, review: Dict[str, Any]
    ) -> None:
        root = self._candidate_local_root
        assert root is not None
        safe_image = PurePosixPath(self._candidate_relative_path(relative_image_path))
        relative_review = safe_image.with_suffix('.review.json')
        destination = (root / relative_review.as_posix()).resolve()
        try:
            destination.relative_to(root)
        except ValueError as error:
            raise ValueError('worker 候选复核路径越出候选目录') from error
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            review, ensure_ascii=False, separators=(',', ':'), sort_keys=True
        ).encode('utf-8')
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='wb',
                prefix='.review-',
                suffix='.tmp',
                dir=str(destination.parent),
                delete=False,
            ) as output:
                temporary_path = Path(output.name)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, destination)
            destination.chmod(0o600)
            self._candidate_json_cache = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    # ---------- 已识别结算截图 ----------

    def list_result_frame_candidates(self) -> List[Dict[str, Any]]:
        """从运行中 BLREC 只读列出已有对局结算截图及其来源信息。"""
        script = """
import json
import os
import sqlite3

root = os.environ.get(
    'BLREC_VAINGLORY_RESULT_FRAME_ROOT', '/cfg/vainglory-result-frames')
connection = sqlite3.connect('file:/cfg/blrec.sqlite3?mode=ro', uri=True)
connection.row_factory = sqlite3.Row
rows = connection.execute('''
    SELECT match.id AS match_id, match.session_id,
           match.result_part_id AS part_id, part.part_index,
           match.result_at_ms, match.duration_seconds, match.started_at_ms,
           match.game_mode, match.confidence,
           match.result_frame_path, session.anchor_name, session.room_id,
           session.title, session.started_at AS session_started_at,
           (SELECT COUNT(*) FROM vainglory_match_players player
            WHERE player.match_id = match.id) AS hero_slot_count
    FROM vainglory_matches match
    JOIN recording_parts part ON part.id = match.result_part_id
    JOIN recording_sessions session ON session.id = match.session_id
    WHERE match.result_frame_path IS NOT NULL
    ORDER BY match.id
''').fetchall()
for row in rows:
    value = dict(row)
    relative = str(value.get('result_frame_path') or '')
    if (not relative or relative.startswith('/') or '..' in relative.split('/')):
        continue
    if not os.path.isfile(os.path.join(root, relative)):
        continue
    value['_container_result_root'] = root
    print(json.dumps(
        value, ensure_ascii=False, separators=(',', ':'), sort_keys=True))
""".strip()
        command = '{} python -c {}'.format(DOCKER_EXEC, shlex.quote(script))
        output = self.run(command, timeout=300, sudo=True).decode(errors='replace')
        result = []
        for line in output.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError('NAS 历史结算图说明不是 JSON 对象')
            result_root = value.pop('_container_result_root', '')
            if result_root:
                self._result_frame_root = self._validated_result_frame_root(result_root)
            self._result_frame_relative_path(value.get('result_frame_path'))
            result.append(value)
        return result

    def read_result_frame(self, relative_path: str) -> bytes:
        """从容器的结算截图目录只读下载一张 PNG。"""
        safe_path = self._result_frame_relative_path(relative_path)
        absolute = '{}/{}'.format(self._result_frame_root.rstrip('/'), safe_path)
        command = '{} cat {}'.format(DOCKER_EXEC, shlex.quote(absolute))
        return self.run(command, timeout=60, sudo=True)

    def read_result_frames(self, relative_paths: Sequence[str]) -> Dict[str, bytes]:
        """一次 SSH 批量读取结算图，避免上千张图重复建立连接。"""
        if not relative_paths:
            return {}
        if len(relative_paths) > 32:
            raise ValueError('单次最多批量读取 32 张结算图')
        paths = [self._result_frame_relative_path(value) for value in relative_paths]
        encoded = base64.urlsafe_b64encode(
            json.dumps(paths, separators=(',', ':')).encode('utf-8')
        ).decode('ascii')
        script = """
import base64
import json
import os
import struct
import sys

root = os.environ.get(
    'BLREC_VAINGLORY_RESULT_FRAME_ROOT', '/cfg/vainglory-result-frames')
paths = json.loads(base64.urlsafe_b64decode(sys.argv[1]).decode('utf-8'))
output = sys.stdout.buffer
for relative in paths:
    with open(os.path.join(root, relative), 'rb') as handle:
        content = handle.read()
    output.write(struct.pack('>Q', len(content)))
    output.write(content)
""".strip()
        command = '{} python -c {} {}'.format(
            DOCKER_EXEC, shlex.quote(script), shlex.quote(encoded)
        )
        payload = self.run(command, timeout=300, sudo=True)
        result: Dict[str, bytes] = {}
        offset = 0
        for path in paths:
            if offset + 8 > len(payload):
                raise RuntimeError('NAS 批量结算图片响应不完整')
            size = struct.unpack('>Q', payload[offset : offset + 8])[0]
            offset += 8
            end = offset + size
            if end > len(payload):
                raise RuntimeError('NAS 批量结算图片内容不完整')
            result[path] = payload[offset:end]
            offset = end
        if offset != len(payload):
            raise RuntimeError('NAS 批量结算图片响应包含多余内容')
        return result

    @staticmethod
    def _validated_result_frame_root(value: object) -> str:
        path = PurePosixPath(str(value or ''))
        if not path.is_absolute() or '..' in path.parts or str(path) == '/':
            raise ValueError('历史结算图片根目录无效')
        return path.as_posix()

    @staticmethod
    def _result_frame_relative_path(value: object) -> str:
        path = PurePosixPath(str(value or ''))
        if (
            not str(value or '')
            or path.is_absolute()
            or '..' in path.parts
            or path.suffix.lower() != '.png'
        ):
            raise ValueError('历史结算图片路径无效')
        return path.as_posix()

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

    def download(
        self,
        remote_path: str,
        local_path: Path,
        total_bytes: int = 0,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        *,
        timeout: int = 7200,
    ) -> Path:
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
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(),
            start_new_session=True,
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

    def coarse_frames(
        self, remote_path: str, *, sample_seconds: int = 2, width: int = 640
    ) -> Iterator[Tuple[int, bytes]]:
        """粗扫:小图、低帧率,用于模型预打分与候选定位。"""
        vf = f'fps=1/{sample_seconds},scale={width}:-2,showinfo'
        cmd = (
            f'{DOCKER_EXEC} ffmpeg -hide_banner '
            f'-i {shlex.quote(self._container_path(remote_path))} '
            f'-vf {shlex.quote(vf)} -q:v 6 -f image2pipe -c:v mjpeg -'
        )
        return self._frames_pts(cmd)

    def dense_frames(
        self,
        remote_path: str,
        *,
        start_ms: int = 0,
        end_ms: Optional[int] = None,
        fps: int = 4,
        width: Optional[int] = None,
    ) -> Iterator[Tuple[int, bytes]]:
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

    def point_frames(
        self, remote_path: str, *, times_ms: Sequence[int], width: Optional[int] = None
    ) -> Iterator[Tuple[int, bytes]]:
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
