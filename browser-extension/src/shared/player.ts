export interface PlayerObservation {
  readonly observedAtMs: number;
  readonly playerDelayMs: number;
}

export function observePlayer(
  document: Document,
  nowMs: number
): PlayerObservation {
  const video = document.querySelector<HTMLVideoElement>('video');
  if (!video || video.seekable.length === 0) {
    return { observedAtMs: nowMs, playerDelayMs: 0 };
  }
  let liveEdge: number;
  try {
    liveEdge = video.seekable.end(video.seekable.length - 1);
  } catch (_error) {
    return { observedAtMs: nowMs, playerDelayMs: 0 };
  }
  const delayMs = Math.round((liveEdge - video.currentTime) * 1000);
  return {
    observedAtMs: nowMs,
    playerDelayMs: Math.max(0, Math.min(300_000, delayMs)),
  };
}
