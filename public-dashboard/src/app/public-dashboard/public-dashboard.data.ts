import { heroSearchSegments } from './public-dashboard.hero-names';
import {
  COMPETITIVE_MODE_OPTIONS,
  DashboardSnapshot,
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
  snapshot: DashboardSnapshot,
  season: SeasonKey,
  mode: ModeFilter,
): readonly PlayerStanding[] {
  return playersForSeason(snapshot, season)
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
  snapshot: DashboardSnapshot,
  season: SeasonKey,
  mode: ModeFilter,
): readonly PlayerRankingRow[] {
  return getPlayerRankings(snapshot, season, mode).map((player, index) => ({
    rank: index + 1,
    player,
  }));
}

export function getHeroRankings(
  snapshot: DashboardSnapshot,
  season: SeasonKey,
  mode: ModeFilter,
): readonly HeroStanding[] {
  return heroesForSeason(snapshot, season)
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
  snapshot: DashboardSnapshot,
  season: SeasonKey,
  mode: ModeFilter,
): readonly HeroRankingRow[] {
  return getHeroRankings(snapshot, season, mode).map((hero, index) => ({
    rank: index + 1,
    hero,
  }));
}

export function getDashboardSummary(
  snapshot: DashboardSnapshot,
  season: SeasonKey,
  mode: ModeFilter,
): DashboardSummary {
  const players = playersForSeason(snapshot, season);
  const heroes = heroesForSeason(snapshot, season);
  const totals = players.reduce(
    (result, player) => ({
      matches: result.matches + player.modes[mode].matches,
      wins: result.wins + player.modes[mode].wins,
    }),
    { matches: 0, wins: 0 },
  );
  return {
    playerCount: players.filter((player) => player.modes[mode].matches > 0)
      .length,
    matchCount: totals.matches,
    winRate: winRate(totals),
    heroCount: heroes.filter((hero) => hero.modes[mode].matches > 0).length,
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
    [player.name, player.roomLabel, ...player.aliases],
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

export function seasonOption(
  snapshot: DashboardSnapshot,
  season: SeasonKey,
): SeasonOption {
  return (
    snapshot.seasons.find((option) => option.key === season) ??
    snapshot.seasons[0]
  );
}

export function playersForSeason(
  snapshot: DashboardSnapshot,
  season: SeasonKey,
): readonly PlayerStanding[] {
  return snapshot.standings[season]?.players ?? [];
}

export function playerForSeason(
  snapshot: DashboardSnapshot,
  season: SeasonKey,
  playerId: number,
): PlayerStanding | undefined {
  return playersForSeason(snapshot, season).find(
    (player) => player.id === playerId,
  );
}

export function findPlayer(
  snapshot: DashboardSnapshot,
  playerId: number,
): PlayerStanding | undefined {
  for (const season of snapshot.seasons) {
    const player = playerForSeason(snapshot, season.key, playerId);
    if (player !== undefined) {
      return player;
    }
  }
  return undefined;
}

export function heroesForSeason(
  snapshot: DashboardSnapshot,
  season: SeasonKey,
): readonly HeroStanding[] {
  return snapshot.standings[season]?.heroes ?? [];
}

export function heroForSeason(
  snapshot: DashboardSnapshot,
  season: SeasonKey,
  heroId: string,
): HeroStanding | undefined {
  const normalizedId = heroId.toLocaleLowerCase();
  return heroesForSeason(snapshot, season).find(
    (hero) =>
      hero.id.toLocaleLowerCase() === normalizedId ||
      hero.name.toLocaleLowerCase() === normalizedId,
  );
}

export function findHero(
  snapshot: DashboardSnapshot,
  heroId: string,
): HeroStanding | undefined {
  for (const season of snapshot.seasons) {
    const hero = heroForSeason(snapshot, season.key, heroId);
    if (hero !== undefined) {
      return hero;
    }
  }
  return undefined;
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
