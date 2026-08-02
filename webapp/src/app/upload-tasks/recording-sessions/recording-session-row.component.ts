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
  | { readonly type: 'favorite'; readonly sessionId: number }
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
  @Input() favoriting = false;
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

  favorite(): void {
    this.rowAction.emit({ type: 'favorite', sessionId: this.session.id });
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

  canFavorite(): boolean {
    return (
      this.scope === 'recordings' &&
      this.session.sourceKind === 'live' &&
      this.session.state === 'closed' &&
      this.session.deletionState === 'none' &&
      (this.session.mediaLibraryItemId === null ||
        this.session.mediaLibraryItemId === undefined)
    );
  }

  canAnalyzeMatches(): boolean {
    return (
      this.scope === 'recordings' &&
      this.session.state === 'closed' &&
      this.session.deletionState === 'none' &&
      this.session.partCount > 0
    );
  }

  vaingloryUrl(): string {
    return `/vainglory?sessionId=${this.session.id}`;
  }

  hasAction(action: RecordingSessionAction): boolean {
    return this.session.availableActions.includes(action);
  }

  hasMoreActions(): boolean {
    return (
      this.canCutCurrentFile() ||
      this.canFavorite() ||
      this.canAnalyzeMatches() ||
      this.session.availableActions.length > 0
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

  matchIndexLabel(): string {
    switch (this.session.matchIndexState) {
      case 'pending':
      case 'analyzing':
        return '对局：识别中';
      case 'ready':
        return this.session.matchCount > 0
          ? `对局：${this.session.matchCount} 局`
          : '对局：未发现';
      case 'failed':
        return '对局：识别失败';
      default:
        return '对局：未识别';
    }
  }

  matchIndexColor(): string {
    switch (this.session.matchIndexState) {
      case 'pending':
      case 'analyzing':
        return 'processing';
      case 'ready':
        return this.session.matchCount > 0 ? 'success' : 'default';
      case 'failed':
        return 'error';
      default:
        return 'default';
    }
  }

  matchDescriptionLabel(): string {
    switch (this.session.matchDescriptionState) {
      case 'prepared':
        return '简介：待发送';
      case 'in_flight':
        return '简介：发送中';
      case 'confirmed':
        return '简介：已发送';
      case 'skipped_no_room':
        return '简介：空间不足';
      default:
        return '简介：未生成';
    }
  }

  matchDescriptionColor(): string {
    switch (this.session.matchDescriptionState) {
      case 'prepared':
      case 'in_flight':
        return 'processing';
      case 'confirmed':
        return 'success';
      case 'skipped_no_room':
        return 'warning';
      default:
        return 'default';
    }
  }

  matchCommentLabel(): string {
    const confirmed = this.session.matchConfirmedCommentCount;
    const total = this.session.matchCommentCount;
    if (
      this.session.matchPublicationState === 'failed' &&
      this.session.matchCommentState !== 'confirmed'
    ) {
      return '评论：失败';
    }
    switch (this.session.matchCommentState) {
      case 'prepared':
        return total > 0 && confirmed > 0
          ? `评论：${confirmed} / ${total}`
          : '评论：待发送';
      case 'in_flight':
        return '评论：发送中';
      case 'unknown_outcome':
        return '评论：待核对';
      case 'confirmed':
        return total > 0
          ? `评论：已发送 ${confirmed} / ${total}`
          : '评论：已发送';
      case 'failed':
        return '评论：失败';
      default:
        return '评论：未生成';
    }
  }

  matchCommentColor(): string {
    if (
      this.session.matchPublicationState === 'failed' &&
      this.session.matchCommentState !== 'confirmed'
    ) {
      return 'error';
    }
    switch (this.session.matchCommentState) {
      case 'prepared':
      case 'in_flight':
        return 'processing';
      case 'unknown_outcome':
        return 'warning';
      case 'confirmed':
        return 'success';
      case 'failed':
        return 'error';
      default:
        return 'default';
    }
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
        break;
    }
    if (job.operatorPaused) {
      return this.remoteOutcomeNeedsConfirmation(job)
        ? this.remoteOutcomeLabel(job)
        : '已暂停';
    }
    if (job.submitState === 'unknown_outcome') {
      return '投稿结果待确认';
    }
    if (job.state === 'paused') {
      if (this.remoteOutcomeNeedsConfirmation(job)) {
        return this.remoteOutcomeLabel(job);
      }
      if (this.submissionVerificationRequired(job)) {
        return '需要验证';
      }
      if (job.submitState === 'confirmed') {
        return '稿件核对失败';
      }
      return this.allPartsConfirmed(job) ? '投稿失败' : '上传失败';
    }
    if (this.automaticRetryPending(job)) {
      return job.reviewReason?.includes('次日')
        ? '等待次日投稿'
        : '等待自动重试';
    }
    return this.uploadJobStateLabel(job.state);
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
    if (job.operatorPaused) {
      return 'warning';
    }
    if (job.submitState === 'unknown_outcome') {
      return 'warning';
    }
    if (job.state === 'paused') {
      return this.submissionVerificationRequired(job) ? 'warning' : 'error';
    }
    if (this.automaticRetryPending(job)) {
      return 'processing';
    }
    return this.uploadJobStateColor(job.state);
  }

  retryActionLabel(job: UploadJobSummary | null): string {
    if (this.immediateSubmissionRetry(job)) {
      return '立即重试';
    }
    if (job?.submitState === 'confirmed') {
      return '继续核对';
    }
    return job && this.allPartsConfirmed(job) ? '重新投稿' : '重试上传';
  }

  resumeActionLabel(job: UploadJobSummary | null): string {
    return job?.operatorPaused && this.remoteOutcomeNeedsConfirmation(job)
      ? '确认后继续投稿'
      : '继续上传';
  }

  immediateSubmissionRetry(job: UploadJobSummary | null): boolean {
    const reason = job?.reviewReason ?? '';
    return Boolean(
      job &&
      this.automaticRetryPending(job) &&
      ['137022', '投稿频控冷却', '投稿过于频繁'].some((marker) =>
        reason.includes(marker),
      ),
    );
  }

  showUploadJobReason(job: UploadJobSummary): boolean {
    return Boolean(
      job.reviewReason &&
      (job.state === 'paused' || this.automaticRetryPending(job)),
    );
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

  private allPartsConfirmed(job: UploadJobSummary): boolean {
    return (
      job.discoveredPartCount > 0 &&
      job.confirmedPartCount === job.discoveredPartCount
    );
  }

  private automaticRetryPending(job: UploadJobSummary): boolean {
    return (
      job.nextAttemptAt > 0 &&
      (job.state === 'uploading' || job.state === 'submitting')
    );
  }

  private submissionVerificationRequired(job: UploadJobSummary): boolean {
    const reason = job.reviewReason ?? '';
    return ['验证码', '人工验证', '安全验证', 'captcha', 'geetest'].some(
      (marker) => reason.toLowerCase().includes(marker),
    );
  }

  private remoteOutcomeNeedsConfirmation(job: UploadJobSummary): boolean {
    const reason = job.reviewReason ?? '';
    return ['结果无法确认', '结果未知', '暂未确认'].some((marker) =>
      reason.includes(marker),
    );
  }

  private remoteOutcomeLabel(job: UploadJobSummary): string {
    const reason = job.reviewReason ?? '';
    return reason.includes('投稿') || reason.includes('封面')
      ? '投稿结果待确认'
      : '上传结果待确认';
  }
}
