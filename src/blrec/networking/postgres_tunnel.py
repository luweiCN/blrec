from __future__ import annotations

import ipaddress
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

from loguru import logger

from .config import load_network_settings
from .manager import NetworkRouteManager, RouteSelection

_SSH_USER = re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]{0,63}$')


@dataclass(frozen=True)
class PostgresTunnelSettings:
    ssh_host: str
    ssh_port: int
    ssh_user: str
    identity_file: Path
    known_hosts_file: Path
    local_port: int
    remote_host: str
    remote_port: int
    network_settings_file: Path

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] = os.environ
    ) -> 'PostgresTunnelSettings':
        ssh_host = environment.get('BLREC_DATABASE_SSH_HOST', '').strip()
        try:
            ipaddress.IPv4Address(ssh_host)
        except ValueError as error:
            raise ValueError('BLREC_DATABASE_SSH_HOST 必须是固定 IP 地址') from error
        ssh_user = environment.get('BLREC_DATABASE_SSH_USER', '').strip()
        if _SSH_USER.fullmatch(ssh_user) is None:
            raise ValueError('BLREC_DATABASE_SSH_USER 无效')
        identity_file = _required_file(
            environment, 'BLREC_DATABASE_SSH_KEY_PATH', secret=True
        )
        known_hosts_file = _required_file(
            environment, 'BLREC_DATABASE_SSH_KNOWN_HOSTS_PATH', secret=False
        )
        remote_host = environment.get(
            'BLREC_DATABASE_TUNNEL_REMOTE_HOST', '127.0.0.1'
        ).strip()
        if remote_host != '127.0.0.1':
            raise ValueError('PostgreSQL 隧道远端只允许连接服务器回环地址')
        settings_file = Path(
            environment.get('BLREC_DEFAULT_SETTINGS_FILE', '/cfg/settings.toml')
        ).expanduser()
        if not settings_file.is_file():
            raise ValueError('网络设置文件不存在')
        return cls(
            ssh_host=ssh_host,
            ssh_port=_port(environment, 'BLREC_DATABASE_SSH_PORT', 22),
            ssh_user=ssh_user,
            identity_file=identity_file,
            known_hosts_file=known_hosts_file,
            local_port=_port(environment, 'BLREC_DATABASE_TUNNEL_LOCAL_PORT', 15432),
            remote_host=remote_host,
            remote_port=_port(environment, 'BLREC_DATABASE_TUNNEL_REMOTE_PORT', 5432),
            network_settings_file=settings_file,
        )

    def command(self, selection: RouteSelection) -> Tuple[str, ...]:
        command = [
            '/usr/bin/ssh',
            '-4',
            '-N',
            '-T',
            '-o',
            'BatchMode=yes',
            '-o',
            'ExitOnForwardFailure=yes',
            '-o',
            'ServerAliveInterval=15',
            '-o',
            'ServerAliveCountMax=3',
            '-o',
            'TCPKeepAlive=yes',
            '-o',
            'StrictHostKeyChecking=yes',
            '-o',
            'UserKnownHostsFile={}'.format(self.known_hosts_file),
            '-o',
            'IdentitiesOnly=yes',
            '-i',
            str(self.identity_file),
            '-p',
            str(self.ssh_port),
            '-L',
            '127.0.0.1:{}:{}:{}'.format(
                self.local_port, self.remote_host, self.remote_port
            ),
        ]
        if selection.source_address is not None:
            command.extend(('-b', selection.source_address))
        command.append('{}@{}'.format(self.ssh_user, self.ssh_host))
        return tuple(command)


def _required_file(environment: Mapping[str, str], name: str, *, secret: bool) -> Path:
    value = environment.get(name, '').strip()
    path = Path(value).expanduser()
    if not value or not path.is_file() or path.is_symlink():
        raise ValueError('{} 必须指向普通文件'.format(name))
    if secret and path.stat().st_mode & 0o077:
        raise ValueError('{} 权限必须为 0600 或更严格'.format(name))
    return path


def _port(environment: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(environment.get(name, str(default)))
    except ValueError as error:
        raise ValueError('{} 必须是端口号'.format(name)) from error
    if not 1 <= value <= 65535:
        raise ValueError('{} 必须是端口号'.format(name))
    return value


def _selection(settings: PostgresTunnelSettings) -> RouteSelection:
    network_settings = load_network_settings(settings.network_settings_file)
    manager = NetworkRouteManager(lambda: network_settings)
    return manager.select(
        'database', anonymous=False, affinity_key='postgres-main-database'
    )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run(environment: Mapping[str, str] = os.environ) -> int:
    settings = PostgresTunnelSettings.from_environment(environment)
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    process: Optional[subprocess.Popen[bytes]] = None
    settings_mtime_ns = 0
    try:
        while not stopped.is_set():
            if process is None:
                selection = _selection(settings)
                settings_mtime_ns = settings.network_settings_file.stat().st_mtime_ns
                logger.info(
                    'PostgreSQL 隧道启动：interface={} source={} role={} local_port={}',
                    selection.interface_name or 'system-default',
                    selection.source_address or 'system-default',
                    selection.role,
                    settings.local_port,
                )
                process = subprocess.Popen(settings.command(selection))
            if stopped.wait(5):
                break
            changed = (
                settings.network_settings_file.stat().st_mtime_ns != settings_mtime_ns
            )
            exited = process.poll() is not None
            if not changed and not exited:
                continue
            if changed:
                logger.info('网络设置已变化，重建 PostgreSQL 隧道')
            else:
                logger.warning('PostgreSQL 隧道已退出，5 秒后重试')
            _terminate(process)
            process = None
    finally:
        if process is not None:
            _terminate(process)
    return 0


def main(values: Sequence[str] = ()) -> None:
    if values:
        raise SystemExit('PostgreSQL tunnel does not accept arguments')
    raise SystemExit(run())


if __name__ == '__main__':
    main()
