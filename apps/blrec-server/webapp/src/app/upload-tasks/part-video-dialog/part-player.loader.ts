import { InjectionToken } from '@angular/core';

export interface PartPlayer {
  pause(): void;
  unload(): void;
  detachMediaElement(): void;
  destroy(): void;
}

export type PartPlayerEvent =
  | { readonly type: 'attached' }
  | { readonly type: 'first_frame' }
  | { readonly type: 'stalled' }
  | {
      readonly type: 'error';
      readonly message: string;
      readonly recoverable: boolean;
    };

export type PartPlayerEventHandler = (event: PartPlayerEvent) => void;

export interface FlvPlaybackSource {
  readonly playbackMode: 'seekable' | 'sequential' | 'active_snapshot';
  readonly durationMs: number | null;
  readonly fileSizeBytes: number | null;
}

export interface PartPlayerFactoryLike {
  attachFlv(
    element: HTMLVideoElement,
    url: string,
    source: FlvPlaybackSource,
    onEvent: PartPlayerEventHandler,
  ): PartPlayer | null;
}

export type PartPlayerLoader = () => Promise<PartPlayerFactoryLike>;

export const loadPartPlayerFactory =
  async (): Promise<PartPlayerFactoryLike> => {
    const module = await import('./part-player.factory');
    return new module.PartPlayerFactory();
  };

export const PART_PLAYER_LOADER = new InjectionToken<PartPlayerLoader>(
  'PART_PLAYER_LOADER',
  { providedIn: 'root', factory: () => loadPartPlayerFactory },
);
