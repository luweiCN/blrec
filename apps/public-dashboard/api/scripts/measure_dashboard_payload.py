#!/usr/bin/env python3
"""Measure dashboard JSON structure without retaining or printing its contents."""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any, Dict
from urllib.request import Request, urlopen


def _encoded(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(',', ':')
    ).encode('utf-8')


def _size(value: Any) -> Dict[str, int]:
    payload = _encoded(value)
    return {
        'raw_bytes': len(payload),
        'gzip_bytes': len(gzip.compress(payload, compresslevel=6)),
    }


def _load(source: str) -> bytes:
    if source.startswith(('http://', 'https://')):
        request = Request(
            source,
            headers={
                'Accept-Encoding': 'identity',
                'User-Agent': 'blrec-dashboard-payload-audit/1.0',
            },
        )
        with urlopen(request, timeout=120) as response:
            return response.read()
    return Path(source).read_bytes()


def measure(source: str) -> Dict[str, Any]:
    payload = _load(source)
    document = json.loads(payload)
    snapshot = document['snapshot']
    trends = document['trends']
    standings = snapshot['standings']
    publications = trends['publications']
    duplicated_hero_pool_bytes = 0
    player_objects = 0
    unique_player_ids = set()
    for season in standings.values():
        for player in season.get('players', []):
            player_objects += 1
            unique_player_ids.add(player.get('id'))
            pools = player.get('heroPools') or {}
            if player.get('heroPool') == pools.get('all'):
                duplicated_hero_pool_bytes += len(_encoded(player['heroPool']))
    return {
        'source': source,
        'document': {
            'raw_bytes': len(payload),
            'gzip_bytes': len(gzip.compress(payload, compresslevel=6)),
        },
        'top_level': {key: _size(value) for key, value in document.items()},
        'snapshot_fields': {key: _size(value) for key, value in snapshot.items()},
        'trends_fields': {key: _size(value) for key, value in trends.items()},
        'standings_seasons': {
            key: {
                **_size(value),
                'fields': {
                    field: _size(field_value) for field, field_value in value.items()
                },
            }
            for key, value in standings.items()
        },
        'counts': {
            'trend_publications': len(publications),
            'snapshot_matches': len(snapshot.get('matches', [])),
            'standings_seasons': len(standings),
            'season_player_objects': player_objects,
            'unique_player_ids': len(unique_player_ids),
        },
        'known_duplication': {
            'hero_pool_equals_hero_pools_all_raw_bytes': duplicated_hero_pool_bytes,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('source', help='Dashboard URL or local JSON file')
    arguments = parser.parse_args()
    print(json.dumps(measure(arguments.source), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
