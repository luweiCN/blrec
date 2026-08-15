from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .database import DatabaseTarget, connect_database

_TREND_MODES = ('all', '3v3', 'brawl', '5v5')
_MAX_TREND_PUBLICATIONS = 180


def _ranked_trend_rows(
    players: Sequence[Mapping[str, Any]], mode: str
) -> List[Mapping[str, Any]]:
    candidates: List[Tuple[int, float, int, float]] = []
    for player in players:
        player_id = int(player['id'])
        performance = player['modes'][mode]
        rating_score = performance['ratingScore']
        matches = int(performance['matches'])
        wins = int(performance['wins'])
        if rating_score is None:
            continue
        candidates.append(
            (
                player_id,
                float(rating_score),
                matches,
                wins / matches if matches else 0.0,
            )
        )
    candidates.sort(key=lambda row: (-row[1], -row[2], -row[3], row[0]))
    return [
        {'playerId': row[0], 'rank': index + 1, 'ratingScore': row[1]}
        for index, row in enumerate(candidates)
    ]


def _trend_standings(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    values: Dict[str, Mapping[str, Any]] = {}
    for season_key, season_standings in snapshot['standings'].items():
        players = season_standings['players']
        values[str(season_key)] = {
            mode: _ranked_trend_rows(players, mode) for mode in _TREND_MODES
        }
    return values


def current_dashboard_publication(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        'snapshotId': str(snapshot['snapshotId']),
        'publicationDate': str(snapshot['publicationDate']),
        'sourceLastMatchId': int(snapshot['sourceLastMatchId']),
        'standings': _trend_standings(snapshot),
    }


def merge_current_dashboard_publication(
    trends: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> Mapping[str, Any]:
    current = current_dashboard_publication(snapshot)
    publication_date = str(current['publicationDate'])
    publications = [
        publication
        for publication in trends.get('publications', ())
        if str(publication.get('publicationDate')) != publication_date
    ]
    publications.append(current)
    publications.sort(key=lambda publication: str(publication['publicationDate']))
    return {
        'schemaVersion': 1,
        'updatedAt': str(snapshot['generatedAt']),
        'publications': publications[-_MAX_TREND_PUBLICATIONS:],
    }


def persist_dashboard_publication(
    database_target: DatabaseTarget,
    *,
    source_revision: int,
    snapshot: Mapping[str, Any],
) -> None:
    now = int(time.time())
    publication_date = str(snapshot['publicationDate'])
    connection = connect_database(database_target)
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            'INSERT INTO dashboard_publications('
            'publication_date,source_revision,snapshot_id,generated_at,'
            'source_last_match_id,updated_at) VALUES(?,?,?,?,?,?) '
            'ON CONFLICT(publication_date) DO UPDATE SET '
            'source_revision=excluded.source_revision,'
            'snapshot_id=excluded.snapshot_id,generated_at=excluded.generated_at,'
            'source_last_match_id=excluded.source_last_match_id,'
            'updated_at=excluded.updated_at',
            (
                publication_date,
                source_revision,
                str(snapshot['snapshotId']),
                str(snapshot['generatedAt']),
                int(snapshot['sourceLastMatchId']),
                now,
            ),
        )
        connection.execute(
            'DELETE FROM dashboard_publication_standings WHERE publication_date=?',
            (publication_date,),
        )
        rows: List[Tuple[str, str, str, int, int, int]] = []
        for season_key, modes in _trend_standings(snapshot).items():
            for mode, standings in modes.items():
                rows.extend(
                    (
                        publication_date,
                        season_key,
                        mode,
                        int(standing['playerId']),
                        int(standing['rank']),
                        int(standing['ratingScore']),
                    )
                    for standing in standings
                )
        connection.executemany(
            'INSERT INTO dashboard_publication_standings('
            'publication_date,season_key,mode,player_id,rank,rating_score'
            ') VALUES(?,?,?,?,?,?)',
            rows,
        )
        connection.execute(
            'DELETE FROM dashboard_publications WHERE publication_date NOT IN('
            'SELECT publication_date FROM dashboard_publications '
            'ORDER BY publication_date DESC LIMIT ?)',
            (_MAX_TREND_PUBLICATIONS,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_dashboard_trends(database_target: DatabaseTarget) -> Mapping[str, Any]:
    connection = connect_database(database_target)
    try:
        publications = connection.execute(
            'SELECT publication_date,snapshot_id,generated_at,source_last_match_id '
            'FROM dashboard_publications ORDER BY publication_date'
        ).fetchall()
        rows = connection.execute(
            'SELECT publication_date,season_key,mode,player_id,rank,rating_score '
            'FROM dashboard_publication_standings '
            'ORDER BY publication_date,season_key,mode,rank'
        ).fetchall()
    finally:
        connection.close()
    standings_by_date: Dict[str, Dict[str, Dict[str, List[Mapping[str, int]]]]] = {}
    for row in rows:
        publication_date = str(row['publication_date'])
        season_key = str(row['season_key'])
        mode = str(row['mode'])
        season_modes = standings_by_date.setdefault(publication_date, {}).setdefault(
            season_key, {trend_mode: [] for trend_mode in _TREND_MODES}
        )
        season_modes.setdefault(mode, []).append(
            {
                'playerId': int(row['player_id']),
                'rank': int(row['rank']),
                'ratingScore': int(row['rating_score']),
            }
        )
    values = []
    for publication in publications:
        publication_date = str(publication['publication_date'])
        values.append(
            {
                'snapshotId': str(publication['snapshot_id']),
                'publicationDate': publication_date,
                'sourceLastMatchId': int(publication['source_last_match_id']),
                'standings': standings_by_date.get(publication_date, {}),
            }
        )
    return {
        'schemaVersion': 1,
        'updatedAt': (
            '' if not publications else str(publications[-1]['generated_at'])
        ),
        'publications': values,
    }
