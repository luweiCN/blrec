export type UploadAccountMode = 'primary' | 'fixed';

export interface RoomUploadPolicy {
  roomId: number;
  accountMode: UploadAccountMode;
  accountId: number | null;
  resolvedAccountId: number | null;
  resolvedAccountName: string | null;
  enabled: boolean;
  titleTemplate: string;
  descriptionTemplate: string;
  tid: number;
  tags: string;
  copyright: 1 | 2;
  source: string;
  autoComment: boolean;
  danmakuBackfill: boolean;
  filters: Record<string, unknown>;
  blockedReason: string | null;
  createdAt: number;
  updatedAt: number;
}

export interface RoomUploadPolicyRequest {
  accountMode: UploadAccountMode;
  accountId: number | null;
  enabled: boolean;
  titleTemplate: string;
  descriptionTemplate: string;
  tid: number;
  tags: string;
  copyright: 1 | 2;
  source: string;
  autoComment: boolean;
  danmakuBackfill: boolean;
  filters: Record<string, unknown>;
}

export interface RoomUploadPolicyDraft extends RoomUploadPolicyRequest {
  roomId: number | null;
}
