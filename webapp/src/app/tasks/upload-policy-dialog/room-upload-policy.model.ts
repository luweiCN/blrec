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
  creationStatementId: number;
  originalAuthorization: boolean;
  source: string;
  isOnlySelf: boolean;
  publishDynamic: boolean;
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

export interface UploadCreationStatement {
  id: number;
  content: string;
}

export interface UploadCategoryCatalog {
  accountId: number;
  credentialVersion: number;
  fetchedAt: number;
  stale: boolean;
  categories: UploadCategoryNode[];
  creationStatements: UploadCreationStatement[];
  creationStatementTip: string;
}
