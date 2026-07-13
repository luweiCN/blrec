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
  credentialVersion: number;
  state: AccountState;
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
