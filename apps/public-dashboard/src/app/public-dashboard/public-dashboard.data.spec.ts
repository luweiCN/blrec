import {
  getHeroPlayerComparisons,
  getHeroRankings,
  getPlayerRankings,
  getPlayerTrend,
  getRankMovement,
  formatEconomy,
  heroGoldPerMinute,
  heroKda,
  heroPeerComparisonText,
  playerKdaForMode,
} from './public-dashboard.data';
import {
  DashboardSnapshot,
  DashboardTrends,
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
  it('formats match economy in the same compact form as the game', () => {
    expect(formatEconomy(40_900)).toBe('40.9K');
    expect(formatEconomy(33_000)).toBe('33.0K');
    expect(formatEconomy(null)).toBe('—');
  });

  it('includes low samples and sorts by rating instead of raw win rate', () => {
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

  it('compares each mode with the previous committed publication', () => {
    const trends: DashboardTrends = {
      schemaVersion: 1,
      updatedAt: '2026-08-03T02:05:00Z',
      publications: [
        trendPublication('snapshot-1', '2026-08-01', 1, 3, 610),
        trendPublication('snapshot-2', '2026-08-02', 1, 2, 618),
        trendPublication(
          TEST_DASHBOARD_SNAPSHOT.snapshotId,
          '2026-08-03',
          1,
          1,
          625,
        ),
      ],
    };

    const trend = getPlayerTrend(
      trends,
      TEST_DASHBOARD_SNAPSHOT.snapshotId,
      '2026-summer',
      '3v3',
      1,
    );

    expect(trend.points.map((point) => point.ratingScore)).toEqual([
      610, 618, 625,
    ]);
    expect(trend.points.every((point) => point.recorded)).toBeTrue();
    expect(trend.rankDelta).toBe(1);
    expect(trend.ratingDelta).toBe(7);
    expect(getRankMovement(trend)).toEqual({
      kind: 'up',
      text: '↑1',
      label: '今日较昨日上升 1 名',
    });
  });

  it('does not combine another mode or an uncommitted snapshot', () => {
    const trends: DashboardTrends = {
      schemaVersion: 1,
      updatedAt: '2026-08-04T02:05:00Z',
      publications: [
        trendPublication('snapshot-1', '2026-08-02', 1, 2, 618),
        trendPublication('snapshot-uncommitted', '2026-08-04', 1, 1, 630),
      ],
    };

    const trend = getPlayerTrend(
      trends,
      TEST_DASHBOARD_SNAPSHOT.snapshotId,
      '2026-summer',
      '3v3',
      1,
    );

    expect(trend.points).toEqual([]);
    expect(getRankMovement(trend).kind).toBe('pending');
  });

  it('fills missing calendar days by carrying the previous daily result', () => {
    const trends: DashboardTrends = {
      schemaVersion: 1,
      updatedAt: '2026-08-03T02:05:00Z',
      publications: [
        trendPublication('snapshot-1', '2026-08-01', 1, 2, 618),
        trendPublication(
          TEST_DASHBOARD_SNAPSHOT.snapshotId,
          '2026-08-03',
          1,
          1,
          625,
        ),
      ],
    };

    const trend = getPlayerTrend(
      trends,
      TEST_DASHBOARD_SNAPSHOT.snapshotId,
      '2026-summer',
      '3v3',
      1,
    );

    expect(trend.points.map((point) => point.publicationDate)).toEqual([
      '2026-08-01',
      '2026-08-02',
      '2026-08-03',
    ]);
    expect(trend.points.map((point) => point.ratingScore)).toEqual([
      618, 618, 625,
    ]);
    expect(trend.points.map((point) => point.recorded)).toEqual([
      true, false, true,
    ]);
    expect(trend.ratingDelta).toBe(7);
  });

  it('sorts hero popularity by usage without the win-rate sample threshold', () => {
    const ranking = getHeroRankings(
      TEST_DASHBOARD_SNAPSHOT,
      '2026-summer',
      '3v3',
      'usage',
    );
    const matches = ranking.map((hero) => hero.modes['3v3'].matches);

    expect(matches.length).toBeGreaterThan(0);
    expect(matches).toEqual([...matches].sort((left, right) => right - left));
    expect(matches.every((value) => value > 0)).toBeTrue();
  });

  it('compares a player hero record with other players using the same hero', () => {
    const records = getHeroPlayerComparisons(
      TEST_DASHBOARD_SNAPSHOT,
      '2026-summer',
      '3v3',
      'Vox',
    );
    const record = records.find((candidate) => candidate.player.id === 1);

    expect(record).toBeDefined();
    expect(record?.playerCount).toBeGreaterThan(1);
    expect(record?.usageRank).toBeGreaterThan(1);
    expect(record?.peers.kind).toBe('available');
    if (record?.peers.kind === 'available') {
      expect(record.peers.players).toBe(record.playerCount - 1);
      expect(record.peers.kda.kind).toBe('available');
      expect(record.peers.economy.kind).toBe('available');
      expect(heroKda(record.usage)).not.toBeNull();
      expect(heroPeerComparisonText(record.peers)).toMatch(/高|低|持平/u);
    }
  });

  it('calculates economy comparison from total gold per total minute', () => {
    const usage = {
      name: 'Vox',
      matches: 2,
      wins: 1,
      stats: {
        kdaMatches: 0,
        kills: 0,
        deaths: 0,
        assists: 0,
        economyMatches: 2,
        economy: 30_000,
        economyDurationSeconds: 1_800,
      },
    };
    const legacyUsage = {
      ...usage,
      stats: {
        ...usage.stats,
        economyDurationSeconds: undefined,
      },
    };

    expect(heroGoldPerMinute(usage)).toBe(1_000);
    expect(heroGoldPerMinute(legacyUsage)).toBeNull();
  });

  it('aggregates player KDA from complete hero samples in the selected mode', () => {
    const player =
      TEST_DASHBOARD_SNAPSHOT.standings['2026-summer'].players[0];
    const summary = playerKdaForMode(player, '3v3');
    const playerWithoutKda: PlayerStanding = {
      ...player,
      heroPools: {
        all: [],
        '3v3': [{ name: 'Caine', matches: 1, wins: 1 }],
        brawl: [],
        '5v5': [],
      },
    };

    expect(summary?.matches).toBe(145);
    expect(summary?.value).toBeCloseTo(3.68, 2);
    expect(playerKdaForMode(playerWithoutKda, '3v3')).toBeNull();
  });
});

function trendPublication(
  snapshotId: string,
  publicationDate: string,
  playerId: number,
  rank: number,
  ratingScore: number,
): DashboardTrends['publications'][number] {
  const standing = { playerId, rank, ratingScore };
  return {
    snapshotId,
    publicationDate,
    sourceLastMatchId: 100,
    standings: {
      '2026-summer': {
        all: [standing],
        '3v3': [standing],
        brawl: [],
        '5v5': [],
      },
    },
  };
}
