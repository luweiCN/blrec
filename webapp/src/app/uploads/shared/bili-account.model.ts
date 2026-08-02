export const ACCOUNT_STATES = [
  'active',
  'paused',
  'refresh_unknown',
  'archived',
] as const;

export type AccountState = (typeof ACCOUNT_STATES)[number];

export const QR_SESSION_STATES = [
  'created',
  'pending',
  'scanned',
  'confirmed',
  'expired',
  'cancelled',
  'failed',
] as const;

export type QrSessionState = (typeof QR_SESSION_STATES)[number];

export interface BiliAccount {
  id: number;
  uid: number;
  displayName: string;
  avatarUrl: string;
  credentialVersion: number;
  credentialExpiresAt: number;
  createdAt: number;
  state: AccountState;
  isPrimary: boolean;
}

export interface QrSession {
  id: string;
  state: QrSessionState;
  qrUrl: string | null;
  expiresAt: number;
  accountId: number | null;
}

export interface RefreshResult {
  credentialVersion: number;
  refreshed: boolean;
}

export interface RelatedUploadJob {
  id: number;
  roomId: number;
  state: string;
}

export interface AccountRelationships {
  accountId: number;
  isPrimary: boolean;
  followPrimaryRoomIds: number[];
  fixedRoomIds: number[];
  reassignableJobs: RelatedUploadJob[];
  blockingJobs: RelatedUploadJob[];
  historicalJobCount: number;
}

export const REMOVAL_MODES = [
  'follow_primary',
  'fixed',
  'disable',
] as const;

export type RemovalMode = (typeof REMOVAL_MODES)[number];

export interface AccountRemovalRequest {
  mode: RemovalMode;
  replacementAccountId?: number;
  newPrimaryAccountId?: number;
}

export interface AccountRemovalResult {
  accountId: number;
  state: 'archived';
}

export const ARCHIVE_MIGRATION_STATES = [
  'discovering',
  'running',
  'completed',
  'failed',
] as const;

export type ArchiveMigrationState = (typeof ARCHIVE_MIGRATION_STATES)[number];

export const ARCHIVE_MIGRATION_ITEM_STATES = [
  'queued',
  'downloading',
  'creating_task',
  'task_created',
  'failed',
] as const;

export type ArchiveMigrationItemState =
  (typeof ARCHIVE_MIGRATION_ITEM_STATES)[number];

export interface ArchiveMigrationRequest {
  sourceUid: number;
  downloadAccountId: number;
  targetAccountId: number;
}

export interface ArchiveMigrationStatus {
  id: number;
  sourceUid: number;
  sourceName: string | null;
  downloadAccountId: number;
  targetAccountId: number;
  state: ArchiveMigrationState;
  progress: number;
  discoveredCount: number;
  completedCount: number;
  failedCount: number;
  error: string | null;
  requestedAt: number;
  startedAt: number | null;
  completedAt: number | null;
  updatedAt: number;
  operatorPaused: boolean;
  dailyLimit: number;
  dailyUsed: number;
  quotaDay: string | null;
}

export interface ArchiveMigrationControl {
  paused?: boolean;
  dailyLimit?: number;
}

export interface ArchiveMigrationItem {
  id: number;
  migrationId: number;
  bvid: string;
  title: string;
  publishedAt: number | null;
  state: ArchiveMigrationItemState;
  progress: number;
  pageCount: number;
  downloadedPageCount: number;
  attemptCount: number;
  sessionId: number | null;
  uploadJobId: number | null;
  uploadState: string | null;
  submitState: string | null;
  commentBranchState: string | null;
  danmakuBranchState: string | null;
  analysisState: string | null;
  targetBvid: string | null;
  error: string | null;
  updatedAt: number;
}

export interface ArchiveMigrationRealtimeSnapshot {
  migrations: readonly ArchiveMigrationStatus[];
  items: Readonly<Record<string, readonly ArchiveMigrationItem[]>>;
}

export type AccountsView =
  | { state: 'loading' }
  | { state: 'ready'; accounts: readonly BiliAccount[] }
  | { state: 'error'; message: string };

type VisibleQr = {
  session: QrSession;
  qrDataUrl: string;
};

export type LoginView =
  | { state: 'idle' }
  | { state: 'creating' }
  | ({ state: 'waiting' | 'scanned' | 'cancelling' } & VisibleQr)
  | { state: 'confirmed'; accountId: number | null }
  | { state: 'expired' | 'cancelled' | 'failed' }
  | { state: 'error'; message: string };

export type QrDisplay = VisibleQr;
