import { ChangeDetectorRef, Component, OnInit } from '@angular/core';

import {
  RecordingArtifactState,
  RecordingPart,
  RecordingSession,
  RecordingSessionState,
  RecordingSessionsView,
} from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';

@Component({
  selector: 'app-recording-sessions',
  templateUrl: './recording-sessions.component.html',
  styleUrls: ['./recording-sessions.component.scss'],
})
export class RecordingSessionsComponent implements OnInit {
  view: RecordingSessionsView = { state: 'loading' };

  constructor(
    private recordingSessions: RecordingSessionService,
    private changeDetector: ChangeDetectorRef
  ) {}

  ngOnInit(): void {
    this.load();
  }

  get sessions(): readonly RecordingSession[] {
    return this.view.state === 'ready' ? this.view.response.sessions : [];
  }

  get degradedReason(): string | null {
    return this.view.state === 'ready'
      ? this.view.response.degradedReason
      : null;
  }

  get errorMessage(): string | null {
    return this.view.state === 'error' ? this.view.message : null;
  }

  load(): void {
    this.view = { state: 'loading' };
    this.recordingSessions.listSessions(50).subscribe({
      next: (response) => {
        this.view = { state: 'ready', response };
        this.changeDetector.markForCheck();
      },
      error: (error: unknown) => {
        this.view = { state: 'error', message: this.describeError(error) };
        this.changeDetector.markForCheck();
      },
    });
  }

  sessionStateLabel(state: RecordingSessionState): string {
    return {
      open: '录制中',
      closed: '已归集',
      cancelled: '已中断',
      manual_review: '需要确认',
      skipped: '已跳过',
    }[state];
  }

  sessionStateColor(state: RecordingSessionState): string {
    return {
      open: 'processing',
      closed: 'success',
      cancelled: 'warning',
      manual_review: 'error',
      skipped: 'default',
    }[state];
  }

  artifactStateLabel(state: RecordingArtifactState): string {
    return {
      recording: '录制中',
      postprocessing: '后处理中',
      ready: '制品就绪',
      failed: '处理失败',
      missing: '文件缺失',
      manual_review: '需要确认',
    }[state];
  }

  artifactStateColor(state: RecordingArtifactState): string {
    return {
      recording: 'processing',
      postprocessing: 'processing',
      ready: 'success',
      failed: 'error',
      missing: 'warning',
      manual_review: 'warning',
    }[state];
  }

  sessionHeader(session: RecordingSession): string {
    const title = session.title || `房间 ${session.roomId}`;
    return `${title} · 房间 ${session.roomId} · ${this.sessionStateLabel(
      session.state
    )}`;
  }

  coverAlt(session: RecordingSession): string {
    return `${session.title || `房间 ${session.roomId}`}的直播封面`;
  }

  areaLabel(session: RecordingSession): string {
    return [session.parentAreaName, session.areaName].filter(Boolean).join(' / ');
  }

  formatDuration(seconds: number | null): string {
    if (seconds === null) {
      return '—';
    }
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainingSeconds = seconds % 60;
    const values: string[] = [];
    if (hours > 0) {
      values.push(`${hours} 小时`);
    }
    if (minutes > 0) {
      values.push(`${minutes} 分`);
    }
    if (remainingSeconds > 0 || values.length === 0) {
      values.push(`${remainingSeconds} 秒`);
    }
    return values.join(' ');
  }

  formatBytes(bytes: number | null): string {
    if (bytes === null) {
      return '—';
    }
    if (bytes < 1024) {
      return `${bytes} B`;
    }
    const units = ['KB', 'MB', 'GB', 'TB'];
    let value = bytes / 1024;
    let unitIndex = 0;
    while (value >= 1024 && unitIndex < units.length - 1) {
      value /= 1024;
      unitIndex += 1;
    }
    const precision = value < 10 && !Number.isInteger(value) ? 1 : 0;
    return `${value.toFixed(precision)} ${units[unitIndex]}`;
  }

  trackSession(_index: number, session: RecordingSession): number {
    return session.id;
  }

  trackPart(_index: number, part: RecordingPart): number {
    return part.id;
  }

  private describeError(error: unknown): string {
    if (error instanceof Error && error.message) {
      return error.message;
    }
    return '录制会话加载失败';
  }
}
