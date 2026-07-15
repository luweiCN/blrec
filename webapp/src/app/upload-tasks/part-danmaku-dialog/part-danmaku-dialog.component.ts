import {
  Component,
  EventEmitter,
  Input,
  OnChanges,
  OnDestroy,
  Output,
  SimpleChanges,
} from '@angular/core';

import { Subscription } from 'rxjs';

import {
  RecordingDanmakuLine,
  RecordingPart,
  RecordingSession,
} from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';

@Component({
  selector: 'app-part-danmaku-dialog',
  templateUrl: './part-danmaku-dialog.component.html',
  styleUrls: ['./part-danmaku-dialog.component.scss'],
})
export class PartDanmakuDialogComponent implements OnChanges, OnDestroy {
  @Input() visible = false;
  @Input() session!: RecordingSession;
  @Input() part!: RecordingPart;
  @Output() visibleChange = new EventEmitter<boolean>();

  items: readonly RecordingDanmakuLine[] = [];
  loading = false;
  error: string | null = null;
  nextCursor: number | null = null;

  private request?: Subscription;

  constructor(private recordingSessions: RecordingSessionService) {}

  get title(): string {
    return `${this.session.title || `房间 ${this.session.roomId}`} · P${
      this.part.partIndex
    } 弹幕`;
  }

  get canLoadMore(): boolean {
    return this.nextCursor !== null && !this.loading;
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (!this.visible || !this.session || !this.part) {
      if (changes['visible'] && !this.visible) {
        this.reset();
      }
      return;
    }
    if (changes['visible'] || changes['part']) {
      this.items = [];
      this.nextCursor = null;
      this.loadDanmaku(0, false);
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

  loadMore(): void {
    if (!this.canLoadMore || this.nextCursor === null) {
      return;
    }
    this.loadDanmaku(this.nextCursor, true);
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

  private loadDanmaku(cursor: number, append: boolean): void {
    this.request?.unsubscribe();
    this.error = null;
    if (!this.part.xmlPath) {
      this.error = '该分 P 没有弹幕文件';
      return;
    }
    this.loading = true;
    this.request = this.recordingSessions
      .listDanmaku(this.part.id, cursor, 100)
      .subscribe({
        next: (page) => {
          this.items = append ? [...this.items, ...page.items] : page.items;
          this.nextCursor = page.nextCursor;
          this.loading = false;
        },
        error: (error: unknown) => {
          this.loading = false;
          this.error = this.describeError(error, '弹幕加载失败');
        },
      });
  }

  private reset(): void {
    this.request?.unsubscribe();
    this.request = undefined;
    this.items = [];
    this.nextCursor = null;
    this.loading = false;
    this.error = null;
  }

  private describeError(error: unknown, fallback: string): string {
    return error instanceof Error && error.message ? error.message : fallback;
  }
}
