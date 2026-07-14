import { Injectable } from '@angular/core';

import mpegts from 'mpegts.js';

export interface PartPlayer {
  pause(): void;
  unload(): void;
  detachMediaElement(): void;
  destroy(): void;
}

@Injectable({ providedIn: 'root' })
export class PartPlayerFactory {
  attachFlv(
    element: HTMLVideoElement,
    url: string,
    isLive: boolean
  ): PartPlayer | null {
    if (!mpegts.isSupported()) {
      return null;
    }
    const player = mpegts.createPlayer(
      { type: 'flv', url, isLive },
      {
        enableWorker: true,
        enableStashBuffer: !isLive,
        lazyLoad: !isLive,
      }
    );
    player.attachMediaElement(element);
    player.load();
    return player;
  }
}
