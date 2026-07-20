import {
  ChangeDetectorRef,
  Component,
  ElementRef,
  EventEmitter,
  Inject,
  Input,
  OnChanges,
  OnDestroy,
  NgZone,
  Output,
  SimpleChanges,
  ViewChild,
} from '@angular/core';

import { Subscription } from 'rxjs';

import {
  RecordingDanmakuLine,
  RecordingMediaAccess,
  RecordingPart,
  RecordingSession,
} from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';
import { PART_PLAYER_LOADER } from './part-player.loader';
import type {
  FlvPlaybackSource,
  PartPlayer,
  PartPlayerEvent,
  PartPlayerLoader,
} from './part-player.loader';

type PlaybackState =
  | { readonly kind: 'idle' }
  | { readonly kind: 'access_loading' }
  | { readonly kind: 'player_loading' }
  | { readonly kind: 'playing' }
  | { readonly kind: 'error'; readonly message: string };

interface DanmakuRecoveryState {
  readonly targetMs: number;
  readonly follow: boolean;
  readonly activeLineIndex: number | null;
  readonly scrollTop: number;
  readonly previousItems: readonly RecordingDanmakuLine[];
  rebuiltItems: readonly RecordingDanmakuLine[];
}

const PLAYBACK_DEADLINE_MS = 10_000;
const MAX_DANMAKU_ROWS = 1_000;

@Component({
  selector: 'app-part-video-dialog',
  templateUrl: './part-video-dialog.component.html',
  styleUrls: ['./part-video-dialog.component.scss'],
})
export class PartVideoDialogComponent implements OnChanges, OnDestroy {
  @Input() visible = false;
  @Input() session!: RecordingSession;
  @Input() part!: RecordingPart;
  @Output() visibleChange = new EventEmitter<boolean>();

  mediaUrl: string | null = null;
  mediaAccess: RecordingMediaAccess | null = null;
  playbackState: PlaybackState = { kind: 'idle' };
  danmakuItems: readonly RecordingDanmakuLine[] = [];
  danmakuLoading = false;
  danmakuError: string | null = null;
  danmakuNextCursor: number | null = null;
  activeDanmakuIndex: number | null = null;
  followDanmaku = true;

  private videoElement: HTMLVideoElement | null = null;
  private danmakuListElement: HTMLElement | null = null;
  private player: PartPlayer | null = null;
  private request?: Subscription;
  private danmakuRequest?: Subscription;
  private deadlineAt = 0;
  private timer: number | null = null;
  private playerGeneration = 0;
  private pendingPlayerGeneration: number | null = null;
  private destroyed = false;

  constructor(
    private recordingSessions: RecordingSessionService,
    @Inject(PART_PLAYER_LOADER) private playerLoader: PartPlayerLoader,
    private changeDetector: ChangeDetectorRef,
    private zone: NgZone,
  ) {}

  @ViewChild('videoElement')
  set videoElementRef(value: ElementRef<HTMLVideoElement> | undefined) {
    this.videoElement = value?.nativeElement ?? null;
    if (this.videoElement === null) {
      this.invalidatePlayer();
      return;
    }
    this.attachFlvPlayer();
  }

  @ViewChild('danmakuList')
  set danmakuListRef(value: ElementRef<HTMLElement> | undefined) {
    this.danmakuListElement = value?.nativeElement ?? null;
  }

  get title(): string {
    return `${this.session.title || `房间 ${this.session.roomId}`} · P${
      this.part.partIndex
    } 播放`;
  }

  get localMediaPath(): string | null {
    if (this.part?.finalExists && this.part.finalPath) {
      return this.part.finalPath;
    }
    if (this.part?.sourceExists) {
      return this.part.sourcePath;
    }
    return null;
  }

  get isFlv(): boolean {
    return this.localMediaPath?.toLowerCase().endsWith('.flv') ?? false;
  }

  get loading(): boolean {
    return (
      this.playbackState.kind === 'access_loading' ||
      this.playbackState.kind === 'player_loading'
    );
  }

  get error(): string | null {
    return this.playbackState.kind === 'error'
      ? this.playbackState.message
      : null;
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (!this.visible || !this.session || !this.part) {
      if (changes['visible'] && !this.visible) {
        this.reset();
      }
      return;
    }
    if (changes['visible'] || changes['part']) {
      this.loadMedia();
    }
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    this.reset();
  }

  handleClose(): void {
    this.visible = false;
    this.reset();
    this.visibleChange.emit(false);
  }

  handleNativeMediaError(): void {
    if (!this.isFlv) {
      this.fail('本地视频播放失败，请重新打开后再试');
    }
  }

  handleNativeMediaStalled(): void {
    if (!this.isFlv) {
      this.fail('本地视频加载停滞，请检查连接后重试');
    }
  }

  handleFirstFrame(): void {
    this.clearTimer();
    this.playbackState = { kind: 'playing' };
    this.changeDetector.markForCheck();
  }

  handleTimeUpdate(): void {
    if (this.videoElement === null || this.danmakuItems.length === 0) {
      this.activeDanmakuIndex = null;
      return;
    }
    const currentMs = Math.max(0, this.videoElement.currentTime * 1_000);
    const first = this.danmakuItems[0];
    if (
      this.followDanmaku &&
      first &&
      first.index > 0 &&
      currentMs + 250 < first.progressMs &&
      !this.danmakuLoading
    ) {
      this.loadDanmaku(0, false, true);
      return;
    }
    const last = this.danmakuItems[this.danmakuItems.length - 1];
    if (
      this.followDanmaku &&
      last &&
      last.progressMs < currentMs &&
      this.danmakuNextCursor !== null &&
      !this.danmakuLoading
    ) {
      this.loadDanmaku(this.danmakuNextCursor, true, true);
    }
    let low = 0;
    let high = this.danmakuItems.length;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (this.danmakuItems[middle].progressMs <= currentMs + 250) {
        low = middle + 1;
      } else {
        high = middle;
      }
    }
    const candidate = low - 1;
    this.activeDanmakuIndex =
      candidate >= 0 &&
      currentMs - this.danmakuItems[candidate].progressMs <= 2_500
        ? candidate
        : null;
    if (this.followDanmaku && this.activeDanmakuIndex !== null) {
      this.scrollToDanmaku(this.activeDanmakuIndex);
    }
  }

  seekDanmaku(item: RecordingDanmakuLine, index: number): void {
    if (this.videoElement === null) {
      return;
    }
    this.videoElement.currentTime = item.progressMs / 1_000;
    this.activeDanmakuIndex = index;
    this.followDanmaku = true;
    this.scrollToDanmaku(index);
  }

  pauseDanmakuFollow(): void {
    this.followDanmaku = false;
  }

  resumeDanmakuFollow(): void {
    this.followDanmaku = true;
    this.handleTimeUpdate();
  }

  loadMoreDanmaku(): void {
    if (this.danmakuNextCursor === null || this.danmakuLoading) {
      return;
    }
    this.followDanmaku = false;
    this.loadDanmaku(this.danmakuNextCursor, true);
  }

  formatDanmakuTime(progressMs: number): string {
    const totalSeconds = Math.floor(progressMs / 1_000);
    const hours = Math.floor(totalSeconds / 3_600);
    const minutes = Math.floor((totalSeconds % 3_600) / 60);
    const seconds = totalSeconds % 60;
    return [hours, minutes, seconds]
      .map((value) => value.toString().padStart(2, '0'))
      .join(':');
  }

  private loadMedia(): void {
    this.request?.unsubscribe();
    this.invalidatePlayer();
    this.mediaUrl = null;
    this.mediaAccess = null;
    this.playbackState = { kind: 'idle' };
    this.loadDanmaku(0, false);
    if (this.localMediaPath === null) {
      this.fail('该分 P 的本地视频不可用');
      return;
    }
    this.deadlineAt = Date.now() + PLAYBACK_DEADLINE_MS;
    this.playbackState = { kind: 'access_loading' };
    this.requestMediaAccess();
  }

  private loadDanmaku(
    cursor: number,
    append: boolean,
    continuePlaybackSync = false,
  ): void {
    if (!this.part.xmlPath) {
      this.danmakuItems = [];
      this.danmakuNextCursor = null;
      this.danmakuLoading = false;
      this.danmakuError = null;
      return;
    }
    if (!append) {
      this.danmakuRequest?.unsubscribe();
      this.danmakuItems = [];
      this.activeDanmakuIndex = null;
      this.danmakuError = null;
    }
    this.danmakuLoading = true;
    this.danmakuRequest = this.recordingSessions
      .listDanmaku(this.part.id, cursor, 500)
      .subscribe({
        next: (page) => {
          this.danmakuItems = this.mergeDanmakuItems(
            append ? this.danmakuItems : [],
            page.items,
          );
          this.danmakuNextCursor =
            page.nextCursor !== null && page.nextCursor >= cursor
              ? page.nextCursor
              : null;
          this.danmakuLoading = false;
          this.changeDetector.markForCheck();
          if (
            continuePlaybackSync &&
            (page.items.length > 0 || page.nextCursor !== cursor)
          ) {
            this.handleTimeUpdate();
          }
        },
        error: (error: unknown) => {
          if (this.recordingSessions.isDanmakuCursorStale(error)) {
            this.recoverDanmakuCursor();
            return;
          }
          this.danmakuLoading = false;
          this.danmakuError = this.describeError(error, '弹幕加载失败');
          this.changeDetector.markForCheck();
        },
      });
  }

  private recoverDanmakuCursor(): void {
    const activeLine =
      this.activeDanmakuIndex === null
        ? null
        : this.danmakuItems[this.activeDanmakuIndex] ?? null;
    const state: DanmakuRecoveryState = {
      targetMs: Math.max(0, (this.videoElement?.currentTime ?? 0) * 1_000),
      follow: this.followDanmaku,
      activeLineIndex: activeLine?.index ?? null,
      scrollTop: this.danmakuListElement?.scrollTop ?? 0,
      previousItems: this.danmakuItems,
      rebuiltItems: [],
    };
    this.danmakuError = null;
    this.danmakuLoading = true;
    this.loadDanmakuRecoveryPage(0, state);
  }

  private loadDanmakuRecoveryPage(
    cursor: number,
    state: DanmakuRecoveryState,
  ): void {
    this.danmakuRequest = this.recordingSessions
      .listDanmaku(this.part.id, cursor, 500)
      .subscribe({
        next: (page) => {
          state.rebuiltItems = this.mergeDanmakuItems(
            state.rebuiltItems,
            page.items,
            false,
          );
          const nextCursor =
            page.nextCursor !== null && page.nextCursor >= cursor
              ? page.nextCursor
              : null;
          const last = state.rebuiltItems[state.rebuiltItems.length - 1];
          if (
            nextCursor !== null &&
            nextCursor > cursor &&
            last !== undefined &&
            last.progressMs < state.targetMs
          ) {
            this.loadDanmakuRecoveryPage(nextCursor, state);
            return;
          }
          this.danmakuItems = this.mergeDanmakuItems(
            state.previousItems,
            state.rebuiltItems,
          );
          this.danmakuNextCursor = nextCursor;
          this.finishDanmakuRecovery(state);
        },
        error: (error: unknown) => {
          this.danmakuItems = state.previousItems;
          this.danmakuNextCursor = null;
          this.danmakuError = this.recordingSessions.isDanmakuCursorStale(error)
            ? null
            : this.describeError(error, '弹幕加载失败');
          this.finishDanmakuRecovery(state);
        },
      });
  }

  private finishDanmakuRecovery(state: DanmakuRecoveryState): void {
    this.danmakuLoading = false;
    this.followDanmaku = state.follow;
    this.activeDanmakuIndex =
      state.activeLineIndex === null
        ? null
        : this.danmakuItems.findIndex(
            (item) => item.index === state.activeLineIndex,
          );
    if (this.activeDanmakuIndex === -1) {
      this.activeDanmakuIndex = null;
    }
    if (this.danmakuListElement !== null) {
      this.danmakuListElement.scrollTop = state.scrollTop;
    }
    this.changeDetector.markForCheck();
  }

  private mergeDanmakuItems(
    current: readonly RecordingDanmakuLine[],
    incoming: readonly RecordingDanmakuLine[],
    bounded = true,
  ): readonly RecordingDanmakuLine[] {
    const byIndex = new Map<number, RecordingDanmakuLine>();
    for (const item of current) {
      byIndex.set(item.index, item);
    }
    for (const item of incoming) {
      byIndex.set(item.index, item);
    }
    const items = [...byIndex.values()].sort((left, right) =>
      left.index - right.index,
    );
    return bounded && items.length > MAX_DANMAKU_ROWS
      ? items.slice(items.length - MAX_DANMAKU_ROWS)
      : items;
  }

  private scrollToDanmaku(index: number): void {
    const list = this.danmakuListElement;
    if (list === null) {
      return;
    }
    const line = list.querySelector<HTMLElement>(
      `[data-danmaku-index="${index}"]`,
    );
    if (line === null) {
      return;
    }
    const top = line.offsetTop;
    const bottom = top + line.offsetHeight;
    if (top < list.scrollTop || bottom > list.scrollTop + list.clientHeight) {
      list.scrollTop = Math.max(
        0,
        top - (list.clientHeight - line.offsetHeight) / 2,
      );
    }
  }

  private requestMediaAccess(): void {
    this.request = this.recordingSessions
      .createMediaAccess(this.part.id)
      .subscribe({
        next: (access) => {
          if (access.retryAfterMs !== null && access.retryAfterMs > 0) {
            this.scheduleRetry(access.retryAfterMs);
            return;
          }
          this.mediaAccess = access;
          this.mediaUrl = this.recordingSessions.mediaUrl(this.part.id, access);
          this.playbackState = { kind: 'player_loading' };
          this.scheduleDeadline();
          this.attachFlvPlayer();
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.fail(this.describeError(error, '本地视频加载失败'));
        },
      });
  }

  private attachFlvPlayer(): void {
    const element = this.videoElement;
    const url = this.mediaUrl;
    const access = this.mediaAccess;
    const partId = this.part?.id;
    if (
      this.destroyed ||
      !this.visible ||
      !this.isFlv ||
      !element ||
      !url ||
      !access ||
      !partId ||
      this.player
    ) {
      return;
    }
    const generation = this.playerGeneration;
    if (this.pendingPlayerGeneration === generation) {
      return;
    }
    const source: FlvPlaybackSource = {
      playbackMode: access.playbackMode,
      durationMs: access.durationMs,
      fileSizeBytes: access.fileSizeBytes,
    };
    this.pendingPlayerGeneration = generation;
    void this.playerLoader()
      .then((factory) => {
        if (
          !this.matchesPlayerIdentity(generation, partId, url, element) ||
          this.pendingPlayerGeneration !== generation
        ) {
          return;
        }
        const player = factory.attachFlv(element, url, source, (event) => {
          if (!this.matchesPlayerIdentity(generation, partId, url, element)) {
            return;
          }
          this.zone.run(() => {
            this.handlePlayerEvent(event);
          });
        });
        if (
          !this.matchesPlayerIdentity(generation, partId, url, element) ||
          this.pendingPlayerGeneration !== generation
        ) {
          this.destroyPlayer(player);
          return;
        }
        this.pendingPlayerGeneration = null;
        this.player = player;
        if (this.player === null) {
          this.fail('当前浏览器不支持 FLV 播放');
        }
      })
      .catch((error: unknown) => {
        if (this.pendingPlayerGeneration !== generation) {
          return;
        }
        this.pendingPlayerGeneration = null;
        if (!this.matchesPlayerIdentity(generation, partId, url, element)) {
          return;
        }
        this.fail(this.describeError(error, 'FLV 播放器加载失败，请重新打开'));
      });
  }

  private matchesPlayerIdentity(
    generation: number,
    partId: number,
    url: string,
    element: HTMLVideoElement,
  ): boolean {
    return (
      !this.destroyed &&
      this.visible &&
      this.playerGeneration === generation &&
      this.part?.id === partId &&
      this.mediaUrl === url &&
      this.videoElement === element &&
      this.isFlv
    );
  }

  private handlePlayerEvent(event: PartPlayerEvent): void {
    if (event.type === 'first_frame') {
      this.handleFirstFrame();
      return;
    }
    if (event.type === 'stalled') {
      this.fail('本地视频加载停滞，请检查连接后重试');
      return;
    }
    if (event.type === 'error') {
      this.fail(event.message);
    }
  }

  private scheduleRetry(delayMs: number): void {
    const remaining = this.deadlineAt - Date.now();
    if (remaining <= 0) {
      this.fail('本地视频打开超时，请稍后重试');
      return;
    }
    this.clearTimer();
    this.timer = window.setTimeout(
      () => {
        this.timer = null;
        this.requestMediaAccess();
      },
      Math.min(delayMs, remaining),
    );
  }

  private scheduleDeadline(): void {
    const remaining = this.deadlineAt - Date.now();
    if (remaining <= 0) {
      this.fail('本地视频打开超时，请稍后重试');
      return;
    }
    this.clearTimer();
    this.timer = window.setTimeout(() => {
      this.timer = null;
      if (this.playbackState.kind !== 'playing') {
        this.fail('本地视频打开超时，请检查录像文件后重试');
      }
    }, remaining);
  }

  private fail(message: string): void {
    this.clearTimer();
    this.playbackState = { kind: 'error', message };
    this.invalidatePlayer();
    this.changeDetector.markForCheck();
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private teardownPlayer(): void {
    const player = this.player;
    this.player = null;
    this.destroyPlayer(player);
  }

  private destroyPlayer(player: PartPlayer | null): void {
    player?.pause();
    player?.unload();
    player?.detachMediaElement();
    player?.destroy();
  }

  private invalidatePlayer(): void {
    this.playerGeneration += 1;
    this.pendingPlayerGeneration = null;
    this.teardownPlayer();
  }

  private reset(): void {
    this.request?.unsubscribe();
    this.request = undefined;
    this.danmakuRequest?.unsubscribe();
    this.danmakuRequest = undefined;
    this.clearTimer();
    this.invalidatePlayer();
    this.mediaUrl = null;
    this.mediaAccess = null;
    this.playbackState = { kind: 'idle' };
    this.danmakuItems = [];
    this.danmakuLoading = false;
    this.danmakuError = null;
    this.danmakuNextCursor = null;
    this.activeDanmakuIndex = null;
    this.followDanmaku = true;
  }

  private describeError(error: unknown, fallback: string): string {
    return error instanceof Error && error.message ? error.message : fallback;
  }
}
