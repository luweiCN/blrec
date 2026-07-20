export interface HighlightMarker {
  readonly id: number;
  readonly roomId: number;
  readonly observedAtMs: number;
  readonly playerDelayMs: number;
  readonly contentAtMs: number;
  readonly title: string;
  readonly anchorName: string;
  readonly name: string;
  readonly note: string;
  readonly source: 'web' | 'browser_extension';
  readonly createdAt: number;
  readonly updatedAt: number;
  readonly recordingPartId?: number | null;
  readonly partAnchorAtMs?: number | null;
  readonly currentTimeMs?: number | null;
  readonly seekableEndMs?: number | null;
  readonly rawDelayMs?: number;
  readonly baselineDelayMs?: number;
  readonly effectiveRewindMs?: number;
}

export interface HighlightTimelinePart {
  readonly partId: number;
  readonly partIndex: number;
  readonly timelineStartMs: number;
  readonly durationMs: number;
  readonly stableEndMs: number;
  readonly recording: boolean;
  readonly mediaKind: 'flv' | 'native';
}

export interface MappedHighlight {
  readonly marker: HighlightMarker;
  readonly partId: number;
  readonly localOffsetMs: number;
  readonly timelineOffsetMs: number;
}

export interface HighlightTimeline {
  readonly sessionId: number;
  readonly roomId: number;
  readonly durationMs: number;
  readonly stableEndMs: number;
  readonly parts: readonly HighlightTimelinePart[];
  readonly markers: readonly MappedHighlight[];
}

export interface HighlightMarkerCount {
  readonly partId: number;
  readonly count: number;
}

export interface HighlightClipInspectionSource {
  readonly partId: number;
  readonly actualStartMs: number;
  readonly actualEndMs: number;
  readonly outputOffsetMs: number;
}

export interface HighlightClipInspection {
  readonly requestedStartMs: number;
  readonly requestedEndMs: number;
  readonly actualStartMs: number;
  readonly actualEndMs: number;
  readonly extraLeadMs: number;
  readonly confirmationRequired: boolean;
  readonly compatible: boolean;
  readonly sources: readonly HighlightClipInspectionSource[];
}

export type HighlightClipState =
  'queued' | 'processing' | 'ready' | 'failed' | 'cancelled';

export interface HighlightClipSource {
  readonly partId: number;
  readonly ordinal: number;
  readonly requestedStartMs: number;
  readonly requestedEndMs: number;
  readonly actualStartMs: number | null;
  readonly actualEndMs: number | null;
}

export interface HighlightClip {
  readonly id: number;
  readonly markerId: number | null;
  readonly roomId: number;
  readonly sourceSessionId: number | null;
  readonly uploadSessionId: number | null;
  readonly name: string;
  readonly requestedStartMs: number;
  readonly requestedEndMs: number;
  readonly actualStartMs: number | null;
  readonly actualEndMs: number | null;
  readonly outputVideoPath: string | null;
  readonly outputXmlPath: string | null;
  readonly state: HighlightClipState;
  readonly confirmationRequired: boolean;
  readonly confirmed: boolean;
  readonly errorMessage: string | null;
  readonly attempt: number;
  readonly createdAt: number;
  readonly updatedAt: number;
  readonly sources: readonly HighlightClipSource[];
  readonly uploadJobId?: number | null;
  readonly uploadState?: string | null;
  readonly uploadPercent?: number | null;
  readonly uploadBvid?: string | null;
  readonly sourceAnchorName?: string;
  readonly sourceTitle?: string;
  readonly durationMs?: number;
  readonly fileSizeBytes: number | null;
}

export interface HighlightClipSummary {
  readonly id: number;
  readonly roomId: number;
  readonly sourceSessionId: number | null;
  readonly name: string;
  readonly state: HighlightClipState;
  readonly errorMessage: string | null;
  readonly createdAt: number;
  readonly updatedAt: number;
  readonly sourceAnchorName: string;
  readonly sourceTitle: string;
  readonly durationMs: number;
  readonly fileSizeBytes: number | null;
  readonly uploadJobId: number | null;
  readonly uploadState: string | null;
  readonly uploadPercent: number | null;
  readonly uploadBvid: string | null;
}

export interface HighlightClipList {
  readonly total: number;
  readonly items: readonly HighlightClipSummary[];
}

export interface CreateHighlightClipRequest {
  readonly markerId: number | null;
  readonly name: string;
  readonly startMs: number;
  readonly endMs: number;
  readonly confirmKeyframe: boolean;
}

export interface HighlightUploadTaskResponse {
  readonly jobId: number;
}

export interface HighlightUploadSessionResponse {
  readonly sessionId: number;
}

export interface HighlightMediaAccess {
  readonly token: string;
  readonly expiresAt: number;
  readonly fileSizeBytes: number;
}

export interface HighlightProgressItem {
  readonly id: number;
  readonly roomId: number;
  readonly name: string;
  readonly state: HighlightClipState;
  readonly attempt: number;
  readonly errorMessage: string | null;
  readonly updatedAt: number;
}

export interface HighlightProgressEvent {
  readonly clips: readonly HighlightProgressItem[];
}
