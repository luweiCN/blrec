import { NavigationEnd, Router, Event as RouterEvent } from '@angular/router';
import { Observable, Subject } from 'rxjs';

import {
  SITE_ANALYTICS_STORAGE_KEY,
  SiteAnalyticsConfig,
  SiteAnalyticsService,
  SiteAnalyticsStorage,
} from './site-analytics.service';

class MemoryAnalyticsStorage implements SiteAnalyticsStorage {
  private readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

function routerStub(events: Observable<RouterEvent>): Router {
  return { url: '/', events } as Router;
}

const ENABLED_CONFIG: SiteAnalyticsConfig = {
  enabled: true,
  endpoint: '/analytics/pixel.svg',
  heartbeatIntervalMs: 120_000,
};

describe('SiteAnalyticsService', () => {
  it('tracks each Angular page once with a stable anonymous visitor ID', () => {
    const events = new Subject<RouterEvent>();
    const storage = new MemoryAnalyticsStorage();
    const fetchSpy = spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(new Response(null, { status: 204 })),
    );
    spyOn(window, 'setInterval').and.returnValue(41);
    const clearIntervalSpy = spyOn(window, 'clearInterval');
    const service = new SiteAnalyticsService(
      routerStub(events),
      document,
      ENABLED_CONFIG,
      storage,
    );

    service.start();
    service.start();
    events.next(new NavigationEnd(1, '/players', '/players'));

    expect(fetchSpy).toHaveBeenCalledTimes(2);
    const firstUrl = new URL(fetchSpy.calls.argsFor(0)[0] as string);
    const secondUrl = new URL(fetchSpy.calls.argsFor(1)[0] as string);
    expect(firstUrl.pathname).toBe('/analytics/pixel.svg');
    expect(firstUrl.searchParams.get('event')).toBe('pageview');
    expect(secondUrl.searchParams.get('event')).toBe('pageview');
    expect(firstUrl.searchParams.get('visitor')).toBe(
      secondUrl.searchParams.get('visitor'),
    );
    expect(storage.getItem(SITE_ANALYTICS_STORAGE_KEY)).toBe(
      firstUrl.searchParams.get('visitor'),
    );

    service.stop();
    events.next(new NavigationEnd(2, '/heroes', '/heroes'));
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(clearIntervalSpy).toHaveBeenCalledOnceWith(41);
  });

  it('does not send analytics from previews or local development', () => {
    const fetchSpy = spyOn(window, 'fetch');
    const setIntervalSpy = spyOn(window, 'setInterval');
    const service = new SiteAnalyticsService(
      routerStub(new Subject<RouterEvent>()),
      document,
      { ...ENABLED_CONFIG, enabled: false },
      new MemoryAnalyticsStorage(),
    );

    service.start();

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(setIntervalSpy).not.toHaveBeenCalled();
  });
});
