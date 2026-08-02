export const MODE_FILTERS = ['all', '3v3', 'brawl', '5v5'] as const;
export const SEASON_KEYS = [
  '2026-summer',
  '2026-spring',
  '2025-autumn',
  'all-time',
] as const;

export type ModeFilter = (typeof MODE_FILTERS)[number];
export type CompetitiveMode = Exclude<ModeFilter, 'all'>;
export type SeasonKey = (typeof SEASON_KEYS)[number];
export type MatchResult = 'W' | 'L';

export interface ModeOption {
  readonly key: ModeFilter;
  readonly label: string;
  readonly description?: string;
}

export interface SeasonOption {
  readonly key: SeasonKey;
  readonly label: string;
  readonly shortLabel: string;
  readonly period: string;
  readonly current: boolean;
}

export interface Performance {
  readonly matches: number;
  readonly wins: number;
  readonly topHero: string;
}

export interface HeroUsage {
  readonly name: string;
  readonly matches: number;
  readonly wins: number;
}

export interface PlayerStanding {
  readonly id: number;
  readonly name: string;
  readonly initial: string;
  readonly roomLabel: string;
  readonly aliases: readonly string[];
  readonly trend: number;
  readonly form: readonly MatchResult[];
  readonly modes: Readonly<Record<ModeFilter, Performance>>;
  readonly heroPool: readonly HeroUsage[];
}

export interface HeroPerformance {
  readonly matches: number;
  readonly wins: number;
  readonly players: number;
}

export interface HeroStanding {
  readonly id: string;
  readonly name: string;
  readonly modes: Readonly<Record<ModeFilter, HeroPerformance>>;
}

export interface DashboardSummary {
  readonly playerCount: number;
  readonly matchCount: number;
  readonly winRate: number;
  readonly heroCount: number;
}

export interface ModeBreakdown {
  readonly key: CompetitiveMode;
  readonly label: string;
  readonly matches: number;
  readonly share: number;
}

export interface PlayerRankingRow {
  readonly rank: number;
  readonly player: PlayerStanding;
}

export interface HeroRankingRow {
  readonly rank: number;
  readonly hero: HeroStanding;
}

export const MODE_OPTIONS: readonly ModeOption[] = [
  { key: 'all', label: '全部模式' },
  { key: '3v3', label: '3V3' },
  {
    key: 'brawl',
    label: '乱斗',
    description: '大乱斗与闪电战合并统计',
  },
  { key: '5v5', label: '5V5' },
];

export const COMPETITIVE_MODE_OPTIONS: readonly {
  readonly key: CompetitiveMode;
  readonly label: string;
}[] = [
  { key: '3v3', label: '3V3' },
  { key: 'brawl', label: '乱斗' },
  { key: '5v5', label: '5V5' },
];

export const SEASON_OPTIONS: readonly SeasonOption[] = [
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
    period: '全部已发布对局',
    current: false,
  },
];

export const CURRENT_SEASON_KEY: SeasonKey = '2026-summer';

export function isModeFilter(value: string): value is ModeFilter {
  return (MODE_FILTERS as readonly string[]).includes(value);
}

export function isSeasonKey(value: string): value is SeasonKey {
  return (SEASON_KEYS as readonly string[]).includes(value);
}
