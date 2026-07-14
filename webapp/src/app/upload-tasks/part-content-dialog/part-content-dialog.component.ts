import {
  Component,
  ElementRef,
  EventEmitter,
  Input,
  OnChanges,
  OnDestroy,
  Output,
  SimpleChanges,
  ViewChild,
} from '@angular/core';

import { Subscription } from 'rxjs';

import {
  RecordingDanmakuLine,
  RecordingPart,
  RecordingSession,
} from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';
import { PartPlayer, PartPlayerFactory } from './part-player.factory';

export type PartContentFocus = 'video' | 'danmaku';

@Component({
  selector: 'app-part-content-dialog',
  templateUrl: './part-content-dialog.component.html',
  styleUrls: ['./part-content-dialog.component.scss'],
})
export class PartContentDialogComponent implements OnChanges, OnDestroy {
  @Input() visible = false;
  @Input() session!: RecordingSession;
  @Input() part!: RecordingPart;
  @Input() focus: PartContentFocus = 'video';
  @Output() visibleChange = new EventEmitter<boolean>();

  mediaUrlValue: string | null = null;
  mediaLoading = false;
  mediaError: string | null = null;
  danmakuItems: readonly RecordingDanmakuLine[] = [];
  danmakuLoading = false;
  danmakuError: string | null = null;
  nextDanmakuCursor: number | null = null;

  private videoElement: HTMLVideoElement | null = null;
  private player: PartPlayer | null = null;
  private mediaRequest?: Subscription;
  private danmakuRequest?: Subscription;

  constructor(
    private recordingSessions: RecordingSessionService,
    private playerFactory: PartPlayerFactory
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
    return this.part
      ? `${this.session.title || `房间 ${this.session.roomId}`} · P${
          this.part.partIndex
        }`
      : '分 P 内容';
  }

  get hasLocalMedia(): boolean {
    return this.localMediaPath !== null;
  }

  get isFlv(): boolean {
    return this.localMediaPath?.toLowerCase().endsWith('.flv') ?? false;
  }

  get remotePartUrl(): string | null {
    const job = this.session?.uploadJob;
    if (
      !job?.bvid ||
      (job.state !== 'approved' && job.state !== 'completed')
    ) {
      return null;
    }
    return `https://www.bilibili.com/video/${encodeURIComponent(
      job.bvid
    )}?p=${this.part.partIndex}`;
  }

  get canLoadMoreDanmaku(): boolean {
    return this.nextDanmakuCursor !== null && !this.danmakuLoading;
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (!this.visible || !this.session || !this.part) {
      if (changes['visible'] && !this.visible) {
        this.reset();
      }
      return;
    }
    if (changes['visible'] || changes['part'] || changes['focus']) {
      this.activate(this.focus);
    }
  }

  ngOnDestroy(): void {
    this.reset();
  }

  switchFocus(focus: PartContentFocus): void {
    if (this.focus === focus) {
      return;
    }
    this.focus = focus;
    this.activate(focus);
  }

  handleClose(): void {
    this.visible = false;
    this.reset();
    this.visibleChange.emit(false);
  }

  loadMoreDanmaku(): void {
    if (this.nextDanmakuCursor === null || this.danmakuLoading) {
      return;
    }
    this.loadDanmaku(this.nextDanmakuCursor, true);
  }

  formatProgress(progressMs: number): string {
    const totalSeconds = Math.floor(progressMs / 1_000);
    const hours = Math.floor(totalSeconds / 3_600);
    const minutes = Math.floor((totalSeconds % 3_600) / 60);
    const seconds = totalSeconds % 60;
    return [hours, minutes, seconds]
      .map((value) => value.toString().padStart(2, '0'))
      .join(':');
  }

  private get localMediaPath(): string | null {
    if (this.part?.finalExists && this.part.finalPath) {
      return this.part.finalPath;
    }
    if (this.part?.sourceExists) {
      return this.part.sourcePath;
    }
    return null;
  }

  private activate(focus: PartContentFocus): void {
    this.mediaRequest?.unsubscribe();
    this.danmakuRequest?.unsubscribe();
    this.teardownPlayer();
    this.mediaUrlValue = null;
    this.mediaError = null;
    this.danmakuError = null;
    if (focus === 'video') {
      this.loadMedia();
      return;
    }
    if (this.danmakuItems.length === 0) {
      this.loadDanmaku(0, false);
    }
  }

  private loadMedia(): void {
    if (!this.hasLocalMedia) {
      return;
    }
    this.mediaLoading = true;
    this.mediaRequest = this.recordingSessions
      .createMediaAccess(this.part.id)
      .subscribe({
        next: (access) => {
          this.mediaUrlValue = this.recordingSessions.mediaUrl(
            this.part.id,
            access
          );
          this.mediaLoading = false;
          this.attachFlvPlayer();
        },
        error: (error: unknown) => {
          this.mediaLoading = false;
          this.mediaError = this.describeError(error, '本地视频加载失败');
        },
      });
  }

  private loadDanmaku(cursor: number, append: boolean): void {
    if (!this.part.xmlPath) {
      this.danmakuError = '该分 P 没有弹幕文件';
      return;
    }
    this.danmakuLoading = true;
    this.danmakuRequest = this.recordingSessions
      .listDanmaku(this.part.id, cursor, 100)
      .subscribe({
        next: (page) => {
          this.danmakuItems = append
            ? [...this.danmakuItems, ...page.items]
            : page.items;
          this.nextDanmakuCursor = page.nextCursor;
          this.danmakuLoading = false;
        },
        error: (error: unknown) => {
          this.danmakuLoading = false;
          this.danmakuError = this.describeError(error, '弹幕加载失败');
        },
      });
  }

  private attachFlvPlayer(): void {
    if (!this.isFlv || !this.videoElement || !this.mediaUrlValue || this.player) {
      return;
    }
    this.player = this.playerFactory.attachFlv(
      this.videoElement,
      this.mediaUrlValue,
      this.part.artifactState === 'recording'
    );
    if (this.player === null) {
      this.mediaError = '当前浏览器不支持 FLV 播放';
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
    this.mediaRequest?.unsubscribe();
    this.danmakuRequest?.unsubscribe();
    this.mediaRequest = undefined;
    this.danmakuRequest = undefined;
    this.teardownPlayer();
    this.mediaUrlValue = null;
    this.mediaLoading = false;
    this.mediaError = null;
    this.danmakuItems = [];
    this.nextDanmakuCursor = null;
    this.danmakuLoading = false;
    this.danmakuError = null;
  }

  private describeError(error: unknown, fallback: string): string {
    return error instanceof Error && error.message ? error.message : fallback;
  }
}
