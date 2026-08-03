import { Injectable } from '@angular/core';

import { environment } from '../../environments/environment';
import { SiteStats, SiteStatsState } from './site-stats.models';

@Injectable({ providedIn: 'root' })
export class SiteStatsService {
  async load(): Promise<SiteStatsState> {
    try {
      const baseUrl = environment.dataBaseUrl.replace(/\/+$/u, '');
      const response = await fetch(`${baseUrl}/site-stats.json`, {
        cache: 'no-store',
      });
      if (!response.ok) {
        throw new Error(`site stats request failed with ${response.status}`);
      }
      return { kind: 'ready', stats: parseSiteStats(await response.json()) };
    } catch (error: unknown) {
      console.error('Unable to load site stats', error);
      return { kind: 'unavailable' };
    }
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function isDateTime(value: unknown): value is string {
  return typeof value === 'string' && !Number.isNaN(Date.parse(value));
}

export function parseSiteStats(value: unknown): SiteStats {
  if (
    !isObject(value) ||
    value['schemaVersion'] !== 1 ||
    !isDateTime(value['generatedAt']) ||
    value['timezone'] !== 'Asia/Shanghai' ||
    !isDateTime(value['trackingStartedAt']) ||
    value['activeWindowMinutes'] !== 5 ||
    !isObject(value['today']) ||
    typeof value['today']['date'] !== 'string' ||
    !/^\d{4}-\d{2}-\d{2}$/u.test(value['today']['date']) ||
    !isNonNegativeInteger(value['today']['visitors']) ||
    !isNonNegativeInteger(value['today']['pageViews']) ||
    !isNonNegativeInteger(value['activeVisitors']) ||
    !isNonNegativeInteger(value['totalPageViews'])
  ) {
    throw new Error('site stats have an unsupported format');
  }
  return value as unknown as SiteStats;
}
