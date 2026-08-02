from blrec.vainglory.ocr import (
    PlayerStats,
    clean_player_name,
    parse_player_stats,
    parse_result_header,
    resolve_player_stats,
    result_end_reason,
)


def test_parse_chinese_result_header_keeps_reason_separate_from_winner() -> None:
    header = parse_result_header('0 29.8k 3 投降 19 41.3k 4\n14:38')

    assert header.result_text == '投降'
    assert header.end_reason == 'surrender'
    assert header.duration_seconds == 14 * 60 + 38
    assert header.left_kills == 3
    assert header.right_kills == 19
    assert header.left_economy == 29_800
    assert header.right_economy == 41_300


def test_parse_multilingual_header_without_a_known_result_word() -> None:
    header = parse_result_header('2 40.9k 9 ПОБЕДА 18 45.8k 5\n7:59')

    assert header.duration_seconds == 7 * 60 + 59
    assert header.left_kills == 9
    assert header.right_kills == 18
    assert header.left_economy == 40_900
    assert header.right_economy == 45_800


def test_parse_header_recovers_common_numeric_ocr_confusion() -> None:
    header = parse_result_header('5 全46.Ik 20 胜利9 46.1K@ 428\n18:17')

    assert header.result_text == '胜利'
    assert header.left_kills == 20
    assert header.right_kills == 9
    assert header.left_economy == 46_100
    assert header.right_economy == 46_100


def test_parse_player_stats_accepts_compact_numeric_ocr() -> None:
    stats = parse_player_stats('10/2/10 15.6k 123')

    assert stats.kills == 10
    assert stats.deaths == 2
    assert stats.assists == 10
    assert stats.economy == 15_600
    assert stats.last_hits == 123


def test_parse_player_stats_recovers_slash_read_as_one() -> None:
    left = parse_player_stats('9/716 17.8k')
    right = parse_player_stats('7/7117 16.1k')

    assert (left.kills, left.deaths, left.assists) == (9, 7, 6)
    assert (right.kills, right.deaths, right.assists) == (7, 7, 17)


def test_result_words_only_describe_the_end_reason() -> None:
    assert result_end_reason('Victory') == 'normal'
    assert result_end_reason('失败') == 'normal'
    assert result_end_reason('Surrender') == 'surrender'
    assert result_end_reason('投降') == 'surrender'


def test_player_name_removes_ocr_whitespace_from_the_display_value() -> None:
    assert clean_player_name(' 3100 _ 冒 充 小 白 ') == '3100_冒充小白'


def test_player_name_removes_kda_accidentally_included_by_the_crop() -> None:
    assert clean_player_name('3100-1_LimitKing7/4/4') == '3100-1_LimitKing'
    assert clean_player_name('player/name') == 'player/name'


def _stats(kda: str, economy: int) -> PlayerStats:
    parsed = parse_player_stats('{} {}k'.format(kda, economy / 1_000))
    return parsed


def test_resolve_player_stats_uses_both_teams_scoreboard_invariants() -> None:
    candidates = (
        (_stats('1/7/1', 12_400),),
        (_stats('5/6/4', 13_200),),
        (_stats('3/6/6', 11_700),),
        (_stats('7/0/10', 16_800), _stats('9/0/10', 16_800), _stats('9/0/10', 16_800)),
        (_stats('9/2/4', 15_500),),
        (_stats('1/7/11', 12_900),),
    )

    resolved = resolve_player_stats(candidates)

    assert (resolved[3].kills, resolved[3].deaths, resolved[3].assists) == (9, 0, 10)
    assert sum(item.kills or 0 for item in resolved[:3]) == sum(
        item.deaths or 0 for item in resolved[3:]
    )
    assert sum(item.kills or 0 for item in resolved[3:]) == sum(
        item.deaths or 0 for item in resolved[:3]
    )


def test_resolve_player_stats_rejects_assist_and_economy_outliers() -> None:
    candidates = (
        (_stats('8/5/7', 17_500),),
        (_stats('4/4/9', 15_800),),
        (_stats('1/12/57', 57_800), _stats('1/12/50', 7_800), _stats('1/12/5', 7_800)),
        (_stats('6/4/8', 16_200),),
        (_stats('7/5/5', 17_100), _stats('7/5/95', 17_100)),
        (_stats('8/4/7', 15_900),),
    )

    resolved = resolve_player_stats(candidates)

    assert resolved[2].assists == 5
    assert resolved[2].economy == 7_800
    assert resolved[4].assists == 5


def test_resolve_player_stats_prefers_header_consistent_kda() -> None:
    candidates = (
        (_stats('7/4/4', 20_200),),
        (_stats('9/5/4', 18_300),),
        (_stats('0/1/11', 14_200),),
        (_stats('5/6/3', 22_500),),
        (_stats('2/4/6', 17_400),),
        (_stats('8/6/55', 13_600), _stats('3/6/5', 13_600)),
    )
    header = parse_result_header('5 52.9k 16 战败 10 53.6k 5\n21:17')

    resolved = resolve_player_stats(candidates, header=header)

    assert (resolved[5].kills, resolved[5].deaths, resolved[5].assists) == (3, 6, 5)


def test_resolve_player_stats_rejects_economy_team_that_cannot_match_header() -> None:
    candidates = (
        (_stats('3/8/8', 19_000),),
        (_stats('8/4/3', 18_400),),
        (_stats('2/7/8', 12_600),),
        (_stats('8/1/9', 1_000),),
        (PlayerStats(6, 4, 12, None),),
        (PlayerStats(5, 8, 8, None),),
    )
    header = parse_result_header('5 50.1k 13 胜利 19 59.5k 5\n22:16')

    resolved = resolve_player_stats(candidates, header=header)

    assert [item.economy for item in resolved[:3]] == [19_000, 18_400, 12_600]
    assert [item.economy for item in resolved[3:]] == [None, None, None]


def test_resolve_player_stats_blanks_a_complete_but_impossible_kda() -> None:
    candidates = (
        (_stats('7/4/4', 20_200),),
        (_stats('9/5/4', 18_300),),
        (_stats('0/1/11', 14_200),),
        (_stats('5/6/3', 22_500),),
        (_stats('2/4/6', 17_400),),
        (_stats('8/6/55', 13_600),),
    )
    header = parse_result_header('5 52.9k 16 战败 10 53.6k 5\n21:17')

    resolved = resolve_player_stats(candidates, header=header)

    assert [item.kills for item in resolved[:3]] == [7, 9, 0]
    assert [item.kills for item in resolved[3:]] == [None, None, None]
    assert resolved[5].assists is None


def test_resolve_player_stats_allows_one_execution_without_a_team_kill() -> None:
    candidates = (
        (_stats('0/4/0', 8_000),),
        (_stats('0/5/0', 8_000),),
        (_stats('0/4/0', 8_000),),
        (_stats('4/0/4', 10_000),),
        (_stats('5/0/3', 10_000),),
        (_stats('3/0/5', 10_000),),
    )
    header = parse_result_header('5 24k 0 战败 12 30k 5\n12:00')

    resolved = resolve_player_stats(candidates, header=header)

    assert [item.deaths for item in resolved[:3]] == [4, 5, 4]
