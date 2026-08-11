from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Set,
    Tuple,
)

import requests

from blrec.networking.config import load_network_settings
from blrec.networking.manager import NetworkRouteManager, RouteSelection
from blrec.networking.requests_session import RoutedRequestsSession

from .api_sync import DashboardApiClient, sync_dashboard_api_once
from .snapshot import SHANGHAI, DashboardExportResult, export_dashboard_files

__all__ = (
    'DashboardPublicationResult',
    'DashboardPublishError',
    'OssDashboardStore',
    'build_dashboard_trends',
    'publish_dashboard_once',
)


LOGGER = logging.getLogger('dashboard-publisher')
SNAPSHOT_PATH = re.compile(r'snapshots/[a-zA-Z0-9-]+\.json\Z')
MATCH_IMAGE_PATH = re.compile(
    r'match-images/[0-9]{3,}/[1-9][0-9]*-[0-9a-f]{16}\.webp\Z'
)
DEFAULT_RETRY_SECONDS = 60
DEFAULT_WATCH_SECONDS = 1
DEFAULT_DEBOUNCE_SECONDS = 2
DEFAULT_RECONCILE_SECONDS = 24 * 60 * 60
TREND_MODES = ('all', '3v3', 'brawl', '5v5')
MAX_TREND_PUBLICATIONS = 180


class DashboardPublishError(RuntimeError):
    pass


class DashboardStore(Protocol):
    def load_manifest(self) -> Optional[bytes]:
        pass

    def load_trends(self) -> Optional[bytes]:
        pass

    def put_snapshot(self, path: str, content: bytes, sha256: str) -> int:
        pass

    def put_trends(self, content: bytes) -> int:
        pass

    def put_manifest(self, content: bytes) -> int:
        pass

    def put_match_image(self, path: str, content: bytes, sha256: str) -> int:
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
    content_revision = value.get('contentRevision')
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
        or (
            content_revision is not None
            and (
                not isinstance(content_revision, str)
                or re.fullmatch(r'[0-9a-f]{64}', content_revision) is None
            )
        )
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


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        + '\n'
    ).encode('utf-8')


def _trend_document(content: bytes) -> Mapping[str, Any]:
    value = _json_mapping(content, '远端趋势数据')
    publications = value.get('publications')
    if (
        value.get('schemaVersion') != 1
        or not isinstance(value.get('updatedAt'), str)
        or not isinstance(publications, list)
        or len(publications) > MAX_TREND_PUBLICATIONS
    ):
        raise DashboardPublishError('远端趋势数据版本或字段无效')

    previous_date: Optional[date] = None
    for publication in publications:
        if not isinstance(publication, Mapping):
            raise DashboardPublishError('远端趋势数据发布记录无效')
        snapshot_id = publication.get('snapshotId')
        publication_date = publication.get('publicationDate')
        source_last_match_id = publication.get('sourceLastMatchId')
        standings = publication.get('standings')
        if (
            not isinstance(snapshot_id, str)
            or re.fullmatch(r'[a-zA-Z0-9-]+', snapshot_id) is None
            or not isinstance(publication_date, str)
            or type(source_last_match_id) is not int
            or source_last_match_id < 0
            or not isinstance(standings, Mapping)
        ):
            raise DashboardPublishError('远端趋势数据发布记录字段无效')
        try:
            parsed_date = date.fromisoformat(publication_date)
        except ValueError as exc:
            raise DashboardPublishError('远端趋势数据发布日期无效') from exc
        if previous_date is not None and parsed_date <= previous_date:
            raise DashboardPublishError('远端趋势数据发布日期没有递增')
        previous_date = parsed_date

        for season_key, modes in standings.items():
            if not isinstance(season_key, str) or not isinstance(modes, Mapping):
                raise DashboardPublishError('远端趋势数据赛季字段无效')
            for mode in TREND_MODES:
                rows = modes.get(mode)
                if not isinstance(rows, list):
                    raise DashboardPublishError('远端趋势数据模式字段无效')
                seen_players: Set[int] = set()
                for index, row in enumerate(rows):
                    if not isinstance(row, Mapping):
                        raise DashboardPublishError('远端趋势数据玩家记录无效')
                    player_id = row.get('playerId')
                    rank = row.get('rank')
                    rating_score = row.get('ratingScore')
                    if (
                        type(player_id) is not int
                        or player_id <= 0
                        or player_id in seen_players
                        or type(rank) is not int
                        or rank != index + 1
                        or type(rating_score) is not int
                        or not 0 <= rating_score <= 1000
                    ):
                        raise DashboardPublishError('远端趋势数据玩家字段无效')
                    seen_players.add(player_id)
    return value


def _ranked_trend_rows(
    players: List[Any], season_key: str, mode: str
) -> List[Mapping[str, int]]:
    candidates: List[Tuple[int, int, int, float]] = []
    seen_players: Set[int] = set()
    for player in players:
        if not isinstance(player, Mapping):
            raise DashboardPublishError('快照 {} 玩家字段无效'.format(season_key))
        player_id = player.get('id')
        modes = player.get('modes')
        if (
            type(player_id) is not int
            or player_id <= 0
            or player_id in seen_players
            or not isinstance(modes, Mapping)
        ):
            raise DashboardPublishError('快照 {} 玩家字段无效'.format(season_key))
        seen_players.add(player_id)
        performance = modes.get(mode)
        if not isinstance(performance, Mapping):
            raise DashboardPublishError(
                '快照 {} {} 模式字段无效'.format(season_key, mode)
            )
        rating_score = performance.get('ratingScore')
        matches = performance.get('matches')
        wins = performance.get('wins')
        if (
            type(matches) is not int
            or matches < 0
            or type(wins) is not int
            or not 0 <= wins <= matches
            or (
                rating_score is not None
                and (
                    type(rating_score) is not int
                    or not 0 <= rating_score <= 1000
                    or matches == 0
                )
            )
        ):
            raise DashboardPublishError(
                '快照 {} {} 模式数据无效'.format(season_key, mode)
            )
        if rating_score is None:
            continue
        candidates.append(
            (player_id, rating_score, matches, wins / matches if matches else 0.0)
        )

    candidates.sort(key=lambda row: (-row[1], -row[2], -row[3], row[0]))
    return [
        {'playerId': row[0], 'rank': index + 1, 'ratingScore': row[1]}
        for index, row in enumerate(candidates)
    ]


def _trend_publication(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    snapshot_id = snapshot.get('snapshotId')
    publication_date = snapshot.get('publicationDate')
    source_last_match_id = snapshot.get('sourceLastMatchId')
    standings = snapshot.get('standings')
    if (
        not isinstance(snapshot_id, str)
        or re.fullmatch(r'[a-zA-Z0-9-]+', snapshot_id) is None
        or not isinstance(publication_date, str)
        or type(source_last_match_id) is not int
        or source_last_match_id < 0
        or not isinstance(standings, Mapping)
    ):
        raise DashboardPublishError('待发布快照无法生成趋势数据')
    try:
        date.fromisoformat(publication_date)
    except ValueError as exc:
        raise DashboardPublishError('待发布快照发布日期无效') from exc

    public_standings: Dict[str, Mapping[str, List[Mapping[str, int]]]] = {}
    for season_key, season_standings in standings.items():
        if not isinstance(season_key, str) or not isinstance(season_standings, Mapping):
            raise DashboardPublishError('待发布快照赛季字段无效')
        players = season_standings.get('players')
        if not isinstance(players, list):
            raise DashboardPublishError('待发布快照 {} 玩家列表无效'.format(season_key))
        public_standings[season_key] = {
            mode: _ranked_trend_rows(players, season_key, mode) for mode in TREND_MODES
        }
    return {
        'snapshotId': snapshot_id,
        'publicationDate': publication_date,
        'sourceLastMatchId': source_last_match_id,
        'standings': public_standings,
    }


def build_dashboard_trends(
    snapshot: Mapping[str, Any], existing_content: Optional[bytes]
) -> bytes:
    generated_at = snapshot.get('generatedAt')
    if not isinstance(generated_at, str) or not generated_at:
        raise DashboardPublishError('待发布快照缺少生成时间')
    current = _trend_publication(snapshot)
    current_date = date.fromisoformat(str(current['publicationDate']))
    publications: List[Mapping[str, Any]] = []
    if existing_content is not None:
        existing = _trend_document(existing_content)
        existing_publications = existing['publications']
        assert isinstance(existing_publications, list)
        for publication in existing_publications:
            assert isinstance(publication, Mapping)
            publication_date = date.fromisoformat(str(publication['publicationDate']))
            if publication_date > current_date:
                raise DashboardPublishError('远端趋势数据包含未来记录')
            if publication_date != current_date:
                publications.append(publication)
    publications.append(current)
    publications.sort(key=lambda value: str(value['publicationDate']))
    publications = publications[-MAX_TREND_PUBLICATIONS:]
    return _json_bytes(
        {'schemaVersion': 1, 'updatedAt': generated_at, 'publications': publications}
    )


def _pending_export(
    database_path: Path,
    state_directory: Path,
    now: datetime,
    exporter: Callable[..., DashboardExportResult],
    *,
    reuse_existing: bool = True,
) -> DashboardExportResult:
    pending = state_directory.expanduser().resolve() / 'pending'
    manifest_path = pending / 'manifest.json'
    if reuse_existing and manifest_path.is_file():
        content = manifest_path.read_bytes()
        manifest = _manifest(content, '本地待发布 manifest')
        if (
            manifest['publicationDate'] == now.astimezone(SHANGHAI).date().isoformat()
            and manifest.get('contentRevision') is not None
        ):
            snapshot_path = pending / str(manifest['snapshotPath'])
            _validate_snapshot(snapshot_path, manifest)
            return DashboardExportResult(
                manifest_path=manifest_path,
                snapshot_path=snapshot_path,
                manifest=manifest,
                sha256=str(manifest['sha256']),
            )
    return exporter(database_path, pending, now=now)


def _discard_pending(exported: DashboardExportResult) -> None:
    shutil.rmtree(exported.manifest_path.parent)


def publish_dashboard_once(
    database_path: Path,
    state_directory: Path,
    store: DashboardStore,
    *,
    now: Optional[datetime] = None,
    exporter: Callable[..., DashboardExportResult] = export_dashboard_files,
    force: bool = False,
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

    exported = _pending_export(
        database_path, state_directory, generated_at, exporter, reuse_existing=not force
    )
    local_manifest_content = exported.manifest_path.read_bytes()
    local_manifest = _manifest(local_manifest_content, '本地待发布 manifest')
    snapshot = _validate_snapshot(exported.snapshot_path, local_manifest)
    local_content_revision = local_manifest.get('contentRevision')
    if not isinstance(local_content_revision, str):
        raise DashboardPublishError('本地待发布 manifest 缺少内容版本')
    if local_manifest['publicationDate'] != today.isoformat():
        raise DashboardPublishError('本地待发布快照不是今天生成的')
    if remote_manifest is not None and int(local_manifest['sourceLastMatchId']) < int(
        remote_manifest['sourceLastMatchId']
    ):
        raise DashboardPublishError('数据源进度发生回退，已停止覆盖远端榜单')
    if (
        remote_manifest is not None
        and not force
        and remote_manifest.get('contentRevision') == local_content_revision
    ):
        _discard_pending(exported)
        source_match_count = snapshot.get('sourceMatchCount')
        return DashboardPublicationResult(
            published=False,
            publication_date=today,
            snapshot_id=str(remote_manifest['snapshotId']),
            source_last_match_id=int(remote_manifest['sourceLastMatchId']),
            source_match_count=(
                source_match_count
                if type(source_match_count) is int and source_match_count >= 0
                else None
            ),
            uploaded_bytes=0,
        )

    trends_content = build_dashboard_trends(snapshot, store.load_trends())
    uploaded_bytes = store.put_snapshot(
        str(local_manifest['snapshotPath']),
        exported.snapshot_path.read_bytes(),
        str(local_manifest['sha256']),
    )
    uploaded_bytes += store.put_trends(trends_content)
    uploaded_bytes += store.put_manifest(local_manifest_content)
    committed = store.load_manifest()
    if committed != local_manifest_content:
        raise DashboardPublishError('远端 manifest 提交后校验失败')
    _discard_pending(exported)

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

    def load_trends(self) -> Optional[bytes]:
        return self._optional_object(self._key('trends.json'))

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

    def put_match_image(self, path: str, content: bytes, sha256: str) -> int:
        if MATCH_IMAGE_PATH.fullmatch(path) is None:
            raise DashboardPublishError('结算图对象路径无效：{}'.format(path))
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

    def put_trends(self, content: bytes) -> int:
        result = self._bucket.put_object(
            self._key('trends.json'),
            content,
            headers={
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Content-Type': 'application/json; charset=utf-8',
            },
            progress_callback=self._progress(),
        )
        if not 200 <= int(result.status) < 300:
            raise DashboardPublishError(
                'OSS 趋势数据上传失败：HTTP {}'.format(result.status)
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
    database: Path
    settings: Path
    state: Path
    endpoint: str
    bucket: str
    prefix: str
    watch_seconds: int
    debounce_seconds: int
    reconcile_seconds: int
    retry_seconds: int
    publish_static_data: bool = True
    api_url: Optional[str] = None
    result_frames: Path = Path('/result-frames')
    public_data_base_url: str = 'https://vg.luwei.host/data'


def _required_environment(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        raise DashboardPublishError('缺少环境变量 {}'.format(name))
    return value


def _publish(
    configuration: _WorkerConfiguration, now: datetime, *, force: bool = False
) -> Optional[DashboardPublicationResult]:
    network_settings = load_network_settings(configuration.settings)
    route_manager = NetworkRouteManager(lambda: network_settings)
    store = OssDashboardStore(
        endpoint=configuration.endpoint,
        bucket_name=configuration.bucket,
        prefix=configuration.prefix,
        access_key_id=_required_environment('ALIBABA_CLOUD_ACCESS_KEY_ID'),
        access_key_secret=_required_environment('ALIBABA_CLOUD_ACCESS_KEY_SECRET'),
        security_token=os.environ.get('ALIBABA_CLOUD_SECURITY_TOKEN') or None,
        route_manager=route_manager,
    )
    api_client: Optional[DashboardApiClient] = None
    result: Optional[DashboardPublicationResult] = None
    api_result = None
    try:
        if configuration.publish_static_data:
            result = publish_dashboard_once(
                configuration.database, configuration.state, store, now=now, force=force
            )
        if configuration.api_url:
            api_client = DashboardApiClient(
                base_url=configuration.api_url,
                token=_required_environment('DASHBOARD_API_TOKEN'),
                route_manager=route_manager,
            )
            api_result = sync_dashboard_api_once(
                database_path=configuration.database,
                state_directory=configuration.state,
                result_frame_directory=configuration.result_frames,
                public_data_base_url=configuration.public_data_base_url,
                image_store=store,
                post_batch=api_client.post_batch,
            )
    finally:
        if api_client is not None:
            api_client.close()
        store.close()
    if result is None:
        LOGGER.info(
            'static_json=disabled purpose=dashboard_publish interface=%s '
            'source_address=%s role=%s',
            store.selection.interface_name or 'system-default',
            store.selection.source_address or 'system-default',
            store.selection.role,
        )
    else:
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
    if api_result is not None:
        LOGGER.info(
            'api_sync=%s batch=%s matches=%s removed=%s image_bytes=%s '
            'purpose=dashboard_publish interface=%s source_address=%s',
            'synced' if api_result.synced else 'current',
            api_result.batch_id or '-',
            api_result.match_count,
            api_result.removed_match_count,
            api_result.uploaded_image_bytes,
            (
                api_client.selection.interface_name
                if api_client is not None and api_client.selection.interface_name
                else 'system-default'
            ),
            (
                api_client.selection.source_address
                if api_client is not None and api_client.selection.source_address
                else 'system-default'
            ),
        )
    return result


def _read_source_revision(database: Path) -> Optional[int]:
    connection = sqlite3.connect(
        'file:{}?mode=ro'.format(database.expanduser().resolve()), uri=True
    )
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='dashboard_source_state'"
        ).fetchone()
        if table is None:
            return None
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
            _publish(configuration, datetime.now(tz=SHANGHAI))
        except Exception:
            LOGGER.exception('排行榜发布失败，%s 秒后重试', configuration.retry_seconds)
            time.sleep(configuration.retry_seconds)
            continue
        first_run = False
        last_revision = revision
        last_success_at = time.monotonic()
        LOGGER.info(
            '排行榜数据源已同步 revision=%s；持续监听，最长 %s 秒后校验一次',
            revision if revision is not None else 'legacy',
            configuration.reconcile_seconds,
        )


def _environment_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise DashboardPublishError('{} 必须是整数'.format(name)) from exc


def _environment_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    raise DashboardPublishError('{} 必须是布尔值'.format(name))


def _parse_args(arguments: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='持续同步虚荣排行榜数据')
    parser.add_argument('--once', action='store_true', help='只检查并发布一次')
    parser.add_argument(
        '--force',
        action='store_true',
        help='与 --once 同用，强制重新生成并发布今天的快照',
    )
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
        '--api-url', default=os.environ.get('DASHBOARD_API_URL') or None
    )
    parser.set_defaults(
        publish_static_data=_environment_bool('DASHBOARD_STATIC_JSON_ENABLED', True)
    )
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
    if arguments.watch_seconds <= 0:
        raise DashboardPublishError('监听间隔必须大于 0')
    if arguments.debounce_seconds <= 0:
        raise DashboardPublishError('合并等待时间必须大于 0')
    if arguments.reconcile_seconds <= 0:
        raise DashboardPublishError('校验间隔必须大于 0')
    if arguments.retry_seconds <= 0:
        raise DashboardPublishError('重试间隔必须大于 0')
    if arguments.force and not arguments.once:
        raise DashboardPublishError('--force 必须与 --once 同时使用')
    if arguments.force and not arguments.publish_static_data:
        raise DashboardPublishError('--force 只适用于静态 JSON 发布')
    if not arguments.publish_static_data and not arguments.api_url:
        raise DashboardPublishError('关闭静态 JSON 后必须配置排行榜 API')
    configuration = _WorkerConfiguration(
        database=arguments.database,
        settings=arguments.settings,
        state=arguments.state,
        endpoint=arguments.endpoint,
        bucket=arguments.bucket,
        prefix=arguments.prefix,
        watch_seconds=arguments.watch_seconds,
        debounce_seconds=arguments.debounce_seconds,
        reconcile_seconds=arguments.reconcile_seconds,
        retry_seconds=arguments.retry_seconds,
        publish_static_data=arguments.publish_static_data,
        api_url=arguments.api_url,
        result_frames=arguments.result_frames,
        public_data_base_url=arguments.public_data_base_url,
    )
    with _exclusive_worker_lock(configuration.state):
        if arguments.once:
            _publish(configuration, datetime.now(tz=SHANGHAI), force=arguments.force)
        else:
            _worker_loop(configuration)


if __name__ == '__main__':
    main()
