import { Injectable } from '@angular/core';

import { environment } from '../../environments/environment';
import {
  DashboardManifest,
  DashboardSnapshot,
  HeroPerformance,
  HeroStanding,
  HeroUsage,
  isSeasonKey,
  MODE_FILTERS,
  Performance,
  PlayerStanding,
  RatingModel,
  SeasonOption,
} from './public-dashboard.models';

export type DashboardLoadState =
  | { readonly kind: 'loading' }
  | {
      readonly kind: 'ready';
      readonly manifest: DashboardManifest;
      readonly snapshot: DashboardSnapshot;
    }
  | { readonly kind: 'error'; readonly message: string };

@Injectable({ providedIn: 'root' })
export class DashboardDataService {
  state: DashboardLoadState = { kind: 'loading' };

  get snapshot(): DashboardSnapshot {
    if (this.state.kind !== 'ready') {
      throw new Error('dashboard data is not ready');
    }
    return this.state.snapshot;
  }

  get snapshotOrNull(): DashboardSnapshot | null {
    return this.state.kind === 'ready' ? this.state.snapshot : null;
  }

  async load(): Promise<void> {
    this.state = { kind: 'loading' };
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
      this.state = { kind: 'ready', manifest, snapshot };
    } catch (error: unknown) {
      console.error('Unable to load dashboard data', error);
      this.state = {
        kind: 'error',
        message: '排行榜数据暂时无法加载，请稍后刷新。',
      };
    }
  }
}

export function initializeDashboardData(
  service: DashboardDataService,
): () => Promise<void> {
  return () => service.load();
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
    (value['matches'] === 0) === (value['ratingScore'] === null)
  );
}

function isRatingModel(value: unknown): value is RatingModel {
  if (!isObject(value)) {
    return false;
  }
  const commonModel =
    value['priorMatches'] === 20 &&
    value['carryoverRate'] === 0.25 &&
    value['credibleLevel'] === 0.9 &&
    value['provisionalMatches'] === 5;
  return (
    commonModel &&
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
    value['wins'] <= value['matches']
  );
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
    isStringArray(value['aliases']) &&
    Number.isInteger(value['trend']) &&
    Array.isArray(value['form']) &&
    value['form'].every((result) => result === 'W' || result === 'L') &&
    hasModes(value['modes'], isPerformance) &&
    Array.isArray(value['heroPool']) &&
    value['heroPool'].every(isHeroUsage)
  );
}

function isHeroStanding(value: unknown): value is HeroStanding {
  if (!isObject(value)) {
    return false;
  }
  return (
    typeof value['id'] === 'string' &&
    typeof value['name'] === 'string' &&
    hasModes(value['modes'], isHeroPerformance)
  );
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

function parseSnapshot(value: unknown): DashboardSnapshot {
  if (
    !isObject(value) ||
    value['schemaVersion'] !== 2 ||
    typeof value['snapshotId'] !== 'string' ||
    typeof value['publicationDate'] !== 'string' ||
    typeof value['generatedAt'] !== 'string' ||
    !isNonNegativeInteger(value['sourceLastMatchId']) ||
    !isNonNegativeInteger(value['sourceMatchCount']) ||
    !isRatingModel(value['ratingModel']) ||
    typeof value['currentSeasonKey'] !== 'string' ||
    !isSeasonKey(value['currentSeasonKey']) ||
    !Array.isArray(value['seasons']) ||
    value['seasons'].length === 0 ||
    !value['seasons'].every(isSeasonOption) ||
    !isObject(value['standings'])
  ) {
    throw new Error('dashboard snapshot has an unsupported format');
  }
  const standings = value['standings'];
  for (const season of value['seasons']) {
    const seasonStandings = standings[season.key];
    if (
      !isObject(seasonStandings) ||
      !Array.isArray(seasonStandings['players']) ||
      !seasonStandings['players'].every(isPlayerStanding) ||
      !Array.isArray(seasonStandings['heroes']) ||
      !seasonStandings['heroes'].every(isHeroStanding)
    ) {
      throw new Error(`dashboard standings are invalid for ${season.key}`);
    }
  }
  if (standings[value['currentSeasonKey']] === undefined) {
    throw new Error('dashboard snapshot is missing its current season');
  }
  return value as unknown as DashboardSnapshot;
}
