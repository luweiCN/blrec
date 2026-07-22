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
    let destroyed = false;
    const pendingErrorTimers = new Set<number>();
    player.on(
      mpegts.Events.ERROR,
      (type: unknown, detail: unknown, info: unknown) => {
        const error = this.describeError(type, detail, info);
        if (error === null) {
          return;
        }
        const timer = window.setTimeout(() => {
          pendingErrorTimers.delete(timer);
          if (!destroyed) {
            onEvent({ type: 'error', ...error });
          }
        });
        pendingErrorTimers.add(timer);
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
        destroyed = true;
        for (const timer of pendingErrorTimers) {
          window.clearTimeout(timer);
        }
        pendingErrorTimers.clear();
        element.removeEventListener('loadeddata', firstFrame);
        element.removeEventListener('playing', firstFrame);
        element.removeEventListener('stalled', stalled);
        player.destroy();
      },
    };
  }

  private describeError(
    type: unknown,
    detail: unknown,
    info: unknown,
  ): { readonly message: string; readonly recoverable: boolean } | null {
    if (type === mpegts.ErrorTypes.NETWORK_ERROR) {
      const code = this.errorCode(info);
      return {
        message:
          code === null
            ? '读取本地视频失败'
            : `读取本地视频失败（HTTP ${code}）`,
        recoverable: true,
      };
    }
    if (detail === mpegts.ErrorDetails.MEDIA_MSE_ERROR) {
      return {
        message: '浏览器视频缓冲异常',
        recoverable: true,
      };
    }
    if (detail === mpegts.ErrorDetails.MEDIA_CODEC_UNSUPPORTED) {
      if (info === 'Flv: Unsupported codec in video frame: 0') {
        return null;
      }
      return {
        message: '该录像的编码当前浏览器无法播放',
        recoverable: false,
      };
    }
    if (
      detail === mpegts.ErrorDetails.MEDIA_FORMAT_ERROR ||
      detail === mpegts.ErrorDetails.MEDIA_FORMAT_UNSUPPORTED
    ) {
      return {
        message: '该录像的媒体格式异常，当前浏览器无法继续播放',
        recoverable: false,
      };
    }
    if (typeof detail === 'string' && detail.length > 0) {
      return {
        message: `本地视频播放失败：${detail}`,
        recoverable: false,
      };
    }
    return {
      message: '本地视频播放失败，请重新打开后再试',
      recoverable: false,
    };
  }

  private errorCode(info: unknown): number | null {
    if (typeof info !== 'object' || info === null || !('code' in info)) {
      return null;
    }
    const code = (info as { code?: unknown }).code;
    return typeof code === 'number' ? code : null;
  }
}
