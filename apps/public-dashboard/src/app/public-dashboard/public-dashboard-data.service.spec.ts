import { environment } from '../../environments/environment';
import { DashboardDataService } from './public-dashboard-data.service';
import { DashboardTrends } from './public-dashboard.models';
import { TEST_DASHBOARD_SNAPSHOT } from './public-dashboard.test-data';

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

describe('DashboardDataService', () => {
  const originalApiBaseUrl = environment.apiBaseUrl;

  afterEach(() => {
    environment.apiBaseUrl = originalApiBaseUrl;
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

  it('accepts the next rating model before the API switches to it', async () => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    const snapshotWithV6 = {
      ...TEST_DASHBOARD_SNAPSHOT,
      ratingModel: { version: 6 },
    } as unknown as typeof TEST_DASHBOARD_SNAPSHOT;
    spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(jsonResponse(apiDocument(snapshotWithV6))),
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
