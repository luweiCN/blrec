from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import Any, Callable, Iterator, List, Optional, Union

import requests

from blrec.networking.config import load_network_settings
from blrec.networking.manager import NetworkRouteManager, RouteSelection
from blrec.networking.requests_session import RoutedRequestsSession

from .api_sync import DashboardApiClient, sync_dashboard_api_once
from .cache_sync import sync_dashboard_cache_once
from .replay_visibility import (
    BilibiliReplayVisibilityChecker,
    ReplayVisibilityCheckError,
)
from .source_database import connect_source_database

__all__ = ('DashboardPublishError', 'OssDashboardStore')

LOGGER = logging.getLogger('dashboard-publisher')
MATCH_IMAGE_PATH = re.compile(
    r'match-images/[0-9]{3,}/[1-9][0-9]*-[0-9a-f]{16}\.webp\Z'
)
DEFAULT_RETRY_SECONDS = 60
DEFAULT_WATCH_SECONDS = 1
DEFAULT_DEBOUNCE_SECONDS = 2
DEFAULT_RECONCILE_SECONDS = 24 * 60 * 60
MIN_REPLAY_VISIBILITY_INTERVAL_SECONDS = 0.5


class DashboardPublishError(RuntimeError):
    pass


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
            raise DashboardPublishError('缺少 oss2，无法发布排行榜图片') from exc

        self._oss2 = oss2
        self._prefix = prefix.strip('/')
        if not self._prefix:
            raise DashboardPublishError('OSS 对象前缀不能为空')
        self._affinity_key = 'dashboard-image-assets'
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
        if MATCH_IMAGE_PATH.fullmatch(relative_path) is None:
            raise DashboardPublishError('结算图对象路径无效：{}'.format(relative_path))
        return '{}/{}'.format(self._prefix, relative_path)

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

    def put_match_image(self, path: str, content: bytes, sha256: str) -> int:
        key = self._key(path)
        existing = self._optional_object(key)
        if existing is not None:
            if hashlib.sha256(existing).hexdigest() != sha256:
                raise DashboardPublishError('远端不可变结算图内容冲突：{}'.format(path))
            return 0
        result = self._bucket.put_object(
            key,
            content,
            headers={
                'Cache-Control': 'public, max-age=31536000, immutable',
                'Content-Type': 'image/webp',
                'x-oss-meta-sha256': sha256,
            },
            progress_callback=self._progress(),
        )
        if not 200 <= int(result.status) < 300:
            raise DashboardPublishError(
                'OSS 结算图上传失败：HTTP {}'.format(result.status)
            )
        return len(content)

    def close(self) -> None:
        self._session.close()


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
    database: Union[Path, str]
    settings: Path
    state: Path
    endpoint: str
    bucket: str
    prefix: str
    watch_seconds: int
    debounce_seconds: int
    reconcile_seconds: int
    retry_seconds: int
    api_url: str
    result_frames: Path
    public_data_base_url: str


def _required_environment(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        raise DashboardPublishError('缺少环境变量 {}'.format(name))
    return value


def _publish(configuration: _WorkerConfiguration) -> None:
    network_settings = load_network_settings(configuration.settings)
    route_manager = NetworkRouteManager(lambda: network_settings)
    api_client: Optional[DashboardApiClient] = None
    store: Optional[OssDashboardStore] = None
    try:
        api_client = DashboardApiClient(
            base_url=configuration.api_url,
            token=_required_environment('DASHBOARD_API_TOKEN'),
            route_manager=route_manager,
        )
        cache_result = sync_dashboard_cache_once(
            database_path=configuration.database,
            state_directory=configuration.state,
            post_batch=api_client.post_cache_batch,
        )
        LOGGER.info(
            'cache_sync=%s batches=%s matches=%s removed=%s revision=%s '
            'purpose=dashboard_publish interface=%s source_address=%s',
            'synced' if cache_result.synced else 'current',
            cache_result.batch_count,
            cache_result.match_count,
            cache_result.removed_match_count,
            cache_result.source_revision,
            api_client.selection.interface_name or 'system-default',
            api_client.selection.source_address or 'system-default',
        )
        store = OssDashboardStore(
            endpoint=configuration.endpoint,
            bucket_name=configuration.bucket,
            prefix=configuration.prefix,
            access_key_id=_required_environment('ALIBABA_CLOUD_ACCESS_KEY_ID'),
            access_key_secret=_required_environment('ALIBABA_CLOUD_ACCESS_KEY_SECRET'),
            security_token=os.environ.get('ALIBABA_CLOUD_SECURITY_TOKEN') or None,
            route_manager=route_manager,
        )
        asset_result = sync_dashboard_api_once(
            database_path=configuration.database,
            state_directory=configuration.state,
            result_frame_directory=configuration.result_frames,
            public_data_base_url=configuration.public_data_base_url,
            image_store=store,
            post_batch=api_client.post_batch,
        )
        LOGGER.info(
            'asset_sync=%s batch=%s images=%s removed=%s image_bytes=%s '
            'purpose=dashboard_publish interface=%s source_address=%s',
            'synced' if asset_result.synced else 'current',
            asset_result.batch_id or '-',
            asset_result.image_count,
            asset_result.removed_match_count,
            asset_result.uploaded_image_bytes,
            api_client.selection.interface_name or 'system-default',
            api_client.selection.source_address or 'system-default',
        )
    finally:
        if api_client is not None:
            api_client.close()
        if store is not None:
            store.close()


def _read_source_revision(database: Union[Path, str]) -> int:
    connection = connect_source_database(database)
    try:
        row = connection.execute(
            'SELECT revision FROM dashboard_source_state WHERE singleton_id=1'
        ).fetchone()
        if row is None or type(row[0]) is not int or int(row[0]) <= 0:
            raise DashboardPublishError('排行榜数据源变更标记无效')
        return int(row[0])
    finally:
        connection.close()


def _worker_loop(configuration: _WorkerConfiguration) -> None:
    first_run = True
    last_revision: Optional[int] = None
    last_success_at = time.monotonic()
    while True:
        try:
            revision = _read_source_revision(configuration.database)
            revision_changed = not first_run and revision != last_revision
            reconciliation_due = (
                not first_run
                and time.monotonic() - last_success_at
                >= configuration.reconcile_seconds
            )
            if not first_run and not revision_changed and not reconciliation_due:
                time.sleep(configuration.watch_seconds)
                continue
            if revision_changed:
                LOGGER.info(
                    '检测到排行榜数据源变更 revision=%s，等待 %s 秒合并写入',
                    revision,
                    configuration.debounce_seconds,
                )
                time.sleep(configuration.debounce_seconds)
                revision = _read_source_revision(configuration.database)
            _publish(configuration)
        except Exception:
            LOGGER.exception('排行榜发布失败，%s 秒后重试', configuration.retry_seconds)
            time.sleep(configuration.retry_seconds)
            continue
        first_run = False
        last_revision = revision
        last_success_at = time.monotonic()
        LOGGER.info(
            '排行榜已校验 revision=%s；最长 %s 秒后再次完整校验',
            revision,
            configuration.reconcile_seconds,
        )


def _replay_visibility_worker(configuration: _WorkerConfiguration) -> None:
    route_manager = NetworkRouteManager(
        lambda: load_network_settings(configuration.settings)
    )
    api_client = DashboardApiClient(
        base_url=configuration.api_url,
        token=_required_environment('DASHBOARD_API_TOKEN'),
        route_manager=route_manager,
    )
    bili_session = RoutedRequestsSession(
        route_manager,
        purpose='bili_api',
        anonymous=True,
        affinity_key='dashboard-replay-visibility',
    )
    checker = BilibiliReplayVisibilityChecker(bili_session)
    last_bili_request_at = 0.0
    try:
        while True:
            try:
                bvid = api_client.claim_replay_visibility(wait_seconds=20)
                if bvid is None:
                    continue
                remaining_interval = MIN_REPLAY_VISIBILITY_INTERVAL_SECONDS - (
                    time.monotonic() - last_bili_request_at
                )
                if remaining_interval > 0:
                    time.sleep(remaining_interval)
                try:
                    public_visible = checker.public_visible(bvid)
                except ReplayVisibilityCheckError as error:
                    api_client.fail_replay_visibility(bvid, error=str(error))
                    LOGGER.warning(
                        'replay_visibility=retry bvid=%s purpose=bili_api error=%s',
                        bvid,
                        error,
                    )
                    continue
                finally:
                    last_bili_request_at = time.monotonic()
                api_client.complete_replay_visibility(
                    bvid, public_visible=public_visible
                )
                LOGGER.info(
                    'replay_visibility=%s bvid=%s purpose=bili_api',
                    'public' if public_visible else 'unavailable',
                    bvid,
                )
            except Exception:
                LOGGER.exception(
                    '排行榜回放可见性核验失败，%s 秒后重试', configuration.retry_seconds
                )
                time.sleep(configuration.retry_seconds)
    finally:
        bili_session.close()
        api_client.close()


def _environment_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise DashboardPublishError('{} 必须是整数'.format(name)) from exc


def _parse_args(arguments: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='持续同步虚荣排行榜缓存与结算图片')
    parser.add_argument('--once', action='store_true', help='只检查并同步一次')
    parser.add_argument(
        '--database',
        default=(
            os.environ.get('DASHBOARD_DATABASE_URL')
            or os.environ.get('DASHBOARD_DATABASE', '/cfg/blrec.sqlite3')
        ),
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
    parser.add_argument('--api-url', default=os.environ.get('DASHBOARD_API_URL') or '')
    parser.add_argument(
        '--result-frames',
        type=Path,
        default=Path(os.environ.get('DASHBOARD_RESULT_FRAMES', '/result-frames')),
    )
    parser.add_argument(
        '--public-data-base-url',
        default=os.environ.get(
            'DASHBOARD_PUBLIC_DATA_BASE_URL', 'https://vg.luwei.host/data'
        ),
    )
    parser.add_argument(
        '--watch-seconds',
        type=int,
        default=_environment_int('DASHBOARD_WATCH_SECONDS', DEFAULT_WATCH_SECONDS),
    )
    parser.add_argument(
        '--debounce-seconds',
        type=int,
        default=_environment_int(
            'DASHBOARD_DEBOUNCE_SECONDS', DEFAULT_DEBOUNCE_SECONDS
        ),
    )
    parser.add_argument(
        '--reconcile-seconds',
        type=int,
        default=_environment_int(
            'DASHBOARD_RECONCILE_SECONDS', DEFAULT_RECONCILE_SECONDS
        ),
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
    for value, label in (
        (arguments.watch_seconds, '监听间隔'),
        (arguments.debounce_seconds, '合并等待时间'),
        (arguments.reconcile_seconds, '校验间隔'),
        (arguments.retry_seconds, '重试间隔'),
    ):
        if value <= 0:
            raise DashboardPublishError('{}必须大于 0'.format(label))
    if not arguments.api_url:
        raise DashboardPublishError('必须配置排行榜写入 API')
    configuration = _WorkerConfiguration(
        database=(
            arguments.database
            if str(arguments.database).startswith(
                ('postgresql://', 'postgresql+psycopg://')
            )
            else Path(arguments.database)
        ),
        settings=arguments.settings,
        state=arguments.state,
        endpoint=arguments.endpoint,
        bucket=arguments.bucket,
        prefix=arguments.prefix,
        watch_seconds=arguments.watch_seconds,
        debounce_seconds=arguments.debounce_seconds,
        reconcile_seconds=arguments.reconcile_seconds,
        retry_seconds=arguments.retry_seconds,
        api_url=arguments.api_url,
        result_frames=arguments.result_frames,
        public_data_base_url=arguments.public_data_base_url,
    )
    with _exclusive_worker_lock(configuration.state):
        if arguments.once:
            _publish(configuration)
        else:
            Thread(
                target=_replay_visibility_worker,
                args=(configuration,),
                name='dashboard-replay-visibility',
                daemon=True,
            ).start()
            _worker_loop(configuration)


if __name__ == '__main__':
    main()
