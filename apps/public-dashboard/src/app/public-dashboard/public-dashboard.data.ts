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
export const PLAYER_WIN_RATE_MIN_MATCHES = 20;
export const OVERVIEW_LIMIT = 10;
export const DETAIL_PAGE_SIZE = 10;

export type HeroRankingSort = 'win-rate' | 'usage';
export type PlayerRankingSort = 'rating' | 'matches' | 'wins' | 'win-rate';

export type HeroPeerComparison =
  | { readonly kind: 'unavailable' }
  | {
      readonly kind: 'available';
      readonly players: number;
      readonly matches: number;
      readonly winRate: number;
      readonly delta: number;
      readonly kda: HeroPeerMetricComparison;
      readonly economy: HeroPeerMetricComparison;
    };

export type HeroPeerMetricComparison =
  | { readonly kind: 'unavailable' }
  | {
      readonly kind: 'available';
      readonly matches: number;
      readonly peerMatches: number;
      readonly value: number;
      readonly peerValue: number;
      readonly delta: number;
    };

export interface HeroPlayerComparison {
  readonly player: PlayerStanding;
  readonly usage: HeroUsage;
  readonly usageRank: number;
  readonly playerCount: number;
  readonly peers: HeroPeerComparison;
}

export interface PlayerKdaSummary {
  readonly value: number;
  readonly matches: number;
}

export type HeroPeerComparisonKind = 'up' | 'down' | 'same' | 'unavailable';

export function heroPeerComparisonText(comparison: HeroPeerComparison): string {
  if (comparison.kind === 'unavailable') {
    return '暂无其他玩家';
  }
  const percentagePoints = comparison.delta * 100;
  if (Math.abs(percentagePoints) < 0.05) {
    return '与其他玩家持平';
  }
  return `${percentagePoints > 0 ? '高' : '低'} ${Math.abs(percentagePoints).toFixed(1)} 个百分点`;
}

export function heroPeerComparisonKind(
  comparison: HeroPeerComparison,
): HeroPeerComparisonKind {
  if (comparison.kind === 'unavailable') {
    return 'unavailable';
  }
  if (Math.abs(comparison.delta) < 0.0005) {
    return 'same';
  }
  return comparison.delta > 0 ? 'up' : 'down';
}

export function heroKda(usage: HeroUsage): number | null {
  const stats = usage.stats;
  return stats === undefined || stats.kdaMatches === 0
    ? null
    : (stats.kills + stats.assists) / Math.max(1, stats.deaths);
}

export function heroGoldPerMinute(usage: HeroUsage): number | null {
  const stats = usage.stats;
  return stats === undefined ||
    stats.economyMatches === 0 ||
    stats.economyDurationSeconds === undefined ||
    stats.economyDurationSeconds === 0
    ? null
    : (stats.economy * 60) / stats.economyDurationSeconds;
}

export function heroPeerMetricText(
  comparison: HeroPeerMetricComparison,
  fractionDigits: number,
): string {
  if (comparison.kind === 'unavailable') {
    return '暂无对比';
  }
  if (Math.abs(comparison.delta) < 0.5 * 10 ** -fractionDigits) {
    return '持平';
  }
  return `${comparison.delta > 0 ? '高' : '低'} ${Math.abs(comparison.delta).toFixed(fractionDigits)}`;
}

export function heroPeerMetricKind(
  comparison: HeroPeerMetricComparison,
): HeroPeerComparisonKind {
  if (comparison.kind === 'unavailable') {
    return 'unavailable';
  }
  if (Math.abs(comparison.delta) < 0.0005) {
    return 'same';
  }
  return comparison.delta > 0 ? 'up' : 'down';
}

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
  sort: PlayerRankingSort = 'rating',
): readonly PlayerRankingRow[] {
  const players = getPlayerRankings(snapshot, season, mode).filter(
    (player) =>
      sort !== 'win-rate' ||
      player.modes[mode].matches >= PLAYER_WIN_RATE_MIN_MATCHES,
  );
  if (sort !== 'rating') {
    players.sort((left, right) => {
      const leftPerformance = left.modes[mode];
      const rightPerformance = right.modes[mode];
      switch (sort) {
        case 'matches':
          return (
            rightPerformance.matches - leftPerformance.matches ||
            rightPerformance.wins - leftPerformance.wins ||
            (rightPerformance.ratingScore ?? 0) -
              (leftPerformance.ratingScore ?? 0) ||
            left.id - right.id
          );
        case 'wins':
          return (
            rightPerformance.wins - leftPerformance.wins ||
            rightPerformance.matches - leftPerformance.matches ||
            (rightPerformance.ratingScore ?? 0) -
              (leftPerformance.ratingScore ?? 0) ||
            left.id - right.id
          );
        case 'win-rate':
          return (
            winRate(rightPerformance) - winRate(leftPerformance) ||
            rightPerformance.matches - leftPerformance.matches ||
            (rightPerformance.ratingScore ?? 0) -
              (leftPerformance.ratingScore ?? 0) ||
            left.id - right.id
          );
      }
    });
  }
  return players.map((player, index) => ({
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
  sort: HeroRankingSort = 'win-rate',
): readonly HeroStanding[] {
  return heroesForSeason(snapshot, season)
    .filter((hero) =>
      sort === 'win-rate'
        ? hero.modes[mode].matches >= HERO_MIN_MATCHES
        : hero.modes[mode].matches > 0,
    )
    .sort((left, right) => {
      const leftPerformance = left.modes[mode];
      const rightPerformance = right.modes[mode];
      if (sort === 'usage') {
        return (
          rightPerformance.matches - leftPerformance.matches ||
          rightPerformance.players - leftPerformance.players ||
          winRate(rightPerformance) - winRate(leftPerformance) ||
          left.name.localeCompare(right.name)
        );
      }
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
  sort: HeroRankingSort = 'win-rate',
): readonly HeroRankingRow[] {
  return getHeroRankings(snapshot, season, mode, sort).map((hero, index) => ({
    rank: index + 1,
    hero,
  }));
}

export function heroPoolForMode(
  player: PlayerStanding,
  mode: ModeFilter,
): readonly HeroUsage[] {
  return player.heroPools?.[mode] ?? (mode === 'all' ? player.heroPool : []);
}

export function playerKdaForMode(
  player: PlayerStanding,
  mode: ModeFilter,
): PlayerKdaSummary | null {
  const totals = heroPoolForMode(player, mode).reduce(
    (result, usage) => {
      const stats = usage.stats;
      return stats === undefined || stats.kdaMatches === 0
        ? result
        : {
            matches: result.matches + stats.kdaMatches,
            kills: result.kills + stats.kills,
            deaths: result.deaths + stats.deaths,
            assists: result.assists + stats.assists,
          };
    },
    { matches: 0, kills: 0, deaths: 0, assists: 0 },
  );
  return totals.matches === 0
    ? null
    : {
        value: (totals.kills + totals.assists) / Math.max(1, totals.deaths),
        matches: totals.matches,
      };
}

export function getHeroPlayerComparisons(
  snapshot: DashboardSnapshot,
  season: SeasonKey,
  mode: ModeFilter,
  heroName: string,
): readonly HeroPlayerComparison[] {
  const normalizedName = heroName.toLocaleLowerCase();
  const usages: {
    readonly player: PlayerStanding;
    readonly usage: HeroUsage;
  }[] = [];
  for (const player of playersForSeason(snapshot, season)) {
    const usage = heroPoolForMode(player, mode).find(
      (candidate) => candidate.name.toLocaleLowerCase() === normalizedName,
    );
    if (usage !== undefined) {
      usages.push({ player, usage });
    }
  }
  usages.sort(
    (left, right) =>
      right.usage.matches - left.usage.matches ||
      right.usage.wins - left.usage.wins ||
      left.player.id - right.player.id,
  );
  const totalMatches = usages.reduce(
    (total, record) => total + record.usage.matches,
    0,
  );
  const totalWins = usages.reduce(
    (total, record) => total + record.usage.wins,
    0,
  );
  const totalStats = usages.reduce(
    (total, record) => ({
      kdaMatches: total.kdaMatches + (record.usage.stats?.kdaMatches ?? 0),
      kills: total.kills + (record.usage.stats?.kills ?? 0),
      deaths: total.deaths + (record.usage.stats?.deaths ?? 0),
      assists: total.assists + (record.usage.stats?.assists ?? 0),
      economyMatches:
        total.economyMatches + (record.usage.stats?.economyMatches ?? 0),
      economy: total.economy + (record.usage.stats?.economy ?? 0),
      economyDurationSeconds:
        total.economyDurationSeconds +
        (record.usage.stats?.economyDurationSeconds ?? 0),
    }),
    {
      kdaMatches: 0,
      kills: 0,
      deaths: 0,
      assists: 0,
      economyMatches: 0,
      economy: 0,
      economyDurationSeconds: 0,
    },
  );
  return usages.map((record, index) => {
    const peerMatches = totalMatches - record.usage.matches;
    const peerWins = totalWins - record.usage.wins;
    const stats = record.usage.stats;
    const peerKdaMatches = totalStats.kdaMatches - (stats?.kdaMatches ?? 0);
    const peerKills = totalStats.kills - (stats?.kills ?? 0);
    const peerDeaths = totalStats.deaths - (stats?.deaths ?? 0);
    const peerAssists = totalStats.assists - (stats?.assists ?? 0);
    const peerEconomyMatches =
      totalStats.economyMatches - (stats?.economyMatches ?? 0);
    const peerEconomy = totalStats.economy - (stats?.economy ?? 0);
    const peerEconomyDurationSeconds =
      totalStats.economyDurationSeconds -
      (stats?.economyDurationSeconds ?? 0);
    const kda = heroKda(record.usage);
    const goldPerMinute = heroGoldPerMinute(record.usage);
    const peerKda =
      peerKdaMatches === 0
        ? null
        : (peerKills + peerAssists) / Math.max(1, peerDeaths);
    const peerGoldPerMinute =
      peerEconomyMatches === 0 || peerEconomyDurationSeconds === 0
        ? null
        : (peerEconomy * 60) / peerEconomyDurationSeconds;
    return {
      ...record,
      usageRank: index + 1,
      playerCount: usages.length,
      peers:
        peerMatches === 0
          ? { kind: 'unavailable' }
          : {
              kind: 'available',
              players: usages.length - 1,
              matches: peerMatches,
              winRate: peerWins / peerMatches,
              delta: winRate(record.usage) - peerWins / peerMatches,
              kda:
                kda === null || peerKda === null
                  ? { kind: 'unavailable' }
                  : {
                      kind: 'available',
                      matches: stats?.kdaMatches ?? 0,
                      peerMatches: peerKdaMatches,
                      value: kda,
                      peerValue: peerKda,
                      delta: kda - peerKda,
                    },
              economy:
                goldPerMinute === null || peerGoldPerMinute === null
                  ? { kind: 'unavailable' }
                  : {
                      kind: 'available',
                      matches: stats?.economyMatches ?? 0,
                      peerMatches: peerEconomyMatches,
                      value: goldPerMinute,
                      peerValue: peerGoldPerMinute,
                      delta: goldPerMinute - peerGoldPerMinute,
                    },
            },
    };
  });
}

export function getPlayerHeroComparisons(
  snapshot: DashboardSnapshot,
  season: SeasonKey,
  mode: ModeFilter,
  playerId: number,
): readonly HeroPlayerComparison[] {
  const player = playerForSeason(snapshot, season, playerId);
  if (player === undefined) {
    return [];
  }
  const comparisons: HeroPlayerComparison[] = [];
  for (const usage of heroPoolForMode(player, mode)) {
    const comparison = getHeroPlayerComparisons(
      snapshot,
      season,
      mode,
      usage.name,
    ).find((record) => record.player.id === playerId);
    if (comparison !== undefined) {
      comparisons.push(comparison);
    }
  }
  return comparisons;
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
