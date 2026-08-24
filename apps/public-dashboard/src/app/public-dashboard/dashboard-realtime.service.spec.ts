import { environment } from '../../environments/environment';
import {
  DashboardEventSource,
  DashboardRealtimeService,
} from './dashboard-realtime.service';

class FakeEventSource implements DashboardEventSource {
  readonly listeners = new Map<
    string,
    Array<(event: { readonly data: string }) => void>
  >();
  closed = false;

  addEventListener(
    type: string,
    listener: (event: { readonly data: string }) => void,
  ): void {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data = ''): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data });
    }
  }
}

describe('DashboardRealtimeService', () => {
  const originalApiBaseUrl = environment.apiBaseUrl;

  afterEach(() => {
    environment.apiBaseUrl = originalApiBaseUrl;
  });

  it('shares one SSE connection and emits typed refresh signals', () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    const source = new FakeEventSource();
    const factory = jasmine
      .createSpy('eventSourceFactory')
      .and.returnValue(source);
    const service = new DashboardRealtimeService(factory);
    const updates: unknown[] = [];
    service.updates$.subscribe((update) => updates.push(update));

    service.start();
    service.start();
    source.emit('resync');
    source.emit('dashboard');
    source.emit('live_rooms');
    source.emit('matches');
    source.emit(
      'resource',
      JSON.stringify({
        resource: 'standings',
        seasonId: '2026-summer',
        revision: 'standings-2',
      }),
    );

    expect(factory).toHaveBeenCalledOnceWith(
      'https://vg-api.luwei.host/v1/events',
    );
    expect(updates).toEqual([
      'resync',
      'dashboard',
      'live_rooms',
      'matches',
      {
        kind: 'resource',
        resource: 'standings',
        seasonId: '2026-summer',
        revision: 'standings-2',
      },
    ]);

    service.stop();
    expect(source.closed).toBeTrue();
  });
});
