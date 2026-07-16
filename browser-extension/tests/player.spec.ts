// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest';

import { PlayerDelayCalibrator } from '../src/shared/player';

describe('Bilibili player observation', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('treats the initial live buffer as a baseline instead of a rewind', () => {
    const video = document.createElement('video');
    video.currentTime = 100.5;
    Object.defineProperty(video, 'seekable', {
      value: { length: 1, start: () => 0, end: () => 119.0 },
    });
    document.body.append(video);

    expect(new PlayerDelayCalibrator().observe(document, 1_000_000)).toEqual({
      observedAtMs: 1_000_000,
      currentTimeMs: 100_500,
      seekableEndMs: 119_000,
      rawDelayMs: 18_500,
      baselineDelayMs: 18_500,
      effectiveRewindMs: 0,
    });
  });

  it('uses zero delay when no playable range exists', () => {
    expect(new PlayerDelayCalibrator().observe(document, 1_000_000)).toEqual({
      observedAtMs: 1_000_000,
      currentTimeMs: null,
      seekableEndMs: null,
      rawDelayMs: 0,
      baselineDelayMs: 0,
      effectiveRewindMs: 0,
    });
  });

  it('subtracts only a deliberate rewind beyond the sampled baseline', () => {
    const video = document.createElement('video');
    video.currentTime = 100.5;
    Object.defineProperty(video, 'seekable', {
      value: { length: 1, start: () => 0, end: () => 119 },
    });
    document.body.append(video);
    const observer = new PlayerDelayCalibrator();
    observer.sample(document);

    video.currentTime = 40.5;
    const rewound = observer.observe(document, 1_000_000);

    expect(rewound.rawDelayMs).toBe(78_500);
    expect(rewound.baselineDelayMs).toBe(18_500);
    expect(rewound.effectiveRewindMs).toBe(60_000);
  });

  it('ignores small live-buffer drift and preserves long rewind observations', () => {
    const video = document.createElement('video');
    video.currentTime = 100;
    Object.defineProperty(video, 'seekable', {
      value: { length: 1, start: () => 0, end: () => 110 },
      configurable: true,
    });
    document.body.append(video);
    const observer = new PlayerDelayCalibrator();
    observer.sample(document);

    video.currentTime = 98;
    expect(observer.observe(document, 1).effectiveRewindMs).toBe(0);

    Object.defineProperty(video, 'seekable', {
      value: { length: 1, start: () => 0, end: () => 1_000 },
    });
    video.currentTime = 0;
    expect(observer.observe(document, 2).rawDelayMs).toBe(1_000_000);
  });
});
