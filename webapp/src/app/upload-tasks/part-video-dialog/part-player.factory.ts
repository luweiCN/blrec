import mpegts from 'mpegts.js';

import type {
  FlvPlaybackSource,
  PartPlayer,
  PartPlayerEventHandler,
} from './part-player.loader';

export class PartPlayerFactory {
  attachFlv(
    element: HTMLVideoElement,
    url: string,
    source: FlvPlaybackSource,
    onEvent: PartPlayerEventHandler,
  ): PartPlayer | null {
    if (!mpegts.isSupported()) {
      return null;
    }
    const sequential =
      source.playbackMode === 'sequential' ||
      (source.playbackMode === 'active_snapshot' && source.durationMs === null);
    const mediaDataSource = {
      type: 'flv',
      url,
      isLive: sequential,
      ...(sequential || source.durationMs === null
        ? {}
        : { duration: source.durationMs }),
      ...(sequential || source.fileSizeBytes === null
        ? {}
        : { filesize: source.fileSizeBytes }),
    };
    const player = mpegts.createPlayer(mediaDataSource, {
      enableWorker: false,
      enableStashBuffer: false,
      lazyLoad: !sequential,
      lazyLoadMaxDuration: 60,
      lazyLoadRecoverDuration: 15,
      autoCleanupSourceBuffer: true,
      autoCleanupMaxBackwardDuration: 120,
      autoCleanupMinBackwardDuration: 60,
    });
    player.on(
      mpegts.Events.ERROR,
      (type: unknown, detail: unknown, info: unknown) => {
        onEvent({
          type: 'error',
          message: this.describeError(type, detail, info),
        });
      },
    );
    let firstFrameReported = false;
    const firstFrame = () => {
      if (!firstFrameReported) {
        firstFrameReported = true;
        onEvent({ type: 'first_frame' });
      }
    };
    const stalled = () => onEvent({ type: 'stalled' });
    element.addEventListener('loadeddata', firstFrame);
    element.addEventListener('playing', firstFrame);
    element.addEventListener('stalled', stalled);
    player.attachMediaElement(element);
    player.load();
    onEvent({ type: 'attached' });
    return {
      pause: () => player.pause(),
      unload: () => player.unload(),
      detachMediaElement: () => player.detachMediaElement(),
      destroy: () => {
        element.removeEventListener('loadeddata', firstFrame);
        element.removeEventListener('playing', firstFrame);
        element.removeEventListener('stalled', stalled);
        player.destroy();
      },
    };
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
