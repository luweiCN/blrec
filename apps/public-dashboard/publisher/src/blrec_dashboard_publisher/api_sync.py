from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Tuple, Union

import requests
from PIL import Image

from blrec.networking.manager import NetworkRouteManager, RouteSelection
from blrec.networking.requests_session import RoutedRequestsSession

from .snapshot import build_dashboard_api_source
from .source_database import connect_source_database


class DashboardApiSyncError(RuntimeError):
    pass


class MatchImageStore(Protocol):
    def put_match_image(self, path: str, content: bytes, sha256: str) -> int:
        pass


@dataclass(frozen=True)
class DashboardApiSyncResult:
    synced: bool
    batch_id: Optional[str]
    match_count: int
    removed_match_count: int
    uploaded_image_bytes: int


class DashboardApiClient:
    def __init__(
        self, *, base_url: str, token: str, route_manager: NetworkRouteManager
    ) -> None:
        normalized_url = base_url.rstrip('/')
        if not normalized_url.startswith('https://'):
            raise DashboardApiSyncError('排行榜 API 必须使用 HTTPS')
        if not token:
            raise DashboardApiSyncError('排行榜 API 写入密钥不能为空')
        self._url = normalized_url + '/v1/ingest/batches'
        self._token = token
        self._route_manager = route_manager
        self._affinity_key = 'dashboard-api-ingest'
        self.selection: RouteSelection = route_manager.select(
            'dashboard_publish', anonymous=False, affinity_key=self._affinity_key
        )
        self._session = RoutedRequestsSession(
            route_manager,
            purpose='dashboard_publish',
            anonymous=False,
            affinity_key=self._affinity_key,
        )

    def post_batch(self, idempotency_key: str, content: bytes) -> Mapping[str, Any]:
        response = self._session.post(
            self._url,
            data=content,
            headers={
                'Authorization': 'Bearer {}'.format(self._token),
                'Content-Type': 'application/json',
                'X-Idempotency-Key': idempotency_key,
            },
            timeout=(10, 120),
        )
        self._route_manager.traffic_meter.record(
            self.selection.interface_name, 'dashboard_publish', 'up', len(content)
        )
        try:
            response.raise_for_status()
            value = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise DashboardApiSyncError(
                '排行榜 API 写入失败：HTTP {}'.format(response.status_code)
            ) from exc
        if not isinstance(value, Mapping) or value.get('status') not in (
            'applied',
            'duplicate',
        ):
            raise DashboardApiSyncError('排行榜 API 返回了无效结果')
        return value

    def close(self) -> None:
        self._session.close()


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        + '\n'
    ).encode('utf-8')


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix='.'.join((path.name, 'tmp-')), dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            target.write(_canonical_bytes(value))
            target.flush()
            os.fsync(target.fileno())
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _load_state(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        return {'schemaVersion': 1, 'playersRevision': '', 'matches': {}}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DashboardApiSyncError('排行榜 API 同步状态损坏') from exc
    if (
        not isinstance(value, Mapping)
        or value.get('schemaVersion') != 1
        or not isinstance(value.get('playersRevision'), str)
        or not isinstance(value.get('matches'), Mapping)
    ):
        raise DashboardApiSyncError('排行榜 API 同步状态版本无效')
    return value


def _resolve_frame(
    root: Path, relative_path: Optional[str]
) -> Tuple[Optional[Path], str]:
    if relative_path is None:
        return None, 'none'
    if not relative_path or Path(relative_path).is_absolute():
        raise DashboardApiSyncError('结算图路径无效')
    root = root.expanduser().resolve()
    candidate = (root / relative_path).resolve()
    try:
        contained = os.path.commonpath((str(root), str(candidate))) == str(root)
    except ValueError:
        contained = False
    if not contained:
        raise DashboardApiSyncError('结算图路径超出挂载目录')
    if not candidate.is_file():
        return None, 'missing:{}'.format(relative_path)
    stat = candidate.stat()
    return candidate, '{}:{}:{}'.format(relative_path, stat.st_size, stat.st_mtime_ns)


def _compress_frame(path: Path) -> Tuple[bytes, int, int]:
    try:
        with Image.open(path) as source:
            source.load()
            image = source.convert('RGB')
            resampling = getattr(Image, 'Resampling', Image).LANCZOS
            image.thumbnail((1600, 1600), resampling)
            output = io.BytesIO()
            image.save(output, format='WEBP', quality=82, method=6)
            return output.getvalue(), image.width, image.height
    except (OSError, ValueError) as exc:
        raise DashboardApiSyncError('结算图无法压缩：{}'.format(path.name)) from exc


def _image_value(
    *, match_id: int, path: Path, public_data_base_url: str, store: MatchImageStore
) -> Tuple[Mapping[str, Any], int]:
    content, width, height = _compress_frame(path)
    digest = hashlib.sha256(content).hexdigest()
    relative_path = 'match-images/{:03d}/{}-{}.webp'.format(
        match_id // 1000, match_id, digest[:16]
    )
    uploaded_bytes = store.put_match_image(relative_path, content, digest)
    return (
        {
            'url': '{}/{}'.format(public_data_base_url.rstrip('/'), relative_path),
            'width': width,
            'height': height,
            'sha256': digest,
            'sourceSignature': '',
        },
        uploaded_bytes,
    )


def _send_outbox(
    path: Path,
    *,
    state_path: Path,
    post_batch: Callable[[str, bytes], Mapping[str, Any]],
) -> DashboardApiSyncResult:
    try:
        envelope = json.loads(path.read_text(encoding='utf-8'))
        batch = envelope['batch']
        next_state = envelope['nextState']
        batch_id = str(envelope['batchId'])
        uploaded_image_bytes = int(envelope.get('uploadedImageBytes', 0))
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DashboardApiSyncError('排行榜 API outbox 损坏') from exc
    if not isinstance(batch, Mapping) or not isinstance(next_state, Mapping):
        raise DashboardApiSyncError('排行榜 API outbox 字段无效')
    post_batch(batch_id, _canonical_bytes(batch))
    _atomic_json(state_path, next_state)
    path.unlink()
    matches = batch.get('matches')
    removed = batch.get('removedMatchIds')
    return DashboardApiSyncResult(
        synced=True,
        batch_id=batch_id,
        match_count=len(matches) if isinstance(matches, list) else 0,
        removed_match_count=len(removed) if isinstance(removed, list) else 0,
        uploaded_image_bytes=uploaded_image_bytes,
    )


def sync_dashboard_api_once(
    *,
    database_path: Union[Path, str],
    state_directory: Path,
    result_frame_directory: Path,
    public_data_base_url: str,
    image_store: MatchImageStore,
    post_batch: Callable[[str, bytes], Mapping[str, Any]],
    source_builder: Callable[[sqlite3.Connection], Mapping[str, Any]] = (
        build_dashboard_api_source
    ),
) -> DashboardApiSyncResult:
    state_directory = state_directory.expanduser().resolve()
    state_path = state_directory / 'api-sync-state.json'
    outbox_directory = state_directory / 'api-outbox'
    pending = sorted(outbox_directory.glob('*.json'))
    if pending:
        return _send_outbox(pending[0], state_path=state_path, post_batch=post_batch)

    connection = connect_source_database(database_path)
    try:
        source = source_builder(connection)
    finally:
        connection.close()
    players = source.get('players')
    source_matches = source.get('matches')
    if not isinstance(players, list) or not isinstance(source_matches, list):
        raise DashboardApiSyncError('排行榜 API 数据源字段无效')

    previous_state = _load_state(state_path)
    previous_matches = previous_state['matches']
    assert isinstance(previous_matches, Mapping)
    players_revision = hashlib.sha256(
        _canonical_bytes({'players': players})
    ).hexdigest()
    changed_players = players_revision != previous_state['playersRevision']
    next_matches: Dict[str, Mapping[str, Any]] = {}
    changed_matches = []
    uploaded_image_bytes = 0
    for source_match in source_matches:
        if (
            not isinstance(source_match, Mapping)
            or type(source_match.get('id')) is not int
        ):
            raise DashboardApiSyncError('排行榜 API 对局字段无效')
        match_id = int(source_match['id'])
        public_match = json.loads(json.dumps(source_match, ensure_ascii=False))
        relative_frame = public_match.pop('resultFramePath', None)
        if relative_frame is not None and not isinstance(relative_frame, str):
            raise DashboardApiSyncError('结算图路径字段无效')
        frame_path, frame_signature = _resolve_frame(
            result_frame_directory, relative_frame
        )
        revision = hashlib.sha256(
            _canonical_bytes({'match': public_match, 'frameSignature': frame_signature})
        ).hexdigest()
        previous = previous_matches.get(str(match_id))
        if isinstance(previous, Mapping) and previous.get('revision') == revision:
            next_matches[str(match_id)] = previous
            continue

        image: Optional[Mapping[str, Any]] = None
        if (
            isinstance(previous, Mapping)
            and previous.get('frameSignature') == frame_signature
            and isinstance(previous.get('image'), Mapping)
        ):
            image = previous['image']
        elif frame_path is not None:
            image, uploaded = _image_value(
                match_id=match_id,
                path=frame_path,
                public_data_base_url=public_data_base_url,
                store=image_store,
            )
            uploaded_image_bytes += uploaded
        if image is not None:
            public_match['resultImage'] = {
                key: image[key] for key in ('url', 'width', 'height')
            }
        next_matches[str(match_id)] = {
            'revision': revision,
            'frameSignature': frame_signature,
            'image': image,
        }
        changed_matches.append(public_match)

    current_ids = set(next_matches)
    removed_match_ids = sorted(
        int(match_id) for match_id in set(previous_matches).difference(current_ids)
    )
    if not changed_players and not changed_matches and not removed_match_ids:
        return DashboardApiSyncResult(
            synced=False,
            batch_id=None,
            match_count=0,
            removed_match_count=0,
            uploaded_image_bytes=0,
        )
    batch = {
        'schemaVersion': 1,
        'generatedAt': source.get('generatedAt'),
        'sourceLastMatchId': source.get('sourceLastMatchId'),
        'players': players,
        'matches': changed_matches,
        'removedMatchIds': removed_match_ids,
    }
    batch_content = _canonical_bytes(batch)
    batch_id = 'dashboard-{}'.format(hashlib.sha256(batch_content).hexdigest()[:40])
    next_state = {
        'schemaVersion': 1,
        'playersRevision': players_revision,
        'matches': next_matches,
    }
    envelope = {
        'schemaVersion': 1,
        'batchId': batch_id,
        'batch': batch,
        'nextState': next_state,
        'uploadedImageBytes': uploaded_image_bytes,
    }
    outbox_path = outbox_directory / '{}.json'.format(batch_id)
    _atomic_json(outbox_path, envelope)
    return _send_outbox(outbox_path, state_path=state_path, post_batch=post_batch)
