import { ChangeDetectorRef, Component, OnInit } from '@angular/core';

import { finalize } from 'rxjs/operators';

import {
  CommentBranchState,
  DanmakuDecisionAction,
  DanmakuItemProgress,
  DanmakuBranchState,
  DanmakuImportState,
  RecordingArtifactState,
  RecordingPart,
  RecordingSession,
  RecordingSessionState,
  RecordingSessionsView,
  UploadJobState,
  UploadPartProgress,
  UploadPartState,
  UploadSubmitState,
} from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';

@Component({
  selector: 'app-recording-sessions',
  templateUrl: './recording-sessions.component.html',
  styleUrls: ['./recording-sessions.component.scss'],
})
export class RecordingSessionsComponent implements OnInit {
  view: RecordingSessionsView = { state: 'loading' };
  pageIndex = 1;
  pageSize = 20;
  readonly pageSizeOptions = [20, 50, 100];
  selectedSession: RecordingSession | null = null;
  detailVisible = false;
  decisionItem: DanmakuItemProgress | null = null;
  decisionAction: DanmakuDecisionAction | null = null;
  decisionReason = '';
  decisionSubmitting = false;
  decisionError: string | null = null;

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

  get total(): number {
    return this.view.state === 'ready' ? this.view.response.total : 0;
  }

  get errorMessage(): string | null {
    return this.view.state === 'error' ? this.view.message : null;
  }

  get decisionVisible(): boolean {
    return this.decisionItem !== null && this.decisionAction !== null;
  }

  get canSubmitDecision(): boolean {
    return !this.decisionSubmitting && this.decisionReason.trim().length > 0;
  }

  load(): void {
    this.view = { state: 'loading' };
    const offset = (this.pageIndex - 1) * this.pageSize;
    this.recordingSessions.listSessions(this.pageSize, offset).subscribe({
      next: (response) => {
        this.view = { state: 'ready', response };
        if (this.selectedSession) {
          this.selectedSession =
            response.sessions.find(
              (session) => session.id === this.selectedSession?.id
            ) ?? null;
          this.detailVisible = this.selectedSession !== null;
        }
        this.changeDetector.markForCheck();
      },
      error: (error: unknown) => {
        this.view = { state: 'error', message: this.describeError(error) };
        this.changeDetector.markForCheck();
      },
    });
  }

  pageIndexChanged(pageIndex: number): void {
    if (pageIndex === this.pageIndex) {
      return;
    }
    this.pageIndex = pageIndex;
    this.load();
  }

  pageSizeChanged(pageSize: number): void {
    if (pageSize === this.pageSize) {
      return;
    }
    this.pageSize = pageSize;
    this.pageIndex = 1;
    this.load();
  }

  openDetails(session: RecordingSession): void {
    this.selectedSession = session;
    this.detailVisible = true;
    this.changeDetector.markForCheck();
  }

  closeDetails(): void {
    this.detailVisible = false;
    this.changeDetector.markForCheck();
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

  uploadJobStateLabel(state: UploadJobState): string {
    return {
      waiting_artifacts: '等待制品',
      ready: '待上传',
      uploading: '上传中',
      submitting: '投稿中',
      waiting_review: '等待审核',
      approved: '审核通过',
      rejected: '审核未通过',
      paused: '已暂停',
      completed: '已完成',
    }[state];
  }

  uploadJobStateColor(state: UploadJobState): string {
    return {
      waiting_artifacts: 'default',
      ready: 'blue',
      uploading: 'processing',
      submitting: 'processing',
      waiting_review: 'gold',
      approved: 'success',
      rejected: 'error',
      paused: 'warning',
      completed: 'success',
    }[state];
  }

  submitStateLabel(state: UploadSubmitState): string {
    return {
      prepared: '投稿：尚未提交',
      in_flight: '投稿：提交中',
      confirmed: '投稿：已确认',
      unknown_outcome: '投稿：结果未知',
      failed_permanent: '投稿：失败',
    }[state];
  }

  commentBranchLabel(state: CommentBranchState): string {
    return `评论：${
      {
        disabled: '未启用',
        pending: '待处理',
        running: '处理中',
        skipped_no_content: '无 SC/上舰内容',
        skipped_source_missing: '弹幕文件缺失',
        completed: '已完成',
        paused: '已暂停',
        failed: '失败',
      }[state]
    }`;
  }

  danmakuBranchLabel(state: DanmakuBranchState): string {
    return `回灌：${
      {
        disabled: '未启用',
        pending: '待处理',
        importing: '导入中',
        publishing: '发送中',
        skipped_source_missing: '弹幕文件缺失',
        completed: '已完成',
        paused: '已暂停',
        failed: '失败',
      }[state]
    }`;
  }

  uploadPartStateLabel(state: UploadPartState): string {
    return {
      prepared: '等待上传',
      preupload: '正在准备',
      uploading: '上传中',
      completing: '正在确认',
      confirmed: '上传已完成',
      unknown_outcome: '上传结果未知',
      failed: '上传失败',
    }[state];
  }

  uploadPartStateColor(state: UploadPartState): string {
    return {
      prepared: 'default',
      preupload: 'processing',
      uploading: 'processing',
      completing: 'processing',
      confirmed: 'success',
      unknown_outcome: 'warning',
      failed: 'error',
    }[state];
  }

  danmakuImportStateLabel(state: DanmakuImportState): string {
    return {
      disabled: '回灌未启用',
      pending: '弹幕待导入',
      importing: '弹幕导入中',
      waiting_capacity: '等待发送额度',
      missing_source: '弹幕文件缺失',
      completed: '弹幕导入完成',
      failed: '弹幕导入失败',
    }[state];
  }

  openDanmakuDecision(
    item: DanmakuItemProgress,
    action: DanmakuDecisionAction
  ): void {
    this.decisionItem = item;
    this.decisionAction = action;
    this.decisionReason = '';
    this.decisionError = null;
    this.changeDetector.markForCheck();
  }

  closeDanmakuDecision(): void {
    if (this.decisionSubmitting) {
      return;
    }
    this.decisionItem = null;
    this.decisionAction = null;
    this.decisionReason = '';
    this.decisionError = null;
    this.changeDetector.markForCheck();
  }

  submitDanmakuDecision(): void {
    const item = this.decisionItem;
    const action = this.decisionAction;
    const reason = this.decisionReason.trim();
    if (!item || !action || !reason || this.decisionSubmitting) {
      return;
    }
    this.decisionSubmitting = true;
    this.decisionError = null;
    this.recordingSessions
      .decideDanmakuItem(item.id, { action, reason })
      .pipe(
        finalize(() => {
          this.decisionSubmitting = false;
          this.changeDetector.markForCheck();
        })
      )
      .subscribe({
        next: () => {
          this.decisionItem = null;
          this.decisionAction = null;
          this.decisionReason = '';
          this.load();
        },
        error: (error: unknown) => {
          this.decisionError = this.describeError(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  decisionTitle(): string {
    return this.decisionAction === 'assume_success'
      ? '将弹幕视为已发送'
      : '接受重复风险并重试';
  }

  formatDanmakuProgress(progressMs: number): string {
    const totalSeconds = Math.floor(progressMs / 1000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    return [hours, minutes, seconds]
      .map((value) => value.toString().padStart(2, '0'))
      .join(':');
  }

  uploadPartFor(
    session: RecordingSession,
    partIndex: number
  ): UploadPartProgress | null {
    return (
      session.uploadJob?.parts.find((part) => part.partIndex === partIndex) ??
      null
    );
  }

  noUploadJobReason(session: RecordingSession): string {
    if (session.state === 'open') {
      return '本场仍在录制，结束并归集后才会创建投稿任务。';
    }
    return '尚未创建投稿任务；请检查房间投稿设置、账号状态和分 P 制品是否就绪。';
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
