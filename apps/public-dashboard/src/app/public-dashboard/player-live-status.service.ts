import { Injectable, OnDestroy } from '@angular/core';
import { BehaviorSubject, Observable } from 'rxjs';

import { environment } from '../../environments/environment';

export interface PlayerLiveRoomStatus {
  readonly roomId: number;
  readonly playerId: number;
  readonly title: string;
  readonly startedAt: string;
}

interface LiveRoomsDocument {
  readonly schemaVersion: 1;
  readonly updatedAt: string;
  readonly rooms: readonly PlayerLiveRoomStatus[];
}

@Injectable({ providedIn: 'root' })
export class PlayerLiveStatusService implements OnDestroy {
  private readonly roomStatusesSubject = new BehaviorSubject<
    ReadonlyMap<number, PlayerLiveRoomStatus>
  >(new Map());
  private started = false;
  private refreshing = false;

  readonly roomStatuses$: Observable<
    ReadonlyMap<number, PlayerLiveRoomStatus>
  > = this.roomStatusesSubject.asObservable();

  start(): void {
    this.started = true;
    if (environment.apiBaseUrl.trim() === '') {
      this.roomStatusesSubject.next(new Map());
      return;
    }
    void this.refresh();
  }

  stop(): void {
    this.started = false;
  }

  ngOnDestroy(): void {
    this.stop();
  }

  async refresh(): Promise<void> {
    if (!this.started || this.refreshing) {
      return;
    }
    this.refreshing = true;
    const apiBaseUrl = environment.apiBaseUrl.replace(/\/+$/u, '');
    try {
      const response = await fetch(`${apiBaseUrl}/live-rooms`, {
        cache: 'no-cache',
      });
      if (!response.ok) {
        throw new Error(`fetch live rooms failed with ${response.status}`);
      }
      const document = parseLiveRoomsDocument(await response.json());
      this.roomStatusesSubject.next(
        new Map(document.rooms.map((room) => [room.roomId, room])),
      );
    } catch (error: unknown) {
      console.warn('Unable to refresh player live status', error);
    } finally {
      this.refreshing = false;
    }
  }
}

function parseLiveRoomsDocument(value: unknown): LiveRoomsDocument {
  if (!isObject(value)) {
    throw new Error('live rooms API returned an unsupported response');
  }
  const rooms = value['rooms'];
  if (
    value['schemaVersion'] !== 1 ||
    typeof value['updatedAt'] !== 'string' ||
    Number.isNaN(Date.parse(value['updatedAt'])) ||
    !Array.isArray(rooms) ||
    rooms.length > 500 ||
    !rooms.every(isLiveRoom)
  ) {
    throw new Error('live rooms API returned an unsupported response');
  }
  const roomIds = rooms.map((room) => room.roomId);
  if (new Set(roomIds).size !== roomIds.length) {
    throw new Error('live rooms API returned duplicate rooms');
  }
  return value as unknown as LiveRoomsDocument;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) > 0;
}

function isLiveRoom(value: unknown): value is PlayerLiveRoomStatus {
  return (
    isObject(value) &&
    isPositiveInteger(value['roomId']) &&
    isPositiveInteger(value['playerId']) &&
    typeof value['title'] === 'string' &&
    value['title'].length <= 240 &&
    typeof value['startedAt'] === 'string' &&
    !Number.isNaN(Date.parse(value['startedAt']))
  );
}
