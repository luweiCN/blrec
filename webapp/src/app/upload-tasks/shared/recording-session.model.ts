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

export type UploadJobDisplayState =
  | 'standard'
  | 'preuploading'
  | 'preuploaded_waiting'
  | 'preupload_paused';

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

export type TranscodeState = 'unknown' | 'ready' | 'processing' | 'failed';

export type TranscodeRepairStage =
  | 'none'
  | 'original'
  | 'original_waiting_review'
  | 'remux'
  | 'remux_waiting_review'
  | 'completed'
  | 'exhausted';

export type UploadRepairState =
  | 'idle'
  | 'queued'
  | 'checking'
  | 'reuploading'
  | 'editing'
  | 'waiting_review'
  | 'not_needed'
  | 'completed'
  | 'failed'
  | 'unknown_outcome';

export type UploadJobAction =
  | 'retry_failed'
  | 'repair_transcode'
  | 'skip_upload'
  | 'repost_as_new'
  | 'pause_upload'
  | 'resume_upload'
  | 'delete_local';

export type RecordingSessionAction =
  | 'set_upload'
  | 'set_skip'
  | 'retry_failed'
  | 'repair_transcode'
  | 'backfill_danmaku'
  | 'repost_as_new'
  | 'pause_upload'
  | 'resume_upload'
  | 'edit_submission'
  | 'edit_task'
  | 'delete_local';

export type RecordingSessionDisplayState =
  | 'recording'
  | 'pending_upload'
  | 'uploading'
  | 'waiting_review'
  | 'completed'
  | 'paused'
  | 'deleting'
  | 'delete_failed'
  | 'not_uploading'
  | 'needs_attention';

export type RecordingSessionScope = 'recordings' | 'uploads';

export interface RecordingSessionFilters {
  readonly scope: RecordingSessionScope;
  readonly query: string;
  readonly recordingState: RecordingSessionState | null;
  readonly uploadState: UploadJobState | 'none' | 'suppressed' | null;
  readonly startedFrom: number | null;
  readonly startedTo: number | null;
  readonly sort: 'newest' | 'oldest';
}

export interface UploadJobActionRequest {
  readonly action: UploadJobAction;
  readonly jobIds: readonly number[];
}

export interface UploadJobActionResult {
  readonly jobId: number;
  readonly accepted: boolean;
  readonly message: string;
}

export interface UploadJobActionResponse {
  readonly results: readonly UploadJobActionResult[];
}

export interface RecordingSessionActionResult {
  readonly sessionId: number;
  readonly accepted: boolean;
  readonly message: string;
}

export interface RecordingSessionActionResponse {
  readonly results: readonly RecordingSessionActionResult[];
}

export interface UploadJobRetryPreviewItem {
  readonly jobId: number;
  readonly roomId: number;
  readonly title: string;
  readonly accountDisplayName: string;
  readonly reason: string;
}

export interface UploadJobRetryPreviewResponse {
  readonly items: readonly UploadJobRetryPreviewItem[];
}

export interface UploadPartProgress {
  readonly id: number;
  readonly partIndex: number;
  readonly uploadState: UploadPartState;
  readonly danmakuImportState: DanmakuImportState;
  readonly remoteFilename: string | null;
  readonly cid: number | null;
  readonly transcodeState: TranscodeState;
  readonly transcodeFailCode: number | null;
  readonly transcodeFailDesc: string | null;
  readonly repairStage?: TranscodeRepairStage;
  readonly repairDiagnostic?: string | null;
  readonly confirmedBytes: number;
  readonly totalBytes: number;
}

export interface DanmakuItemProgress {
  readonly id: number;
  readonly partIndex: number;
  readonly progressMs: number;
  readonly content: string;
  readonly errorMessage: string | null;
}

export interface RecordingMediaAccess {
  readonly token: string;
  readonly expiresAt: number;
  readonly snapshotId: string | null;
  readonly durationMs: number | null;
  readonly fileSizeBytes: number;
  readonly recording: boolean;
  readonly playbackMode: 'seekable' | 'sequential' | 'active_snapshot';
  readonly indexState: string;
  readonly retryAfterMs: number | null;
  readonly requestId: string;
}

export interface RecordingDanmakuLine {
  readonly index: number;
  readonly progressMs: number;
  readonly mode: number;
  readonly fontSize: number;
  readonly color: number;
  readonly user: string | null;
  readonly uid: number | null;
  readonly content: string;
}

export interface RecordingDanmakuPage {
  readonly items: readonly RecordingDanmakuLine[];
  readonly nextCursor: number | null;
}

export type SubmissionVerificationState =
  | 'pending'
  | 'passed'
  | 'different'
  | 'partial'
  | 'failed';

export interface SubmissionVerification {
  readonly state: SubmissionVerificationState;
  readonly checked: readonly string[];
  readonly missing: readonly string[];
  readonly mismatches: readonly string[];
  readonly differences?: Readonly<
    Record<string, { readonly expected: unknown; readonly actual: unknown }>
  >;
  readonly unverifiable?: readonly string[];
  readonly error?: string | null;
}

export interface UploadJobProgress {
  readonly id: number;
  readonly accountId: number;
  readonly accountUid: number;
  readonly accountDisplayName: string;
  readonly state: UploadJobState;
  readonly submitState: UploadSubmitState;
  readonly preuploadFinalized: boolean;
  readonly displayState: UploadJobDisplayState;
  readonly commentBranchState: CommentBranchState;
  readonly danmakuBranchState: DanmakuBranchState;
  readonly aid: number | null;
  readonly bvid: string | null;
  readonly reviewReason: string | null;
  readonly attempt: number;
  readonly nextAttemptAt: number;
  readonly createdAt: number;
  readonly updatedAt: number;
  readonly danmakuTotal: number;
  readonly danmakuConfirmed: number;
  readonly danmakuPending: number;
  readonly danmakuUnknown: number;
  readonly danmakuFailed: number;
  readonly repairState: UploadRepairState;
  readonly repairMessage: string | null;
  readonly repairError: string | null;
  readonly canRetry: boolean;
  readonly canRepair: boolean;
  readonly canSkip: boolean;
  readonly canRepost: boolean;
  readonly canDelete: boolean;
  readonly operatorPaused: boolean;
  readonly scheduledPublishAt: number | null;
  readonly collectionBranchState:
    | 'disabled'
    | 'pending'
    | 'running'
    | 'completed'
    | 'failed';
  readonly collectionError: string | null;
  readonly submissionVerificationState: SubmissionVerificationState;
  readonly submissionVerifiedAt: number | null;
  readonly submissionVerification: SubmissionVerification | null;
  readonly commentError: string | null;
  readonly danmakuError: string | null;
  readonly canPause: boolean;
  readonly canResume: boolean;
  readonly canEdit: boolean;
  readonly confirmedBytes: number;
  readonly totalBytes: number;
  readonly percent: number;
  readonly bytesPerSecond: number | null;
  readonly etaSeconds: number | null;
  readonly currentPartIndex: number | null;
  readonly confirmedPartCount: number;
  readonly discoveredPartCount: number;
  readonly unknownDanmakuItems: readonly DanmakuItemProgress[];
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
  readonly mediaIndexState?:
    | 'pending'
    | 'indexing'
    | 'ready'
    | 'failed'
    | 'not_required';
  readonly mediaIndexError?: string | null;
  readonly mediaIndexProgress?: number;
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
  readonly uploadIntent: 'none' | 'auto' | 'upload' | 'skip';
  readonly uploadDecision: 'follow_room' | 'upload' | 'skip';
  readonly submissionInherited: boolean;
  readonly uploadResolutionState:
    | 'pending'
    | 'not_requested'
    | 'configuration_required'
    | 'job_created';
  readonly uploadResolutionError: string | null;
  readonly uploadSuppressed: boolean;
  readonly deletionState: 'none' | 'requested' | 'deleting' | 'failed';
  readonly deletionError: string | null;
  readonly sourceKind: 'live' | 'highlight';
  readonly highlightClipId: number | null;
  readonly displayState: RecordingSessionDisplayState;
  readonly availableActions: readonly RecordingSessionAction[];
  readonly uploadJob: UploadJobProgress | null;
  readonly parts: readonly RecordingPart[];
}

export interface RecordingSessionsResponse {
  readonly degradedReason: string | null;
  readonly total: number;
  readonly sessions: readonly RecordingSession[];
}

export type RecordingSessionsView =
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly response: RecordingSessionsResponse }
  | { readonly state: 'error'; readonly message: string };

export interface UploadTaskSettings {
  readonly jobId: number;
  readonly accountId: number;
  readonly settings: Readonly<Record<string, unknown>>;
  readonly editable: boolean;
  readonly blockedReason: string | null;
}

export interface UploadTaskSettingsUpdateResponse {
  readonly collectionCleared: boolean;
  readonly task: UploadTaskSettings;
}
