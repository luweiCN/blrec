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
