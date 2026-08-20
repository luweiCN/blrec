export const MODE_FILTERS = ['all', '3v3', 'brawl', '5v5'] as const;

export type ModeFilter = (typeof MODE_FILTERS)[number];
export type CompetitiveMode = Exclude<ModeFilter, 'all'>;
export type SeasonKey =
  | `${number}-${'spring' | 'summer' | 'autumn' | 'winter'}`
  | 'all-time';
export type MatchResult = 'W' | 'L';
export type HeroDataScope = 'streamer' | 'environment';

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

export interface DashboardTrendStanding {
  readonly playerId: number;
  readonly rank: number;
  readonly ratingScore: number;
}

export interface DashboardTrendPublication {
  readonly snapshotId: string;
  readonly publicationDate: string;
  readonly sourceLastMatchId: number;
  readonly standings: Readonly<
    Record<
      string,
      Readonly<Record<ModeFilter, readonly DashboardTrendStanding[]>>
    >
  >;
}

export interface DashboardTrends {
  readonly schemaVersion: 1;
  readonly updatedAt: string;
  readonly publications: readonly DashboardTrendPublication[];
}

export interface Performance {
  readonly matches: number;
  readonly wins: number;
  readonly topHero: string;
  readonly ratingScore: number | null;
  readonly currentRatingScore?: number | null;
  readonly provisional: boolean;
  readonly ratingForecast?: RatingForecast | null;
}

export interface RatingGoalForecast {
  readonly targetDisplayScore: number;
  readonly allWinMatches: number;
  readonly currentWinRateMatches: number | null;
}

export interface RatingForecast {
  readonly nextWinScore: number;
  readonly nextLossScore: number;
  readonly nextDivision: RatingGoalForecast | null;
  readonly nextTier: RatingGoalForecast | null;
  readonly ultimate: RatingGoalForecast;
}

interface LegacyRatingModelBase {
  readonly priorMatches: 20;
  readonly carryoverRate: 0.25;
  readonly credibleLevel: 0.9;
  readonly provisionalMatches: 5;
}

export interface RatingModelV1 extends LegacyRatingModelBase {
  readonly version: 1;
}

export interface RatingModelV2 extends LegacyRatingModelBase {
  readonly version: 2;
  readonly minimumOutcomeDelta: 1;
}

export interface RatingModelV3 {
  readonly version: 3;
  readonly priorMatches: 20;
  readonly carryoverMatchCap: 200;
  readonly provisionalMatches: 5;
  readonly neutralDisplayScore: 1200;
  readonly seasonResetDisplayScore: 1000;
  readonly probabilityScale: 1800;
  readonly minimumOutcomeDelta: 1;
  readonly catchupRate: 0.08;
  readonly catchupLimit: 45;
  readonly catchupProtectionGap: 150;
  readonly catchupLossMultiplier: 0.5;
}

export interface RatingModelV4 {
  readonly version: 4;
}

export interface RatingModelV5 {
  readonly version: 5;
}

export interface RatingModelV6 {
  readonly version: 6;
}

export interface RatingModelV7 {
  readonly version: 7;
}

export type RatingModel =
  | RatingModelV1
  | RatingModelV2
  | RatingModelV3
  | RatingModelV4
  | RatingModelV5
  | RatingModelV6
  | RatingModelV7;

export interface HeroUsage {
  readonly name: string;
  readonly matches: number;
  readonly wins: number;
  readonly stats?: HeroUsageStats;
}

export interface HeroUsageStats {
  readonly kdaMatches: number;
  readonly kills: number;
  readonly deaths: number;
  readonly assists: number;
  readonly economyMatches: number;
  readonly economy: number;
  readonly economyDurationSeconds?: number;
}

export type HeroPools = Readonly<
  Record<ModeFilter, readonly HeroUsage[]>
>;

export interface PlayerStanding {
  readonly id: number;
  readonly name: string;
  readonly initial: string;
  readonly roomLabel: string;
  readonly roomIds: readonly number[];
  readonly aliases: readonly string[];
  readonly trend: number;
  readonly form: readonly MatchResult[];
  readonly modes: Readonly<Record<ModeFilter, Performance>>;
  readonly heroPool: readonly HeroUsage[];
  readonly heroPools?: HeroPools;
}

export interface HeroPerformance {
  readonly matches: number;
  readonly wins: number;
  readonly players: number;
}

export interface HeroSynergy {
  readonly name: string;
  readonly matches: number;
  readonly wins: number;
  readonly delta?: number;
}

export interface HeroSynergyRanking {
  readonly best: readonly HeroSynergy[];
  readonly worst: readonly HeroSynergy[];
}

export interface HeroCounterRanking {
  readonly counters: readonly HeroSynergy[];
  readonly counteredBy: readonly HeroSynergy[];
}

export interface HeroStanding {
  readonly id: string;
  readonly name: string;
  readonly modes: Readonly<Record<ModeFilter, HeroPerformance>>;
  readonly synergies?: Readonly<Record<ModeFilter, HeroSynergyRanking>>;
  readonly counters?: Readonly<Record<ModeFilter, HeroCounterRanking>>;
}

export interface DashboardMatchPlayer {
  readonly slot?: number;
  readonly name: string;
  readonly heroName: string;
  readonly kills: number | null;
  readonly deaths: number | null;
  readonly assists: number | null;
  readonly economy: number | null;
  readonly lastHits: number | null;
  readonly isRecordedPlayer: boolean;
}

export interface DashboardMatchTeam {
  readonly role?: 'ally' | 'enemy';
  readonly side: 'left' | 'right';
  readonly color: 'teal' | 'orange';
  readonly kills: number | null;
  readonly economy: number | null;
  readonly players: readonly DashboardMatchPlayer[];
}

export interface DashboardMatchReplay {
  readonly kind: 'match' | 'full';
  readonly url: string;
}

export interface DashboardMatchResultImage {
  readonly url: string;
  readonly width: number;
  readonly height: number;
}

export interface DashboardMatchRating {
  readonly scope: ModeFilter;
  readonly seasonKey: SeasonKey;
  readonly matchNumber: number;
  readonly scoreBefore: number;
  readonly scoreDelta: number;
  readonly scoreAfter: number;
  readonly provisional: boolean;
  readonly modelVersion: number;
}

export interface DashboardMatch {
  readonly id: number;
  readonly playerId: number;
  readonly seasonKey: Exclude<SeasonKey, 'all-time'>;
  readonly mode: CompetitiveMode;
  readonly playedAt: string;
  readonly durationSeconds: number;
  readonly result: MatchResult;
  readonly streamTitle?: string;
  readonly analysisProvisional?: boolean;
  readonly duplicateOfMatchId: number | null;
  readonly duplicateReviewState: 'none' | 'pending' | 'confirmed' | 'dismissed';
  readonly ally: DashboardMatchTeam;
  readonly enemy: DashboardMatchTeam;
  readonly replay?: DashboardMatchReplay | null;
  readonly replayStatus?: 'available' | 'checking' | 'unavailable';
  readonly resultImage?: DashboardMatchResultImage | null;
  readonly rating?: DashboardMatchRating | null;
}

export interface SeasonStandings {
  readonly players: readonly PlayerStanding[];
  readonly heroes: readonly HeroStanding[];
  readonly environmentHeroes?: readonly HeroStanding[];
}

export interface DashboardSnapshot {
  readonly schemaVersion: 3;
  readonly snapshotId: string;
  readonly contentRevision?: string;
  readonly publicationDate: string;
  readonly generatedAt: string;
  readonly sourceLastMatchId: number;
  readonly sourceMatchCount: number;
  readonly ratingModel: RatingModel;
  readonly currentSeasonKey: SeasonKey;
  readonly seasons: readonly SeasonOption[];
  readonly standings: Readonly<Record<string, SeasonStandings>>;
  readonly matches: readonly DashboardMatch[];
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
  { key: 'all', label: '全部模式', description: '汇总所有已收录模式' },
  { key: '3v3', label: '3V3', description: '三人峡谷对局' },
  {
    key: 'brawl',
    label: '乱斗',
    description: '大乱斗与闪电战合并统计',
  },
  { key: '5v5', label: '5V5', description: '五人战场对局' },
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
    /^\d{4}-(spring|summer|autumn|winter)$/u.test(value)
  );
}
