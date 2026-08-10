import { DashboardDataService } from './public-dashboard-data.service';
import {
  DashboardManifest,
  DashboardTrends,
} from './public-dashboard.models';
import { TEST_DASHBOARD_SNAPSHOT } from './public-dashboard.test-data';

const MANIFEST: DashboardManifest = {
  schemaVersion: 1,
  snapshotId: TEST_DASHBOARD_SNAPSHOT.snapshotId,
  snapshotPath: `snapshots/${TEST_DASHBOARD_SNAPSHOT.snapshotId}.json`,
  publicationDate: TEST_DASHBOARD_SNAPSHOT.publicationDate,
  generatedAt: TEST_DASHBOARD_SNAPSHOT.generatedAt,
  sourceLastMatchId: TEST_DASHBOARD_SNAPSHOT.sourceLastMatchId,
  sha256: 'a'.repeat(64),
  bytes: 1024,
};

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

function jsonResponse(value: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(value),
  } as Response;
}

describe('DashboardDataService', () => {
  it('loads the manifest before its immutable snapshot', async () => {
    const fetchSpy = spyOn(window, 'fetch').and.returnValues(
      Promise.resolve(jsonResponse(MANIFEST)),
      Promise.resolve(jsonResponse(TEST_DASHBOARD_SNAPSHOT)),
      Promise.resolve(jsonResponse(TRENDS)),
    );
    const service = new DashboardDataService();

    await service.load();

    expect(service.state.kind).toBe('ready');
    expect(service.snapshot.snapshotId).toBe(MANIFEST.snapshotId);
    expect(fetchSpy.calls.argsFor(0)).toEqual([
      'data/manifest.json',
      { cache: 'no-store' },
    ]);
    expect(fetchSpy.calls.argsFor(1)).toEqual([
      `data/${MANIFEST.snapshotPath}`,
      { cache: 'force-cache' },
    ]);
    expect(fetchSpy.calls.argsFor(2)).toEqual([
      `data/trends.json?v=${TEST_DASHBOARD_SNAPSHOT.snapshotId}`,
      { cache: 'no-store' },
    ]);
    expect(service.trends).toBe(TRENDS);
  });

  it('keeps accepting version 1 snapshots during the rollout', async () => {
    const legacySnapshot = {
      ...TEST_DASHBOARD_SNAPSHOT,
      ratingModel: {
        version: 1,
        priorMatches: 20,
        carryoverRate: 0.25,
        credibleLevel: 0.9,
        provisionalMatches: 5,
      },
    };
    spyOn(window, 'fetch').and.returnValues(
      Promise.resolve(jsonResponse(MANIFEST)),
      Promise.resolve(jsonResponse(legacySnapshot)),
      Promise.resolve(jsonResponse(TRENDS)),
    );
    const service = new DashboardDataService();

    await service.load();

    expect(service.state.kind).toBe('ready');
  });

  it('keeps the ranking available when trend data is missing or invalid', async () => {
    spyOn(console, 'warn');
    spyOn(window, 'fetch').and.returnValues(
      Promise.resolve(jsonResponse(MANIFEST)),
      Promise.resolve(jsonResponse(TEST_DASHBOARD_SNAPSHOT)),
      Promise.resolve(jsonResponse({ schemaVersion: 99 })),
    );
    const service = new DashboardDataService();

    await service.load();

    expect(service.state.kind).toBe('ready');
    expect(service.trends).toBeNull();
  });

  it('rejects version 2 rating metadata without its outcome delta', async () => {
    spyOn(console, 'error');
    const invalidSnapshot = {
      ...TEST_DASHBOARD_SNAPSHOT,
      ratingModel: {
        version: 2,
        priorMatches: 20,
        carryoverRate: 0.25,
        credibleLevel: 0.9,
        provisionalMatches: 5,
      },
    };
    spyOn(window, 'fetch').and.returnValues(
      Promise.resolve(jsonResponse(MANIFEST)),
      Promise.resolve(jsonResponse(invalidSnapshot)),
    );
    const service = new DashboardDataService();

    await service.load();

    expect(service.state.kind).toBe('error');
  });

  it('rejects a manifest that points outside the data directory', async () => {
    spyOn(console, 'error');
    spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(
        jsonResponse({ ...MANIFEST, snapshotPath: '../private.json' }),
      ),
    );
    const service = new DashboardDataService();

    await service.load();

    expect(service.state.kind).toBe('error');
    expect(service.snapshotOrNull).toBeNull();
  });
});
