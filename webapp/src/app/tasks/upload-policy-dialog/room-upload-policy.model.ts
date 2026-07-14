export type UploadAccountMode = 'primary' | 'fixed';

export interface RoomUploadPolicyRequest {
  accountMode: UploadAccountMode;
  accountId: number | null;
  enabled: boolean;
  titleTemplate: string;
  descriptionTemplate: string;
  partTitleTemplate: string;
  dynamicTemplate: string;
  tid: number;
  tags: string;
  copyright: 1 | 2;
  source: string;
  isOnlySelf: boolean;
  publishDynamic: boolean;
  noReprint: boolean;
  upSelectionReply: boolean;
  upCloseReply: boolean;
  upCloseDanmu: boolean;
  autoComment: boolean;
  danmakuBackfill: boolean;
  filters: Record<string, unknown>;
}

export interface RoomUploadPolicy extends RoomUploadPolicyRequest {
  roomId: number;
  resolvedAccountId: number | null;
  resolvedAccountName: string | null;
  blockedReason: string | null;
  createdAt: number;
  updatedAt: number;
}

export interface RoomUploadPolicyDraft
  extends Omit<RoomUploadPolicyRequest, 'tid'> {
  tid: number | null;
}

export interface UploadCategoryNode {
  id: number;
  name: string;
  description: string;
  children: UploadCategoryNode[];
}

export interface UploadCategoryCatalog {
  accountId: number;
  credentialVersion: number;
  fetchedAt: number;
  stale: boolean;
  categories: UploadCategoryNode[];
}
