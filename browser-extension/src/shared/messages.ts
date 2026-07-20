export interface PairMessage {
  readonly type: 'PAIR';
  readonly backendUrl: string;
  readonly username: string;
}

export interface RoomStatusMessage {
  readonly type: 'ROOM_STATUS';
  readonly roomId: number;
}

export interface CollectMessage {
  readonly type: 'COLLECT';
  readonly roomId: number;
  readonly upload: boolean;
}

export interface ControlOperationMessage {
  readonly type: 'CONTROL_OPERATION';
  readonly operationId: string;
}

export interface AddHighlightMessage {
  readonly type: 'ADD_HIGHLIGHT';
  readonly roomId: number;
  readonly observedAtMs: number;
  readonly playerDelayMs: number;
  readonly currentTimeMs: number | null;
  readonly seekableEndMs: number | null;
  readonly rawDelayMs: number;
  readonly baselineDelayMs: number;
  readonly effectiveRewindMs: number;
  readonly name: string;
  readonly title: string;
  readonly anchorName: string;
}

export type BackgroundMessage =
  | PairMessage
  | RoomStatusMessage
  | CollectMessage
  | ControlOperationMessage
  | AddHighlightMessage;

export type BackgroundResponse<T = unknown> =
  | { readonly ok: true; readonly data: T }
  | { readonly ok: false; readonly message: string };
