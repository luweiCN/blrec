import { heroesForSeason, playersForSeason } from './public-dashboard.mock-data';
import {
  DashboardSnapshot,
  SeasonKey,
  SeasonOption,
  SeasonStandings,
} from './public-dashboard.models';

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
      heroes: heroesForSeason(season.key as SeasonKey),
    };
    return result;
  },
  {},
);

export const TEST_DASHBOARD_SNAPSHOT: DashboardSnapshot = {
  schemaVersion: 2,
  snapshotId: '20260803T020500Z-testdata',
  publicationDate: '2026-08-03',
  generatedAt: '2026-08-03T02:05:00Z',
  sourceLastMatchId: 12345,
  sourceMatchCount: 2468,
  ratingModel: {
    version: 2,
    priorMatches: 20,
    carryoverRate: 0.25,
    credibleLevel: 0.9,
    provisionalMatches: 5,
    minimumOutcomeDelta: 1,
  },
  currentSeasonKey: '2026-summer',
  seasons: SEASONS,
  standings,
};
