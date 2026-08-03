import { getPlayerRankings } from './public-dashboard.data';
import {
  DashboardSnapshot,
  Performance,
  PlayerStanding,
} from './public-dashboard.models';
import { TEST_DASHBOARD_SNAPSHOT } from './public-dashboard.test-data';

function withPerformance(
  player: PlayerStanding,
  performance: Performance,
): PlayerStanding {
  return {
    ...player,
    modes: { ...player.modes, '3v3': performance },
  };
}

describe('public dashboard player rankings', () => {
  it('includes low samples and sorts by Bayesian rating instead of raw win rate', () => {
    const sourcePlayers =
      TEST_DASHBOARD_SNAPSHOT.standings['2026-summer'].players;
    const lowSample = withPerformance(sourcePlayers[0], {
      matches: 3,
      wins: 3,
      topHero: 'Caine',
      ratingScore: 600,
      provisional: true,
    });
    const established = withPerformance(sourcePlayers[1], {
      matches: 20,
      wins: 12,
      topHero: 'Vox',
      ratingScore: 650,
      provisional: false,
    });
    const snapshot: DashboardSnapshot = {
      ...TEST_DASHBOARD_SNAPSHOT,
      standings: {
        ...TEST_DASHBOARD_SNAPSHOT.standings,
        '2026-summer': {
          ...TEST_DASHBOARD_SNAPSHOT.standings['2026-summer'],
          players: [lowSample, established],
        },
      },
    };

    const ranking = getPlayerRankings(snapshot, '2026-summer', '3v3');

    expect(ranking.map((player) => player.id)).toEqual([
      established.id,
      lowSample.id,
    ]);
  });
});
