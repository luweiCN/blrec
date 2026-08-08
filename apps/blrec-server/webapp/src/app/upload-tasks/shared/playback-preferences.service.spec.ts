import { TestBed } from '@angular/core/testing';

import {
  DEFAULT_PLAYBACK_VOLUME,
  PlaybackPreferencesService,
} from './playback-preferences.service';

describe('PlaybackPreferencesService', () => {
  let service: PlaybackPreferencesService;

  beforeEach(() => {
    localStorage.removeItem('blrec-playback-volume');
    localStorage.removeItem('blrec-playback-rate');
    localStorage.removeItem('blrec-playback-position-42');
    service = TestBed.inject(PlaybackPreferencesService);
  });

  afterEach(() => {
    localStorage.removeItem('blrec-playback-volume');
    localStorage.removeItem('blrec-playback-rate');
    localStorage.removeItem('blrec-playback-position-42');
  });

  it('defaults to half volume and remembers later changes', () => {
    expect(service.volume).toBe(DEFAULT_PLAYBACK_VOLUME);

    service.rememberVolume(0.7);

    expect(service.volume).toBe(0.7);
  });

  it('remembers playback position by recording part', () => {
    service.rememberPosition(42, 12.3456);

    expect(service.position(42)).toBe(12.346);

    service.clearPosition(42);
    expect(service.position(42)).toBeNull();
  });

  it('defaults to normal speed and remembers a supported playback rate', () => {
    expect(service.rate).toBe(1);

    service.rememberRate(1.5);

    expect(service.rate).toBe(1.5);
  });
});
