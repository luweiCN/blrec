import { Clipboard } from '@angular/cdk/clipboard';
import {
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';

import { Subscription } from 'rxjs';
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
  RecordingSessionAction,
  RecordingSessionDisplayState,
  RecordingSessionFilters,
  RecordingSessionState,
  RecordingSessionsView,
  TranscodeState,
  UploadJobProgress,
  UploadJobRetryPreviewItem,
  UploadJobState,
  UploadSubmitState,
  UploadPartProgress,
  UploadPartState,
} from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';
import { RealtimeService } from '../../core/services/realtime.service';

interface RealtimeUploadJobProgress {
  readonly jobId: number;
  readonly sessionId: number;
  readonly state: UploadJobState;
  readonly submitState: UploadSubmitState;
  readonly aid: number | null;
  readonly bvid: string | null;
  readonly confirmedBytes: number;
  readonly totalBytes: number;
  readonly percent: number;
  readonly bytesPerSecond: number | null;
  readonly etaSeconds: number | null;
  readonly currentPartIndex: number | null;
}

@Component({
  selector: 'app-recording-sessions',
  templateUrl: './recording-sessions.component.html',
  styleUrls: ['./recording-sessions.component.scss'],
})
export class RecordingSessionsComponent implements OnInit, OnDestroy {
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
  videoVisible = false;
  videoSession: RecordingSession | null = null;
  videoPart: RecordingPart | null = null;
  danmakuVisible = false;
  danmakuSession: RecordingSession | null = null;
  danmakuPart: RecordingPart | null = null;
  readonly selectedSessionIds = new Set<number>();
  uploadAction: RecordingSessionAction | null = null;
  uploadActionSessionIds: readonly number[] = [];
  uploadActionSubmitting = false;
  uploadActionError: string | null = null;
  keyword = '';
  recordingState: RecordingSessionState | null = null;
  uploadState: UploadJobState | 'none' | 'suppressed' | null = null;
  sortOrder: 'newest' | 'oldest' = 'newest';
  dateRange: Date[] | null = null;
  retryAllLoading = false;
  retryPreviewLoading = false;
  retryPreviewVisible = false;
  retryPreviewItems: readonly UploadJobRetryPreviewItem[] = [];
  taskEditVisible = false;
  taskEditJobIds: readonly number[] = [];
  private realtimeSubscription?: Subscription;

  readonly recordingStateOptions = [
    { label: '录制中', value: 'open' },
    { label: '已归集', value: 'closed' },
    { label: '已中断', value: 'cancelled' },
    { label: '自动恢复中', value: 'manual_review' },
    { label: '已跳过', value: 'skipped' },
  ];
  readonly uploadStateOptions = [
    { label: '未创建任务', value: 'none' },
    { label: '已设为不上传', value: 'suppressed' },
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
    private message: NzMessageService,
    private realtime: RealtimeService
  ) {}

  ngOnInit(): void {
    this.load();
    this.realtimeSubscription = this.realtime.events$.subscribe((event) => {
      if (event.type === 'resync') {
        if (this.view.state !== 'loading') {
          this.load();
        }
        return;
      }
      if (event.type === 'upload_progress') {
        this.applyRealtimeUploadProgress(event.data);
      }
    });
  }

  ngOnDestroy(): void {
    this.realtimeSubscription?.unsubscribe();
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

  get selectedSessionCount(): number {
    return this.selectedSessionIds.size;
  }

  get selectedSessionIdsArray(): readonly number[] {
    return [...this.selectedSessionIds];
  }

  get uploadActionVisible(): boolean {
    return this.uploadAction !== null && this.uploadActionSessionIds.length > 0;
  }

  get pageSessionIds(): readonly number[] {
    return this.sessions.map((session) => session.id);
  }

  get allPageSessionsSelected(): boolean {
    return (
      this.pageSessionIds.length > 0 &&
      this.pageSessionIds.every((sessionId) =>
        this.selectedSessionIds.has(sessionId)
      )
    );
  }

  get somePageSessionsSelected(): boolean {
    const selected = this.pageSessionIds.filter((sessionId) =>
      this.selectedSessionIds.has(sessionId)
    ).length;
    return selected > 0 && selected < this.pageSessionIds.length;
  }

  load(): void {
    this.view = { state: 'loading' };
    const offset = (this.pageIndex - 1) * this.pageSize;
    this.recordingSessions
      .listSessions(this.pageSize, offset, this.filters())
      .subscribe({
        next: (response) => {
          this.view = { state: 'ready', response };
          const currentSessionIds = new Set(
            response.sessions.map((session) => session.id)
          );
          for (const sessionId of this.selectedSessionIds) {
            if (!currentSessionIds.has(sessionId)) {
              this.selectedSessionIds.delete(sessionId);
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
    this.selectedSessionIds.clear();
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
    if (this.retryAllLoading || this.retryPreviewLoading) {
      return;
    }
    this.retryPreviewLoading = true;
    this.recordingSessions
      .previewRetryFailedJobs()
      .pipe(
        finalize(() => {
          this.retryPreviewLoading = false;
          this.changeDetector.markForCheck();
        })
      )
      .subscribe({
        next: (response) => {
          this.retryPreviewItems = response.items;
          if (response.items.length === 0) {
            this.message.info('没有可安全重试的失败录像');
            return;
          }
          this.retryPreviewVisible = true;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.message.error(`读取失败录像出错：${this.describeError(error)}`);
        },
      });
  }

  closeRetryFailedPreview(): void {
    if (this.retryAllLoading) {
      return;
    }
    this.retryPreviewVisible = false;
    this.retryPreviewItems = [];
    this.changeDetector.markForCheck();
  }

  submitRetryAllFailedJobs(): void {
    if (this.retryAllLoading || this.retryPreviewItems.length === 0) {
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
          if (rejected.length > 0) {
            this.message.warning(
              `已重新排队 ${accepted.length} 个任务，跳过 ${rejected.length} 个：${rejected[0].message}`
            );
          } else {
            this.message.success(`已重新排队 ${accepted.length} 个失败任务`);
          }
          this.retryPreviewVisible = false;
          this.retryPreviewItems = [];
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

  isSessionSelected(sessionId: number): boolean {
    return this.selectedSessionIds.has(sessionId);
  }

  setSessionSelected(sessionId: number, selected: boolean): void {
    if (selected) {
      this.selectedSessionIds.add(sessionId);
    } else {
      this.selectedSessionIds.delete(sessionId);
    }
    this.changeDetector.markForCheck();
  }

  setAllPageSessionsSelected(selected: boolean): void {
    for (const sessionId of this.pageSessionIds) {
      if (selected) {
        this.selectedSessionIds.add(sessionId);
      } else {
        this.selectedSessionIds.delete(sessionId);
      }
    }
    this.changeDetector.markForCheck();
  }

  openSessionAction(
    action: RecordingSessionAction,
    sessionIds: readonly number[]
  ): void {
    const uniqueSessionIds = [
      ...new Set(sessionIds.filter((sessionId) => sessionId > 0)),
    ];
    if (uniqueSessionIds.length === 0 || this.uploadActionSubmitting) {
      return;
    }
    this.uploadAction = action;
    this.uploadActionSessionIds = uniqueSessionIds;
    this.uploadActionError = null;
    this.changeDetector.markForCheck();
  }

  closeUploadAction(): void {
    if (this.uploadActionSubmitting) {
      return;
    }
    this.uploadAction = null;
    this.uploadActionSessionIds = [];
    this.uploadActionError = null;
    this.changeDetector.markForCheck();
  }

  submitUploadAction(): void {
    const action = this.uploadAction;
    const sessionIds = [...this.uploadActionSessionIds];
    if (!action || sessionIds.length === 0 || this.uploadActionSubmitting) {
      return;
    }
    this.uploadActionSubmitting = true;
    this.uploadActionError = null;
    this.recordingSessions
      .runSessionAction(action, sessionIds)
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
              .map((result) => `场次 ${result.sessionId}：${result.message}`)
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
          this.uploadActionSessionIds = [];
          this.selectedSessionIds.clear();
          this.load();
        },
        error: (error: unknown) => {
          this.uploadActionError = this.describeError(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  uploadActionTitle(): string {
    return {
      retry_failed: '重试上传',
      repair_transcode: '修复转码',
      backfill_danmaku: '回灌弹幕',
      set_upload: '设为本场上传',
      set_skip: '设为本场不上传',
      repost_as_new: '重新投稿',
      pause_upload: '暂停上传',
      resume_upload: '继续上传',
      edit_task: '修改任务',
      delete_local: '删除',
    }[this.uploadAction ?? 'retry_failed'];
  }

  uploadActionDescription(): string {
    return {
      retry_failed:
        '系统只会重新排队可以安全重试的失败任务；投稿或分 P 结果未知时不会自动重试。',
      repair_transcode:
        '系统会先核对 B 站稿件状态；只有明确转码失败、本地原文件仍完整的分 P 才会重新上传，并继续使用原稿件。',
      backfill_danmaku:
        '系统会把本场录制的弹幕发送到已经审核通过的对应分 P；已存在发送记录时不会重复创建。',
      set_upload: '本场文件就绪后会使用当前投稿设置创建上传任务。',
      set_skip: '本场不会上传；录制任务本身仍会继续监控下一场直播。',
      repost_as_new:
        '系统会使用本地成品重新创建一个 B 站稿件。原稿件不会删除，旧 BV 号会保存在本地历史中。',
      pause_upload: '系统会在当前分片的安全检查点暂停，并保留已经完成的上传进度。',
      resume_upload: '系统会从已经保存的上传进度继续，不会重新上传已确认的分片。',
      edit_task: '只有尚未开始上传的任务可以修改投稿账号和本场投稿设置。',
      delete_local:
        '只删除本系统中的任务记录及该场次归属的本地录像、弹幕文件；绝不会删除或修改 B 站上的稿件。',
    }[this.uploadAction ?? 'retry_failed'];
  }

  hasAction(sessionId: number, action: RecordingSessionAction): boolean {
    const session = this.sessions.find((item) => item.id === sessionId);
    return session?.availableActions.includes(action) ?? false;
  }

  hasMoreActions(session: RecordingSession): boolean {
    return session.availableActions.some((action) => action !== 'delete_local');
  }

  openTaskEdit(jobIds: readonly number[]): void {
    const uniqueJobIds = [...new Set(jobIds.filter((jobId) => jobId > 0))];
    if (uniqueJobIds.length === 0) {
      return;
    }
    this.taskEditJobIds = uniqueJobIds;
    this.taskEditVisible = true;
    this.changeDetector.markForCheck();
  }

  closeTaskEdit(): void {
    this.taskEditVisible = false;
    this.taskEditJobIds = [];
    this.changeDetector.markForCheck();
  }

  taskEditSaved(): void {
    this.closeTaskEdit();
    this.selectedSessionIds.clear();
    this.load();
  }

  selectedEditableJobIds(): readonly number[] {
    return this.sessions
      .filter(
        (session) =>
          this.selectedSessionIds.has(session.id) &&
          session.availableActions.includes('edit_task') &&
          session.uploadJob !== null
      )
      .map((session) => session.uploadJob?.id)
      .filter((jobId): jobId is number => jobId !== undefined);
  }

  selectedSupports(action: RecordingSessionAction): boolean {
    return this.sessions.some(
      (session) =>
        this.selectedSessionIds.has(session.id) &&
        session.availableActions.includes(action)
    );
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

  openPartVideo(session: RecordingSession, part: RecordingPart): void {
    this.videoSession = session;
    this.videoPart = part;
    this.videoVisible = true;
    this.changeDetector.markForCheck();
  }

  videoVisibilityChanged(visible: boolean): void {
    this.videoVisible = visible;
    if (!visible) {
      this.videoSession = null;
      this.videoPart = null;
    }
    this.changeDetector.markForCheck();
  }

  openPartDanmaku(session: RecordingSession, part: RecordingPart): void {
    this.danmakuSession = session;
    this.danmakuPart = part;
    this.danmakuVisible = true;
    this.changeDetector.markForCheck();
  }

  danmakuVisibilityChanged(visible: boolean): void {
    this.danmakuVisible = visible;
    if (!visible) {
      this.danmakuSession = null;
      this.danmakuPart = null;
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

  displayStateDetail(session: RecordingSession): string {
    if (session.displayState === 'recording') {
      return ['auto', 'upload'].includes(session.uploadIntent)
        ? '本场结束后上传'
        : '本场不上传';
    }
    if (session.displayState === 'pending_upload' && !session.uploadJob) {
      return '正在准备上传任务';
    }
    if (session.displayState === 'not_uploading') {
      return '保留本地录像';
    }
    if (session.displayState === 'delete_failed') {
      return session.deletionError ?? '删除未完成，可以重新尝试';
    }
    return '';
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

  remotePartUrl(session: RecordingSession, partIndex: number): string | null {
    const uploadPart = this.uploadPartFor(session, partIndex);
    if (uploadPart?.cid === null || uploadPart?.cid === undefined) {
      return null;
    }
    return this.archiveUrl(session, partIndex);
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

  collectionBranchLabel(
    state: UploadJobProgress['collectionBranchState']
  ): string {
    return {
      disabled: '未加入',
      pending: '待处理',
      running: '处理中',
      completed: '已加入',
      failed: '失败',
    }[state];
  }

  submissionVerificationLabel(
    state: UploadJobProgress['submissionVerificationState']
  ): string {
    return {
      pending: '等待核验',
      passed: '可核验项一致',
      different: '发现差异',
      partial: '部分完成',
      failed: '核验失败',
    }[state];
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

  transcodeRepairStageLabel(
    stage: NonNullable<UploadPartProgress['repairStage']>
  ): string {
    return {
      none: '未修复',
      original: '正在重传原文件',
      original_waiting_review: '原文件已重传，等待转码',
      remux: '正在重新封装',
      remux_waiting_review: '重新封装已上传，等待转码',
      completed: '自动修复完成',
      exhausted: '自动修复失败',
    }[stage];
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
    if (session.uploadSuppressed) {
      return '本场已设为不上传，本地录像仍会按保留策略管理。';
    }
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

  formatEta(seconds: number | null): string {
    if (seconds === null) {
      return '预计剩余 —';
    }
    if (seconds <= 0) {
      return '即将完成';
    }
    return `预计剩余 ${this.formatDuration(seconds)}`;
  }

  formatRate(bytesPerSecond: number | null): string {
    return bytesPerSecond === null
      ? '速度 —'
      : `${this.formatBytes(bytesPerSecond)}/s`;
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

  private applyRealtimeUploadProgress(data: unknown): void {
    const updates = this.realtimeUploadJobs(data);
    if (updates === null || this.view.state !== 'ready') {
      return;
    }
    const byJobId = new Map(updates.map((item) => [item.jobId, item]));
    let stateChanged = false;
    let changed = false;
    const sessions = this.view.response.sessions.map((session) => {
      const job = session.uploadJob;
      const update = job ? byJobId.get(job.id) : undefined;
      if (!job || !update) {
        return session;
      }
      changed = true;
      stateChanged =
        stateChanged ||
        job.state !== update.state ||
        job.submitState !== update.submitState ||
        job.aid !== update.aid ||
        job.bvid !== update.bvid;
      return {
        ...session,
        uploadJob: {
          ...job,
          state: update.state,
          submitState: update.submitState,
          aid: update.aid,
          bvid: update.bvid,
          confirmedBytes: update.confirmedBytes,
          totalBytes: update.totalBytes,
          percent: update.percent,
          bytesPerSecond: update.bytesPerSecond,
          etaSeconds: update.etaSeconds,
          currentPartIndex: update.currentPartIndex,
        },
      };
    });
    if (!changed) {
      return;
    }
    this.view = {
      state: 'ready',
      response: { ...this.view.response, sessions },
    };
    if (this.selectedSession !== null) {
      this.selectedSession =
        sessions.find((session) => session.id === this.selectedSession?.id) ??
        null;
    }
    this.changeDetector.markForCheck();
    if (stateChanged) {
      this.load();
    }
  }

  private realtimeUploadJobs(data: unknown): RealtimeUploadJobProgress[] | null {
    if (typeof data !== 'object' || data === null || !('jobs' in data)) {
      return null;
    }
    const jobs = (data as { jobs: unknown }).jobs;
    return Array.isArray(jobs) ? (jobs as RealtimeUploadJobProgress[]) : null;
  }
}
