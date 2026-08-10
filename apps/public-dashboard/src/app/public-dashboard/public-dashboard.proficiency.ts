import {
  getHeroPlayerComparisons,
  heroAverageEconomy,
  heroKda,
  heroPoolForMode,
  playerForSeason,
  winRate,
} from './public-dashboard.data';
import {
  DashboardSnapshot,
  ModeFilter,
  SeasonKey,
} from './public-dashboard.models';

import type { HeroPlayerComparison } from './public-dashboard.data';

const WIN_RATE_PRIOR_MATCHES = 8;
const EXPERIENCE_SCALE_MATCHES = 20;
const METRIC_PRIOR_MATCHES = 5;

const EXPERIENCE_WEIGHT = 0.35;
const WIN_RATE_WEIGHT = 0.4;
const KDA_WEIGHT = 0.15;
const ECONOMY_WEIGHT = 0.1;

export type HeroProficiencyLevel =
  | '大师'
  | '精通'
  | '熟练'
  | '常用'
  | '初试';

export type PlayerHeroSort = 'proficiency' | 'usage' | 'win-rate' | 'kda';

export interface HeroProficiency extends HeroPlayerComparison {
  readonly score: number;
  readonly level: HeroProficiencyLevel;
}

export function heroProficiencyLevel(score: number): HeroProficiencyLevel {
  if (score >= 75) {
    return '大师';
  }
  if (score >= 65) {
    return '精通';
  }
  if (score >= 55) {
    return '熟练';
  }
  if (score >= 45) {
    return '常用';
  }
  return '初试';
}

export function getHeroProficiencies(
  snapshot: DashboardSnapshot,
  season: SeasonKey,
  mode: ModeFilter,
  heroName: string,
): readonly HeroProficiency[] {
  const records = getHeroPlayerComparisons(snapshot, season, mode, heroName);
  const kdaValues = records
    .map((record) => heroKda(record.usage))
    .filter(isNumber);
  const economyValues = records
    .map((record) => heroAverageEconomy(record.usage))
    .filter(isNumber);

  return records
    .map((record) => {
      const stats = record.usage.stats;
      const experience =
        1 - Math.exp(-record.usage.matches / EXPERIENCE_SCALE_MATCHES);
      const stableWinRate =
        (record.usage.wins + 0.5 * WIN_RATE_PRIOR_MATCHES) /
        (record.usage.matches + WIN_RATE_PRIOR_MATCHES);
      const kdaPercentile = stablePercentile(
        heroKda(record.usage),
        stats?.kdaMatches ?? 0,
        kdaValues,
      );
      const economyPercentile = stablePercentile(
        heroAverageEconomy(record.usage),
        stats?.economyMatches ?? 0,
        economyValues,
      );
      const score = Math.round(
        100 *
          (experience * EXPERIENCE_WEIGHT +
            stableWinRate * WIN_RATE_WEIGHT +
            kdaPercentile * KDA_WEIGHT +
            economyPercentile * ECONOMY_WEIGHT),
      );
      return {
        ...record,
        score,
        level: heroProficiencyLevel(score),
      };
    })
    .sort(compareProficiency);
}

export function getHeroProficiencyLeader(
  snapshot: DashboardSnapshot,
  season: SeasonKey,
  mode: ModeFilter,
  heroName: string,
): HeroProficiency | null {
  return getHeroProficiencies(snapshot, season, mode, heroName)[0] ?? null;
}

export function getPlayerHeroProficiencies(
  snapshot: DashboardSnapshot,
  season: SeasonKey,
  mode: ModeFilter,
  playerId: number,
  sort: PlayerHeroSort = 'proficiency',
): readonly HeroProficiency[] {
  const player = playerForSeason(snapshot, season, playerId);
  if (player === undefined) {
    return [];
  }
  const proficiencies: HeroProficiency[] = [];
  for (const usage of heroPoolForMode(player, mode)) {
    const proficiency = getHeroProficiencies(
      snapshot,
      season,
      mode,
      usage.name,
    ).find((record) => record.player.id === playerId);
    if (proficiency !== undefined) {
      proficiencies.push(proficiency);
    }
  }
  return proficiencies.sort((left, right) =>
    comparePlayerHeroProficiency(left, right, sort),
  );
}

function stablePercentile(
  value: number | null,
  matches: number,
  population: readonly number[],
): number {
  if (value === null) {
    return 0.5;
  }
  const confidence = matches / (matches + METRIC_PRIOR_MATCHES);
  return 0.5 + (percentile(value, population) - 0.5) * confidence;
}

function percentile(value: number, population: readonly number[]): number {
  if (population.length <= 1) {
    return 0.5;
  }
  const lower = population.filter((candidate) => candidate < value).length;
  const equal = population.filter((candidate) => candidate === value).length;
  return (lower + (equal - 1) / 2) / (population.length - 1);
}

function compareProficiency(
  left: HeroProficiency,
  right: HeroProficiency,
): number {
  return (
    right.score - left.score ||
    right.usage.matches - left.usage.matches ||
    winRate(right.usage) - winRate(left.usage) ||
    left.player.id - right.player.id
  );
}

function comparePlayerHeroProficiency(
  left: HeroProficiency,
  right: HeroProficiency,
  sort: PlayerHeroSort,
): number {
  switch (sort) {
    case 'proficiency':
      return (
        compareProficiency(left, right) ||
        left.usage.name.localeCompare(right.usage.name)
      );
    case 'usage':
      return (
        right.usage.matches - left.usage.matches ||
        right.score - left.score ||
        winRate(right.usage) - winRate(left.usage) ||
        left.usage.name.localeCompare(right.usage.name)
      );
    case 'win-rate':
      return (
        winRate(right.usage) - winRate(left.usage) ||
        right.usage.matches - left.usage.matches ||
        right.score - left.score ||
        left.usage.name.localeCompare(right.usage.name)
      );
    case 'kda':
      return (
        (heroKda(right.usage) ?? Number.NEGATIVE_INFINITY) -
          (heroKda(left.usage) ?? Number.NEGATIVE_INFINITY) ||
        (right.usage.stats?.kdaMatches ?? 0) -
          (left.usage.stats?.kdaMatches ?? 0) ||
        right.usage.matches - left.usage.matches ||
        left.usage.name.localeCompare(right.usage.name)
      );
  }
}

function isNumber(value: number | null): value is number {
  return value !== null;
}
