from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional, Sequence, Tuple, cast


def exact_match_fingerprint(
    *,
    mode: str,
    duration_seconds: int,
    winner_side: str,
    teams: Sequence[Mapping[str, Any]],
) -> Optional[str]:
    """Return a conservative identity for an exactly recognized result screen.

    The identity deliberately ignores viewpoint-only fields such as ally/enemy,
    team colour, slot order, and the recorded player marker.  Every gameplay
    value must be present and agree exactly; incomplete observations are left
    ungrouped so an uncertain pair continues to count as two matches.
    """

    public_mode = {'aram': 'brawl', 'other': 'brawl'}.get(mode, mode)
    expected_team_sizes = (
        {3} if public_mode == '3v3' else {5} if public_mode == '5v5' else {3, 5}
    )
    if (
        public_mode not in {'3v3', 'brawl', '5v5'}
        or duration_seconds <= 0
        or winner_side not in {'left', 'right'}
        or len(teams) != 2
    ):
        return None

    team_values = []
    seen_sides = set()
    for team in teams:
        side = str(team.get('side') or '')
        kills = team.get('kills')
        economy = team.get('economy')
        players = team.get('players')
        if (
            side not in {'left', 'right'}
            or side in seen_sides
            or not isinstance(kills, int)
            or not isinstance(economy, int)
            or not isinstance(players, Sequence)
            or isinstance(players, (str, bytes))
            or len(players) not in expected_team_sizes
        ):
            return None
        seen_sides.add(side)
        participant_values = []
        for raw_player in players:
            if not isinstance(raw_player, Mapping):
                return None
            player = cast(Mapping[str, Any], raw_player)
            hero_name = str(player.get('hero_name') or '').strip().casefold()
            statistics = tuple(
                player.get(field) for field in ('kills', 'deaths', 'assists', 'economy')
            )
            if not hero_name or any(not isinstance(value, int) for value in statistics):
                return None
            participant_values.append((hero_name, *statistics))
        team_values.append(
            (side == winner_side, kills, economy, tuple(sorted(participant_values)))
        )

    if len({len(team[3]) for team in team_values}) != 1:
        return None

    payload: Tuple[Any, ...] = (
        public_mode,
        duration_seconds,
        tuple(sorted(team_values)),
    )
    serialized = json.dumps(
        payload, ensure_ascii=False, separators=(',', ':'), sort_keys=False
    )
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()
