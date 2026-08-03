import { DashboardDataService } from './public-dashboard-data.service';
import { DashboardManifest } from './public-dashboard.models';
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
