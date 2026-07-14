export type UploadAccountMode = 'primary' | 'fixed';
export type UploadCoverMode = 'live' | 'custom';
export type UploadRetentionMode =
  | 'never'
  | 'upload_completed'
  | 'submitted'
  | 'approved'
  | 'capacity';

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
  collectionSeasonId: number | null;
  collectionSectionId: number | null;
  coverMode: UploadCoverMode;
  coverAssetId: number | null;
  publishDelaySeconds: number;
  retentionMode: UploadRetentionMode;
  retentionDays: number;
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

export interface CoverAsset {
  id: number;
  filename: string;
  mimeType: 'image/jpeg' | 'image/png';
  width: number;
  height: number;
  byteSize: number;
  createdAt: number;
  contentUrl: string;
}

export interface BiliCollectionSection {
  id: number;
  title: string;
}

export interface BiliCollection {
  id: number;
  title: string;
  description: string;
  coverUrl: string;
  state: number;
  rejectReason: string;
  selectable: boolean;
  sections: BiliCollectionSection[];
}

export interface BiliCollectionCatalog {
  accountId: number;
  collections: BiliCollection[];
}

export interface BiliCollectionCreateRequest {
  accountMode: UploadAccountMode;
  accountId: number | null;
  title: string;
  description: string;
  coverAssetId: number;
}

export interface BiliCollectionCreation {
  accountId: number;
  collection: BiliCollection;
}
