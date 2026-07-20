import { NgZone } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';

import {
  EVENT_SOURCE_FACTORY,
  EventSourceLike,
  RealtimeService,
  realtimeTopicsForUrl,
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
    this.emitRaw(type, JSON.stringify(data));
  }

  emitRaw(type: string, data: string): void {
    const event = new MessageEvent(type, { data });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

describe('realtimeTopicsForUrl', () => {
  it('maps realtime routes to canonical backend topics', () => {
    expect(realtimeTopicsForUrl('/tasks')).toEqual(['tasks']);
    expect(realtimeTopicsForUrl('/network')).toEqual(['network']);
    expect(realtimeTopicsForUrl('/recordings')).toEqual(['upload_progress']);
    expect(realtimeTopicsForUrl('/upload-tasks')).toEqual([
      'upload_progress',
    ]);
    expect(realtimeTopicsForUrl('/clips')).toEqual([
      'upload_progress',
      'highlight_progress',
    ]);
  });

  it('ignores query strings and fragments while preserving nested routes', () => {
    expect(realtimeTopicsForUrl('/tasks/100/detail?tab=status#files')).toEqual([
      'tasks',
    ]);
    expect(realtimeTopicsForUrl('/clips?state=processing#queue')).toEqual([
      'upload_progress',
      'highlight_progress',
    ]);
  });

  it('returns unique topics from the backend whitelist', () => {
    const backendTopics = new Set([
      'tasks',
      'network',
      'upload_progress',
      'highlight_progress',
    ]);

    for (const url of [
      '/tasks',
      '/network',
      '/recordings',
      '/upload-tasks',
      '/clips',
    ]) {
      const topics = realtimeTopicsForUrl(url);
      expect(new Set(topics).size).toBe(topics.length);
      expect(topics.every((topic) => backendTopics.has(topic))).toBeTrue();
    }
  });
});

describe('RealtimeService', () => {
  let factory: jasmine.Spy;
  let router: { url: string };
  let sources: FakeEventSource[];

  beforeEach(() => {
    router = { url: '/tasks' };
    sources = [];
    factory = jasmine.createSpy('eventSourceFactory').and.callFake(() => {
      const source = new FakeEventSource();
      sources.push(source);
      return source;
    });
    TestBed.configureTestingModule({
      providers: [
        RealtimeService,
        { provide: EVENT_SOURCE_FACTORY, useValue: factory },
        { provide: Router, useValue: router },
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
  });

  it('shares one route-specific EventSource and emits subscribed events', () => {
    router.url = '/clips?state=processing#queue';
    const service = TestBed.inject(RealtimeService);
    const first = jasmine.createSpy('first');
    const second = jasmine.createSpy('second');

    const firstSubscription = service.events$.subscribe(first);
    const secondSubscription = service.events$.subscribe(second);
    const source = sources[0];
    source.emit('tasks', { tasks: [{ roomId: 1 }] });
    source.emit('upload_progress', { jobs: [{ jobId: 2 }] });
    source.emit('highlight_progress', {
      clips: [{ id: 3, state: 'processing' }],
    });

    expect(factory).toHaveBeenCalledOnceWith(
      '/api/v1/realtime?topics=upload_progress%2Chighlight_progress'
    );
    expect(first).toHaveBeenCalledWith({
      type: 'upload_progress',
      data: { jobs: [{ jobId: 2 }] },
    });
    expect(first).toHaveBeenCalledWith({
      type: 'highlight_progress',
      data: { clips: [{ id: 3, state: 'processing' }] },
    });
    expect(first).not.toHaveBeenCalledWith({
      type: 'tasks',
      data: { tasks: [{ roomId: 1 }] },
    });
    expect(second).toHaveBeenCalledTimes(2);

    firstSubscription.unsubscribe();
    expect(source.close).not.toHaveBeenCalled();
    secondSubscription.unsubscribe();
    expect(source.close).toHaveBeenCalledOnceWith();
  });

  it('uses the current route when the shared connection is next created', () => {
    const service = TestBed.inject(RealtimeService);

    const firstSubscription = service.events$.subscribe();
    firstSubscription.unsubscribe();
    router.url = '/network?view=traffic#interfaces';
    const secondSubscription = service.events$.subscribe();

    expect(factory.calls.allArgs()).toEqual([
      ['/api/v1/realtime?topics=tasks'],
      ['/api/v1/realtime?topics=network'],
    ]);
    expect(sources[0].close).toHaveBeenCalledOnceWith();

    secondSubscription.unsubscribe();
  });

  it('suppresses only the first well-formed resync for each EventSource', () => {
    const received = jasmine.createSpy('received');
    TestBed.inject(RealtimeService).events$.subscribe(received);
    const source = sources[0];

    source.emit('resync', { reason: 'bootstrap' });
    source.emit('resync', { reason: 'overflow' });

    expect(received).toHaveBeenCalledOnceWith({
      type: 'resync',
      data: { reason: 'overflow' },
    });
  });

  it('does not treat a malformed payload as the bootstrap resync', () => {
    const received: unknown[] = [];
    TestBed.inject(RealtimeService).events$.subscribe((event) => {
      received.push(event);
    });
    const source = sources[0];

    source.emitRaw('resync', '{broken');
    source.emit('resync', { reason: 'bootstrap' });
    source.emit('resync', { reason: 'reconnect' });

    expect(received).toEqual([
      { type: 'resync', data: {} },
      { type: 'resync', data: { reason: 'reconnect' } },
    ]);
  });

  it('enters Angular only for subscribed and control events', () => {
    const zone = TestBed.inject(NgZone);
    const deliveredInsideAngular: boolean[] = [];
    TestBed.inject(RealtimeService).events$.subscribe(() => {
      deliveredInsideAngular.push(NgZone.isInAngularZone());
    });
    const source = sources[0];

    expect(source.listeners.has('network')).toBeFalse();
    zone.runOutsideAngular(() => {
      source.emit('network', { interfaces: [] });
      source.emit('tasks', { tasks: [] });
      source.emit('heartbeat', {});
    });

    expect(deliveredInsideAngular).toEqual([true, true]);
  });
});
