import { SiteStatsService } from './site-stats.service';

const SITE_STATS = {
  schemaVersion: 1,
  generatedAt: '2026-08-04T10:05:00+08:00',
  timezone: 'Asia/Shanghai',
  trackingStartedAt: '2026-08-04T00:00:00+08:00',
  activeWindowMinutes: 5,
  today: {
    date: '2026-08-04',
    visitors: 18,
    pageViews: 63,
  },
  activeVisitors: 4,
  totalPageViews: 126,
} as const;

function jsonResponse(value: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(value),
  } as Response;
}

describe('SiteStatsService', () => {
  it('loads and validates the independently published site stats', async () => {
    const fetchSpy = spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(jsonResponse(SITE_STATS)),
    );
    const service = new SiteStatsService();

    const state = await service.load();

    expect(state).toEqual({ kind: 'ready', stats: SITE_STATS });
    expect(fetchSpy).toHaveBeenCalledOnceWith('data/site-stats.json', {
      cache: 'no-store',
    });
  });

  it('does not display malformed or fabricated values', async () => {
    spyOn(console, 'error');
    spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(
        jsonResponse({
          ...SITE_STATS,
          today: { ...SITE_STATS.today, visitors: -1 },
        }),
      ),
    );
    const service = new SiteStatsService();

    expect(await service.load()).toEqual({ kind: 'unavailable' });
  });
});
