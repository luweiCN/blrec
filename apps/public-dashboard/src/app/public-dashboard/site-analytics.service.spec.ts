import { NavigationEnd, Router, Event as RouterEvent } from '@angular/router';
import { Observable, Subject } from 'rxjs';

import {
  SITE_ANALYTICS_STORAGE_KEY,
  SiteAnalyticsConfig,
  SiteAnalyticsService,
  SiteAnalyticsStorage,
  analyticsDevice,
  analyticsPage,
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

    expect(fetchSpy).toHaveBeenCalledTimes(4);
    const firstUrl = new URL(fetchSpy.calls.argsFor(0)[0] as string);
    const secondUrl = new URL(fetchSpy.calls.argsFor(1)[0] as string);
    const thirdUrl = new URL(fetchSpy.calls.argsFor(2)[0] as string);
    const fourthUrl = new URL(fetchSpy.calls.argsFor(3)[0] as string);
    expect(firstUrl.pathname).toBe('/analytics/pixel.svg');
    expect(firstUrl.searchParams.get('event')).toBe('pageview');
    expect(firstUrl.search).toMatch(
      /^\?event=pageview&visitor=[0-9a-f-]{16,64}$/u,
    );
    expect(secondUrl.searchParams.get('event')).toBe('detail');
    expect(secondUrl.searchParams.get('kind')).toBe('pageview');
    expect(secondUrl.searchParams.get('page')).toBe('overview');
    expect(secondUrl.searchParams.get('source')).toMatch(/^(direct|internal)$/u);
    expect(secondUrl.searchParams.get('device')).toMatch(
      /^(mobile|tablet|desktop)$/u,
    );
    expect(thirdUrl.searchParams.get('event')).toBe('pageview');
    expect(fourthUrl.searchParams.get('event')).toBe('detail');
    expect(fourthUrl.searchParams.get('page')).toBe('players');
    expect(firstUrl.searchParams.get('visitor')).toBe(
      secondUrl.searchParams.get('visitor'),
    );
    expect(secondUrl.searchParams.get('visitor')).toBe(
      fourthUrl.searchParams.get('visitor'),
    );
    expect(storage.getItem(SITE_ANALYTICS_STORAGE_KEY)).toBe(
      firstUrl.searchParams.get('visitor'),
    );

    service.stop();
    events.next(new NavigationEnd(2, '/heroes', '/heroes'));
    expect(fetchSpy).toHaveBeenCalledTimes(4);
    expect(clearIntervalSpy).toHaveBeenCalledOnceWith(41);
  });

  it('normalizes detail routes without collecting player or hero IDs', () => {
    expect(analyticsPage('/players/56?mode=3v3')).toBe('player-detail');
    expect(analyticsPage('/heroes/blackfeather')).toBe('hero-detail');
    expect(analyticsPage('/guide/download')).toBe('guide-download');
  });

  it('classifies viewport widths into coarse device groups', () => {
    expect(analyticsDevice(390)).toBe('mobile');
    expect(analyticsDevice(900)).toBe('tablet');
    expect(analyticsDevice(1440)).toBe('desktop');
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
