import { environment } from '../../environments/environment';
import {
  DashboardEventSource,
  DashboardRealtimeService,
} from './dashboard-realtime.service';

class FakeEventSource implements DashboardEventSource {
  readonly listeners = new Map<string, Array<() => void>>();
  closed = false;

  addEventListener(type: string, listener: () => void): void {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener();
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
    const updates: string[] = [];
    service.updates$.subscribe((update) => updates.push(update));

    service.start();
    service.start();
    source.emit('resync');
    source.emit('dashboard');
    source.emit('live_rooms');
    source.emit('matches');

    expect(factory).toHaveBeenCalledOnceWith(
      'https://vg-api.luwei.host/v1/events',
    );
    expect(updates).toEqual(['resync', 'dashboard', 'live_rooms', 'matches']);

    service.stop();
    expect(source.closed).toBeTrue();
  });
});
