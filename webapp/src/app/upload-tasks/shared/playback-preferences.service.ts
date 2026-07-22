import { Injectable } from '@angular/core';

import { StorageService } from 'src/app/core/services/storage.service';

const PLAYBACK_VOLUME_KEY = 'blrec-playback-volume';
const PLAYBACK_POSITION_PREFIX = 'blrec-playback-position-';

export const DEFAULT_PLAYBACK_VOLUME = 0.5;

@Injectable({ providedIn: 'root' })
export class PlaybackPreferencesService {
  constructor(private storage: StorageService) {}

  get volume(): number {
    const stored = this.storage.getData(PLAYBACK_VOLUME_KEY);
    if (stored === null) {
      return DEFAULT_PLAYBACK_VOLUME;
    }
    const volume = Number(stored);
    return Number.isFinite(volume) && volume >= 0 && volume <= 1
      ? volume
      : DEFAULT_PLAYBACK_VOLUME;
  }

  rememberVolume(value: number): number {
    const volume = Math.max(0, Math.min(1, value));
    this.storage.setData(PLAYBACK_VOLUME_KEY, String(volume));
    return volume;
  }

  position(partId: number): number | null {
    const stored = this.storage.getData(this.positionKey(partId));
    if (stored === null) {
      return null;
    }
    const seconds = Number(stored);
    return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
  }

  rememberPosition(partId: number, seconds: number): void {
    if (!Number.isInteger(partId) || partId <= 0 || !Number.isFinite(seconds)) {
      return;
    }
    this.storage.setData(
      this.positionKey(partId),
      Math.max(0, seconds).toFixed(3),
    );
  }

  clearPosition(partId: number): void {
    this.storage.removeData(this.positionKey(partId));
  }

  private positionKey(partId: number): string {
    return `${PLAYBACK_POSITION_PREFIX}${partId}`;
  }
}
