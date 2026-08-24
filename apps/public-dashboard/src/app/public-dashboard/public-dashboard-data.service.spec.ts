import { environment } from '../../environments/environment';
import { DASHBOARD_OWNER_TOKEN_STORAGE_KEY } from './dashboard-owner-access.service';
import { DashboardDataService } from './public-dashboard-data.service';
import { DashboardTrends } from './public-dashboard.models';
import {
  TEST_DASHBOARD_SNAPSHOT,
  TEST_DASHBOARD_TRENDS,
} from './public-dashboard.test-data';

const TRENDS: DashboardTrends = {
  schemaVersion: 1,
  updatedAt: TEST_DASHBOARD_SNAPSHOT.generatedAt,
  publications: [
    {
      snapshotId: TEST_DASHBOARD_SNAPSHOT.snapshotId,
      publicationDate: TEST_DASHBOARD_SNAPSHOT.publicationDate,
      sourceLastMatchId: TEST_DASHBOARD_SNAPSHOT.sourceLastMatchId,
      standings: {},
    },
  ],
};

function apiDocument(
  snapshot = TEST_DASHBOARD_SNAPSHOT,
  trends: DashboardTrends = TRENDS,
): object {
  return { snapshot, trends };
}

function jsonResponse(value: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(value),
  } as Response;
}

function v2Summary(): object {
  const resources = TEST_DASHBOARD_SNAPSHOT.seasons.reduce<Record<string, string>>(
    (result, season) => {
      result[season.key] = `${season.key}-1`;
      return result;
    },
    {},
  );
  return {
    schemaVersion: 1,
    snapshotId: TEST_DASHBOARD_SNAPSHOT.snapshotId,
    contentRevision: TEST_DASHBOARD_SNAPSHOT.contentRevision,
    publicationDate: TEST_DASHBOARD_SNAPSHOT.publicationDate,
    generatedAt: TEST_DASHBOARD_SNAPSHOT.generatedAt,
    sourceLastMatchId: TEST_DASHBOARD_SNAPSHOT.sourceLastMatchId,
    sourceMatchCount: TEST_DASHBOARD_SNAPSHOT.sourceMatchCount,
    ratingModel: TEST_DASHBOARD_SNAPSHOT.ratingModel,
    currentSeasonKey: TEST_DASHBOARD_SNAPSHOT.currentSeasonKey,
    seasons: TEST_DASHBOARD_SNAPSHOT.seasons,
    playersById: {},
    resources: {
      standings: resources,
      environment: resources,
      trends: 'trends-1',
      matches: 'matches-1',
      liveRooms: 'rooms-1',
    },
  };
}

function v2Standings(seasonId: string): object {
  const standings = TEST_DASHBOARD_SNAPSHOT.standings[seasonId];
  return {
    schemaVersion: 1,
    seasonId,
    players: standings.players.map(({ heroPool: _heroPool, ...player }) => player),
    heroes: standings.heroes,
  };
}

describe('DashboardDataService', () => {
  const originalApiBaseUrl = environment.apiBaseUrl;
  const originalUseDashboardV2 = environment.useDashboardV2;

  afterEach(() => {
    environment.apiBaseUrl = originalApiBaseUrl;
    environment.useDashboardV2 = originalUseDashboardV2;
    window.sessionStorage.removeItem(DASHBOARD_OWNER_TOKEN_STORAGE_KEY);
  });

  it('loads only v2 summary and current-season standings on the first view', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    environment.useDashboardV2 = true;
    const fetchSpy = spyOn(window, 'fetch').and.returnValues(
      Promise.resolve(jsonResponse(v2Summary())),
      Promise.resolve(
        jsonResponse(v2Standings(TEST_DASHBOARD_SNAPSHOT.currentSeasonKey)),
      ),
    );
    const service = new DashboardDataService();

    await service.load();

    expect(service.state.kind).toBe('ready');
    expect(service.snapshot.currentSeasonKey).toBe('2026-summer');
    expect(service.snapshot.standings['2026-summer'].players.length).toBeGreaterThan(0);
    expect(service.snapshot.standings['2026-spring'].players).toEqual([]);
    expect(service.snapshot.standings['2026-summer'].players[0].heroPool).toBe(
      service.snapshot.standings['2026-summer'].players[0].heroPools!.all,
    );
    expect(service.trends).toBeNull();
    expect(fetchSpy.calls.allArgs()).toEqual([
      [
        'https://vg-api.luwei.host/v2/dashboard/summary',
        { cache: 'no-cache' },
      ],
      [
        'https://vg-api.luwei.host/v2/standings?seasonId=2026-summer',
        { cache: 'no-cache' },
      ],
    ]);
  });

  it('loads environment data only after its view asks for it', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    environment.useDashboardV2 = true;
    const seasonId = TEST_DASHBOARD_SNAPSHOT.currentSeasonKey;
    const fetchSpy = spyOn(window, 'fetch').and.returnValues(
      Promise.resolve(jsonResponse(v2Summary())),
      Promise.resolve(jsonResponse(v2Standings(seasonId))),
      Promise.resolve(
        jsonResponse({
          schemaVersion: 1,
          seasonId,
          environmentHeroes: TEST_DASHBOARD_SNAPSHOT.standings[seasonId].heroes,
        }),
      ),
    );
    const service = new DashboardDataService();

    await service.load();
    expect(
      service.snapshot.standings[seasonId].environmentHeroes,
    ).toBeUndefined();

    await service.ensureEnvironment(seasonId);

    expect(
      service.snapshot.standings[seasonId].environmentHeroes?.length,
    ).toBeGreaterThan(0);
    expect(fetchSpy.calls.mostRecent().args[0]).toBe(
      'https://vg-api.luwei.host/v2/environment?seasonId=2026-summer',
    );
  });

  it('ignores resource events for unopened views and deduplicates revisions', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    environment.useDashboardV2 = true;
    const seasonId = TEST_DASHBOARD_SNAPSHOT.currentSeasonKey;
    const environmentDocument = {
      schemaVersion: 1,
      seasonId,
      environmentHeroes: TEST_DASHBOARD_SNAPSHOT.standings[seasonId].heroes,
    };
    const fetchSpy = spyOn(window, 'fetch').and.returnValues(
      Promise.resolve(jsonResponse(v2Summary())),
      Promise.resolve(jsonResponse(v2Standings(seasonId))),
      Promise.resolve(jsonResponse(environmentDocument)),
      Promise.resolve(jsonResponse(environmentDocument)),
    );
    const service = new DashboardDataService();
    await service.load();

    expect(
      await service.refreshResource({
        kind: 'resource',
        resource: 'environment',
        seasonId,
        revision: 'environment-2',
      }),
    ).toBeFalse();
    expect(fetchSpy).toHaveBeenCalledTimes(2);

    await service.ensureEnvironment(seasonId);
    expect(
      await service.refreshResource({
        kind: 'resource',
        resource: 'environment',
        seasonId,
        revision: 'environment-2',
      }),
    ).toBeTrue();
    expect(
      await service.refreshResource({
        kind: 'resource',
        resource: 'environment',
        seasonId,
        revision: 'environment-2',
      }),
    ).toBeFalse();
    expect(fetchSpy).toHaveBeenCalledTimes(4);
  });

  it('loads only the requested players and mode for v2 trends', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    environment.useDashboardV2 = true;
    const seasonId = TEST_DASHBOARD_SNAPSHOT.currentSeasonKey;
    const playerId = TEST_DASHBOARD_SNAPSHOT.standings[seasonId].players[0].id;
    const publications = TEST_DASHBOARD_TRENDS.publications.map((publication) => ({
      snapshotId: publication.snapshotId,
      publicationDate: publication.publicationDate,
      standings: {
        [seasonId]: {
          '3v3': publication.standings[seasonId]['3v3'].filter(
            (standing) => standing.playerId === playerId,
          ),
        },
      },
    }));
    const fetchSpy = spyOn(window, 'fetch').and.returnValues(
      Promise.resolve(jsonResponse(v2Summary())),
      Promise.resolve(jsonResponse(v2Standings(seasonId))),
      Promise.resolve(
        jsonResponse({
          schemaVersion: 1,
          updatedAt: TEST_DASHBOARD_TRENDS.updatedAt,
          query: { seasonId, mode: '3v3', playerIds: [playerId] },
          publications,
        }),
      ),
    );
    const service = new DashboardDataService();

    await service.load();
    await service.ensureTrends(seasonId, '3v3', [playerId]);

    expect(fetchSpy.calls.mostRecent().args[0]).toBe(
      `https://vg-api.luwei.host/v2/trends?seasonId=${seasonId}&mode=3v3&playerIds=${playerId}`,
    );
    expect(service.trends?.publications[0].standings[seasonId].all).toEqual([]);
    expect(
      service.trends?.publications[0].standings[seasonId]['3v3'][0].playerId,
    ).toBe(playerId);
  });

  it('adds the session owner credential without putting it in the URL', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    const token = 'c'.repeat(64);
    window.sessionStorage.setItem(DASHBOARD_OWNER_TOKEN_STORAGE_KEY, token);
    const fetchSpy = spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(jsonResponse(apiDocument())),
    );
    const service = new DashboardDataService();

    await service.load();

    expect(fetchSpy).toHaveBeenCalledOnceWith(
      'https://vg-api.luwei.host/v1/dashboard',
      {
        cache: 'no-cache',
        headers: { Authorization: `Bearer ${token}` },
      },
    );
  });

  it('loads the database-backed dashboard API directly', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    const fetchSpy = spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(
        jsonResponse(
          apiDocument({ ...TEST_DASHBOARD_SNAPSHOT, matches: [] }),
        ),
      ),
    );
    const service = new DashboardDataService();

    await service.load();

    expect(service.state.kind).toBe('ready');
    expect(service.snapshot.matches).toEqual([]);
    expect(service.trends).toBe(TRENDS);
    expect(fetchSpy.calls.allArgs()).toEqual([
      ['https://vg-api.luwei.host/v1/dashboard', { cache: 'no-cache' }],
    ]);
  });

  it('accepts rating model version 7 used by the API', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    const snapshotWithV7 = {
      ...TEST_DASHBOARD_SNAPSHOT,
      ratingModel: { version: 7 },
    } as unknown as typeof TEST_DASHBOARD_SNAPSHOT;
    spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(jsonResponse(apiDocument(snapshotWithV7))),
    );
    const service = new DashboardDataService();

    await service.load();

    expect(service.state.kind).toBe('ready');
  });

  it('validates forecasts against the current score below a season peak', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    const seasonKey = TEST_DASHBOARD_SNAPSHOT.currentSeasonKey;
    const standings = TEST_DASHBOARD_SNAPSHOT.standings[seasonKey];
    const player = standings.players[0];
    const performance = player.modes['3v3'];
    const snapshotWithPeak = {
      ...TEST_DASHBOARD_SNAPSHOT,
      standings: {
        ...TEST_DASHBOARD_SNAPSHOT.standings,
        [seasonKey]: {
          ...standings,
          players: [
            {
              ...player,
              modes: {
                ...player.modes,
                '3v3': {
                  ...performance,
                  ratingScore: 940,
                  currentRatingScore: 900,
                  ratingForecast: {
                    ...performance.ratingForecast,
                    nextWinScore: 902,
                    nextLossScore: 896,
                  },
                },
              },
            },
            ...standings.players.slice(1),
          ],
        },
      },
    } as unknown as typeof TEST_DASHBOARD_SNAPSHOT;
    spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(jsonResponse(apiDocument(snapshotWithPeak))),
    );
    const service = new DashboardDataService();

    await service.load();

    expect(service.state.kind).toBe('ready');
  });

  it('does not fall back to a stale static JSON file', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    spyOn(console, 'error');
    const fetchSpy = spyOn(window, 'fetch').and.returnValue(
      Promise.reject(new Error('API unavailable')),
    );
    const service = new DashboardDataService();

    await service.load();

    expect(service.state.kind).toBe('error');
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy.calls.mostRecent().args[0]).toBe(
      'https://vg-api.luwei.host/v1/dashboard',
    );
  });

  it('notifies mounted pages only when the database revision changes', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    const nextSnapshot = {
      ...TEST_DASHBOARD_SNAPSHOT,
      snapshotId: '20260812T000000Z-refresh',
      publicationDate: '2026-08-12',
      generatedAt: '2026-08-12T00:00:00Z',
      contentRevision: 'b'.repeat(64),
    };
    const nextTrends: DashboardTrends = {
      ...TRENDS,
      publications: [
        ...TRENDS.publications,
        {
          snapshotId: nextSnapshot.snapshotId,
          publicationDate: nextSnapshot.publicationDate,
          sourceLastMatchId: nextSnapshot.sourceLastMatchId,
          standings: {},
        },
      ],
    };
    spyOn(window, 'fetch').and.returnValues(
      Promise.resolve(jsonResponse(apiDocument())),
      Promise.resolve(jsonResponse(apiDocument(nextSnapshot, nextTrends))),
    );
    const service = new DashboardDataService();
    const revisions: string[] = [];
    service.revision$.subscribe((revision) => revisions.push(revision));

    await service.load();
    const changed = await service.refresh();

    expect(changed).toBeTrue();
    expect(revisions).toEqual([
      TEST_DASHBOARD_SNAPSHOT.contentRevision ??
        TEST_DASHBOARD_SNAPSHOT.snapshotId,
      nextSnapshot.contentRevision,
    ]);
  });

  it('keeps the current data when a realtime refresh fails', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    spyOn(console, 'warn');
    spyOn(window, 'fetch').and.returnValues(
      Promise.resolve(jsonResponse(apiDocument())),
      Promise.reject(new Error('temporary failure')),
    );
    const service = new DashboardDataService();

    await service.load();
    const changed = await service.refresh();

    expect(changed).toBeFalse();
    expect(service.state.kind).toBe('ready');
    expect(service.snapshot.snapshotId).toBe(
      TEST_DASHBOARD_SNAPSHOT.snapshotId,
    );
  });

  it('rejects an API document whose trend history omits the current result', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    spyOn(console, 'error');
    spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(
        jsonResponse(
          apiDocument(TEST_DASHBOARD_SNAPSHOT, {
            ...TRENDS,
            publications: [],
          }),
        ),
      ),
    );
    const service = new DashboardDataService();

    await service.load();

    expect(service.state.kind).toBe('error');
  });
});
