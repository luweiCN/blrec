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

export type UploadJobState =
  | 'waiting_artifacts'
  | 'ready'
  | 'uploading'
  | 'submitting'
  | 'waiting_review'
  | 'approved'
  | 'rejected'
  | 'paused'
  | 'completed';

export type UploadSubmitState =
  | 'prepared'
  | 'in_flight'
  | 'confirmed'
  | 'unknown_outcome'
  | 'failed_permanent';

export type CommentBranchState =
  | 'disabled'
  | 'pending'
  | 'running'
  | 'skipped_no_content'
  | 'skipped_source_missing'
  | 'completed'
  | 'paused'
  | 'failed';

export type DanmakuBranchState =
  | 'disabled'
  | 'pending'
  | 'importing'
  | 'publishing'
  | 'skipped_source_missing'
  | 'completed'
  | 'paused'
  | 'failed';

export type UploadPartState =
  | 'prepared'
  | 'preupload'
  | 'uploading'
  | 'completing'
  | 'confirmed'
  | 'unknown_outcome'
  | 'failed';

export type DanmakuImportState =
  | 'disabled'
  | 'pending'
  | 'importing'
  | 'waiting_capacity'
  | 'missing_source'
  | 'completed'
  | 'failed';

export interface UploadPartProgress {
  readonly id: number;
  readonly partIndex: number;
  readonly uploadState: UploadPartState;
  readonly danmakuImportState: DanmakuImportState;
  readonly remoteFilename: string | null;
  readonly cid: number | null;
}

export interface UploadJobProgress {
  readonly id: number;
  readonly accountId: number;
  readonly accountUid: number;
  readonly accountDisplayName: string;
  readonly state: UploadJobState;
  readonly submitState: UploadSubmitState;
  readonly commentBranchState: CommentBranchState;
  readonly danmakuBranchState: DanmakuBranchState;
  readonly aid: number | null;
  readonly bvid: string | null;
  readonly reviewReason: string | null;
  readonly attempt: number;
  readonly nextAttemptAt: number;
  readonly createdAt: number;
  readonly updatedAt: number;
  readonly parts: readonly UploadPartProgress[];
}

export interface RecordingPart {
  readonly id: number;
  readonly runId: string;
  readonly partIndex: number;
  readonly sourcePath: string;
  readonly finalPath: string | null;
  readonly xmlPath: string | null;
  readonly recordStartTime: number;
  readonly recordEndTime: number | null;
  readonly recordDurationSeconds: number | null;
  readonly fileSizeBytes: number | null;
  readonly danmakuCount: number;
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
  readonly title: string;
  readonly coverUrl: string;
  readonly coverPath: string | null;
  readonly anchorUid: number | null;
  readonly anchorName: string;
  readonly areaId: number | null;
  readonly areaName: string;
  readonly parentAreaId: number | null;
  readonly parentAreaName: string;
  readonly liveEndTime: number | null;
  readonly partCount: number;
  readonly danmakuCount: number;
  readonly totalFileSizeBytes: number;
  readonly recordDurationSeconds: number;
  readonly uploadJob: UploadJobProgress | null;
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
