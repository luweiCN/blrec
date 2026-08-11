import { firstValueFrom } from 'rxjs';
import { filter, take } from 'rxjs/operators';

import { environment } from '../../environments/environment';
import { PlayerLiveStatusService } from './player-live-status.service';

function jsonResponse(value: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(value),
  } as Response;
}

describe('PlayerLiveStatusService', () => {
  const originalApiBaseUrl = environment.apiBaseUrl;
  let service: PlayerLiveStatusService;

  beforeEach(() => {
    environment.apiBaseUrl = 'https://vg-api.luwei.host/v1';
    service = new PlayerLiveStatusService();
  });

  afterEach(() => {
    service.stop();
    environment.apiBaseUrl = originalApiBaseUrl;
  });

  it('loads current live rooms from the lightweight API', async () => {
    const fetchSpy = spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(
        jsonResponse({
          schemaVersion: 1,
          updatedAt: '2026-08-11T11:30:05Z',
          rooms: [
            {
              roomId: 24767459,
              playerId: 56,
              title: '今晚三排上分',
              startedAt: '2026-08-11T11:30:00Z',
            },
          ],
        }),
      ),
    );
    const liveRooms = firstValueFrom(
      service.roomStatuses$.pipe(
        filter((rooms) => rooms.size > 0),
        take(1),
      ),
    );

    service.start();

    expect((await liveRooms).get(24767459)?.title).toBe('今晚三排上分');
    expect(fetchSpy.calls.mostRecent().args).toEqual([
      'https://vg-api.luwei.host/v1/live-rooms',
      { cache: 'no-cache' },
    ]);
  });

  it('keeps the previous status when the API response is invalid', async () => {
    spyOn(console, 'warn');
    spyOn(window, 'fetch').and.returnValue(
      Promise.resolve(jsonResponse({ schemaVersion: 1, rooms: [] })),
    );
    let latestSize = -1;
    const subscription = service.roomStatuses$.subscribe((rooms) => {
      latestSize = rooms.size;
    });

    service.start();
    await Promise.resolve();
    await Promise.resolve();

    expect(latestSize).toBe(0);
    subscription.unsubscribe();
  });
});
