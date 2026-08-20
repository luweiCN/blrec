from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Union

from .api_sync import _atomic_json, _canonical_bytes
from .snapshot import build_dashboard_cache_source
from .source_database import connect_source_database, is_postgres


class DashboardCacheSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class DashboardCacheSyncResult:
    synced: bool
    batch_count: int
    match_count: int
    removed_match_count: int
    source_revision: int


def _empty_state() -> Mapping[str, Any]:
    return {
        'schemaVersion': 1,
        'sourceRevision': 0,
        'playersRevision': '',
        'matches': {},
    }


def _load_state(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        return _empty_state()
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DashboardCacheSyncError('排行榜缓存同步状态损坏') from exc
    if (
        not isinstance(value, Mapping)
        or value.get('schemaVersion') != 1
        or type(value.get('sourceRevision')) is not int
        or not isinstance(value.get('playersRevision'), str)
        or not isinstance(value.get('matches'), Mapping)
    ):
        raise DashboardCacheSyncError('排行榜缓存同步状态版本无效')
    return value


def _consistent_source(
    database_path: Union[Path, str], source_builder: Callable[[Any], Mapping[str, Any]]
) -> tuple[int, Mapping[str, Any]]:
    connection = connect_source_database(database_path)
    try:
        connection.execute('BEGIN')
        if is_postgres(database_path):
            connection.execute(
                'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'
            )
        revision_row = connection.execute(
            'SELECT revision FROM dashboard_source_state WHERE singleton_id=1'
        ).fetchone()
        if revision_row is None or int(revision_row[0]) <= 0:
            raise DashboardCacheSyncError('排行榜缓存数据源 revision 无效')
        revision = int(revision_row[0])
        source = source_builder(connection)
        connection.execute('COMMIT')
        return revision, source
    except Exception:
        if getattr(connection, 'in_transaction', False):
            connection.execute('ROLLBACK')
        raise
    finally:
        connection.close()


def _send_outboxes(
    paths: list[Path],
    *,
    state_path: Path,
    post_batch: Callable[[str, bytes], Mapping[str, Any]],
) -> DashboardCacheSyncResult:
    batch_count = 0
    match_count = 0
    removed_match_count = 0
    source_revision = 0
    for path in paths:
        try:
            envelope = json.loads(path.read_text(encoding='utf-8'))
            batch = envelope['batch']
            batch_id = str(envelope['batchId'])
            next_state = envelope.get('nextState')
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError) as exc:
            raise DashboardCacheSyncError('排行榜缓存 outbox 损坏') from exc
        if not isinstance(batch, Mapping):
            raise DashboardCacheSyncError('排行榜缓存 outbox 批次无效')
        post_batch(batch_id, _canonical_bytes(batch))
        if next_state is not None:
            if not isinstance(next_state, Mapping):
                raise DashboardCacheSyncError('排行榜缓存 outbox 状态无效')
            _atomic_json(state_path, next_state)
        path.unlink()
        batch_count += 1
        matches = batch.get('matches')
        removed = batch.get('removedMatchIds')
        match_count += len(matches) if isinstance(matches, list) else 0
        removed_match_count += len(removed) if isinstance(removed, list) else 0
        source_revision = int(batch.get('sourceRevision', 0))
    return DashboardCacheSyncResult(
        synced=bool(paths),
        batch_count=batch_count,
        match_count=match_count,
        removed_match_count=removed_match_count,
        source_revision=source_revision,
    )


def sync_dashboard_cache_once(
    *,
    database_path: Union[Path, str],
    state_directory: Path,
    post_batch: Callable[[str, bytes], Mapping[str, Any]],
    source_builder: Callable[[Any], Mapping[str, Any]] = build_dashboard_cache_source,
    max_batch_matches: int = 500,
) -> DashboardCacheSyncResult:
    if max_batch_matches <= 0:
        raise ValueError('dashboard cache batch size must be positive')
    state_directory = state_directory.expanduser().resolve()
    state_path = state_directory / 'cache-sync-state.json'
    outbox_directory = state_directory / 'cache-api-outbox'
    pending = sorted(outbox_directory.glob('*.json'))
    if pending:
        return _send_outboxes(pending, state_path=state_path, post_batch=post_batch)

    source_revision, source = _consistent_source(database_path, source_builder)
    players = source.get('players')
    source_matches = source.get('matches')
    if not isinstance(players, list) or not isinstance(source_matches, list):
        raise DashboardCacheSyncError('排行榜缓存数据源字段无效')
    previous_state = _load_state(state_path)
    previous_matches = previous_state['matches']
    assert isinstance(previous_matches, Mapping)
    players_revision = hashlib.sha256(
        _canonical_bytes({'players': players})
    ).hexdigest()
    changed_players = players_revision != previous_state['playersRevision']
    next_matches: Dict[str, Mapping[str, str]] = {}
    changed_matches = []
    for source_match in source_matches:
        if (
            not isinstance(source_match, Mapping)
            or type(source_match.get('id')) is not int
        ):
            raise DashboardCacheSyncError('排行榜缓存对局字段无效')
        match_id = int(source_match['id'])
        revision = hashlib.sha256(_canonical_bytes({'match': source_match})).hexdigest()
        next_matches[str(match_id)] = {'revision': revision}
        previous = previous_matches.get(str(match_id))
        if not isinstance(previous, Mapping) or previous.get('revision') != revision:
            changed_matches.append(source_match)

    removed_match_ids = sorted(
        int(match_id) for match_id in set(previous_matches).difference(next_matches)
    )
    previous_revision = int(previous_state['sourceRevision'])
    if (
        not changed_players
        and not changed_matches
        and not removed_match_ids
        and previous_revision == source_revision
    ):
        return DashboardCacheSyncResult(
            synced=False,
            batch_count=0,
            match_count=0,
            removed_match_count=0,
            source_revision=source_revision,
        )
    bootstrap = previous_revision == 0
    if not bootstrap and len(changed_matches) > max_batch_matches:
        raise DashboardCacheSyncError(
            '排行榜缓存增量超过单批上限，需要在 direct 模式重新引导'
        )
    chunks = [
        changed_matches[index : index + max_batch_matches]
        for index in range(0, len(changed_matches), max_batch_matches)
    ] or [[]]
    next_state = {
        'schemaVersion': 1,
        'sourceRevision': source_revision,
        'playersRevision': players_revision,
        'matches': next_matches,
    }
    envelopes = []
    for index, matches in enumerate(chunks):
        final = index == len(chunks) - 1
        batch = {
            'schemaVersion': 2,
            'sourceRevision': source_revision,
            'publish': final,
            'reset': bootstrap and index == 0,
            'generatedAt': source.get('generatedAt'),
            'sourceLastMatchId': source.get('sourceLastMatchId'),
            'players': players,
            'matches': matches,
            'removedMatchIds': removed_match_ids if final else [],
        }
        content = _canonical_bytes(batch)
        batch_id = 'dashboard-cache-{}'.format(hashlib.sha256(content).hexdigest()[:40])
        envelopes.append(
            {
                'schemaVersion': 1,
                'batchId': batch_id,
                'batch': batch,
                'nextState': next_state if final else None,
            }
        )
    outbox_directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, envelope in enumerate(envelopes):
        path = outbox_directory / '{:06d}-{}.json'.format(index, envelope['batchId'])
        _atomic_json(path, envelope)
        paths.append(path)
    return _send_outboxes(paths, state_path=state_path, post_batch=post_batch)
