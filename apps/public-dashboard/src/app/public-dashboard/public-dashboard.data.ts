import { heroSearchSegments } from './public-dashboard.hero-names';
import {
  COMPETITIVE_MODE_OPTIONS,
  DashboardSnapshot,
  DashboardSummary,
  DashboardTrendPublication,
  DashboardTrendStanding,
  DashboardTrends,
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

export const HERO_MIN_MATCHES = 20;
export const OVERVIEW_LIMIT = 10;
export const DETAIL_PAGE_SIZE = 10;

export interface PlayerTrendPoint {
  readonly publicationDate: string;
  readonly rank: number;
  readonly ratingScore: number;
}

export interface PlayerTrend {
  readonly points: readonly PlayerTrendPoint[];
  readonly current: PlayerTrendPoint | null;
  readonly previous: PlayerTrendPoint | null;
  readonly hasBaseline: boolean;
  readonly rankDelta: number | null;
  readonly ratingDelta: number | null;
}

export type RankMovement =
  | { readonly kind: 'pending'; readonly text: '—'; readonly label: string }
  | { readonly kind: 'new'; readonly text: '新'; readonly label: string }
  | { readonly kind: 'same'; readonly text: '—'; readonly label: string }
  | { readonly kind: 'up'; readonly text: string; readonly label: string }
  | { readonly kind: 'down'; readonly text: string; readonly label: string };

const EMPTY_PLAYER_TREND: PlayerTrend = {
  points: [],
  current: null,
  previous: null,
  hasBaseline: false,
  rankDelta: null,
  ratingDelta: null,
};

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
    .filter((player) => player.modes[mode].ratingScore !== null)
    .sort(
      (left, right) =>
        (right.modes[mode].ratingScore ?? 0) -
          (left.modes[mode].ratingScore ?? 0) ||
        right.modes[mode].matches - left.modes[mode].matches ||
        winRate(right.modes[mode]) - winRate(left.modes[mode]) ||
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

function trendStanding(
  publication: DashboardTrendPublication,
  season: SeasonKey,
  mode: ModeFilter,
  playerId: number,
): DashboardTrendStanding | undefined {
  return publication.standings[season]?.[mode].find(
    (standing) => standing.playerId === playerId,
  );
}

export function getPlayerTrend(
  trends: DashboardTrends | null | undefined,
  currentSnapshotId: string,
  season: SeasonKey,
  mode: ModeFilter,
  playerId: number,
): PlayerTrend {
  if (trends === null || trends === undefined) {
    return EMPTY_PLAYER_TREND;
  }
  const currentIndex = trends.publications.findIndex(
    (publication) => publication.snapshotId === currentSnapshotId,
  );
  if (currentIndex < 0) {
    return EMPTY_PLAYER_TREND;
  }
  const currentPublication = trends.publications[currentIndex];
  const currentStanding = trendStanding(
    currentPublication,
    season,
    mode,
    playerId,
  );
  if (currentStanding === undefined) {
    return EMPTY_PLAYER_TREND;
  }

  const points: PlayerTrendPoint[] = [];
  let previous: PlayerTrendPoint | null = null;
  for (let index = 0; index <= currentIndex; index += 1) {
    const publication = trends.publications[index];
    const standing = trendStanding(publication, season, mode, playerId);
    if (standing === undefined) {
      continue;
    }
    const point: PlayerTrendPoint = {
      publicationDate: publication.publicationDate,
      rank: standing.rank,
      ratingScore: standing.ratingScore,
    };
    points.push(point);
    if (index < currentIndex) {
      previous = point;
    }
  }
  const current = points[points.length - 1];
  return {
    points,
    current,
    previous,
    hasBaseline: currentIndex > 0,
    rankDelta: previous === null ? null : previous.rank - current.rank,
    ratingDelta:
      previous === null ? null : current.ratingScore - previous.ratingScore,
  };
}

export function getRankMovement(trend: PlayerTrend): RankMovement {
  if (!trend.hasBaseline) {
    return {
      kind: 'pending',
      text: '—',
      label: '趋势将在下一次数据发布后生成',
    };
  }
  if (trend.previous === null) {
    return {
      kind: 'new',
      text: '新',
      label: '较上次数据发布新上榜',
    };
  }
  if (trend.rankDelta === null || trend.rankDelta === 0) {
    return {
      kind: 'same',
      text: '—',
      label: '较上次数据发布排名不变',
    };
  }
  if (trend.rankDelta > 0) {
    return {
      kind: 'up',
      text: `↑${trend.rankDelta}`,
      label: `较上次数据发布上升 ${trend.rankDelta} 名`,
    };
  }
  return {
    kind: 'down',
    text: `↓${Math.abs(trend.rankDelta)}`,
    label: `较上次数据发布下降 ${Math.abs(trend.rankDelta)} 名`,
  };
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
