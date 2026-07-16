import mpegts from 'mpegts.js';

import { PartPlayerFactory } from './part-player.factory';

describe('PartPlayerFactory', () => {
  let createPlayer: jasmine.Spy;
  let player: jasmine.SpyObj<ReturnType<typeof mpegts.createPlayer>>;

  beforeEach(() => {
    player = jasmine.createSpyObj<ReturnType<typeof mpegts.createPlayer>>(
      'Player',
      ['on', 'attachMediaElement', 'load']
    );
    spyOn(mpegts, 'isSupported').and.returnValue(true);
    createPlayer = spyOn(mpegts, 'createPlayer').and.returnValue(player);
  });

  it('loads a finite recording snapshot lazily without a transmuxing worker', () => {
    const element = document.createElement('video');

    new PartPlayerFactory().attachFlv(
      element,
      '/api/media?signed',
      {
        isLive: false,
        durationMs: 12_500,
        fileSizeBytes: 1_024 * 1_024 * 1_024,
      },
      () => undefined
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
        enableStashBuffer: false,
        lazyLoad: true,
      })
    );
    expect(player.attachMediaElement).toHaveBeenCalledOnceWith(element);
    expect(player.load).toHaveBeenCalled();
  });
});
