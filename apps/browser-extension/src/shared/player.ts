export interface PlayerObservation {
  readonly observedAtMs: number;
  readonly currentTimeMs: number | null;
  readonly seekableEndMs: number | null;
  readonly rawDelayMs: number;
  readonly baselineDelayMs: number;
  readonly effectiveRewindMs: number;
}

const MAX_DELAY_MS = 86_400_000;
const BUFFER_DRIFT_TOLERANCE_MS = 5_000;

interface PlayerPosition {
  readonly video: HTMLVideoElement;
  readonly currentTimeMs: number;
  readonly seekableEndMs: number;
  readonly rawDelayMs: number;
}

export class PlayerDelayCalibrator {
  private readonly baselines = new WeakMap<HTMLVideoElement, number>();

  sample(document: Document): void {
    const position = this.position(document);
    if (!position) {
      return;
    }
    const previous = this.baselines.get(position.video);
    this.baselines.set(
      position.video,
      previous === undefined
        ? position.rawDelayMs
        : Math.min(previous, position.rawDelayMs),
    );
  }

  observe(document: Document, nowMs: number): PlayerObservation {
    const position = this.position(document);
    if (!position) {
      return {
        observedAtMs: nowMs,
        currentTimeMs: null,
        seekableEndMs: null,
        rawDelayMs: 0,
        baselineDelayMs: 0,
        effectiveRewindMs: 0,
      };
    }
    const sampledBaseline = this.baselines.get(position.video);
    const baselineDelayMs = sampledBaseline ?? position.rawDelayMs;
    if (sampledBaseline === undefined) {
      this.baselines.set(position.video, baselineDelayMs);
    } else if (position.rawDelayMs < sampledBaseline) {
      this.baselines.set(position.video, position.rawDelayMs);
    }
    const delta = Math.max(0, position.rawDelayMs - baselineDelayMs);
    return {
      observedAtMs: nowMs,
      currentTimeMs: position.currentTimeMs,
      seekableEndMs: position.seekableEndMs,
      rawDelayMs: position.rawDelayMs,
      baselineDelayMs,
      effectiveRewindMs:
        delta <= BUFFER_DRIFT_TOLERANCE_MS ? 0 : delta,
    };
  }

  private position(document: Document): PlayerPosition | null {
    const video = document.querySelector<HTMLVideoElement>('video');
    if (!video || video.seekable.length === 0) {
      return null;
    }
    let liveEdge: number;
    try {
      liveEdge = video.seekable.end(video.seekable.length - 1);
    } catch (_error) {
      return null;
    }
    if (!Number.isFinite(liveEdge) || !Number.isFinite(video.currentTime)) {
      return null;
    }
    const currentTimeMs = Math.max(0, Math.round(video.currentTime * 1_000));
    const seekableEndMs = Math.max(0, Math.round(liveEdge * 1_000));
    const rawDelayMs = Math.max(
      0,
      Math.min(MAX_DELAY_MS, seekableEndMs - currentTimeMs),
    );
    return { video, currentTimeMs, seekableEndMs, rawDelayMs };
  }
}
