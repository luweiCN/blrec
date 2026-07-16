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
import { PartPlayer, PartPlayerFactory } from './part-player.factory';

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
  loading = false;
  error: string | null = null;

  private videoElement: HTMLVideoElement | null = null;
  private player: PartPlayer | null = null;
  private request?: Subscription;

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
      this.error = '本地视频播放失败，请重新打开后再试';
    }
  }

  handleNativeMediaStalled(): void {
    if (!this.isFlv) {
      this.error = '本地视频加载停滞，请检查连接后重试';
    }
  }

  private loadMedia(): void {
    this.request?.unsubscribe();
    this.teardownPlayer();
    this.mediaUrl = null;
    this.mediaAccess = null;
    this.error = null;
    if (this.localMediaPath === null) {
      this.error = '该分 P 的本地视频不可用';
      return;
    }
    this.loading = true;
    this.request = this.recordingSessions
      .createMediaAccess(this.part.id)
      .subscribe({
        next: (access) => {
          this.mediaAccess = access;
          this.mediaUrl = this.recordingSessions.mediaUrl(this.part.id, access);
          this.loading = false;
          this.attachFlvPlayer();
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.loading = false;
          this.error = this.describeError(error, '本地视频加载失败');
          this.changeDetector.markForCheck();
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
        isLive: false,
        durationMs: access?.durationMs ?? null,
        fileSizeBytes: access?.fileSizeBytes ?? null,
      },
      (message) => {
        this.zone.run(() => {
          this.error = message;
          this.teardownPlayer();
          this.changeDetector.markForCheck();
        });
      }
    );
    if (this.player === null) {
      this.error = '当前浏览器不支持 FLV 播放';
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
    this.teardownPlayer();
    this.mediaUrl = null;
    this.mediaAccess = null;
    this.loading = false;
    this.error = null;
  }

  private describeError(error: unknown, fallback: string): string {
    return error instanceof Error && error.message ? error.message : fallback;
  }
}
