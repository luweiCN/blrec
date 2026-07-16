import {
  ChangeDetectorRef,
  Component,
  ElementRef,
  EventEmitter,
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
  RecordingMediaAccess,
  RecordingPart,
  RecordingSession,
} from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';
import {
  PartPlayer,
  PartPlayerEvent,
  PartPlayerFactory,
} from './part-player.factory';

type PlaybackState =
  | { readonly kind: 'idle' }
  | { readonly kind: 'access_loading' }
  | { readonly kind: 'player_loading' }
  | { readonly kind: 'playing' }
  | { readonly kind: 'error'; readonly message: string };

const PLAYBACK_DEADLINE_MS = 10_000;

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

  private videoElement: HTMLVideoElement | null = null;
  private player: PartPlayer | null = null;
  private request?: Subscription;
  private deadlineAt = 0;
  private timer: number | null = null;

  constructor(
    private recordingSessions: RecordingSessionService,
    private playerFactory: PartPlayerFactory,
    private changeDetector: ChangeDetectorRef,
    private zone: NgZone
  ) {}

  @ViewChild('videoElement')
  set videoElementRef(value: ElementRef<HTMLVideoElement> | undefined) {
    this.videoElement = value?.nativeElement ?? null;
    if (this.videoElement === null) {
      this.teardownPlayer();
      return;
    }
    this.attachFlvPlayer();
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

  private loadMedia(): void {
    this.request?.unsubscribe();
    this.teardownPlayer();
    this.mediaUrl = null;
    this.mediaAccess = null;
    this.playbackState = { kind: 'idle' };
    if (this.localMediaPath === null) {
      this.fail('该分 P 的本地视频不可用');
      return;
    }
    this.deadlineAt = Date.now() + PLAYBACK_DEADLINE_MS;
    this.playbackState = { kind: 'access_loading' };
    this.requestMediaAccess();
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
    if (!this.isFlv || !this.videoElement || !this.mediaUrl || this.player) {
      return;
    }
    const access = this.mediaAccess;
    this.player = this.playerFactory.attachFlv(
      this.videoElement,
      this.mediaUrl,
      {
        playbackMode: access?.playbackMode ?? 'sequential',
        durationMs: access?.durationMs ?? null,
        fileSizeBytes: access?.fileSizeBytes ?? null,
      },
      (event) => {
        this.zone.run(() => {
          this.handlePlayerEvent(event);
        });
      }
    );
    if (this.player === null) {
      this.fail('当前浏览器不支持 FLV 播放');
    }
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
    this.timer = window.setTimeout(() => {
      this.timer = null;
      this.requestMediaAccess();
    }, Math.min(delayMs, remaining));
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
    this.teardownPlayer();
    this.changeDetector.markForCheck();
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private teardownPlayer(): void {
    if (this.player === null) {
      return;
    }
    this.player.pause();
    this.player.unload();
    this.player.detachMediaElement();
    this.player.destroy();
    this.player = null;
  }

  private reset(): void {
    this.request?.unsubscribe();
    this.request = undefined;
    this.clearTimer();
    this.teardownPlayer();
    this.mediaUrl = null;
    this.mediaAccess = null;
    this.playbackState = { kind: 'idle' };
  }

  private describeError(error: unknown, fallback: string): string {
    return error instanceof Error && error.message ? error.message : fallback;
  }
}
