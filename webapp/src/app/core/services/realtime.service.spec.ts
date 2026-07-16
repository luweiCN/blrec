import { TestBed } from '@angular/core/testing';

import {
  EVENT_SOURCE_FACTORY,
  EventSourceLike,
  RealtimeService,
} from './realtime.service';
import { UrlService } from './url.service';

class FakeEventSource implements EventSourceLike {
  readonly listeners = new Map<string, EventListener[]>();
  readonly close = jasmine.createSpy('close');

  addEventListener(type: string, listener: EventListener): void {
    const values = this.listeners.get(type) ?? [];
    values.push(listener);
    this.listeners.set(type, values);
  }

  removeEventListener(type: string, listener: EventListener): void {
    this.listeners.set(
      type,
      (this.listeners.get(type) ?? []).filter((value) => value !== listener)
    );
  }

  emit(type: string, data: unknown): void {
    const event = new MessageEvent(type, { data: JSON.stringify(data) });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

describe('RealtimeService', () => {
  it('shares one EventSource and emits named events', () => {
    const source = new FakeEventSource();
    const factory = jasmine
      .createSpy('eventSourceFactory')
      .and.returnValue(source);
    TestBed.configureTestingModule({
      providers: [
        RealtimeService,
        { provide: EVENT_SOURCE_FACTORY, useValue: factory },
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
    const service = TestBed.inject(RealtimeService);
    const first = jasmine.createSpy('first');
    const second = jasmine.createSpy('second');

    const firstSubscription = service.events$.subscribe(first);
    const secondSubscription = service.events$.subscribe(second);
    source.emit('tasks', { tasks: [{ roomId: 1 }] });

    expect(factory).toHaveBeenCalledOnceWith('/api/v1/realtime');
    expect(first).toHaveBeenCalledWith({
      type: 'tasks',
      data: { tasks: [{ roomId: 1 }] },
    });
    expect(second).toHaveBeenCalledTimes(1);

    firstSubscription.unsubscribe();
    expect(source.close).not.toHaveBeenCalled();
    secondSubscription.unsubscribe();
    expect(source.close).toHaveBeenCalledOnceWith();
  });

  it('turns malformed payloads into a resync event', () => {
    const source = new FakeEventSource();
    TestBed.configureTestingModule({
      providers: [
        RealtimeService,
        { provide: EVENT_SOURCE_FACTORY, useValue: () => source },
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
    const received = jasmine.createSpy('received');
    TestBed.inject(RealtimeService).events$.subscribe(received);
    const event = new MessageEvent('network', { data: '{broken' });

    for (const listener of source.listeners.get('network') ?? []) {
      listener(event);
    }

    expect(received).toHaveBeenCalledWith({ type: 'resync', data: {} });
  });
});
