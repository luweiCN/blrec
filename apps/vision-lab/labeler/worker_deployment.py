"""通过 SSH 向 Mac Analysis Worker 发布完整、不可变的模型包。"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence
from uuid import uuid4

from . import config

_PACKAGE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$')
_SSH_HOST = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$')
_SSH_USER = re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]{0,63}$')
_LAUNCHD_LABEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')

_REMOTE_STATUS_SCRIPT = r'''
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).expanduser()
label = sys.argv[2]
plist_path = Path(sys.argv[3]).expanduser()
package_path = root / 'current'
if not package_path.exists() and plist_path.is_file():
    try:
        plist = plistlib.loads(plist_path.read_bytes())
        args = list(plist.get('ProgramArguments') or [])
        index = args.index('--model-package')
        package_path = Path(args[index + 1]).expanduser()
    except (KeyError, ValueError, IndexError, OSError, plistlib.InvalidFileException):
        pass
package_id = ''
pipeline_version = ''
try:
    manifest = json.loads((package_path.resolve() / 'manifest.json').read_text('utf-8'))
    package_id = str(manifest.get('package_id') or '')
    pipeline_version = str(manifest.get('pipeline_version') or '')
except (FileNotFoundError, json.JSONDecodeError, OSError):
    pass
service = 'gui/{}/{}'.format(os.getuid(), label)
process = subprocess.run(
    ['launchctl', 'print', service], capture_output=True, text=True, check=False
)
output = process.stdout or ''
running = process.returncode == 0 and (
    'state = running' in output or 'pid =' in output
)
print(json.dumps({
    'ok': True,
    'package_id': package_id,
    'pipeline_version': pipeline_version,
    'worker_state': 'running' if running else 'stopped',
    'current_path': str(package_path),
}, ensure_ascii=False))
'''

_REMOTE_DEPLOY_SCRIPT = r'''
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REQUIRED_MODEL_ROLES = (
    'match_flow', 'hero_select', 'match_mode', 'result_mode', 'result_panel',
    'hero_avatar', 'hero_identity', 'player_position', 'afk_status',
)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract(archive, destination):
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > 128:
            raise ValueError('模型包文件数量异常')
        if sum(item.file_size for item in members) > 2 * 1024 * 1024 * 1024:
            raise ValueError('模型包解压后体积异常')
        root = destination.resolve()
        for item in members:
            target = (destination / item.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError as error:
                raise ValueError('模型包包含不安全路径') from error
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('模型包不能包含符号链接')
        source.extractall(destination)


def verify_package(path, expected_id):
    root = path.resolve()
    manifest = json.loads((root / 'manifest.json').read_text('utf-8'))
    if manifest.get('status') != 'ready':
        raise ValueError('只有 ready 模型包可以部署')
    if str(manifest.get('package_id') or '') != expected_id:
        raise ValueError('模型包 ID 与发布请求不一致')
    if int(manifest.get('schema_version') or 0) not in (1, 2):
        raise ValueError('模型包 schema_version 不受支持')
    models = manifest.get('models')
    if not isinstance(models, dict):
        raise ValueError('模型包 models 无效')
    missing = [role for role in REQUIRED_MODEL_ROLES if role not in models]
    if missing:
        raise ValueError('模型包缺少角色: {}'.format(', '.join(missing)))
    for role in REQUIRED_MODEL_ROLES:
        spec = models[role]
        if not isinstance(spec, dict):
            raise ValueError('{} 模型配置无效'.format(role))
        relative = Path(str(spec.get('file') or ''))
        if relative.is_absolute():
            raise ValueError('{} 模型路径必须是相对路径'.format(role))
        model_path = (root / relative).resolve()
        try:
            model_path.relative_to(root)
        except ValueError as error:
            raise ValueError('{} 模型路径越界'.format(role)) from error
        expected_hash = str(spec.get('sha256') or '').lower()
        if len(expected_hash) != 64 or not model_path.is_file():
            raise ValueError('{} 模型文件或 SHA-256 无效'.format(role))
        if sha256(model_path) != expected_hash:
            raise ValueError('{} 模型 SHA-256 校验失败'.format(role))
    return manifest


def package_id_at(path):
    try:
        manifest = json.loads((path.resolve() / 'manifest.json').read_text('utf-8'))
        return str(manifest.get('package_id') or '')
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ''


def replace_symlink(link, target):
    temporary = link.with_name('.current-{}'.format(os.getpid()))
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, link)


def write_plist(path, payload, mode):
    temporary = path.with_name('.{}.tmp'.format(path.name))
    temporary.write_bytes(plistlib.dumps(payload))
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def launch_state(label):
    service = 'gui/{}/{}'.format(os.getuid(), label)
    process = subprocess.run(
        ['launchctl', 'print', service], capture_output=True, text=True, check=False
    )
    output = process.stdout or ''
    return process.returncode == 0 and (
        'state = running' in output or 'pid =' in output
    )


def restart_worker(plist_path, label):
    domain = 'gui/{}'.format(os.getuid())
    service = '{}/{}'.format(domain, label)
    subprocess.run(
        ['launchctl', 'bootout', service], capture_output=True, text=True, check=False
    )
    time.sleep(0.5)
    bootstrap = subprocess.run(
        ['launchctl', 'bootstrap', domain, str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if bootstrap.returncode != 0:
        raise RuntimeError(
            'launchd 重新加载失败: {}'.format(bootstrap.stderr.strip())
        )
    kickstart = subprocess.run(
        ['launchctl', 'kickstart', '-k', service],
        capture_output=True,
        text=True,
        check=False,
    )
    if kickstart.returncode != 0:
        raise RuntimeError('Worker 启动失败: {}'.format(kickstart.stderr.strip()))


def log_offsets(plist):
    result = {}
    for key in ('StandardOutPath', 'StandardErrorPath'):
        value = str(plist.get(key) or '')
        if not value:
            continue
        path = Path(value).expanduser()
        result[path] = path.stat().st_size if path.is_file() else 0
    return result


def wait_until_loaded(label, package_id, offsets, timeout_seconds=45):
    deadline = time.monotonic() + timeout_seconds
    stable = 0
    collected = ''
    needle = 'package_id={}'.format(package_id)
    while time.monotonic() < deadline:
        running = launch_state(label)
        stable = stable + 1 if running else 0
        for path, offset in offsets.items():
            if not path.is_file():
                continue
            with path.open('rb') as handle:
                handle.seek(offset)
                content = handle.read(256 * 1024)
            text = content.decode('utf-8', errors='replace')
            collected += text
            offsets[path] = offset + len(content)
        if running and needle in collected:
            return 'startup_log'
        if stable >= 8:
            return 'stable_process'
        time.sleep(1)
    raise RuntimeError(
        'Worker 切换后没有稳定启动: {}'.format(collected[-1200:].strip())
    )


package_id = sys.argv[1]
archive = Path(sys.argv[2])
model_root = Path(sys.argv[3]).expanduser()
label = sys.argv[4]
plist_path = Path(sys.argv[5]).expanduser()
incoming = None
original_plist = None
original_mode = None
previous_target = None
current = model_root / 'current'
switched = False
try:
    if not plist_path.is_file():
        raise FileNotFoundError('找不到 Worker launchd 配置: {}'.format(plist_path))
    model_root.mkdir(parents=True, exist_ok=True)
    incoming = Path(tempfile.mkdtemp(prefix='.incoming-', dir=model_root))
    safe_extract(archive, incoming)
    candidate = incoming / package_id
    if not candidate.is_dir():
        raise ValueError('模型包 ZIP 顶层目录必须与 package_id 一致')
    verify_package(candidate, package_id)
    destination = model_root / package_id
    if destination.exists():
        verify_package(destination, package_id)
    else:
        os.replace(candidate, destination)

    original_plist = plist_path.read_bytes()
    original_mode = plist_path.stat().st_mode & 0o777
    plist = plistlib.loads(original_plist)
    arguments = list(plist.get('ProgramArguments') or [])
    try:
        model_index = arguments.index('--model-package') + 1
        previous_argument = Path(arguments[model_index]).expanduser()
    except (ValueError, IndexError) as error:
        raise ValueError('Worker launchd 缺少 --model-package 参数') from error
    if current.is_symlink():
        previous_target = current.resolve()
    elif current.exists():
        raise ValueError('Worker current 路径存在但不是符号链接')
    elif previous_argument.is_dir():
        previous_target = previous_argument.resolve()
    previous_package_id = (
        '' if previous_target is None else package_id_at(previous_target)
    )
    if previous_target == destination.resolve() and launch_state(label):
        print(json.dumps({
            'ok': True,
            'package_id': package_id,
            'previous_package_id': previous_package_id,
            'worker_state': 'running',
            'verification': 'already_active',
        }, ensure_ascii=False))
        raise SystemExit(0)

    offsets = log_offsets(plist)
    replace_symlink(current, destination.resolve())
    arguments[model_index] = str(current)
    plist['ProgramArguments'] = arguments
    write_plist(plist_path, plist, original_mode)
    switched = True
    restart_worker(plist_path, label)
    verification = wait_until_loaded(label, package_id, offsets)
    print(json.dumps({
        'ok': True,
        'package_id': package_id,
        'previous_package_id': previous_package_id,
        'worker_state': 'running',
        'verification': verification,
    }, ensure_ascii=False))
except SystemExit:
    raise
except Exception as error:
    rollback_error = ''
    if switched and original_plist is not None and original_mode is not None:
        try:
            original = plistlib.loads(original_plist)
            write_plist(plist_path, original, original_mode)
            if previous_target is None:
                current.unlink(missing_ok=True)
            else:
                replace_symlink(current, previous_target)
            restart_worker(plist_path, label)
        except Exception as rollback:
            rollback_error = '; 回滚失败: {}'.format(rollback)
    print(json.dumps({
        'ok': False,
        'error': '{}{}'.format(error, rollback_error),
        'worker_state': 'running' if launch_state(label) else 'stopped',
    }, ensure_ascii=False))
    raise SystemExit(1)
finally:
    archive.unlink(missing_ok=True)
    if incoming is not None:
        shutil.rmtree(incoming, ignore_errors=True)
'''


@dataclass(frozen=True)
class WorkerDeploymentTarget:
    host: str
    user: str
    model_root: str
    launchd_label: str
    launchd_plist: str
    port: int = 22
    identity_file: Optional[Path] = None

    @property
    def display_name(self) -> str:
        return '{}@{}:{}'.format(self.user, self.host, self.port)


def configured_target() -> WorkerDeploymentTarget:
    try:
        port = int(config.WORKER_SSH_PORT)
    except ValueError as error:
        raise ValueError('Worker SSH 端口必须是整数') from error
    identity = (
        None
        if not config.WORKER_SSH_IDENTITY
        else Path(config.WORKER_SSH_IDENTITY).expanduser()
    )
    return WorkerDeploymentTarget(
        host=config.WORKER_SSH_HOST,
        user=config.WORKER_SSH_USER,
        port=port,
        identity_file=identity,
        model_root=config.WORKER_MODEL_ROOT,
        launchd_label=config.WORKER_LAUNCHD_LABEL,
        launchd_plist=config.WORKER_LAUNCHD_PLIST,
    )


class WorkerDeploymentClient:
    def __init__(
        self,
        target: WorkerDeploymentTarget,
        *,
        run_process: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        self.target = target
        self._run_process = run_process
        self._validate_target()

    def status(self) -> Dict[str, Any]:
        command = self._ssh_command(
            self._remote_python_command(
                self.target.model_root,
                self.target.launchd_label,
                self.target.launchd_plist,
            )
        )
        result = self._run(
            command, input=_REMOTE_STATUS_SCRIPT.encode('utf-8'), timeout=15
        )
        return self._json_result(result.stdout)

    def deploy(self, archive: Path, package_id: str) -> Dict[str, Any]:
        if not _PACKAGE_ID.fullmatch(package_id):
            raise ValueError('模型包 ID 只能包含字母、数字、连字符和下划线')
        archive = Path(archive).resolve()
        if not archive.is_file() or archive.stat().st_size == 0:
            raise FileNotFoundError(archive)
        remote_archive = '/tmp/blrec-model-{}-{}.zip'.format(
            package_id, uuid4().hex[:12]
        )
        self._run(self._scp_command(archive, remote_archive), timeout=300)
        command = self._ssh_command(
            self._remote_python_command(
                package_id,
                remote_archive,
                self.target.model_root,
                self.target.launchd_label,
                self.target.launchd_plist,
            )
        )
        result = self._run(
            command, input=_REMOTE_DEPLOY_SCRIPT.encode('utf-8'), timeout=180
        )
        payload = self._json_result(result.stdout)
        if payload.get('package_id') != package_id:
            raise RuntimeError('Worker 返回的模型包版本与部署请求不一致')
        return payload

    def _validate_target(self) -> None:
        if not _SSH_HOST.fullmatch(self.target.host):
            raise ValueError('Worker SSH 主机格式无效')
        if not _SSH_USER.fullmatch(self.target.user):
            raise ValueError('Worker SSH 用户格式无效')
        if not 1 <= int(self.target.port) <= 65535:
            raise ValueError('Worker SSH 端口超出范围')
        if not _LAUNCHD_LABEL.fullmatch(self.target.launchd_label):
            raise ValueError('Worker launchd 标识格式无效')
        if not self.target.model_root or not self.target.launchd_plist:
            raise ValueError('Worker 模型目录和 launchd 配置不能为空')
        identity = self.target.identity_file
        if identity is not None and not identity.is_file():
            raise ValueError('Worker SSH 私钥文件不存在')

    def _connection_options(self, *, scp: bool = False) -> Sequence[str]:
        options = [
            '-o',
            'BatchMode=yes',
            '-o',
            'StrictHostKeyChecking=accept-new',
            '-o',
            'ConnectTimeout=10',
            '-P' if scp else '-p',
            str(self.target.port),
        ]
        if self.target.identity_file is not None:
            options.extend(['-i', str(self.target.identity_file)])
        return options

    def _ssh_command(self, remote_command: str) -> list[str]:
        return [
            'ssh',
            *self._connection_options(),
            self.target.display_name.rsplit(':', 1)[0],
            remote_command,
        ]

    def _scp_command(self, archive: Path, remote_archive: str) -> list[str]:
        destination = self.target.display_name.rsplit(':', 1)[0]
        return [
            'scp',
            *self._connection_options(scp=True),
            str(archive),
            '{}:{}'.format(destination, remote_archive),
        ]

    @staticmethod
    def _remote_python_command(*arguments: str) -> str:
        return shlex.join(['/usr/bin/python3', '-', *arguments])

    def _run(self, command: list[str], **kwargs: Any) -> Any:
        result = self._run_process(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            **kwargs,
        )
        if result.returncode != 0:
            message = result.stderr.decode('utf-8', errors='replace').strip()
            if not message and result.stdout:
                try:
                    payload = json.loads(result.stdout.decode('utf-8'))
                    message = (
                        str(payload.get('error') or '')
                        if isinstance(payload, dict)
                        else ''
                    )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    message = result.stdout.decode('utf-8', errors='replace').strip()
            raise RuntimeError(message or 'Worker SSH 命令失败')
        return result

    @staticmethod
    def _json_result(output: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(output.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError('Worker 返回了无法解析的部署结果') from error
        if not isinstance(payload, dict):
            raise RuntimeError('Worker 部署结果格式无效')
        if payload.get('ok') is not True:
            raise RuntimeError(str(payload.get('error') or 'Worker 部署失败'))
        return payload
