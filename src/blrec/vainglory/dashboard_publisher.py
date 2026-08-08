from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, List, Mapping, Optional, Protocol

import requests

from blrec.networking.manager import NetworkRouteManager, RouteSelection
from blrec.networking.requests_session import RoutedRequestsSession
from blrec.setting.models import Settings

from .dashboard_snapshot import SHANGHAI, DashboardExportResult, export_dashboard_files

__all__ = (
    'DashboardPublicationResult',
    'DashboardPublishError',
    'OssDashboardStore',
    'next_publication_at',
    'publish_dashboard_once',
)


LOGGER = logging.getLogger('dashboard-publisher')
SNAPSHOT_PATH = re.compile(r'snapshots/[a-zA-Z0-9-]+\.json\Z')
DEFAULT_RETRY_SECONDS = 15 * 60


class DashboardPublishError(RuntimeError):
    pass


class DashboardStore(Protocol):
    def load_manifest(self) -> Optional[bytes]:
        pass

    def put_snapshot(self, path: str, content: bytes, sha256: str) -> int:
        pass

    def put_manifest(self, content: bytes) -> int:
        pass


@dataclass(frozen=True)
class DashboardPublicationResult:
    published: bool
    publication_date: date
    snapshot_id: str
    source_last_match_id: int
    source_match_count: Optional[int]
    uploaded_bytes: int


def _json_mapping(content: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(content.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DashboardPublishError('{}不是有效 JSON'.format(label)) from exc
    if not isinstance(value, Mapping):
        raise DashboardPublishError('{}不是 JSON 对象'.format(label))
    return value


def _manifest(content: bytes, label: str) -> Mapping[str, Any]:
    value = _json_mapping(content, label)
    if value.get('schemaVersion') != 1:
        raise DashboardPublishError('{}版本不受支持'.format(label))
    snapshot_id = value.get('snapshotId')
    snapshot_path = value.get('snapshotPath')
    publication_date = value.get('publicationDate')
    source_last_match_id = value.get('sourceLastMatchId')
    sha256 = value.get('sha256')
    byte_count = value.get('bytes')
    if (
        not isinstance(snapshot_id, str)
        or not snapshot_id
        or not isinstance(snapshot_path, str)
        or SNAPSHOT_PATH.fullmatch(snapshot_path) is None
        or snapshot_path != 'snapshots/{}.json'.format(snapshot_id)
        or not isinstance(publication_date, str)
        or type(source_last_match_id) is not int
        or source_last_match_id < 0
        or not isinstance(sha256, str)
        or re.fullmatch(r'[0-9a-f]{64}', sha256) is None
        or type(byte_count) is not int
        or byte_count <= 0
    ):
        raise DashboardPublishError('{}字段无效'.format(label))
    try:
        date.fromisoformat(publication_date)
    except ValueError as exc:
        raise DashboardPublishError('{}发布日期无效'.format(label)) from exc
    return value


def _validate_snapshot(path: Path, manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    if not path.is_file():
        raise DashboardPublishError('本地待发布快照不存在：{}'.format(path))
    content = path.read_bytes()
    if len(content) != manifest['bytes']:
        raise DashboardPublishError('本地待发布快照长度与 manifest 不一致')
    digest = hashlib.sha256(content).hexdigest()
    if digest != manifest['sha256']:
        raise DashboardPublishError('本地待发布快照摘要与 manifest 不一致')
    snapshot = _json_mapping(content, '本地待发布快照')
    if snapshot.get('snapshotId') != manifest['snapshotId']:
        raise DashboardPublishError('本地待发布快照 ID 与 manifest 不一致')
    return snapshot


def _pending_export(
    database_path: Path,
    state_directory: Path,
    now: datetime,
    exporter: Callable[..., DashboardExportResult],
) -> DashboardExportResult:
    pending = state_directory.expanduser().resolve() / 'pending'
    manifest_path = pending / 'manifest.json'
    if manifest_path.is_file():
        content = manifest_path.read_bytes()
        manifest = _manifest(content, '本地待发布 manifest')
        if manifest['publicationDate'] == now.astimezone(SHANGHAI).date().isoformat():
            snapshot_path = pending / str(manifest['snapshotPath'])
            _validate_snapshot(snapshot_path, manifest)
            return DashboardExportResult(
                manifest_path=manifest_path,
                snapshot_path=snapshot_path,
                manifest=manifest,
                sha256=str(manifest['sha256']),
            )
    return exporter(database_path, pending, now=now)


def publish_dashboard_once(
    database_path: Path,
    state_directory: Path,
    store: DashboardStore,
    *,
    now: Optional[datetime] = None,
    exporter: Callable[..., DashboardExportResult] = export_dashboard_files,
) -> DashboardPublicationResult:
    generated_at = now or datetime.now(tz=SHANGHAI)
    if generated_at.tzinfo is None:
        raise DashboardPublishError('发布时间必须包含时区')
    today = generated_at.astimezone(SHANGHAI).date()
    remote_content = store.load_manifest()
    remote_manifest = (
        _manifest(remote_content, '远端 manifest')
        if remote_content is not None
        else None
    )
    if remote_manifest is not None:
        remote_date = date.fromisoformat(str(remote_manifest['publicationDate']))
        if remote_date > today:
            raise DashboardPublishError('远端 manifest 的发布日期来自未来')
        if remote_date == today:
            return DashboardPublicationResult(
                published=False,
                publication_date=today,
                snapshot_id=str(remote_manifest['snapshotId']),
                source_last_match_id=int(remote_manifest['sourceLastMatchId']),
                source_match_count=None,
                uploaded_bytes=0,
            )

    exported = _pending_export(database_path, state_directory, generated_at, exporter)
    local_manifest_content = exported.manifest_path.read_bytes()
    local_manifest = _manifest(local_manifest_content, '本地待发布 manifest')
    snapshot = _validate_snapshot(exported.snapshot_path, local_manifest)
    if local_manifest['publicationDate'] != today.isoformat():
        raise DashboardPublishError('本地待发布快照不是今天生成的')
    if remote_manifest is not None and int(local_manifest['sourceLastMatchId']) < int(
        remote_manifest['sourceLastMatchId']
    ):
        raise DashboardPublishError('数据源进度发生回退，已停止覆盖远端榜单')

    uploaded_bytes = store.put_snapshot(
        str(local_manifest['snapshotPath']),
        exported.snapshot_path.read_bytes(),
        str(local_manifest['sha256']),
    )
    uploaded_bytes += store.put_manifest(local_manifest_content)
    committed = store.load_manifest()
    if committed != local_manifest_content:
        raise DashboardPublishError('远端 manifest 提交后校验失败')

    source_match_count = snapshot.get('sourceMatchCount')
    if type(source_match_count) is not int or source_match_count < 0:
        source_match_count = None
    return DashboardPublicationResult(
        published=True,
        publication_date=today,
        snapshot_id=str(local_manifest['snapshotId']),
        source_last_match_id=int(local_manifest['sourceLastMatchId']),
        source_match_count=source_match_count,
        uploaded_bytes=uploaded_bytes,
    )


class _RoutedOssSession:
    def __init__(
        self, oss2_module: Any, route_manager: NetworkRouteManager, affinity_key: str
    ) -> None:
        self._oss2 = oss2_module
        self._session = RoutedRequestsSession(
            route_manager,
            purpose='dashboard_publish',
            anonymous=False,
            affinity_key=affinity_key,
        )

    def do_request(self, request: Any, timeout: float) -> Any:
        try:
            response = self._session.request(
                request.method,
                request.url,
                data=request.data,
                params=request.params,
                headers=request.headers,
                stream=True,
                timeout=timeout,
                proxies=request.proxies,
            )
        except requests.RequestException as exc:
            raise self._oss2.exceptions.RequestError(exc) from exc
        return self._oss2.http.Response(response)

    def close(self) -> None:
        self._session.close()


class OssDashboardStore:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket_name: str,
        access_key_id: str,
        access_key_secret: str,
        route_manager: NetworkRouteManager,
        security_token: Optional[str] = None,
        prefix: str = 'data',
    ) -> None:
        try:
            import oss2
        except ImportError as exc:
            raise DashboardPublishError('缺少 oss2，无法发布排行榜数据') from exc

        self._oss2 = oss2
        self._prefix = prefix.strip('/')
        if not self._prefix:
            raise DashboardPublishError('OSS 对象前缀不能为空')
        self._affinity_key = 'dashboard-publication'
        self.selection: RouteSelection = route_manager.select(
            'dashboard_publish', anonymous=False, affinity_key=self._affinity_key
        )
        self._route_manager = route_manager
        self._session = _RoutedOssSession(oss2, route_manager, self._affinity_key)
        auth: Any
        if security_token:
            auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
        else:
            auth = oss2.Auth(access_key_id, access_key_secret)
        self._bucket = oss2.Bucket(
            auth, endpoint, bucket_name, session=self._session, connect_timeout=30
        )

    def _key(self, relative_path: str) -> str:
        normalized = relative_path.strip('/')
        if (
            not normalized
            or '..' in normalized.split('/')
            or normalized.startswith('site-stats')
        ):
            raise DashboardPublishError('不允许发布 OSS 对象：{}'.format(relative_path))
        return '{}/{}'.format(self._prefix, normalized)

    def _optional_object(self, key: str) -> Optional[bytes]:
        try:
            return self._bucket.get_object(key).read()
        except self._oss2.exceptions.OssError as exc:
            if int(getattr(exc, 'status', 0) or 0) == 404:
                return None
            raise

    def _progress(self) -> Callable[[int, Optional[int]], None]:
        consumed = 0

        def record(current: int, _total: Optional[int]) -> None:
            nonlocal consumed
            delta = max(0, current - consumed)
            consumed = max(consumed, current)
            self._route_manager.traffic_meter.record(
                self.selection.interface_name, 'dashboard_publish', 'up', delta
            )

        return record

    def load_manifest(self) -> Optional[bytes]:
        return self._optional_object(self._key('manifest.json'))

    def put_snapshot(self, path: str, content: bytes, sha256: str) -> int:
        if SNAPSHOT_PATH.fullmatch(path) is None:
            raise DashboardPublishError('快照对象路径无效：{}'.format(path))
        key = self._key(path)
        existing = self._optional_object(key)
        if existing is not None:
            if hashlib.sha256(existing).hexdigest() != sha256:
                raise DashboardPublishError('远端不可变快照内容冲突：{}'.format(path))
            return 0
        result = self._bucket.put_object(
            key,
            content,
            headers={
                'Cache-Control': 'public, max-age=31536000, immutable',
                'Content-Type': 'application/json; charset=utf-8',
                'x-oss-meta-sha256': sha256,
            },
            progress_callback=self._progress(),
        )
        if not 200 <= int(result.status) < 300:
            raise DashboardPublishError(
                'OSS 快照上传失败：HTTP {}'.format(result.status)
            )
        return len(content)

    def put_manifest(self, content: bytes) -> int:
        result = self._bucket.put_object(
            self._key('manifest.json'),
            content,
            headers={
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Content-Type': 'application/json; charset=utf-8',
            },
            progress_callback=self._progress(),
        )
        if not 200 <= int(result.status) < 300:
            raise DashboardPublishError(
                'OSS manifest 上传失败：HTTP {}'.format(result.status)
            )
        return len(content)

    def close(self) -> None:
        self._session.close()


def next_publication_at(now: datetime, hour: int, minute: int) -> datetime:
    if now.tzinfo is None:
        raise DashboardPublishError('当前时间必须包含时区')
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise DashboardPublishError('每日发布时间无效')
    local = now.astimezone(SHANGHAI)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


@contextmanager
def _exclusive_worker_lock(state_directory: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError as exc:
        raise DashboardPublishError('排行榜 worker 只支持类 Unix 文件锁') from exc

    state_directory.mkdir(parents=True, exist_ok=True)
    lock_path = state_directory / 'worker.lock'
    with lock_path.open('a+', encoding='utf-8') as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DashboardPublishError('已有排行榜 worker 正在运行') from exc
        yield


@dataclass(frozen=True)
class _WorkerConfiguration:
    database: Path
    settings: Path
    state: Path
    endpoint: str
    bucket: str
    prefix: str
    schedule_hour: int
    schedule_minute: int
    retry_seconds: int


def _required_environment(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        raise DashboardPublishError('缺少环境变量 {}'.format(name))
    return value


def _publish(
    configuration: _WorkerConfiguration, now: datetime
) -> DashboardPublicationResult:
    settings = Settings.load(str(configuration.settings))
    route_manager = NetworkRouteManager(lambda: settings.network)
    store = OssDashboardStore(
        endpoint=configuration.endpoint,
        bucket_name=configuration.bucket,
        prefix=configuration.prefix,
        access_key_id=_required_environment('ALIBABA_CLOUD_ACCESS_KEY_ID'),
        access_key_secret=_required_environment('ALIBABA_CLOUD_ACCESS_KEY_SECRET'),
        security_token=os.environ.get('ALIBABA_CLOUD_SECURITY_TOKEN') or None,
        route_manager=route_manager,
    )
    try:
        result = publish_dashboard_once(
            configuration.database, configuration.state, store, now=now
        )
    finally:
        store.close()
    LOGGER.info(
        'publication=%s date=%s snapshot=%s source_last_match_id=%s '
        'source_match_count=%s uploaded_bytes=%s purpose=dashboard_publish '
        'interface=%s source_address=%s role=%s',
        'published' if result.published else 'current',
        result.publication_date.isoformat(),
        result.snapshot_id,
        result.source_last_match_id,
        result.source_match_count,
        result.uploaded_bytes,
        store.selection.interface_name or 'system-default',
        store.selection.source_address or 'system-default',
        store.selection.role,
    )
    return result


def _worker_loop(configuration: _WorkerConfiguration) -> None:
    while True:
        now = datetime.now(tz=SHANGHAI)
        try:
            _publish(configuration, now)
        except Exception:
            LOGGER.exception('排行榜发布失败，%s 秒后重试', configuration.retry_seconds)
            time.sleep(configuration.retry_seconds)
            continue
        next_run = next_publication_at(
            now, configuration.schedule_hour, configuration.schedule_minute
        )
        delay = max(1.0, (next_run - now).total_seconds())
        LOGGER.info('下次检查时间：%s', next_run.isoformat())
        time.sleep(delay)


def _environment_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise DashboardPublishError('{} 必须是整数'.format(name)) from exc


def _parse_args(arguments: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='每日生成并发布虚荣排行榜 JSON')
    parser.add_argument('--once', action='store_true', help='只检查并发布一次')
    parser.add_argument(
        '--database',
        type=Path,
        default=Path(os.environ.get('DASHBOARD_DATABASE', '/cfg/blrec.sqlite3')),
    )
    parser.add_argument(
        '--settings',
        type=Path,
        default=Path(os.environ.get('BLREC_CONFIG', '/cfg/settings.toml')),
    )
    parser.add_argument(
        '--state', type=Path, default=Path(os.environ.get('DASHBOARD_STATE', '/state'))
    )
    parser.add_argument(
        '--endpoint',
        default=os.environ.get('OSS_ENDPOINT', 'https://oss-cn-beijing.aliyuncs.com'),
    )
    parser.add_argument(
        '--bucket', default=os.environ.get('OSS_BUCKET', 'luwei-vainglory')
    )
    parser.add_argument('--prefix', default=os.environ.get('OSS_PREFIX', 'data'))
    parser.add_argument(
        '--schedule-hour',
        type=int,
        default=_environment_int('DASHBOARD_SCHEDULE_HOUR', 0),
    )
    parser.add_argument(
        '--schedule-minute',
        type=int,
        default=_environment_int('DASHBOARD_SCHEDULE_MINUTE', 5),
    )
    parser.add_argument(
        '--retry-seconds',
        type=int,
        default=_environment_int('DASHBOARD_RETRY_SECONDS', DEFAULT_RETRY_SECONDS),
    )
    return parser.parse_args(arguments)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get('DASHBOARD_LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    logging.getLogger('oss2').setLevel(logging.WARNING)
    arguments = _parse_args()
    if arguments.retry_seconds <= 0:
        raise DashboardPublishError('重试间隔必须大于 0')
    configuration = _WorkerConfiguration(
        database=arguments.database,
        settings=arguments.settings,
        state=arguments.state,
        endpoint=arguments.endpoint,
        bucket=arguments.bucket,
        prefix=arguments.prefix,
        schedule_hour=arguments.schedule_hour,
        schedule_minute=arguments.schedule_minute,
        retry_seconds=arguments.retry_seconds,
    )
    with _exclusive_worker_lock(configuration.state):
        if arguments.once:
            _publish(configuration, datetime.now(tz=SHANGHAI))
        else:
            _worker_loop(configuration)


if __name__ == '__main__':
    main()
