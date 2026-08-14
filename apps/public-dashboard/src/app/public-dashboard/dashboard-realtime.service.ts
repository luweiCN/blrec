import { Inject, Injectable, InjectionToken } from '@angular/core';
import { Subject } from 'rxjs';

import { environment } from '../../environments/environment';

export type DashboardRealtimeUpdate =
  | 'resync'
  | 'dashboard'
  | 'live_rooms';

export interface DashboardEventSource {
  addEventListener(type: string, listener: () => void): void;
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
    for (const update of ['resync', 'dashboard', 'live_rooms'] as const) {
      eventSource.addEventListener(update, () => {
        this.updatesSubject.next(update);
      });
    }
    this.eventSource = eventSource;
  }

  stop(): void {
    this.eventSource?.close();
    this.eventSource = null;
  }
}
