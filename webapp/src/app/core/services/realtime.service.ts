import { Inject, Injectable, InjectionToken, NgZone } from '@angular/core';
import { Observable } from 'rxjs';
import { share } from 'rxjs/operators';

import { UrlService } from './url.service';

const EVENT_TYPES = [
  'resync',
  'tasks',
  'network',
  'upload_progress',
  'highlight_progress',
  'heartbeat',
] as const;

export type RealtimeEventType = (typeof EVENT_TYPES)[number];

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
    @Inject(EVENT_SOURCE_FACTORY) private eventSourceFactory: EventSourceFactory
  ) {
    this.events$ = new Observable<RealtimeEvent>((subscriber) => {
      const source = this.eventSourceFactory(
        this.url.makeApiUrl('/api/v1/realtime')
      );
      const listeners = new Map<string, EventListener>();
      for (const type of EVENT_TYPES) {
        const listener: EventListener = (event) => {
          if (!(event instanceof MessageEvent)) {
            return;
          }
          let value: RealtimeEvent;
          try {
            value = { type, data: JSON.parse(String(event.data)) };
          } catch (_error) {
            value = { type: 'resync', data: {} };
          }
          this.zone.run(() => subscriber.next(value));
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
