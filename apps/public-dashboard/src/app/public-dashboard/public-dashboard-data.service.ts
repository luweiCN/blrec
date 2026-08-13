import { Injectable } from '@angular/core';
import { Subject } from 'rxjs';

import { environment } from '../../environments/environment';
import {
  DashboardManifest,
  DashboardMatch,
  DashboardMatchPlayer,
  DashboardMatchReplay,
  DashboardMatchTeam,
  DashboardSnapshot,
  DashboardTrendPublication,
  DashboardTrends,
  HeroPerformance,
  HeroCounterRanking,
  HeroStanding,
  HeroSynergy,
  HeroSynergyRanking,
  HeroUsage,
  HeroUsageStats,
  isModeFilter,
  isSeasonKey,
  MODE_FILTERS,
  Performance,
  PlayerStanding,
  RatingForecast,
  RatingGoalForecast,
  RatingModel,
  SeasonOption,
} from './public-dashboard.models';

const MAX_TREND_PUBLICATIONS = 180;

export type DashboardLoadState =
  | { readonly kind: 'loading' }
  | {
      readonly kind: 'ready';
      readonly source: 'api' | 'static';
      readonly manifest: DashboardManifest | null;
      readonly snapshot: DashboardSnapshot;
      readonly trends: DashboardTrends | null;
    }
  | { readonly kind: 'error'; readonly message: string };

@Injectable({ providedIn: 'root' })
export class DashboardDataService {
  state: DashboardLoadState = { kind: 'loading' };
  private readonly revisionSubject = new Subject<string>();
  readonly revision$ = this.revisionSubject.asObservable();
  private refreshPromise: Promise<boolean> | null = null;

  get snapshot(): DashboardSnapshot {
    if (this.state.kind !== 'ready') {
      throw new Error('dashboard data is not ready');
    }
    return this.state.snapshot;
  }

  get snapshotOrNull(): DashboardSnapshot | null {
    return this.state.kind === 'ready' ? this.state.snapshot : null;
  }

  get trends(): DashboardTrends | null {
    return this.state.kind === 'ready' ? this.state.trends : null;
  }

  async load(): Promise<void> {
    const previousRevision = this.readyRevision();
    this.state = { kind: 'loading' };
    const apiBaseUrl = environment.apiBaseUrl.replace(/\/+$/u, '');
    if (apiBaseUrl !== '') {
      try {
        const document = parseDashboardApiDocument(
          await fetchJson(`${apiBaseUrl}/dashboard`, 'no-cache'),
        );
        this.state = {
          kind: 'ready',
          source: 'api',
          manifest: null,
          snapshot: document.snapshot,
          trends: document.trends,
        };
        this.emitRevisionIfChanged(previousRevision);
        return;
      } catch (error: unknown) {
        console.warn(
          'Unable to load dashboard API, falling back to static data',
          error,
        );
      }
    }
    try {
      const baseUrl = environment.dataBaseUrl.replace(/\/+$/u, '');
      const manifest = parseManifest(
        await fetchJson(`${baseUrl}/manifest.json`, 'no-store'),
      );
      if (!/^snapshots\/[a-zA-Z0-9-]+\.json$/u.test(manifest.snapshotPath)) {
        throw new Error('dashboard manifest contains an invalid snapshot path');
      }
      const snapshot = parseSnapshot(
        await fetchJson(`${baseUrl}/${manifest.snapshotPath}`, 'force-cache'),
      );
      if (snapshot.snapshotId !== manifest.snapshotId) {
        throw new Error('dashboard manifest and snapshot do not match');
      }
      const trends = await loadTrends(baseUrl, snapshot.snapshotId);
      this.state = {
        kind: 'ready',
        source: 'static',
        manifest,
        snapshot,
        trends,
      };
      this.emitRevisionIfChanged(previousRevision);
    } catch (error: unknown) {
      console.error('Unable to load dashboard data', error);
      this.state = {
        kind: 'error',
        message: '排行榜数据暂时无法加载，请稍后刷新。',
      };
    }
  }

  refresh(): Promise<boolean> {
    if (this.refreshPromise !== null) {
      return this.refreshPromise;
    }
    this.refreshPromise = this.refreshReadyState().finally(() => {
      this.refreshPromise = null;
    });
    return this.refreshPromise;
  }

  private async refreshReadyState(): Promise<boolean> {
    if (this.state.kind !== 'ready') {
      return false;
    }
    const previous = this.state;
    try {
      const apiBaseUrl = environment.apiBaseUrl.replace(/\/+$/u, '');
      let next: Extract<DashboardLoadState, { readonly kind: 'ready' }>;
      if (apiBaseUrl !== '') {
        const document = parseDashboardApiDocument(
          await fetchJson(`${apiBaseUrl}/dashboard`, 'no-cache'),
        );
        next = {
          kind: 'ready',
          source: 'api',
          manifest: null,
          snapshot: document.snapshot,
          trends: document.trends,
        };
      } else {
        const baseUrl = environment.dataBaseUrl.replace(/\/+$/u, '');
        const manifest = parseManifest(
          await fetchJson(`${baseUrl}/manifest.json`, 'no-store'),
        );
        if (!/^snapshots\/[a-zA-Z0-9-]+\.json$/u.test(manifest.snapshotPath)) {
          throw new Error('dashboard manifest contains an invalid snapshot path');
        }
        if (manifest.snapshotId === previous.snapshot.snapshotId) {
          return false;
        }
        const snapshot = parseSnapshot(
          await fetchJson(`${baseUrl}/${manifest.snapshotPath}`, 'force-cache'),
        );
        if (snapshot.snapshotId !== manifest.snapshotId) {
          throw new Error('dashboard manifest and snapshot do not match');
        }
        next = {
          kind: 'ready',
          source: 'static',
          manifest,
          snapshot,
          trends: await loadTrends(baseUrl, snapshot.snapshotId),
        };
      }
      const previousRevision =
        previous.snapshot.contentRevision ?? previous.snapshot.snapshotId;
      const nextRevision = next.snapshot.contentRevision ?? next.snapshot.snapshotId;
      if (nextRevision === previousRevision) {
        return false;
      }
      this.state = next;
      this.revisionSubject.next(nextRevision);
      return true;
    } catch (error: unknown) {
      console.warn('Unable to refresh dashboard data', error);
      return false;
    }
  }

  private readyRevision(): string | null {
    if (this.state.kind !== 'ready') {
      return null;
    }
    return this.state.snapshot.contentRevision ?? this.state.snapshot.snapshotId;
  }

  private emitRevisionIfChanged(previousRevision: string | null): void {
    const nextRevision = this.readyRevision();
    if (nextRevision !== null && nextRevision !== previousRevision) {
      this.revisionSubject.next(nextRevision);
    }
  }
}

function parseDashboardApiDocument(value: unknown): {
  readonly snapshot: DashboardSnapshot;
  readonly trends: DashboardTrends;
} {
  if (!isObject(value)) {
    throw new Error('dashboard API returned an unsupported response');
  }
  const snapshot = parseSnapshot(value['snapshot']);
  const trends = parseTrends(value['trends']);
  if (
    !trends.publications.some(
      (publication) => publication.snapshotId === snapshot.snapshotId,
    )
  ) {
    throw new Error('dashboard API snapshot and trends do not match');
  }
  return { snapshot, trends };
}

async function fetchJson(
  url: string,
  cache: RequestCache,
): Promise<unknown> {
  const response = await fetch(url, { cache });
  if (!response.ok) {
    throw new Error(`fetch ${url} failed with ${response.status}`);
  }
  return response.json() as Promise<unknown>;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function isStringArray(value: unknown): value is readonly string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function isPositiveIntegerArray(value: unknown): value is readonly number[] {
  return (
    Array.isArray(value) &&
    value.every((item) => isNonNegativeInteger(item) && item > 0) &&
    new Set(value).size === value.length
  );
}

function isSeasonOption(value: unknown): value is SeasonOption {
  if (!isObject(value)) {
    return false;
  }
  return (
    typeof value['key'] === 'string' &&
    isSeasonKey(value['key']) &&
    typeof value['label'] === 'string' &&
    typeof value['shortLabel'] === 'string' &&
    typeof value['period'] === 'string' &&
    typeof value['current'] === 'boolean'
  );
}

function isPerformance(value: unknown): value is Performance {
  if (!isObject(value)) {
    return false;
  }
  return (
    isNonNegativeInteger(value['matches']) &&
    isNonNegativeInteger(value['wins']) &&
    value['wins'] <= value['matches'] &&
    typeof value['topHero'] === 'string' &&
    (value['ratingScore'] === null ||
      (isNonNegativeInteger(value['ratingScore']) &&
        value['ratingScore'] <= 1000)) &&
    typeof value['provisional'] === 'boolean' &&
    (value['matches'] === 0) === (value['ratingScore'] === null) &&
    (value['ratingForecast'] === undefined ||
      (value['ratingScore'] === null
        ? value['ratingForecast'] === null
        : isRatingForecast(value['ratingForecast'], value['ratingScore'])))
  );
}

function isRatingGoalForecast(value: unknown): value is RatingGoalForecast {
  if (!isObject(value)) {
    return false;
  }
  return (
    isNonNegativeInteger(value['targetDisplayScore']) &&
    value['targetDisplayScore'] <= 3000 &&
    isNonNegativeInteger(value['allWinMatches']) &&
    (value['currentWinRateMatches'] === null ||
      isNonNegativeInteger(value['currentWinRateMatches']))
  );
}

function isRatingForecast(
  value: unknown,
  currentScore: number,
): value is RatingForecast {
  if (!isObject(value)) {
    return false;
  }
  return (
    isNonNegativeInteger(value['nextWinScore']) &&
    value['nextWinScore'] <= 1000 &&
    value['nextWinScore'] >= currentScore &&
    isNonNegativeInteger(value['nextLossScore']) &&
    value['nextLossScore'] <= currentScore &&
    (value['nextDivision'] === null ||
      isRatingGoalForecast(value['nextDivision'])) &&
    (value['nextTier'] === null || isRatingGoalForecast(value['nextTier'])) &&
    isRatingGoalForecast(value['ultimate'])
  );
}

function isRatingModel(value: unknown): value is RatingModel {
  if (!isObject(value)) {
    return false;
  }
  if (value['version'] === 3) {
    return (
      value['priorMatches'] === 20 &&
      value['carryoverMatchCap'] === 200 &&
      value['provisionalMatches'] === 5 &&
      value['neutralDisplayScore'] === 1200 &&
      value['seasonResetDisplayScore'] === 1000 &&
      value['probabilityScale'] === 1800 &&
      value['minimumOutcomeDelta'] === 1 &&
      value['catchupRate'] === 0.08 &&
      value['catchupLimit'] === 45 &&
      value['catchupProtectionGap'] === 150 &&
      value['catchupLossMultiplier'] === 0.5
    );
  }
  const legacyModel =
    value['priorMatches'] === 20 &&
    value['carryoverRate'] === 0.25 &&
    value['credibleLevel'] === 0.9 &&
    value['provisionalMatches'] === 5;
  return (
    legacyModel &&
    (value['version'] === 1 ||
      (value['version'] === 2 && value['minimumOutcomeDelta'] === 1))
  );
}

function isHeroPerformance(value: unknown): value is HeroPerformance {
  if (!isObject(value)) {
    return false;
  }
  return (
    isNonNegativeInteger(value['matches']) &&
    isNonNegativeInteger(value['wins']) &&
    value['wins'] <= value['matches'] &&
    isNonNegativeInteger(value['players'])
  );
}

function hasModes(
  value: unknown,
  validate: (performance: unknown) => boolean,
): boolean {
  return (
    isObject(value) &&
    MODE_FILTERS.every((mode) => validate(value[mode]))
  );
}

function isHeroUsage(value: unknown): value is HeroUsage {
  if (!isObject(value)) {
    return false;
  }
  return (
    typeof value['name'] === 'string' &&
    isNonNegativeInteger(value['matches']) &&
    isNonNegativeInteger(value['wins']) &&
    value['wins'] <= value['matches'] &&
    (value['stats'] === undefined ||
      isHeroUsageStats(value['stats'], Number(value['matches'])))
  );
}

function isHeroUsageStats(
  value: unknown,
  maximumMatches: number,
): value is HeroUsageStats {
  if (!isObject(value)) {
    return false;
  }
  return (
    isNonNegativeInteger(value['kdaMatches']) &&
    isNonNegativeInteger(value['kills']) &&
    isNonNegativeInteger(value['deaths']) &&
    isNonNegativeInteger(value['assists']) &&
    isNonNegativeInteger(value['economyMatches']) &&
    isNonNegativeInteger(value['economy']) &&
    (value['economyDurationSeconds'] === undefined ||
      isNonNegativeInteger(value['economyDurationSeconds'])) &&
    value['kdaMatches'] <= maximumMatches &&
    value['economyMatches'] <= maximumMatches
  );
}

function isHeroUsageList(value: unknown): value is readonly HeroUsage[] {
  return Array.isArray(value) && value.every(isHeroUsage);
}

function isPlayerStanding(value: unknown): value is PlayerStanding {
  if (!isObject(value)) {
    return false;
  }
  return (
    isNonNegativeInteger(value['id']) &&
    typeof value['name'] === 'string' &&
    value['name'].length > 0 &&
    typeof value['initial'] === 'string' &&
    typeof value['roomLabel'] === 'string' &&
    isPositiveIntegerArray(value['roomIds']) &&
    isStringArray(value['aliases']) &&
    Number.isInteger(value['trend']) &&
    Array.isArray(value['form']) &&
    value['form'].every((result) => result === 'W' || result === 'L') &&
    hasModes(value['modes'], isPerformance) &&
    isHeroUsageList(value['heroPool']) &&
    (value['heroPools'] === undefined ||
      hasModes(value['heroPools'], isHeroUsageList))
  );
}

function isHeroSynergy(value: unknown): value is HeroSynergy {
  return (
    isObject(value) &&
    typeof value['name'] === 'string' &&
    value['name'].length > 0 &&
    isNonNegativeInteger(value['matches']) &&
    isNonNegativeInteger(value['wins']) &&
    value['wins'] <= value['matches'] &&
    (value['delta'] === undefined ||
      (typeof value['delta'] === 'number' &&
        Number.isFinite(value['delta']) &&
        Math.abs(value['delta']) <= 1))
  );
}

function isHeroCounterRanking(value: unknown): value is HeroCounterRanking {
  return (
    isObject(value) &&
    Array.isArray(value['counters']) &&
    value['counters'].every(isHeroSynergy) &&
    Array.isArray(value['counteredBy']) &&
    value['counteredBy'].every(isHeroSynergy)
  );
}

function isHeroSynergyRanking(value: unknown): value is HeroSynergyRanking {
  return (
    isObject(value) &&
    Array.isArray(value['best']) &&
    value['best'].every(isHeroSynergy) &&
    Array.isArray(value['worst']) &&
    value['worst'].every(isHeroSynergy)
  );
}

function isHeroStanding(value: unknown): value is HeroStanding {
  if (!isObject(value)) {
    return false;
  }
  return (
    typeof value['id'] === 'string' &&
    typeof value['name'] === 'string' &&
    hasModes(value['modes'], isHeroPerformance) &&
    (value['synergies'] === undefined ||
      hasModes(value['synergies'], isHeroSynergyRanking)) &&
    (value['counters'] === undefined ||
      hasModes(value['counters'], isHeroCounterRanking))
  );
}

function isNullableNonNegativeInteger(value: unknown): boolean {
  return value === null || isNonNegativeInteger(value);
}

function isMatchPlayer(value: unknown): value is DashboardMatchPlayer {
  return (
    isObject(value) &&
    (value['slot'] === undefined ||
      (isNonNegativeInteger(value['slot']) && value['slot'] > 0)) &&
    typeof value['name'] === 'string' &&
    value['name'].length > 0 &&
    typeof value['heroName'] === 'string' &&
    isNullableNonNegativeInteger(value['kills']) &&
    isNullableNonNegativeInteger(value['deaths']) &&
    isNullableNonNegativeInteger(value['assists']) &&
    isNullableNonNegativeInteger(value['economy']) &&
    isNullableNonNegativeInteger(value['lastHits']) &&
    typeof value['isRecordedPlayer'] === 'boolean'
  );
}

function isMatchTeam(value: unknown): value is DashboardMatchTeam {
  return (
    isObject(value) &&
    (value['role'] === undefined ||
      value['role'] === 'ally' ||
      value['role'] === 'enemy') &&
    (value['side'] === 'left' || value['side'] === 'right') &&
    (value['color'] === 'teal' || value['color'] === 'orange') &&
    isNullableNonNegativeInteger(value['kills']) &&
    isNullableNonNegativeInteger(value['economy']) &&
    Array.isArray(value['players']) &&
    value['players'].length <= 5 &&
    value['players'].every(isMatchPlayer)
  );
}

function isMatchResultImage(value: unknown): boolean {
  return (
    isObject(value) &&
    typeof value['url'] === 'string' &&
    /^https:\/\/vg\.luwei\.host\/data\/match-images\/[0-9]{3,}\/[1-9][0-9]*-[0-9a-f]{16}\.webp$/u.test(
      value['url'],
    ) &&
    isNonNegativeInteger(value['width']) &&
    value['width'] > 0 &&
    isNonNegativeInteger(value['height']) &&
    value['height'] > 0
  );
}

function isMatchRating(value: unknown): boolean {
  return (
    isObject(value) &&
    typeof value['scope'] === 'string' &&
    isModeFilter(value['scope']) &&
    typeof value['seasonKey'] === 'string' &&
    isSeasonKey(value['seasonKey']) &&
    isNonNegativeInteger(value['matchNumber']) &&
    value['matchNumber'] > 0 &&
    isNonNegativeInteger(value['scoreBefore']) &&
    isNonNegativeInteger(value['scoreAfter']) &&
    Number.isInteger(value['scoreDelta']) &&
    Number(value['scoreAfter']) ===
      Number(value['scoreBefore']) + Number(value['scoreDelta']) &&
    typeof value['provisional'] === 'boolean' &&
    isNonNegativeInteger(value['modelVersion']) &&
    value['modelVersion'] > 0
  );
}

function isMatchReplay(value: unknown): value is DashboardMatchReplay {
  return (
    isObject(value) &&
    (value['kind'] === 'match' || value['kind'] === 'full') &&
    typeof value['url'] === 'string' &&
    /^https:\/\/www\.bilibili\.com\/video\/[0-9A-Za-z]{10,20}(?:\?p=\d+&t=\d+)?$/u.test(
      value['url'],
    )
  );
}

export function isDashboardMatch(value: unknown): value is DashboardMatch {
  return (
    isObject(value) &&
    isNonNegativeInteger(value['id']) &&
    value['id'] > 0 &&
    isNonNegativeInteger(value['playerId']) &&
    value['playerId'] > 0 &&
    typeof value['seasonKey'] === 'string' &&
    value['seasonKey'] !== 'all-time' &&
    isSeasonKey(value['seasonKey']) &&
    (value['mode'] === '3v3' ||
      value['mode'] === 'brawl' ||
      value['mode'] === '5v5') &&
    typeof value['playedAt'] === 'string' &&
    !Number.isNaN(Date.parse(value['playedAt'])) &&
    isNonNegativeInteger(value['durationSeconds']) &&
    (value['result'] === 'W' || value['result'] === 'L') &&
    (value['streamTitle'] === undefined ||
      typeof value['streamTitle'] === 'string') &&
    isMatchTeam(value['ally']) &&
    isMatchTeam(value['enemy']) &&
    value['ally'].side !== value['enemy'].side &&
    (value['replay'] === undefined ||
      value['replay'] === null ||
      isMatchReplay(value['replay'])) &&
    (value['resultImage'] === undefined ||
      value['resultImage'] === null ||
      isMatchResultImage(value['resultImage'])) &&
    (value['rating'] === undefined ||
      value['rating'] === null ||
      isMatchRating(value['rating']))
  );
}

function isTrendStandingList(value: unknown): boolean {
  if (!Array.isArray(value)) {
    return false;
  }
  const playerIds = new Set<number>();
  return value.every((standing, index) => {
    if (
      !isObject(standing) ||
      !isNonNegativeInteger(standing['playerId']) ||
      standing['playerId'] === 0 ||
      playerIds.has(standing['playerId']) ||
      standing['rank'] !== index + 1 ||
      !isNonNegativeInteger(standing['ratingScore']) ||
      standing['ratingScore'] > 1000
    ) {
      return false;
    }
    playerIds.add(standing['playerId']);
    return true;
  });
}

function isTrendPublication(value: unknown): value is DashboardTrendPublication {
  if (
    !isObject(value) ||
    typeof value['snapshotId'] !== 'string' ||
    !/^[a-zA-Z0-9-]+$/u.test(value['snapshotId']) ||
    typeof value['publicationDate'] !== 'string' ||
    !/^\d{4}-\d{2}-\d{2}$/u.test(value['publicationDate']) ||
    !isNonNegativeInteger(value['sourceLastMatchId']) ||
    !isObject(value['standings'])
  ) {
    return false;
  }
  return Object.entries(value['standings']).every(
    ([season, modes]) =>
      isSeasonKey(season) && hasModes(modes, isTrendStandingList),
  );
}

function parseTrends(value: unknown): DashboardTrends {
  if (
    !isObject(value) ||
    value['schemaVersion'] !== 1 ||
    typeof value['updatedAt'] !== 'string' ||
    !Array.isArray(value['publications']) ||
    value['publications'].length > MAX_TREND_PUBLICATIONS ||
    !value['publications'].every(isTrendPublication)
  ) {
    throw new Error('dashboard trends have an unsupported format');
  }
  const dates = value['publications'].map(
    (publication) => publication.publicationDate,
  );
  if (dates.some((date, index) => index > 0 && date <= dates[index - 1])) {
    throw new Error('dashboard trend publications are not chronological');
  }
  return value as unknown as DashboardTrends;
}

async function loadTrends(
  baseUrl: string,
  snapshotId: string,
): Promise<DashboardTrends | null> {
  try {
    const value = await fetchJson(
      `${baseUrl}/trends.json?v=${encodeURIComponent(snapshotId)}`,
      'no-store',
    );
    const trends = parseTrends(value);
    return trends.publications.some(
      (publication) => publication.snapshotId === snapshotId,
    )
      ? trends
      : null;
  } catch (error: unknown) {
    console.warn('Unable to load dashboard trends', error);
    return null;
  }
}

function parseManifest(value: unknown): DashboardManifest {
  if (
    !isObject(value) ||
    value['schemaVersion'] !== 1 ||
    typeof value['snapshotId'] !== 'string' ||
    typeof value['snapshotPath'] !== 'string' ||
    typeof value['publicationDate'] !== 'string' ||
    typeof value['generatedAt'] !== 'string' ||
    !isNonNegativeInteger(value['sourceLastMatchId']) ||
    typeof value['sha256'] !== 'string' ||
    !/^[0-9a-f]{64}$/u.test(value['sha256']) ||
    !isNonNegativeInteger(value['bytes'])
  ) {
    throw new Error('dashboard manifest has an unsupported format');
  }
  return value as unknown as DashboardManifest;
}

function roomIdsFromLegacyLabel(value: unknown): readonly number[] {
  if (typeof value !== 'string') {
    return [];
  }
  return (value.match(/\d+/gu) ?? [])
    .map((match) => Number(match))
    .filter((roomId, index, roomIds) =>
      roomId > 0 && roomIds.indexOf(roomId) === index,
    );
}

function normalizeLegacySnapshot(value: unknown): unknown {
  if (!isObject(value) || value['schemaVersion'] !== 2) {
    return value;
  }
  const standings = value['standings'];
  if (!isObject(standings)) {
    return value;
  }
  const normalizedStandings: Record<string, unknown> = {};
  for (const [seasonKey, seasonStandings] of Object.entries(standings)) {
    normalizedStandings[seasonKey] =
      isObject(seasonStandings) && Array.isArray(seasonStandings['players'])
        ? {
            ...seasonStandings,
            players: seasonStandings['players'].map((player) =>
              isObject(player)
                ? {
                    ...player,
                    roomIds: roomIdsFromLegacyLabel(player['roomLabel']),
                  }
                : player,
            ),
          }
        : seasonStandings;
  }
  return {
    ...value,
    schemaVersion: 3,
    standings: normalizedStandings,
    matches: [],
  };
}

function parseSnapshot(value: unknown): DashboardSnapshot {
  const snapshot = normalizeLegacySnapshot(value);
  if (!isObject(snapshot)) {
    throw new Error('dashboard snapshot has an unsupported format');
  }
  const matches = snapshot['matches'];
  if (
    snapshot['schemaVersion'] !== 3 ||
    typeof snapshot['snapshotId'] !== 'string' ||
    typeof snapshot['publicationDate'] !== 'string' ||
    typeof snapshot['generatedAt'] !== 'string' ||
    !isNonNegativeInteger(snapshot['sourceLastMatchId']) ||
    !isNonNegativeInteger(snapshot['sourceMatchCount']) ||
    !isRatingModel(snapshot['ratingModel']) ||
    typeof snapshot['currentSeasonKey'] !== 'string' ||
    !isSeasonKey(snapshot['currentSeasonKey']) ||
    !Array.isArray(snapshot['seasons']) ||
    snapshot['seasons'].length === 0 ||
    !snapshot['seasons'].every(isSeasonOption) ||
    !isObject(snapshot['standings']) ||
    !Array.isArray(matches) ||
    !matches.every(isDashboardMatch)
  ) {
    throw new Error('dashboard snapshot has an unsupported format');
  }
  const standings = snapshot['standings'];
  for (const season of snapshot['seasons']) {
    const seasonStandings = standings[season.key];
    if (
      !isObject(seasonStandings) ||
      !Array.isArray(seasonStandings['players']) ||
      !seasonStandings['players'].every(isPlayerStanding) ||
      !Array.isArray(seasonStandings['heroes']) ||
      !seasonStandings['heroes'].every(isHeroStanding) ||
      (seasonStandings['environmentHeroes'] !== undefined &&
        (!Array.isArray(seasonStandings['environmentHeroes']) ||
          !seasonStandings['environmentHeroes'].every(isHeroStanding)))
    ) {
      throw new Error(`dashboard standings are invalid for ${season.key}`);
    }
  }
  if (standings[snapshot['currentSeasonKey']] === undefined) {
    throw new Error('dashboard snapshot is missing its current season');
  }
  const allTimeStandings = standings['all-time'];
  const playerIds = new Set(
    isObject(allTimeStandings) && Array.isArray(allTimeStandings['players'])
      ? allTimeStandings['players']
          .filter(isPlayerStanding)
          .map((player) => player.id)
      : [],
  );
  const matchIds = new Set<number>();
  for (const [index, match] of matches.entries()) {
    if (matchIds.has(match.id) || !playerIds.has(match.playerId)) {
      throw new Error('dashboard matches contain an invalid player or match ID');
    }
    if (
      index > 0 &&
      Date.parse(match.playedAt) > Date.parse(matches[index - 1].playedAt)
    ) {
      throw new Error('dashboard matches are not sorted by live time');
    }
    matchIds.add(match.id);
  }
  return snapshot as unknown as DashboardSnapshot;
}
