from blrec.vainglory.deduplication import exact_match_fingerprint


def _teams(*, economy: bool = True):
    values = []
    for side_index, side in enumerate(('left', 'right')):
        values.append(
            {
                'side': side,
                'kills': 12 + side_index,
                'economy': 31_000 + side_index * 1_000 if economy else None,
                'players': [
                    {
                        'hero_name': 'Hero-{}-{}'.format(side_index, slot),
                        'kills': slot,
                        'deaths': 4 - slot,
                        'assists': slot + 2,
                        'economy': 10_000 + slot * 100 if economy else None,
                    }
                    for slot in range(1, 4)
                ],
            }
        )
    return values


def test_exact_fingerprint_ignores_mirrored_team_sides_and_slot_order() -> None:
    teams = _teams()
    mirrored = [
        {**teams[1], 'side': 'left', 'players': list(reversed(teams[1]['players']))},
        {**teams[0], 'side': 'right', 'players': list(reversed(teams[0]['players']))},
    ]

    first = exact_match_fingerprint(
        mode='3v3',
        duration_seconds=900,
        winner_side='right',
        recorded_player_side='left',
        teams=teams,
    )
    replay = exact_match_fingerprint(
        mode='3v3',
        duration_seconds=900,
        winner_side='left',
        recorded_player_side='right',
        teams=mirrored,
    )

    assert first is not None
    assert replay == first


def test_exact_fingerprint_accepts_aram_without_economy() -> None:
    fingerprint = exact_match_fingerprint(
        mode='aram',
        duration_seconds=742,
        winner_side='left',
        recorded_player_side='left',
        teams=_teams(economy=False),
    )

    assert fingerprint is not None


def test_exact_fingerprint_rejects_incomplete_or_changed_results() -> None:
    teams = _teams()
    incomplete = [dict(team) for team in teams]
    incomplete[0] = {**incomplete[0], 'players': incomplete[0]['players'][:-1]}
    changed = [dict(team) for team in teams]
    changed_players = [dict(player) for player in changed[0]['players']]
    changed_players[0]['assists'] += 1
    changed[0] = {**changed[0], 'players': changed_players}

    original = exact_match_fingerprint(
        mode='3v3',
        duration_seconds=900,
        winner_side='right',
        recorded_player_side='right',
        teams=teams,
    )

    assert (
        exact_match_fingerprint(
            mode='3v3',
            duration_seconds=900,
            winner_side='right',
            recorded_player_side='right',
            teams=incomplete,
        )
        is None
    )
    assert (
        exact_match_fingerprint(
            mode='3v3',
            duration_seconds=900,
            winner_side='right',
            recorded_player_side='right',
            teams=changed,
        )
        != original
    )


def test_exact_fingerprint_distinguishes_duo_streamers_by_recorded_hero() -> None:
    teams = _teams()

    first_streamer = exact_match_fingerprint(
        mode='3v3',
        duration_seconds=900,
        winner_side='left',
        recorded_player_side='left',
        recorded_hero_name='Hero-0-1',
        teams=teams,
    )
    teammate = exact_match_fingerprint(
        mode='3v3',
        duration_seconds=900,
        winner_side='left',
        recorded_player_side='left',
        recorded_hero_name='Hero-0-2',
        teams=teams,
    )
    replay = exact_match_fingerprint(
        mode='3v3',
        duration_seconds=900,
        winner_side='left',
        recorded_player_side='left',
        recorded_hero_name='Hero-0-1',
        teams=teams,
    )

    assert first_streamer is not None
    assert teammate is not None
    assert teammate != first_streamer
    assert replay == first_streamer
