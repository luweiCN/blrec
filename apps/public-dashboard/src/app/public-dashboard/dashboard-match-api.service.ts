import { Injectable } from '@angular/core';

import { environment } from '../../environments/environment';
import { isDashboardMatch } from './public-dashboard-data.service';
import {
  DashboardMatch,
  ModeFilter,
  SeasonKey,
} from './public-dashboard.models';

export interface DashboardMatchPageQuery {
  readonly page: number;
  readonly pageSize: number;
  readonly seasonKey: SeasonKey;
  readonly mode: ModeFilter;
  readonly playerId?: number;
  readonly query: string;
  readonly heroes: readonly string[];
}

export interface DashboardMatchPage {
  readonly items: readonly DashboardMatch[];
  readonly page: number;
  readonly pageSize: number;
  readonly total: number;
}

export interface DashboardMatchSummaryQuery {
  readonly seasonKey: SeasonKey;
  readonly mode: ModeFilter;
  readonly playerId?: number;
}

export interface DashboardMatchSummary {
  readonly matches: number;
  readonly wins: number;
  readonly players: number;
  readonly averageDurationSeconds: number;
  readonly replays: number;
}

@Injectable({ providedIn: 'root' })
export class DashboardMatchApiService {
  get enabled(): boolean {
    return environment.apiBaseUrl.trim() !== '';
  }

  async list(
    query: DashboardMatchPageQuery,
  ): Promise<DashboardMatchPage | null> {
    if (!this.enabled) {
      return null;
    }
    const parameters = new URLSearchParams({
      page: String(query.page),
      pageSize: String(query.pageSize),
      ratingScope: query.mode,
      ratingSeason: query.seasonKey,
    });
    if (query.seasonKey !== 'all-time') {
      parameters.set('season', query.seasonKey);
    }
    if (query.mode !== 'all') {
      parameters.set('mode', query.mode);
    }
    if (query.playerId !== undefined) {
      parameters.set('playerId', String(query.playerId));
    }
    if (query.query.trim() !== '') {
      parameters.set('q', query.query.trim());
    }
    if (query.heroes.length > 0) {
      parameters.set('heroes', query.heroes.join(','));
    }
    const baseUrl = environment.apiBaseUrl.replace(/\/+$/u, '');
    try {
      const response = await fetch(
        `${baseUrl}/matches?${parameters.toString()}`,
        { cache: 'no-store' },
      );
      if (!response.ok) {
        throw new Error(`match API returned ${response.status}`);
      }
      return parseMatchPage(await response.json());
    } catch (error: unknown) {
      console.warn('Unable to load matches from dashboard API', error);
      return null;
    }
  }

  async summary(
    query: DashboardMatchSummaryQuery,
  ): Promise<DashboardMatchSummary | null> {
    if (!this.enabled) {
      return null;
    }
    const parameters = new URLSearchParams();
    if (query.seasonKey !== 'all-time') {
      parameters.set('season', query.seasonKey);
    }
    if (query.mode !== 'all') {
      parameters.set('mode', query.mode);
    }
    if (query.playerId !== undefined) {
      parameters.set('playerId', String(query.playerId));
    }
    const baseUrl = environment.apiBaseUrl.replace(/\/+$/u, '');
    const suffix = parameters.toString();
    const url = `${baseUrl}/matches/summary${suffix === '' ? '' : `?${suffix}`}`;
    try {
      const response = await fetch(url, { cache: 'no-cache' });
      if (!response.ok) {
        throw new Error(`match summary API returned ${response.status}`);
      }
      return parseMatchSummary(await response.json());
    } catch (error: unknown) {
      console.warn('Unable to load match summary from dashboard API', error);
      return null;
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function parseMatchPage(value: unknown): DashboardMatchPage {
  if (
    !isRecord(value) ||
    !Array.isArray(value['items']) ||
    !value['items'].every(isDashboardMatch) ||
    !isNonNegativeInteger(value['page']) ||
    value['page'] < 1 ||
    !isNonNegativeInteger(value['pageSize']) ||
    value['pageSize'] < 1 ||
    !isNonNegativeInteger(value['total'])
  ) {
    throw new Error('match API returned an unsupported response');
  }
  return {
    items: value['items'],
    page: value['page'],
    pageSize: value['pageSize'],
    total: value['total'],
  };
}

function parseMatchSummary(value: unknown): DashboardMatchSummary {
  if (
    !isRecord(value) ||
    !isNonNegativeInteger(value['matches']) ||
    !isNonNegativeInteger(value['wins']) ||
    value['wins'] > value['matches'] ||
    !isNonNegativeInteger(value['players']) ||
    !isNonNegativeInteger(value['averageDurationSeconds']) ||
    !isNonNegativeInteger(value['replays']) ||
    value['replays'] > value['matches']
  ) {
    throw new Error('match summary API returned an unsupported response');
  }
  return {
    matches: value['matches'],
    wins: value['wins'],
    players: value['players'],
    averageDurationSeconds: value['averageDurationSeconds'],
    replays: value['replays'],
  };
}
