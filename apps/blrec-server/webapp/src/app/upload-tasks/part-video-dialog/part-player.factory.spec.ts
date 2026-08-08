import mpegts from 'mpegts.js';
import { fakeAsync, tick } from '@angular/core/testing';

import { PartPlayerFactory } from './part-player.factory';
import type {
  FlvPlaybackSource,
  PartPlayerEventHandler,
} from './part-player.loader';

describe('PartPlayerFactory', () => {
  let createPlayer: jasmine.Spy;
  let player: jasmine.SpyObj<ReturnType<typeof mpegts.createPlayer>>;

  beforeEach(() => {
    player = jasmine.createSpyObj<ReturnType<typeof mpegts.createPlayer>>(
      'Player',
      ['on', 'attachMediaElement', 'load'],
    );
    spyOn(mpegts, 'isSupported').and.returnValue(true);
    createPlayer = spyOn(mpegts, 'createPlayer').and.returnValue(player);
  });

  it('loads a finite recording snapshot lazily without a transmuxing worker', () => {
    const element = document.createElement('video');
    const source: FlvPlaybackSource = {
      playbackMode: 'seekable',
      durationMs: 12_500,
      fileSizeBytes: 1_024 * 1_024 * 1_024,
    };
    const onEvent: PartPlayerEventHandler = () => undefined;

    new PartPlayerFactory().attachFlv(
      element,
      '/api/media?signed',
      source,
      onEvent,
    );

    expect(createPlayer).toHaveBeenCalledWith(
      {
        type: 'flv',
        url: '/api/media?signed',
        isLive: false,
        duration: 12_500,
        filesize: 1_024 * 1_024 * 1_024,
      },
      jasmine.objectContaining({
        enableWorker: false,
        enableStashBuffer: true,
        lazyLoad: true,
        lazyLoadMaxDuration: 120,
        lazyLoadRecoverDuration: 60,
      }),
    );
    expect(player.attachMediaElement).toHaveBeenCalledOnceWith(element);
    expect(player.load).toHaveBeenCalled();
  });

  it('plays an unindexed FLV sequentially without advertising file metadata', () => {
    const element = document.createElement('video');

    new PartPlayerFactory().attachFlv(
      element,
      '/api/media?signed',
      {
        playbackMode: 'sequential',
        durationMs: null,
        fileSizeBytes: 1_024,
      },
      () => undefined,
    );

    expect(createPlayer).toHaveBeenCalledWith(
      { type: 'flv', url: '/api/media?signed', isLive: true },
      jasmine.objectContaining({ lazyLoad: false }),
    );
  });

  it('marks an MSE buffer failure as recoverable instead of a codec error', fakeAsync(() => {
    const onEvent = jasmine.createSpy<PartPlayerEventHandler>('onEvent');
    new PartPlayerFactory().attachFlv(
      document.createElement('video'),
      '/api/media?signed',
      {
        playbackMode: 'seekable',
        durationMs: 10_000,
        fileSizeBytes: 1_024,
      },
      onEvent,
    );
    onEvent.calls.reset();
    const errorListener = player.on.calls.argsFor(0)[1] as (
      type: unknown,
      detail: unknown,
      info: unknown,
    ) => void;

    errorListener(
      mpegts.ErrorTypes.MEDIA_ERROR,
      mpegts.ErrorDetails.MEDIA_MSE_ERROR,
      { code: 22 },
    );

    expect(onEvent).not.toHaveBeenCalled();
    tick();
    expect(onEvent).toHaveBeenCalledWith({
      type: 'error',
      message: '浏览器视频缓冲异常',
      recoverable: true,
    });
  }));

  it('only reports codec incompatibility for the explicit codec error', fakeAsync(() => {
    const onEvent = jasmine.createSpy<PartPlayerEventHandler>('onEvent');
    new PartPlayerFactory().attachFlv(
      document.createElement('video'),
      '/api/media?signed',
      {
        playbackMode: 'seekable',
        durationMs: 10_000,
        fileSizeBytes: 1_024,
      },
      onEvent,
    );
    onEvent.calls.reset();
    const errorListener = player.on.calls.argsFor(0)[1] as (
      type: unknown,
      detail: unknown,
      info: unknown,
    ) => void;

    errorListener(
      mpegts.ErrorTypes.MEDIA_ERROR,
      mpegts.ErrorDetails.MEDIA_CODEC_UNSUPPORTED,
      {},
    );

    tick();
    expect(onEvent).toHaveBeenCalledWith({
      type: 'error',
      message: '该录像的编码当前浏览器无法播放',
      recoverable: false,
    });
  }));

  it('keeps playing past an isolated malformed FLV video tag', fakeAsync(() => {
    const onEvent = jasmine.createSpy<PartPlayerEventHandler>('onEvent');
    new PartPlayerFactory().attachFlv(
      document.createElement('video'),
      '/api/media?signed',
      {
        playbackMode: 'seekable',
        durationMs: 10_000,
        fileSizeBytes: 1_024,
      },
      onEvent,
    );
    onEvent.calls.reset();
    const errorListener = player.on.calls.argsFor(0)[1] as (
      type: unknown,
      detail: unknown,
      info: unknown,
    ) => void;

    errorListener(
      mpegts.ErrorTypes.MEDIA_ERROR,
      mpegts.ErrorDetails.MEDIA_CODEC_UNSUPPORTED,
      'Flv: Unsupported codec in video frame: 0',
    );
    tick();

    expect(onEvent).not.toHaveBeenCalled();
  }));
});
