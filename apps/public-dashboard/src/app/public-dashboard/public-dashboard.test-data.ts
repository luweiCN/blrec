import { heroesForSeason, playersForSeason } from './public-dashboard.mock-data';
import {
  DashboardMatch,
  DashboardMatchPlayer,
  DashboardSnapshot,
  HeroStanding,
  DashboardTrendPublication,
  DashboardTrends,
  SeasonKey,
  SeasonOption,
  SeasonStandings,
} from './public-dashboard.models';

function heroesWithTestSynergies(
  seasonKey: SeasonKey,
): readonly HeroStanding[] {
  return heroesForSeason(seasonKey).map((hero) => {
    if (hero.name !== 'Caine') {
      return hero;
    }
    const ranking = {
      best: [
        { name: 'Ardan', matches: 15, wins: 12 },
        { name: 'Lyra', matches: 9, wins: 7 },
      ],
      worst: [
        { name: 'Vox', matches: 8, wins: 3 },
        { name: 'Ringo', matches: 7, wins: 2 },
      ],
    };
    return {
      ...hero,
      synergies: {
        all: ranking,
        '3v3': ranking,
        brawl: { best: [], worst: [] },
        '5v5': { best: [], worst: [] },
      },
    };
  });
}

const SEASONS: readonly SeasonOption[] = [
  {
    key: '2026-summer',
    label: '2026 夏季赛',
    shortLabel: '本期 · 夏季赛',
    period: '2026.05.01—08.31',
    current: true,
  },
  {
    key: '2026-spring',
    label: '2026 春季赛',
    shortLabel: '2026 春季赛',
    period: '2026.01.01—04.30',
    current: false,
  },
  {
    key: '2025-autumn',
    label: '2025 秋季赛',
    shortLabel: '2025 秋季赛',
    period: '2025.09.01—12.31',
    current: false,
  },
  {
    key: 'all-time',
    label: '跨赛季总榜',
    shortLabel: '总榜',
    period: '全部已收录对局',
    current: false,
  },
];

const standings = SEASONS.reduce<Record<string, SeasonStandings>>(
  (result, season) => {
    result[season.key] = {
      players: playersForSeason(season.key as SeasonKey),
      heroes: heroesWithTestSynergies(season.key as SeasonKey),
    };
    return result;
  },
  {},
);

function matchPlayer(
  name: string,
  heroName: string,
  economy: number,
  isRecordedPlayer = false,
): DashboardMatchPlayer {
  return {
    name,
    heroName,
    kills: isRecordedPlayer ? 6 : 3,
    deaths: isRecordedPlayer ? 1 : 2,
    assists: isRecordedPlayer ? 5 : 7,
    economy,
    lastHits: Math.round(economy / 28),
    isRecordedPlayer,
  };
}

const MATCH_HEROES = [
  ['Caine', 'Ardan', 'Gwen', 'Koshka', 'Vox', 'Lance'],
  ['Celeste', 'Catherine', 'Taka', 'Ringo', 'Lyra', 'Reim'],
] as const;

export const TEST_DASHBOARD_MATCHES: readonly DashboardMatch[] = Array.from(
  { length: 12 },
  (_, index): DashboardMatch => {
    const heroes = MATCH_HEROES[index % MATCH_HEROES.length];
    const won = index % 4 !== 3;
    const playerId = (index % 3) + 1;
    return {
      id: 1200 - index,
      playerId,
      seasonKey: '2026-summer',
      mode: '3v3',
      playedAt: `2026-08-${String(3 - Math.floor(index / 5)).padStart(2, '0')}T${String(20 - (index % 5)).padStart(2, '0')}:00:00Z`,
      durationSeconds: 780 + index * 5,
      result: won ? 'W' : 'L',
      ally: {
        side: 'left',
        color: 'teal',
        kills: won ? 14 : 5,
        economy: won ? 40_900 : 31_200,
        players: [
          matchPlayer('星河', heroes[0], 16_500, true),
          matchPlayer('不是小白', heroes[1], 13_600),
          matchPlayer('茉莉', heroes[2], 10_700),
        ],
      },
      enemy: {
        side: 'right',
        color: 'orange',
        kills: won ? 3 : 13,
        economy: won ? 33_000 : 42_100,
        players: [
          matchPlayer('猪国栋', heroes[3], 14_100),
          matchPlayer('dove', heroes[4], 11_100),
          matchPlayer('不要输给小白', heroes[5], 7_800),
        ],
      },
      ...(index === 0
        ? {
            replay: {
              kind: 'match' as const,
              url: 'https://www.bilibili.com/video/BV1test00001?p=2&t=120',
            },
          }
        : {}),
    };
  },
);

export const TEST_DASHBOARD_SNAPSHOT: DashboardSnapshot = {
  schemaVersion: 3,
  snapshotId: '20260803T020500Z-testdata',
  publicationDate: '2026-08-03',
  generatedAt: '2026-08-03T02:05:00Z',
  sourceLastMatchId: 12345,
  sourceMatchCount: 2468,
  ratingModel: {
    version: 4,
  },
  currentSeasonKey: '2026-summer',
  seasons: SEASONS,
  standings,
  matches: TEST_DASHBOARD_MATCHES,
};

function trendPublication(
  snapshotId: string,
  publicationDate: string,
  rank: number,
  ratingScore: number,
): DashboardTrendPublication {
  const standing = { playerId: 1, rank, ratingScore };
  const ranking = [
    ...Array.from({ length: rank - 1 }, (_, index) => ({
      playerId: index + 2,
      rank: index + 1,
      ratingScore: Math.min(1000, ratingScore + (rank - index) * 5),
    })),
    standing,
  ];
  return {
    snapshotId,
    publicationDate,
    sourceLastMatchId: 12345,
    standings: {
      '2026-summer': {
        all: ranking,
        '3v3': ranking,
        brawl: [],
        '5v5': [],
      },
    },
  };
}

export const TEST_DASHBOARD_TRENDS: DashboardTrends = {
  schemaVersion: 1,
  updatedAt: TEST_DASHBOARD_SNAPSHOT.generatedAt,
  publications: [
    trendPublication('20260801T020500Z-trend', '2026-08-01', 3, 672),
    trendPublication('20260802T020500Z-trend', '2026-08-02', 2, 680),
    trendPublication(
      TEST_DASHBOARD_SNAPSHOT.snapshotId,
      TEST_DASHBOARD_SNAPSHOT.publicationDate,
      1,
      686,
    ),
  ],
};
