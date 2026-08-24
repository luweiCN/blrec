import { Inject, Injectable, InjectionToken } from '@angular/core';
import { Subject } from 'rxjs';

import { environment } from '../../environments/environment';

export type DashboardLegacyRealtimeUpdate =
  | 'resync'
  | 'dashboard'
  | 'live_rooms'
  | 'matches';

export interface DashboardResourceRealtimeUpdate {
  readonly kind: 'resource';
  readonly resource: 'summary' | 'standings' | 'trends' | 'environment';
  readonly revision: string;
  readonly seasonId?: string;
  readonly mode?: string;
}

export type DashboardRealtimeUpdate =
  | DashboardLegacyRealtimeUpdate
  | DashboardResourceRealtimeUpdate;

export interface DashboardEventMessage {
  readonly data: string;
}

export interface DashboardEventSource {
  addEventListener(
    type: string,
    listener: (event: DashboardEventMessage) => void,
  ): void;
  close(): void;
}

export type DashboardEventSourceFactory = (url: string) => DashboardEventSource;

export const DASHBOARD_EVENT_SOURCE_FACTORY =
  new InjectionToken<DashboardEventSourceFactory>(
    'DASHBOARD_EVENT_SOURCE_FACTORY',
    {
      providedIn: 'root',
      factory: () => (url: string) =>
        new EventSource(url) as unknown as DashboardEventSource,
    },
  );

@Injectable({ providedIn: 'root' })
export class DashboardRealtimeService {
  private readonly updatesSubject = new Subject<DashboardRealtimeUpdate>();
  private eventSource: DashboardEventSource | null = null;

  readonly updates$ = this.updatesSubject.asObservable();

  constructor(
    @Inject(DASHBOARD_EVENT_SOURCE_FACTORY)
    private readonly eventSourceFactory: DashboardEventSourceFactory,
  ) {}

  start(): void {
    if (this.eventSource !== null) {
      return;
    }
    const apiBaseUrl = environment.apiBaseUrl.replace(/\/+$/u, '');
    if (apiBaseUrl === '') {
      return;
    }
    const eventSource = this.eventSourceFactory(`${apiBaseUrl}/events`);
    for (const update of [
      'resync',
      'dashboard',
      'live_rooms',
      'matches',
    ] as const) {
      eventSource.addEventListener(update, () => {
        this.updatesSubject.next(update);
      });
    }
    eventSource.addEventListener('resource', (event) => {
      const update = parseResourceUpdate(event.data);
      if (update !== null) {
        this.updatesSubject.next(update);
      }
    });
    this.eventSource = eventSource;
  }

  stop(): void {
    this.eventSource?.close();
    this.eventSource = null;
  }
}

function parseResourceUpdate(value: string): DashboardResourceRealtimeUpdate | null {
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!isRecord(parsed)) {
      return null;
    }
    const resource = parsed['resource'];
    const revision = parsed['revision'];
    if (
      (resource !== 'summary' &&
        resource !== 'standings' &&
        resource !== 'trends' &&
        resource !== 'environment') ||
      typeof revision !== 'string' ||
      revision === ''
    ) {
      return null;
    }
    const seasonId = parsed['seasonId'];
    const mode = parsed['mode'];
    return {
      kind: 'resource',
      resource,
      revision,
      ...(typeof seasonId === 'string' ? { seasonId } : {}),
      ...(typeof mode === 'string' ? { mode } : {}),
    };
  } catch {
    return null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
