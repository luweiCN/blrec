import {
  DASHBOARD_MODE_STORAGE_KEY,
  DashboardModeService,
  DashboardModeStorage,
} from './dashboard-mode.service';

class MemoryModeStorage implements DashboardModeStorage {
  private readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

describe('DashboardModeService', () => {
  it('defaults to 3V3 when no preference has been saved', () => {
    const service = new DashboardModeService(new MemoryModeStorage());

    expect(service.mode).toBe('3v3');
  });

  it('persists a selection for the next page load', () => {
    const storage = new MemoryModeStorage();
    const service = new DashboardModeService(storage);

    service.selectMode('brawl');

    expect(service.mode).toBe('brawl');
    expect(storage.getItem(DASHBOARD_MODE_STORAGE_KEY)).toBe('brawl');
    expect(new DashboardModeService(storage).mode).toBe('brawl');
  });

  it('ignores an invalid saved mode', () => {
    const storage = new MemoryModeStorage();
    storage.setItem(DASHBOARD_MODE_STORAGE_KEY, 'ranked');

    expect(new DashboardModeService(storage).mode).toBe('3v3');
  });
});
