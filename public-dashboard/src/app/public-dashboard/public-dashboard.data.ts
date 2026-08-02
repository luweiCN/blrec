import {
  heroesForSeason,
  playersForSeason,
} from './public-dashboard.mock-data';
import { heroSearchSegments } from './public-dashboard.hero-names';
import {
  COMPETITIVE_MODE_OPTIONS,
  DashboardSummary,
  HeroRankingRow,
  HeroStanding,
  HeroUsage,
  ModeBreakdown,
  ModeFilter,
  MODE_OPTIONS,
  Performance,
  PlayerRankingRow,
  PlayerStanding,
  SeasonKey,
  SeasonOption,
  SEASON_OPTIONS,
} from './public-dashboard.models';
import { matchesSearchSegments } from './public-dashboard.search';

export const PLAYER_MIN_MATCHES = 20;
export const HERO_MIN_MATCHES = 20;
export const OVERVIEW_LIMIT = 10;
export const DETAIL_PAGE_SIZE = 10;

export function winRate(value: {
  readonly matches: number;
  readonly wins: number;
}): number {
  return value.matches === 0 ? 0 : value.wins / value.matches;
}

export function getPlayerRankings(
  season: SeasonKey,
  mode: ModeFilter,
): readonly PlayerStanding[] {
  return playersForSeason(season)
    .filter((player) => player.modes[mode].matches >= PLAYER_MIN_MATCHES)
    .sort(
      (left, right) =>
        winRate(right.modes[mode]) - winRate(left.modes[mode]) ||
        right.modes[mode].wins - left.modes[mode].wins ||
        right.modes[mode].matches - left.modes[mode].matches ||
        left.id - right.id,
    );
}

export function getPlayerRankingRows(
  season: SeasonKey,
  mode: ModeFilter,
): readonly PlayerRankingRow[] {
  return getPlayerRankings(season, mode).map((player, index) => ({
    rank: index + 1,
    player,
  }));
}

export function getHeroRankings(
  season: SeasonKey,
  mode: ModeFilter,
): readonly HeroStanding[] {
  return heroesForSeason(season)
    .filter((hero) => hero.modes[mode].matches >= HERO_MIN_MATCHES)
    .sort((left, right) => {
      const leftPerformance = left.modes[mode];
      const rightPerformance = right.modes[mode];
      return (
        winRate(rightPerformance) - winRate(leftPerformance) ||
        rightPerformance.matches - leftPerformance.matches ||
        left.name.localeCompare(right.name)
      );
    });
}

export function getHeroRankingRows(
  season: SeasonKey,
  mode: ModeFilter,
): readonly HeroRankingRow[] {
  return getHeroRankings(season, mode).map((hero, index) => ({
    rank: index + 1,
    hero,
  }));
}

export function getDashboardSummary(
  season: SeasonKey,
  mode: ModeFilter,
): DashboardSummary {
  const players = playersForSeason(season);
  const heroes = heroesForSeason(season);
  const totals = players.reduce(
    (result, player) => ({
      matches: result.matches + player.modes[mode].matches,
      wins: result.wins + player.modes[mode].wins,
    }),
    { matches: 0, wins: 0 },
  );
  return {
    playerCount: players.length,
    matchCount: totals.matches,
    winRate: winRate(totals),
    heroCount: heroes.length,
  };
}

export function getModeBreakdown(
  player: PlayerStanding,
): readonly ModeBreakdown[] {
  const allMatches = player.modes.all.matches;
  return COMPETITIVE_MODE_OPTIONS.map((mode) => ({
    ...mode,
    matches: player.modes[mode.key].matches,
    share: allMatches === 0 ? 0 : player.modes[mode.key].matches / allMatches,
  }));
}

export function playerMatchesQuery(
  player: PlayerStanding,
  query: string,
): boolean {
  return matchesSearchSegments(
    [
      player.name,
      player.roomLabel,
      ...player.aliases,
      ...heroSearchSegments(player.modes.all.topHero),
    ],
    query,
  );
}

export function heroMatchesQuery(hero: HeroStanding, query: string): boolean {
  return matchesSearchSegments(heroSearchSegments(hero.name), query);
}

export function heroImage(heroName: string): string {
  return 'assets/vainglory/heroes/' + heroName.toLowerCase() + '.jpg';
}

export function modeLabel(mode: ModeFilter): string {
  return (
    MODE_OPTIONS.find((option) => option.key === mode)?.label ?? '全部模式'
  );
}

export function seasonOption(season: SeasonKey): SeasonOption {
  return (
    SEASON_OPTIONS.find((option) => option.key === season) ?? SEASON_OPTIONS[0]
  );
}

export function selectedHeroWinRate(hero: HeroUsage): number {
  return winRate(hero);
}

export function performanceForPlayer(
  player: PlayerStanding,
  mode: ModeFilter,
): Performance {
  return player.modes[mode];
}
