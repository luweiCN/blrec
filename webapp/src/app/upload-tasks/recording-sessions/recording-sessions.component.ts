import { Clipboard } from '@angular/cdk/clipboard';
import { ChangeDetectorRef, Component, OnInit } from '@angular/core';

import { finalize } from 'rxjs/operators';
import { NzMessageService } from 'ng-zorro-antd/message';

import {
  CommentBranchState,
  DanmakuDecisionAction,
  DanmakuItemProgress,
  DanmakuBranchState,
  DanmakuImportState,
  RecordingArtifactState,
  RecordingPart,
  RecordingSession,
  RecordingSessionFilters,
  RecordingSessionState,
  RecordingSessionsView,
  TranscodeState,
  UploadJobAction,
  UploadJobProgress,
  UploadJobState,
  UploadPartProgress,
  UploadPartState,
} from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';
import { PartContentFocus } from '../part-content-dialog/part-content-dialog.component';

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
  contentVisible = false;
  contentSession: RecordingSession | null = null;
  contentPart: RecordingPart | null = null;
  contentFocus: PartContentFocus = 'video';
  readonly selectedJobIds = new Set<number>();
  uploadAction: UploadJobAction | null = null;
  uploadActionJobIds: readonly number[] = [];
  uploadActionSubmitting = false;
  uploadActionError: string | null = null;
  keyword = '';
  recordingState: RecordingSessionState | null = null;
  uploadState: UploadJobState | 'none' | null = null;
  sortOrder: 'newest' | 'oldest' = 'newest';
  dateRange: Date[] | null = null;
  retryAllLoading = false;

  readonly recordingStateOptions = [
    { label: '录制中', value: 'open' },
    { label: '已归集', value: 'closed' },
    { label: '已中断', value: 'cancelled' },
    { label: '自动恢复中', value: 'manual_review' },
    { label: '已跳过', value: 'skipped' },
  ];
  readonly uploadStateOptions = [
    { label: '未创建任务', value: 'none' },
    { label: '等待制品', value: 'waiting_artifacts' },
    { label: '待上传', value: 'ready' },
    { label: '上传中', value: 'uploading' },
    { label: '投稿中', value: 'submitting' },
    { label: '等待审核', value: 'waiting_review' },
    { label: '审核通过', value: 'approved' },
    { label: '审核未通过', value: 'rejected' },
    { label: '已暂停', value: 'paused' },
    { label: '后续处理完成', value: 'completed' },
  ];

  constructor(
    private recordingSessions: RecordingSessionService,
    private changeDetector: ChangeDetectorRef,
    private clipboard: Clipboard,
    private message: NzMessageService
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

  get selectedJobCount(): number {
    return this.selectedJobIds.size;
  }

  get selectedJobIdsArray(): readonly number[] {
    return [...this.selectedJobIds];
  }

  get uploadActionVisible(): boolean {
    return this.uploadAction !== null && this.uploadActionJobIds.length > 0;
  }

  get pageJobIds(): readonly number[] {
    return this.sessions
      .map((session) => session.uploadJob?.id)
      .filter((jobId): jobId is number => jobId !== undefined);
  }

  get allPageJobsSelected(): boolean {
    return (
      this.pageJobIds.length > 0 &&
      this.pageJobIds.every((jobId) => this.selectedJobIds.has(jobId))
    );
  }

  get somePageJobsSelected(): boolean {
    const selected = this.pageJobIds.filter((jobId) =>
      this.selectedJobIds.has(jobId)
    ).length;
    return selected > 0 && selected < this.pageJobIds.length;
  }

  load(): void {
    this.view = { state: 'loading' };
    const offset = (this.pageIndex - 1) * this.pageSize;
    this.recordingSessions
      .listSessions(this.pageSize, offset, this.filters())
      .subscribe({
        next: (response) => {
          this.view = { state: 'ready', response };
          const currentJobIds = new Set(
            response.sessions
              .map((session) => session.uploadJob?.id)
              .filter((jobId): jobId is number => jobId !== undefined)
          );
          for (const jobId of this.selectedJobIds) {
            if (!currentJobIds.has(jobId)) {
              this.selectedJobIds.delete(jobId);
            }
          }
          if (this.detailVisible && this.selectedSession) {
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

  applyFilters(): void {
    this.pageIndex = 1;
    this.selectedJobIds.clear();
    this.load();
  }

  clearFilters(): void {
    this.keyword = '';
    this.recordingState = null;
    this.uploadState = null;
    this.sortOrder = 'newest';
    this.dateRange = null;
    this.applyFilters();
  }

  dateRangeChanged(value: Date[] | null): void {
    this.dateRange = value;
    this.applyFilters();
  }

  retryAllFailedJobs(): void {
    if (this.retryAllLoading) {
      return;
    }
    this.retryAllLoading = true;
    this.recordingSessions
      .retryFailedJobs()
      .pipe(
        finalize(() => {
          this.retryAllLoading = false;
          this.changeDetector.markForCheck();
        })
      )
      .subscribe({
        next: (response) => {
          const accepted = response.results.filter((result) => result.accepted);
          const rejected = response.results.filter((result) => !result.accepted);
          if (response.results.length === 0) {
            this.message.success('没有可安全重试的失败录像');
          } else if (rejected.length > 0) {
            this.message.warning(
              `已重新排队 ${accepted.length} 个任务，跳过 ${rejected.length} 个：${rejected[0].message}`
            );
          } else {
            this.message.success(`已重新排队 ${accepted.length} 个失败任务`);
          }
          this.load();
        },
        error: (error: unknown) => {
          this.message.error(`重试失败录像出错：${this.describeError(error)}`);
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

  isJobSelected(jobId: number): boolean {
    return this.selectedJobIds.has(jobId);
  }

  setJobSelected(jobId: number, selected: boolean): void {
    if (selected) {
      this.selectedJobIds.add(jobId);
    } else {
      this.selectedJobIds.delete(jobId);
    }
    this.changeDetector.markForCheck();
  }

  setAllPageJobsSelected(selected: boolean): void {
    for (const jobId of this.pageJobIds) {
      if (selected) {
        this.selectedJobIds.add(jobId);
      } else {
        this.selectedJobIds.delete(jobId);
      }
    }
    this.changeDetector.markForCheck();
  }

  openUploadAction(action: UploadJobAction, jobIds: readonly number[]): void {
    const uniqueJobIds = [...new Set(jobIds.filter((jobId) => jobId > 0))];
    if (uniqueJobIds.length === 0 || this.uploadActionSubmitting) {
      return;
    }
    this.uploadAction = action;
    this.uploadActionJobIds = uniqueJobIds;
    this.uploadActionError = null;
    this.changeDetector.markForCheck();
  }

  closeUploadAction(): void {
    if (this.uploadActionSubmitting) {
      return;
    }
    this.uploadAction = null;
    this.uploadActionJobIds = [];
    this.uploadActionError = null;
    this.changeDetector.markForCheck();
  }

  submitUploadAction(): void {
    const action = this.uploadAction;
    const jobIds = [...this.uploadActionJobIds];
    if (!action || jobIds.length === 0 || this.uploadActionSubmitting) {
      return;
    }
    this.uploadActionSubmitting = true;
    this.uploadActionError = null;
    this.recordingSessions
      .runJobAction(action, jobIds)
      .pipe(
        finalize(() => {
          this.uploadActionSubmitting = false;
          this.changeDetector.markForCheck();
        })
      )
      .subscribe({
        next: (response) => {
          const accepted = response.results.filter((result) => result.accepted);
          const rejected = response.results.filter((result) => !result.accepted);
          if (accepted.length === 0) {
            this.uploadActionError = rejected
              .map((result) => `任务 ${result.jobId}：${result.message}`)
              .join('；');
            this.changeDetector.markForCheck();
            return;
          }
          if (rejected.length > 0) {
            this.message.warning(
              `已接受 ${accepted.length} 个任务，${rejected.length} 个未执行：${rejected[0].message}`
            );
          } else if (accepted.length === 1) {
            this.message.success(accepted[0].message);
          } else {
            this.message.success(`已接受 ${accepted.length} 个任务`);
          }
          this.uploadAction = null;
          this.uploadActionJobIds = [];
          this.selectedJobIds.clear();
          this.load();
        },
        error: (error: unknown) => {
          this.uploadActionError = this.describeError(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  uploadActionTitle(): string {
    return this.uploadAction === 'repair_transcode'
      ? '检查并修复转码异常'
      : '重试失败任务';
  }

  uploadActionDescription(): string {
    if (this.uploadAction === 'repair_transcode') {
      return '系统会先核对 B 站稿件状态；只有明确转码失败、本地原文件仍完整的分 P 才会重新上传，并继续使用原稿件。';
    }
    return '系统只会重新排队可以安全重试的失败任务；投稿或分 P 结果未知时不会自动重试。';
  }

  openDetails(session: RecordingSession): void {
    this.selectedSession = session;
    this.detailVisible = true;
    this.changeDetector.markForCheck();
  }

  closeDetails(): void {
    this.detailVisible = false;
    this.selectedSession = null;
    this.changeDetector.markForCheck();
  }

  openPartContent(
    session: RecordingSession,
    part: RecordingPart,
    focus: PartContentFocus
  ): void {
    this.contentSession = session;
    this.contentPart = part;
    this.contentFocus = focus;
    this.contentVisible = true;
    this.changeDetector.markForCheck();
  }

  contentVisibilityChanged(visible: boolean): void {
    this.contentVisible = visible;
    if (!visible) {
      this.contentSession = null;
      this.contentPart = null;
    }
    this.changeDetector.markForCheck();
  }

  sessionStateLabel(state: RecordingSessionState): string {
    return {
      open: '录制中',
      closed: '已归集',
      cancelled: '已中断',
      manual_review: '自动恢复中',
      skipped: '已跳过',
    }[state];
  }

  sessionStateColor(state: RecordingSessionState): string {
    return {
      open: 'processing',
      closed: 'success',
      cancelled: 'warning',
      manual_review: 'processing',
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
      manual_review: '自动恢复中',
    }[state];
  }

  artifactStateColor(state: RecordingArtifactState): string {
    return {
      recording: 'processing',
      postprocessing: 'processing',
      ready: 'success',
      failed: 'error',
      missing: 'warning',
      manual_review: 'processing',
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
      completed: '后续处理完成',
    }[state];
  }

  archiveUrl(
    session: RecordingSession,
    partIndex?: number
  ): string | null {
    const job = session.uploadJob;
    if (!job?.bvid || (job.state !== 'approved' && job.state !== 'completed')) {
      return null;
    }
    const part = partIndex === undefined ? '' : `?p=${partIndex}`;
    return `https://www.bilibili.com/video/${encodeURIComponent(
      job.bvid
    )}${part}`;
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

  uploadDisplayStateLabel(job: UploadJobProgress): string {
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

  uploadDisplayStateColor(job: UploadJobProgress): string {
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

  private filters(): RecordingSessionFilters {
    let startedFrom: number | null = null;
    let startedTo: number | null = null;
    if (this.dateRange?.length === 2) {
      const from = new Date(this.dateRange[0]);
      from.setHours(0, 0, 0, 0);
      const to = new Date(this.dateRange[1]);
      to.setHours(23, 59, 59, 999);
      startedFrom = Math.floor(from.getTime() / 1000);
      startedTo = Math.floor(to.getTime() / 1000);
    }
    return {
      query: this.keyword,
      recordingState: this.recordingState,
      uploadState: this.uploadState,
      startedFrom,
      startedTo,
      sort: this.sortOrder,
    };
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

  transcodeStateLabel(state: TranscodeState): string {
    return {
      unknown: '尚未检查转码',
      ready: '转码正常',
      processing: 'B 站转码中',
      failed: 'B 站转码失败',
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

  fileName(path: string): string {
    const segments = path.split(/[\\/]/).filter(Boolean);
    return segments[segments.length - 1] ?? path;
  }

  copyPath(path: string): void {
    if (this.clipboard.copy(path)) {
      this.message.success('已复制完整路径');
      return;
    }
    this.message.error('复制失败，请重试');
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
