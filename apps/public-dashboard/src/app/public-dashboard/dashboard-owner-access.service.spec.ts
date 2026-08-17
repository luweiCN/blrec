import { environment } from '../../environments/environment';
import {
  dashboardRequestInit,
  DASHBOARD_OWNER_TOKEN_STORAGE_KEY,
  DashboardOwnerAccessService,
} from './dashboard-owner-access.service';

function response(ok: boolean, status: number): Response {
  return { ok, status } as Response;
}

describe('DashboardOwnerAccessService', () => {
  const originalApiBaseUrl = environment.apiBaseUrl;

  beforeEach(() => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    window.sessionStorage.removeItem(DASHBOARD_OWNER_TOKEN_STORAGE_KEY);
  });

  afterEach(() => {
    environment.apiBaseUrl = originalApiBaseUrl;
    window.sessionStorage.removeItem(DASHBOARD_OWNER_TOKEN_STORAGE_KEY);
  });

  it('validates and stores an owner token only for the current session', async () => {
    const token = 'a'.repeat(64);
    const fetchSpy = spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(response(true, 200)),
    );
    const service = new DashboardOwnerAccessService();

    expect(await service.unlock(token)).toBeTrue();
    expect(service.active).toBeTrue();
    expect(fetchSpy).toHaveBeenCalledOnceWith(
      'https://vg-api.luwei.host/v1/owner/session',
      {
        cache: 'no-store',
        headers: { Authorization: `Bearer ${token}` },
      },
    );
    expect(dashboardRequestInit('no-cache')).toEqual({
      cache: 'no-cache',
      headers: { Authorization: `Bearer ${token}` },
    });

    service.lock();
    expect(service.active).toBeFalse();
    expect(dashboardRequestInit('no-cache')).toEqual({ cache: 'no-cache' });
  });

  it('does not retain rejected or malformed tokens', async () => {
    const fetchSpy = spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(response(false, 401)),
    );
    const service = new DashboardOwnerAccessService();

    expect(await service.unlock('short')).toBeFalse();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(await service.unlock('b'.repeat(64))).toBeFalse();
    expect(service.active).toBeFalse();
    expect(window.sessionStorage.getItem(DASHBOARD_OWNER_TOKEN_STORAGE_KEY)).toBeNull();
  });
});
