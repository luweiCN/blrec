from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional, Sequence, Tuple, cast

FINGERPRINT_VERSION = 'v1'


def exact_match_fingerprint(
    *,
    mode: str,
    duration_seconds: int,
    winner_side: str,
    recorded_player_side: str,
    teams: Sequence[Mapping[str, Any]],
) -> Optional[str]:
    """Return a conservative identity for one completely recognized result."""

    normalized_mode = {'other': 'brawl'}.get(mode, mode)
    expected_team_sizes = {'3v3': {3}, 'aram': {3}, 'brawl': {3, 5}, '5v5': {5}}
    allowed_team_sizes = expected_team_sizes.get(normalized_mode)
    if (
        allowed_team_sizes is None
        or duration_seconds <= 0
        or winner_side not in {'left', 'right'}
        or len(teams) != 2
    ):
        return None

    economy_required = normalized_mode in {'3v3', '5v5'}
    team_values = []
    seen_sides = set()
    economy_presence = set()
    for team in teams:
        side = str(team.get('side') or '')
        kills = team.get('kills')
        economy = team.get('economy')
        players = team.get('players')
        if (
            side not in {'left', 'right'}
            or side in seen_sides
            or type(kills) is not int
            or not isinstance(players, Sequence)
            or isinstance(players, (str, bytes))
            or len(players) not in allowed_team_sizes
        ):
            return None
        if economy_required and type(economy) is not int:
            return None
        if not economy_required and economy is not None and type(economy) is not int:
            return None
        seen_sides.add(side)
        economy_presence.add(economy is not None)
        participant_values = []
        for raw_player in players:
            if not isinstance(raw_player, Mapping):
                return None
            player = cast(Mapping[str, Any], raw_player)
            hero_name = str(player.get('hero_name') or '').strip().casefold()
            statistics = tuple(
                player.get(field) for field in ('kills', 'deaths', 'assists')
            )
            player_economy = player.get('economy')
            if not hero_name or any(type(value) is not int for value in statistics):
                return None
            if economy_required and type(player_economy) is not int:
                return None
            if (
                not economy_required
                and player_economy is not None
                and type(player_economy) is not int
            ):
                return None
            economy_presence.add(player_economy is not None)
            participant_values.append((hero_name, *statistics, player_economy))
        team_values.append(
            (side == winner_side, kills, economy, tuple(sorted(participant_values)))
        )

    if len({len(team[3]) for team in team_values}) != 1:
        return None
    if not economy_required and len(economy_presence) > 1:
        return None

    payload: Tuple[Any, ...] = (
        normalized_mode,
        duration_seconds,
        (
            recorded_player_side == winner_side
            if recorded_player_side in {'left', 'right'}
            else None
        ),
        tuple(sorted(team_values)),
    )
    serialized = json.dumps(
        payload, ensure_ascii=False, separators=(',', ':'), sort_keys=False
    )
    digest = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
    return '{}:{}'.format(FINGERPRINT_VERSION, digest)
