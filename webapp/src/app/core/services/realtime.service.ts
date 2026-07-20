import { Inject, Injectable, InjectionToken, NgZone } from '@angular/core';
import { Router } from '@angular/router';
import { Observable } from 'rxjs';
import { share } from 'rxjs/operators';

import { UrlService } from './url.service';

const REALTIME_TOPICS = [
  'tasks',
  'network',
  'upload_progress',
  'highlight_progress',
] as const;

const CONTROL_EVENT_TYPES = [
  'resync',
  'heartbeat',
] as const;

const EVENT_TYPES = [...REALTIME_TOPICS, ...CONTROL_EVENT_TYPES] as const;

export type RealtimeEventType = (typeof EVENT_TYPES)[number];

type RealtimeTopic = (typeof REALTIME_TOPICS)[number];

const ROUTE_TOPICS: ReadonlyArray<
  readonly [route: string, topics: readonly RealtimeTopic[]]
> = [
  ['/tasks', ['tasks']],
  ['/network', ['network']],
  ['/recordings', ['upload_progress']],
  ['/upload-tasks', ['upload_progress']],
  ['/clips', ['upload_progress', 'highlight_progress']],
];

export function realtimeTopicsForUrl(
  url: string
): readonly RealtimeEventType[] {
  const path = (url.split(/[?#]/, 1)[0] || '/').replace(/\/+$/, '') || '/';
  const requested = new Set<RealtimeTopic>();
  for (const [route, topics] of ROUTE_TOPICS) {
    if (path !== route && !path.startsWith(`${route}/`)) {
      continue;
    }
    for (const topic of topics) {
      requested.add(topic);
    }
  }
  return REALTIME_TOPICS.filter((topic) => requested.has(topic));
}

export interface RealtimeEvent {
  readonly type: RealtimeEventType;
  readonly data: unknown;
}

export interface EventSourceLike {
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

export const EVENT_SOURCE_FACTORY = new InjectionToken<EventSourceFactory>(
  'BLREC_EVENT_SOURCE_FACTORY',
  {
    providedIn: 'root',
    factory: () => (url) => new EventSource(url, { withCredentials: true }),
  }
);

@Injectable({ providedIn: 'root' })
export class RealtimeService {
  readonly events$: Observable<RealtimeEvent>;

  constructor(
    private url: UrlService,
    private zone: NgZone,
    private router: Router,
    @Inject(EVENT_SOURCE_FACTORY) private eventSourceFactory: EventSourceFactory
  ) {
    this.events$ = new Observable<RealtimeEvent>((subscriber) => {
      const topics = realtimeTopicsForUrl(this.router.url);
      const params = encodeURIComponent(topics.join(','));
      const source = this.eventSourceFactory(
        this.url.makeApiUrl(`/api/v1/realtime?topics=${params}`)
      );
      const listeners = new Map<string, EventListener>();
      let bootstrapResyncPending = true;
      for (const type of [...topics, ...CONTROL_EVENT_TYPES]) {
        const listener: EventListener = (event) => {
          if (!(event instanceof MessageEvent)) {
            return;
          }
          let data: unknown;
          try {
            data = JSON.parse(String(event.data));
          } catch (_error) {
            this.zone.run(() =>
              subscriber.next({ type: 'resync', data: {} })
            );
            return;
          }
          if (type === 'resync' && bootstrapResyncPending) {
            bootstrapResyncPending = false;
            return;
          }
          this.zone.run(() => subscriber.next({ type, data }));
        };
        listeners.set(type, listener);
        source.addEventListener(type, listener);
      }
      return () => {
        for (const [type, listener] of listeners) {
          source.removeEventListener(type, listener);
        }
        source.close();
      };
    }).pipe(share());
  }
}
