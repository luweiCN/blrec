import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from '@angular/core';

import {
  RecordingSessionAction,
  RecordingSessionDisplayState,
  RecordingSessionScope,
  RecordingSessionSummary,
  UploadJobState,
  UploadJobSummary,
} from '../shared/recording-session.model';

export type RecordingSessionServerAction = Exclude<
  RecordingSessionAction,
  'edit_submission' | 'edit_task'
>;

export type RecordingSessionRowAction =
  | {
      readonly type: 'selected';
      readonly sessionId: number;
      readonly selected: boolean;
    }
  | { readonly type: 'details'; readonly sessionId: number }
  | { readonly type: 'cut-current'; readonly sessionId: number }
  | { readonly type: 'edit-submission'; readonly sessionId: number }
  | {
      readonly type: 'session-action';
      readonly sessionId: number;
      readonly action: RecordingSessionServerAction;
    }
  | { readonly type: 'edit-task'; readonly jobId: number };

@Component({
  // eslint-disable-next-line @angular-eslint/component-selector -- Attribute host preserves native table markup.
  selector: 'tr[app-recording-session-row]',
  templateUrl: './recording-session-row.component.html',
  styleUrls: ['./recording-session-row.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RecordingSessionRowComponent {
  @Input() session!: RecordingSessionSummary;
  @Input() selected = false;
  @Input() scope: RecordingSessionScope = 'uploads';
  @Input() cutting = false;
  @Output() readonly rowAction = new EventEmitter<RecordingSessionRowAction>();

  selectionChanged(selected: boolean): void {
    this.rowAction.emit({
      type: 'selected',
      sessionId: this.session.id,
      selected,
    });
  }

  showDetails(): void {
    this.rowAction.emit({ type: 'details', sessionId: this.session.id });
  }

  cutCurrent(): void {
    this.rowAction.emit({ type: 'cut-current', sessionId: this.session.id });
  }

  editSubmission(): void {
    this.rowAction.emit({
      type: 'edit-submission',
      sessionId: this.session.id,
    });
  }

  runSessionAction(action: RecordingSessionServerAction): void {
    this.rowAction.emit({
      type: 'session-action',
      sessionId: this.session.id,
      action,
    });
  }

  editTask(jobId: number): void {
    this.rowAction.emit({ type: 'edit-task', jobId });
  }

  canCutCurrentFile(): boolean {
    return (
      this.scope === 'recordings' &&
      this.session.sourceKind === 'live' &&
      this.session.state === 'open'
    );
  }

  hasAction(action: RecordingSessionAction): boolean {
    return this.session.availableActions.includes(action);
  }

  hasMoreActions(): boolean {
    return (
      this.canCutCurrentFile() ||
      this.session.availableActions.some((action) => action !== 'delete_local')
    );
  }

  archiveUrl(): string | null {
    const job = this.session.uploadJob;
    if (!job?.bvid || (job.state !== 'approved' && job.state !== 'completed')) {
      return null;
    }
    return `https://www.bilibili.com/video/${encodeURIComponent(job.bvid)}`;
  }

  coverAlt(): string {
    return `${this.session.title || `房间 ${this.session.roomId}`}的直播封面`;
  }

  recordingPartCountLabel(): string {
    return this.session.state === 'open'
      ? `${this.session.partCount} 个已发现分 P`
      : `${this.session.partCount} 个分 P`;
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

  formatRate(bytesPerSecond: number | null): string {
    return bytesPerSecond === null
      ? '速度 —'
      : `${this.formatBytes(bytesPerSecond)}/s`;
  }

  uploadDisplayStateLabel(job: UploadJobSummary): string {
    if (job.displayState !== 'standard') {
      return {
        preuploading: '录制中 · 正在预上传',
        preuploaded_waiting: '录制中 · 已预上传，等待新分 P',
        preupload_paused: '录制中 · 预上传已暂停',
      }[job.displayState];
    }
    switch (job.repairState) {
      case 'queued':
        return '等待检查转码';
      case 'checking':
        return '检查转码中';
      case 'reuploading':
        return '重传异常分 P';
      case 'editing':
        return '更新原稿件';
      case 'waiting_review':
        return '等待修复审核';
      case 'unknown_outcome':
        return '修复结果待核对';
      case 'failed':
        return '转码修复失败';
      default:
        return this.uploadJobStateLabel(job.state);
    }
  }

  uploadDisplayStateColor(job: UploadJobSummary): string {
    if (job.displayState === 'preupload_paused') {
      return 'warning';
    }
    if (job.displayState !== 'standard') {
      return 'processing';
    }
    if (
      ['queued', 'checking', 'reuploading', 'editing'].includes(job.repairState)
    ) {
      return 'processing';
    }
    if (job.repairState === 'waiting_review') {
      return 'gold';
    }
    if (job.repairState === 'failed') {
      return 'error';
    }
    if (job.repairState === 'unknown_outcome') {
      return 'warning';
    }
    return this.uploadJobStateColor(job.state);
  }

  displayStateLabel(state: RecordingSessionDisplayState): string {
    return {
      recording: '录制中',
      pending_upload: '待上传',
      uploading: '上传处理中',
      waiting_review: '等待审核',
      completed: '审核通过',
      paused: '已暂停',
      deleting: '正在删除',
      delete_failed: '删除失败',
      not_uploading: '不上传',
      needs_attention: '处理异常',
    }[state];
  }

  displayStateColor(state: RecordingSessionDisplayState): string {
    return {
      recording: 'processing',
      pending_upload: 'blue',
      uploading: 'processing',
      waiting_review: 'gold',
      completed: 'success',
      paused: 'warning',
      deleting: 'processing',
      delete_failed: 'error',
      not_uploading: 'default',
      needs_attention: 'error',
    }[state];
  }

  displayStateDetail(): string {
    if (this.session.displayState === 'recording') {
      return ['auto', 'upload'].includes(this.session.uploadIntent)
        ? '本场结束后上传'
        : '本场不上传';
    }
    if (
      this.session.displayState === 'pending_upload' &&
      !this.session.uploadJob
    ) {
      return '正在准备上传任务';
    }
    if (this.session.displayState === 'not_uploading') {
      return '保留本地录像';
    }
    if (this.session.displayState === 'delete_failed') {
      return this.session.deletionError ?? '删除未完成，可以重新尝试';
    }
    return '';
  }

  preuploadPartDetail(job: UploadJobSummary): string | null {
    if (job.preuploadFinalized) {
      return null;
    }
    return `已预上传 ${job.confirmedPartCount} / ${job.discoveredPartCount} 个已封口分 P`;
  }

  collectionBranchLabel(
    state: UploadJobSummary['collectionBranchState'],
  ): string {
    return {
      disabled: '未加入',
      pending: '待处理',
      running: '处理中',
      completed: '已加入',
      failed: '失败',
    }[state];
  }

  private uploadJobStateLabel(state: UploadJobState): string {
    return {
      waiting_artifacts: '等待制品',
      ready: '待上传',
      uploading: '上传中',
      submitting: '投稿中',
      waiting_review: '等待审核',
      approved: '审核通过',
      rejected: '审核未通过',
      paused: '已暂停',
      completed: '后续处理完成',
    }[state];
  }

  private uploadJobStateColor(state: UploadJobState): string {
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
}
