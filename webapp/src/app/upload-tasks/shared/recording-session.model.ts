export const RECORDING_SESSION_STATES = [
  'open',
  'closed',
  'cancelled',
  'manual_review',
  'skipped',
] as const;

export type RecordingSessionState =
  (typeof RECORDING_SESSION_STATES)[number];

export const RECORDING_ARTIFACT_STATES = [
  'recording',
  'postprocessing',
  'ready',
  'failed',
  'missing',
  'manual_review',
] as const;

export type RecordingArtifactState =
  (typeof RECORDING_ARTIFACT_STATES)[number];

export interface RecordingPart {
  readonly id: number;
  readonly runId: string;
  readonly partIndex: number;
  readonly sourcePath: string;
  readonly finalPath: string | null;
  readonly xmlPath: string | null;
  readonly recordStartTime: number;
  readonly artifactState: RecordingArtifactState;
  readonly xmlCompleted: boolean;
  readonly sourceExists: boolean;
  readonly finalExists: boolean;
  readonly errorMessage: string | null;
}

export interface RecordingSession {
  readonly id: number;
  readonly roomId: number;
  readonly broadcastSessionKey: string;
  readonly liveStartTime: number | null;
  readonly state: RecordingSessionState;
  readonly startedAt: number;
  readonly endedAt: number | null;
  readonly parts: readonly RecordingPart[];
}

export interface RecordingSessionsResponse {
  readonly degradedReason: string | null;
  readonly sessions: readonly RecordingSession[];
}

export type RecordingSessionsView =
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly response: RecordingSessionsResponse }
  | { readonly state: 'error'; readonly message: string };
