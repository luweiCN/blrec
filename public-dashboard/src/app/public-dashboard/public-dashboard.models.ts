export const MODE_FILTERS = ['all', '3v3', 'brawl', '5v5'] as const;

export type ModeFilter = (typeof MODE_FILTERS)[number];
export type CompetitiveMode = Exclude<ModeFilter, 'all'>;
export type SeasonKey =
  | `${number}-${'spring' | 'summer' | 'autumn'}`
  | 'all-time';
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

export interface DashboardManifest {
  readonly schemaVersion: 1;
  readonly snapshotId: string;
  readonly snapshotPath: string;
  readonly publicationDate: string;
  readonly generatedAt: string;
  readonly sourceLastMatchId: number;
  readonly sha256: string;
  readonly bytes: number;
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

export interface SeasonStandings {
  readonly players: readonly PlayerStanding[];
  readonly heroes: readonly HeroStanding[];
}

export interface DashboardSnapshot {
  readonly schemaVersion: 2;
  readonly snapshotId: string;
  readonly publicationDate: string;
  readonly generatedAt: string;
  readonly sourceLastMatchId: number;
  readonly sourceMatchCount: number;
  readonly currentSeasonKey: SeasonKey;
  readonly seasons: readonly SeasonOption[];
  readonly standings: Readonly<Record<string, SeasonStandings>>;
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

export function isModeFilter(value: string): value is ModeFilter {
  return (MODE_FILTERS as readonly string[]).includes(value);
}

export function isSeasonKey(value: string): value is SeasonKey {
  return (
    value === 'all-time' ||
    /^\d{4}-(spring|summer|autumn)$/u.test(value)
  );
}
