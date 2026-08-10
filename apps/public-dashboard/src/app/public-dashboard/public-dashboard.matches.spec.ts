import {
  TEST_DASHBOARD_MATCHES,
  TEST_DASHBOARD_SNAPSHOT,
} from './public-dashboard.test-data';
import {
  currentMatchStreak,
  filterDashboardMatches,
} from './public-dashboard.matches';

describe('public dashboard match filters', () => {
  const players = TEST_DASHBOARD_SNAPSHOT.standings['all-time'].players;

  it('searches participant names with Chinese, pinyin and initials', () => {
    expect(
      filterDashboardMatches(TEST_DASHBOARD_MATCHES, players, {
        seasonKey: '2026-summer',
        mode: 'all',
        playerQuery: 'moli',
        selectedHeroes: [],
      }).length,
    ).toBe(12);
    expect(
      filterDashboardMatches(TEST_DASHBOARD_MATCHES, players, {
        seasonKey: '2026-summer',
        mode: 'all',
        playerQuery: 'LC',
        selectedHeroes: [],
      }).every((match) => match.playerId === 2),
    ).toBeTrue();
  });

  it('requires every selected hero to appear anywhere in the lineup', () => {
    const matching = filterDashboardMatches(TEST_DASHBOARD_MATCHES, players, {
      seasonKey: '2026-summer',
      mode: '3v3',
      playerQuery: '',
      selectedHeroes: ['Caine', 'Vox'],
    });
    const missing = filterDashboardMatches(TEST_DASHBOARD_MATCHES, players, {
      seasonKey: '2026-summer',
      mode: '3v3',
      playerQuery: '',
      selectedHeroes: ['Caine', 'Celeste'],
    });

    expect(matching.length).toBe(6);
    expect(missing).toEqual([]);
  });

  it('searches each live title as its own pinyin segment', () => {
    const titledMatch = {
      ...TEST_DASHBOARD_MATCHES[0],
      streamTitle: '茉莉深夜排位',
    };

    expect(
      filterDashboardMatches([titledMatch], players, {
        seasonKey: '2026-summer',
        mode: 'all',
        playerQuery: 'molishenye',
        selectedHeroes: [],
      }),
    ).toEqual([titledMatch]);
    expect(
      filterDashboardMatches([titledMatch], players, {
        seasonKey: '2026-summer',
        mode: 'all',
        playerQuery: 'moliLC',
        selectedHeroes: [],
      }),
    ).toEqual([]);
  });

  it('keeps the profile player fixed while searching other participants', () => {
    const fixedPlayerMatches = TEST_DASHBOARD_MATCHES.filter(
      (match) => match.playerId === 1,
    );
    const participantMatches = filterDashboardMatches(
      TEST_DASHBOARD_MATCHES,
      players,
      {
        seasonKey: 'all-time',
        mode: 'all',
        fixedPlayerId: 1,
        playerQuery: '茉莉',
        selectedHeroes: [],
      },
    );
    const profileNameMatches = filterDashboardMatches(
      TEST_DASHBOARD_MATCHES,
      players,
      {
        seasonKey: 'all-time',
        mode: 'all',
        fixedPlayerId: 1,
        playerQuery: '星河',
        selectedHeroes: [],
      },
    );

    expect(participantMatches).toEqual(fixedPlayerMatches);
    expect(profileNameMatches).toEqual([]);
  });

  it('calculates the current consecutive result from newest to oldest', () => {
    expect(currentMatchStreak(TEST_DASHBOARD_MATCHES)).toEqual({
      result: 'W',
      matches: 3,
    });
  });
});
