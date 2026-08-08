export type MediaLibraryKind = 'broadcast' | 'clip';
export type MediaLibraryOrigin = 'recording' | 'upload';
export type MediaLibraryState = 'uploading' | 'moving' | 'ready' | 'failed';

export interface MediaLibraryPart {
  readonly itemId: number;
  readonly partIndex: number;
  readonly recordingPartId: number | null;
  readonly originalFilename: string;
  readonly expectedSize: number;
  readonly receivedSize: number;
  readonly state: 'pending' | 'uploading' | 'uploaded' | 'ready' | 'failed';
  readonly error: string | null;
  readonly durationSeconds: number | null;
}

export interface MediaLibrarySubmission {
  readonly aid: number;
  readonly bvid: string;
  readonly state: string;
  readonly accountId: number;
  readonly accountName: string;
  readonly occurredAt: number;
  readonly current: boolean;
}

export interface MediaLibraryItem {
  readonly id: number;
  readonly sessionId: number;
  readonly kind: MediaLibraryKind;
  readonly origin: MediaLibraryOrigin;
  readonly displayName: string;
  readonly note: string;
  readonly state: MediaLibraryState;
  readonly error: string | null;
  readonly createdAt: number;
  readonly updatedAt: number;
  readonly roomId: number;
  readonly sourceTitle: string;
  readonly anchorName: string;
  readonly startedAt: number;
  readonly tags: readonly string[];
  readonly parts: readonly MediaLibraryPart[];
  readonly submissions: readonly MediaLibrarySubmission[];
}

export interface MediaLibraryList {
  readonly total: number;
  readonly items: readonly MediaLibraryItem[];
}

export interface CreateMediaImportRequest {
  readonly kind: MediaLibraryKind;
  readonly displayName: string;
  readonly note: string;
  readonly tags: readonly string[];
  readonly roomId: number;
  readonly anchorName: string;
  readonly parts: readonly {
    readonly filename: string;
    readonly sizeBytes: number;
  }[];
}

export interface UpdateMediaLibraryItemRequest {
  readonly displayName: string;
  readonly note: string;
  readonly tags: readonly string[];
}

export interface DeleteMediaLibraryItemResponse {
  readonly state: 'requested';
  readonly generation: number;
}
