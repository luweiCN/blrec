// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest';

import { observePlayer } from '../src/shared/player';

describe('Bilibili player observation', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('measures delay from the current position to the seekable live edge', () => {
    const video = document.createElement('video');
    video.currentTime = 100.5;
    Object.defineProperty(video, 'seekable', {
      value: { length: 1, start: () => 0, end: () => 119.0 },
    });
    document.body.append(video);

    expect(observePlayer(document, 1_000_000)).toEqual({
      observedAtMs: 1_000_000,
      playerDelayMs: 18_500,
    });
  });

  it('uses zero delay when no playable range exists', () => {
    expect(observePlayer(document, 1_000_000)).toEqual({
      observedAtMs: 1_000_000,
      playerDelayMs: 0,
    });
  });

  it('clamps extreme and negative delays', () => {
    const video = document.createElement('video');
    video.currentTime = 400;
    Object.defineProperty(video, 'seekable', {
      value: { length: 1, start: () => 0, end: () => 1000 },
    });
    document.body.append(video);
    expect(observePlayer(document, 1).playerDelayMs).toBe(300_000);

    video.currentTime = 1100;
    expect(observePlayer(document, 2).playerDelayMs).toBe(0);
  });
});
