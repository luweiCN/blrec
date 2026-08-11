import { environment } from '../../environments/environment';
import { DashboardMatchApiService } from './dashboard-match-api.service';

function jsonResponse(value: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(value),
  } as Response;
}

describe('DashboardMatchApiService', () => {
  const originalApiBaseUrl = environment.apiBaseUrl;

  beforeEach(() => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
  });

  afterEach(() => {
    environment.apiBaseUrl = originalApiBaseUrl;
  });

  it('loads filtered match summary data from the server', async () => {
    const fetchSpy = spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(
        jsonResponse({
          matches: 12,
          wins: 8,
          players: 3,
          averageDurationSeconds: 907,
          replays: 9,
        }),
      ),
    );
    const service = new DashboardMatchApiService();

    const summary = await service.summary({
      seasonKey: '2026-summer',
      mode: '3v3',
    });

    expect(summary).toEqual({
      matches: 12,
      wins: 8,
      players: 3,
      averageDurationSeconds: 907,
      replays: 9,
    });
    expect(fetchSpy.calls.mostRecent().args[0]).toBe(
      'https://vg-api.luwei.host/v1/matches/summary?season=2026-summer&mode=3v3',
    );
  });

  it('returns null instead of exposing an invalid summary contract', async () => {
    spyOn(console, 'warn');
    spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(jsonResponse({ matches: -1 })),
    );
    const service = new DashboardMatchApiService();

    const summary = await service.summary({
      seasonKey: 'all-time',
      mode: 'all',
    });

    expect(summary).toBeNull();
  });

  it('rejects a failed match list request so the page can show an error state', async () => {
    spyOn(window, 'fetch').and.returnValue(
      Promise.resolve({ ok: false, status: 503 } as Response),
    );
    const service = new DashboardMatchApiService();

    await expectAsync(
      service.list({
        page: 1,
        pageSize: 20,
        seasonKey: '2026-summer',
        mode: '3v3',
        query: '',
        heroes: [],
      }),
    ).toBeRejectedWithError(/503/u);
  });
});
