import { Injectable } from '@angular/core';

import mpegts from 'mpegts.js';

export interface PartPlayer {
  pause(): void;
  unload(): void;
  detachMediaElement(): void;
  destroy(): void;
}

export type PartPlayerErrorHandler = (message: string) => void;

export interface FlvPlaybackSource {
  readonly isLive: boolean;
  readonly durationMs: number | null;
  readonly fileSizeBytes: number | null;
}

@Injectable({ providedIn: 'root' })
export class PartPlayerFactory {
  attachFlv(
    element: HTMLVideoElement,
    url: string,
    source: FlvPlaybackSource,
    onError: PartPlayerErrorHandler
  ): PartPlayer | null {
    if (!mpegts.isSupported()) {
      return null;
    }
    const mediaDataSource = {
      type: 'flv',
      url,
      isLive: source.isLive,
      ...(source.durationMs === null ? {} : { duration: source.durationMs }),
      ...(source.fileSizeBytes === null
        ? {}
        : { filesize: source.fileSizeBytes }),
    };
    const player = mpegts.createPlayer(mediaDataSource, {
      enableWorker: false,
      enableStashBuffer: false,
      lazyLoad: !source.isLive,
      lazyLoadMaxDuration: 60,
      lazyLoadRecoverDuration: 15,
      autoCleanupSourceBuffer: true,
      autoCleanupMaxBackwardDuration: 120,
      autoCleanupMinBackwardDuration: 60,
    });
    player.on(
      mpegts.Events.ERROR,
      (type: unknown, detail: unknown, info: unknown) => {
        onError(this.describeError(type, detail, info));
      }
    );
    player.attachMediaElement(element);
    player.load();
    return player;
  }

  private describeError(type: unknown, detail: unknown, info: unknown): string {
    const code = this.errorCode(info);
    if (code !== null) {
      return `读取本地视频失败（HTTP ${code}）`;
    }
    if (type === mpegts.ErrorTypes.MEDIA_ERROR) {
      return '该录像的编码当前浏览器无法播放';
    }
    if (type === mpegts.ErrorTypes.NETWORK_ERROR) {
      return '读取本地视频失败，请检查连接后重新打开';
    }
    if (typeof detail === 'string' && detail.length > 0) {
      return `本地视频播放失败：${detail}`;
    }
    return '本地视频播放失败，请重新打开后再试';
  }

  private errorCode(info: unknown): number | null {
    if (typeof info !== 'object' || info === null || !('code' in info)) {
      return null;
    }
    const code = (info as { code?: unknown }).code;
    return typeof code === 'number' ? code : null;
  }
}
