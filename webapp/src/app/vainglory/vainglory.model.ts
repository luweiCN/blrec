export const TEAM_COLORS = ['teal', 'orange'] as const;
export type TeamColor = (typeof TEAM_COLORS)[number];

export const MATCH_END_REASONS = ['normal', 'surrender', 'unknown'] as const;
export type MatchEndReason = (typeof MATCH_END_REASONS)[number];

export const GAME_MODES = ['3v3', '5v5', 'aram', 'other', 'unknown'] as const;
export type GameMode = (typeof GAME_MODES)[number];

export const MATCH_KINDS = ['pvp', 'bot', 'practice', 'unknown'] as const;
export type MatchKind = (typeof MATCH_KINDS)[number];

export const VIEW_CONTEXTS = ['played', 'observed', 'unknown'] as const;
export type ViewContext = (typeof VIEW_CONTEXTS)[number];

export type VaingloryMatchSessionSort = 'analyzed' | 'started';

export type VaingloryPublicationState =
  | 'prepared'
  | 'running'
  | 'confirmed'
  | 'paused'
  | 'failed';
export type VaingloryDescriptionState =
  | 'prepared'
  | 'in_flight'
  | 'confirmed'
  | 'skipped_no_room';
export type VaingloryPinState = 'prepared' | 'in_flight' | 'confirmed';
export type VaingloryChapterState = 'prepared' | 'confirmed' | 'skipped';

export type ScanState = 'pending' | 'analyzing' | 'ready' | 'failed';

export type RecordedPlayerSource = 'automatic' | 'manual';
export type HeroRecognitionSource = 'automatic' | 'manual';
export type RecordedPlayerState =
  | 'pending'
  | 'uncertain'
  | 'automatic'
  | 'manual'
  | 'unsupported';

export type ArchiveSyncState =
  'idle' | 'discovering' | 'running' | 'ready' | 'failed';

export interface VaingloryArchiveSync {
  readonly accountId: number;
  readonly state: ArchiveSyncState;
  readonly progress: number;
  readonly discoveredCount: number;
  readonly completedCount: number;
  readonly error: string | null;
  readonly requestedAt: number;
  readonly startedAt: number | null;
  readonly completedAt: number | null;
  readonly updatedAt: number;
  readonly operatorPaused: boolean;
  readonly dailyLimit: number;
  readonly dailyUsed: number;
  readonly quotaDay: string | null;
  readonly nextPage: number;
  readonly discoveryComplete: boolean;
}

export interface VaingloryArchiveSyncControl {
  readonly paused?: boolean;
  readonly dailyLimit?: number;
}

export type ArchiveBackfillStage =
  | 'queued'
  | 'reading_metadata'
  | 'download_pending'
  | 'downloading'
  | 'analysis_pending'
  | 'scanning_video'
  | 'locating_results'
  | 'ocr_recognition'
  | 'publication_pending'
  | 'publishing_description'
  | 'publishing_comments'
  | 'pinning_comment'
  | 'completed'
  | 'managed_elsewhere'
  | 'failed';

export interface VaingloryArchiveBackfillItem {
  readonly id: number;
  readonly accountId: number;
  readonly aid: number;
  readonly bvid: string;
  readonly title: string;
  readonly publishedAt: number | null;
  readonly state: string;
  readonly stage: ArchiveBackfillStage;
  readonly progress: number;
  readonly pageCount: number;
  readonly completedPageCount: number;
  readonly currentPage: number | null;
  readonly currentPartTitle: string | null;
  readonly downloadProgress: number;
  readonly downloadedBytes: number;
  readonly totalBytes: number | null;
  readonly analysisState: string | null;
  readonly analysisProgress: number;
  readonly matchCount: number;
  readonly publicationState: string | null;
  readonly descriptionState: string | null;
  readonly commentCount: number;
  readonly confirmedCommentCount: number;
  readonly pinState: string | null;
  readonly publicationProgress: number;
  readonly error: string | null;
  readonly updatedAt: number;
}

export type VaingloryAnalysisQueueCategory =
  | 'manual'
  | 'realtime'
  | 'archive'
  | 'migration'
  | 'backlog';

export interface VaingloryAnalysisQueueItem {
  readonly partId: number;
  readonly sessionId: number;
  readonly partIndex: number;
  readonly title: string;
  readonly anchorName: string;
  readonly state: 'pending' | 'analyzing';
  readonly stage: 'video_scan' | 'ocr_waiting' | 'ocr_recognition';
  readonly category: VaingloryAnalysisQueueCategory;
  readonly progress: number;
  readonly requestedAt: number;
  readonly startedAt: number | null;
  readonly updatedAt: number;
  readonly partCount: number;
  readonly completedPartCount: number;
}

export interface VaingloryAnalysisQueue {
  readonly workerState: 'running' | 'stopped' | 'failed';
  readonly active: readonly VaingloryAnalysisQueueItem[];
  readonly queued: readonly VaingloryAnalysisQueueItem[];
  readonly pendingCount: number;
  readonly manualPending: number;
  readonly realtimePending: number;
  readonly archivePending: number;
  readonly migrationPending: number;
  readonly backlogPending: number;
}

export interface VaingloryIndexSummary {
  readonly matchCount: number;
  readonly sessionCount: number;
  readonly anchorCount: number;
  readonly unassignedSessionCount: number;
  readonly winCount: number;
  readonly lossCount: number;
  readonly unknownCount: number;
  readonly playerSlotCount: number;
  readonly recognizedHeroCount: number;
}

export interface VaingloryArchiveBackfillRealtimeSnapshot {
  readonly syncs: readonly VaingloryArchiveSync[];
  readonly items: Readonly<Record<string, readonly VaingloryArchiveBackfillItem[]>>;
}

export interface VaingloryIndexRealtimeSnapshot {
  readonly sampledAt: number;
  readonly analysisQueue: VaingloryAnalysisQueue;
  readonly indexSummary: VaingloryIndexSummary;
}

export interface VaingloryArchiveContentReview {
  readonly id: number;
  readonly accountId: number;
  readonly accountName: string;
  readonly aid: number;
  readonly bvid: string;
  readonly title: string;
  readonly publishedAt: number | null;
  readonly reason: string;
}

export interface VaingloryArchiveContentReviewList {
  readonly total: number;
  readonly items: readonly VaingloryArchiveContentReview[];
}

export interface VaingloryScanJob {
  readonly sessionId: number;
  readonly state: ScanState;
  readonly progress: number;
  readonly algorithmVersion: number;
  readonly matchCount: number;
  readonly error: string | null;
  readonly requestedAt: number;
  readonly startedAt: number | null;
  readonly completedAt: number | null;
  readonly updatedAt: number;
}

export interface VaingloryHero {
  readonly id: number;
  readonly label: string;
  readonly fingerprint: string;
  readonly thumbnailUrl: string;
}

export interface VaingloryMatchPlayer {
  readonly side: 'left' | 'right';
  readonly slot: number;
  readonly name: string;
  readonly heroId: number | null;
  readonly heroLabel: string;
  readonly heroSource: HeroRecognitionSource;
  readonly kills: number | null;
  readonly deaths: number | null;
  readonly assists: number | null;
  readonly economy: number | null;
  readonly lastHits: number | null;
  readonly confidence: number;
  readonly isRecordedPlayer: boolean;
}

export interface VaingloryMatch {
  readonly id: number;
  readonly sessionId: number;
  readonly sessionTitle: string;
  readonly sessionStartedAt: number;
  readonly partId: number;
  readonly partIndex: number;
  readonly title: string;
  readonly sourceTitle: string;
  readonly uploadTitle: string;
  readonly gameMode: GameMode;
  readonly teamSize: number | null;
  readonly matchKind: MatchKind;
  readonly viewContext: ViewContext;
  readonly statsEligible: boolean;
  readonly statsExclusionReason: string | null;
  readonly startedAtMs: number;
  readonly resultAtMs: number;
  readonly durationSeconds: number | null;
  readonly resultText: string;
  readonly endReason: MatchEndReason;
  readonly leftColor: TeamColor;
  readonly rightColor: TeamColor;
  readonly winnerSide: 'left' | 'right' | 'unknown';
  readonly winnerColor: TeamColor | 'unknown';
  readonly leftKills: number | null;
  readonly rightKills: number | null;
  readonly leftEconomy: number | null;
  readonly rightEconomy: number | null;
  readonly confidence: number;
  readonly accountId: number | null;
  readonly bvid: string | null;
  readonly archivePage: number | null;
  readonly resultFrameUrl: string | null;
  readonly recordedPlayerConfidence: number | null;
  readonly recordedPlayerSource: RecordedPlayerSource;
  readonly recordedPlayerState: RecordedPlayerState;
  readonly players: readonly VaingloryMatchPlayer[];
}

export interface VaingloryMatchList {
  readonly total: number;
  readonly items: readonly VaingloryMatch[];
}

export interface VaingloryMatchSession {
  readonly sessionId: number;
  readonly title: string;
  readonly sourceTitle: string;
  readonly anchorName: string;
  readonly startedAt: number;
  readonly matchCount: number;
  readonly tealWinCount: number;
  readonly orangeWinCount: number;
  readonly winCount: number;
  readonly lossCount: number;
  readonly unknownCount: number;
  readonly surrenderCount: number;
  readonly durationSeconds: number;
  readonly gameModes: readonly GameMode[];
  readonly statsIncluded?: boolean;
  readonly bvid?: string | null;
  readonly publicationState: VaingloryPublicationState | null;
  readonly descriptionState: VaingloryDescriptionState | null;
  readonly pinState: VaingloryPinState | null;
  readonly chapterState: VaingloryChapterState | null;
}

export interface VaingloryMatchSessionList {
  readonly total: number;
  readonly items: readonly VaingloryMatchSession[];
}

export interface VaingloryAnchorStats {
  readonly anchorUid: number | null;
  readonly anchorName: string;
  readonly roomId: number;
  readonly sessionCount: number;
  readonly matchCount: number;
  readonly winCount: number;
  readonly lossCount: number;
  readonly unknownCount: number;
  readonly winRate: number;
}

export type VaingloryPlayerOrigin = 'automatic' | 'manual';

export interface VaingloryPlayerRoom {
  readonly roomId: number;
  readonly anchorUid: number | null;
  readonly anchorName: string;
}

export interface VaingloryPlayer {
  readonly id: number;
  readonly name: string;
  readonly origin: VaingloryPlayerOrigin;
  readonly rooms: readonly VaingloryPlayerRoom[];
  readonly createdAt: number;
  readonly updatedAt: number;
}

export interface VaingloryGameModeStats {
  readonly gameMode: GameMode;
  readonly matchCount: number;
  readonly winCount: number;
  readonly lossCount: number;
  readonly unknownCount: number;
  readonly winRate: number;
}

export interface VaingloryHeroStats {
  readonly heroId: number;
  readonly heroLabel: string;
  readonly playerCount: number;
  readonly matchCount: number;
  readonly winCount: number;
  readonly lossCount: number;
  readonly unknownCount: number;
  readonly winRate: number;
}

export interface VaingloryPlayerStats {
  readonly playerId: number;
  readonly playerName: string;
  readonly rooms: readonly VaingloryPlayerRoom[];
  readonly sessionCount: number;
  readonly matchCount: number;
  readonly winCount: number;
  readonly lossCount: number;
  readonly unknownCount: number;
  readonly winRate: number;
  readonly modes: readonly VaingloryGameModeStats[];
  readonly heroes: readonly VaingloryHeroStats[];
}

export interface VainglorySessionTitleUpdate {
  readonly title: string;
}

export interface VainglorySessionAnchorUpdate {
  readonly anchorName: string;
}

export interface VaingloryMatchFilters {
  readonly playerName: string;
  readonly heroIds: readonly number[];
  readonly winnerColor: TeamColor | null;
  readonly gameMode: GameMode | null;
  readonly sessionId: number | null;
  readonly sourceTitle?: string;
  readonly anchorName?: string | null;
  readonly statsIncluded?: boolean | null;
}
