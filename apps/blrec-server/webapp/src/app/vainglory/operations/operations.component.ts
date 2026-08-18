import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { Router } from '@angular/router';

import { Subject } from 'rxjs';
import { finalize, map, takeUntil } from 'rxjs/operators';

import { RealtimeService } from '../../core/services/realtime.service';
import {
  ArchiveMigrationItem,
  ArchiveMigrationRealtimeSnapshot,
  ArchiveMigrationStatus,
  BiliAccount,
} from '../../uploads/shared/bili-account.model';
import { BiliAccountService } from '../../uploads/shared/bili-account.service';
import {
  ArchiveBackfillStage,
  VaingloryAnalysisQueue,
  VaingloryAnalysisQueueSummary,
  VaingloryAnalysisWorkerNodeStatus,
  VaingloryArchiveBackfillItem,
  VaingloryArchiveBackfillRealtimeSnapshot,
  VaingloryArchiveDownloadQueue,
  VaingloryArchiveDownloadQueueItem,
  VaingloryArchiveDownloadQueueState,
  VaingloryArchiveSync,
  VaingloryIndexRealtimeSnapshot,
  VaingloryPublicationAudit,
  VaingloryPublicationRecord,
  VaingloryPublicationRecordFilter,
} from '../vainglory.model';
import { VaingloryService } from '../vainglory.service';

type OperationsTab = 'live' | 'tasks' | 'workers' | 'history' | 'publication';
type HistoryQueueItem = VaingloryArchiveBackfillItem | ArchiveMigrationItem;
type HistoryQueuePage = {
  readonly total: number;
  readonly items: readonly HistoryQueueItem[];
};

@Component({
  selector: 'app-vainglory-operations',
  templateUrl: './operations.component.html',
  styleUrls: ['./operations.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OperationsComponent implements OnInit, OnDestroy {
  readonly tabs: readonly { key: OperationsTab; label: string }[] = [
    { key: 'live', label: '实时直播' },
    { key: 'tasks', label: '处理队列' },
    { key: 'workers', label: 'Worker 节点' },
    { key: 'history', label: '历史流水线' },
    { key: 'publication', label: '稿件回填' },
  ];
  activeTab: OperationsTab = 'live';
  queue: VaingloryAnalysisQueue | null = null;
  sampledAt: number | null = null;
  archiveSyncs: readonly VaingloryArchiveSync[] = [];
  archiveDownloadQueue: VaingloryArchiveDownloadQueue | null = null;
  archiveDownloadQueueLoading = true;
  archiveDownloadQueueSaving = false;
  archiveDownloadConcurrencyDraft: number | null = null;
  archiveDownloadQueueDialogVisible = false;
  archiveDownloadQueueDialogState: VaingloryArchiveDownloadQueueState =
    'pending';
  archiveDownloadQueueDialogItems: readonly VaingloryArchiveDownloadQueueItem[] =
    [];
  archiveDownloadQueueDialogTotal = 0;
  archiveDownloadQueueDialogArchiveCount = 0;
  archiveDownloadQueueDialogPage = 1;
  archiveDownloadQueueDialogLoading = false;
  archiveDownloadQueueDialogRefreshing = false;
  archiveDownloadRetryAllLoading = false;
  readonly archiveDownloadQueuePageSize = 30;
  readonly retryingDownloadPartIds = new Set<number>();
  archiveItemsByAccountId: ReadonlyMap<
    number,
    readonly VaingloryArchiveBackfillItem[]
  > = new Map();
  accounts: readonly BiliAccount[] = [];
  migrations: readonly ArchiveMigrationStatus[] = [];
  migrationItemsById: ReadonlyMap<
    number,
    readonly ArchiveMigrationItem[]
  > = new Map();
  publicationAudit: VaingloryPublicationAudit | null = null;
  readonly publicationFilters: readonly {
    key: VaingloryPublicationRecordFilter;
    label: string;
  }[] = [
    { key: 'all', label: '全部' },
    { key: 'processing', label: '处理中' },
    { key: 'verified', label: '已验证' },
    { key: 'needs_action', label: '需要处理' },
  ];
  publicationFilter: VaingloryPublicationRecordFilter = 'all';
  publicationRecords: readonly VaingloryPublicationRecord[] = [];
  publicationRecordTotal = 0;
  publicationRecordPage = 1;
  readonly publicationRecordPageSize = 20;
  publicationRecordsLoading = true;
  accountsLoading = true;
  migrationLoading = true;
  auditLoading = true;
  auditQueuing = false;
  pageError: string | null = null;
  actionMessage: string | null = null;
  workerDialogVisible = false;
  workerDialogMode: 'create' | 'edit' = 'create';
  workerIdDraft = '';
  workerNameDraft = '';
  workerSaving = false;
  editingWorker: VaingloryAnalysisWorkerNodeStatus | null = null;
  readonly updatingWorkerIds = new Set<string>();
  readonly archiveControlIds = new Set<number>();
  readonly migrationControlIds = new Set<number>();
  readonly retryingPublicationIds = new Set<number>();
  readonly archiveDailyLimitDrafts = new Map<number, number>();
  readonly migrationDailyLimitDrafts = new Map<number, number>();
  processingQueueVisible = false;
  processingQueueLoading = false;
  processingQueueItems: readonly VaingloryAnalysisQueueSummary[] = [];
  processingQueueTotal = 0;
  processingQueuePage = 1;
  readonly queuePageSize = 20;
  historyQueueVisible = false;
  historyQueueLoading = false;
  historyQueueSource = '';
  historyQueueItems: readonly HistoryQueueItem[] = [];
  historyQueueTotal = 0;
  historyQueuePage = 1;

  private readonly destroy$ = new Subject<void>();
  private archiveDownloadQueueDialogRequestId = 0;

  constructor(
    private changeDetector: ChangeDetectorRef,
    private realtime: RealtimeService,
    private vainglory: VaingloryService,
    private accountsService: BiliAccountService,
    private router: Router,
  ) {}

  ngOnInit(): void {
    this.loadAccounts();
    this.loadMigrations();
    this.loadArchiveDownloadQueue();
    this.loadPublicationAudit();
    this.loadPublicationRecords();
    this.realtime.events$
      .pipe(takeUntil(this.destroy$))
      .subscribe((event) => {
        if (event.type === 'resync') {
          this.loadAccounts(false);
          this.loadMigrations(false);
          this.loadArchiveDownloadQueue(false);
          this.loadPublicationAudit(false);
          this.loadPublicationRecords(false);
          return;
        }
        if (event.type === 'vainglory_index') {
          this.applyIndexSnapshot(event.data);
        } else if (event.type === 'archive_backfill') {
          this.applyArchiveSnapshot(event.data);
        } else if (event.type === 'archive_migration') {
          this.applyMigrationSnapshot(event.data);
        }
      });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  get onlineWorkerCount(): number {
    return (
      this.queue?.workers.filter(
        (worker) => worker.enabled && worker.state === 'running',
      ).length ?? 0
    );
  }

  get workerCount(): number {
    return this.queue?.workers.length ?? 0;
  }

  get activeTaskCount(): number {
    return this.queue?.active.length ?? 0;
  }

  get pendingTaskCount(): number {
    return this.queue?.pendingCount ?? 0;
  }

  get activeHistoryCount(): number {
    return (
      this.archiveSyncs.filter(
        (sync) => !sync.operatorPaused && sync.state !== 'ready',
      ).length +
      this.migrations.filter(
        (migration) =>
          !migration.operatorPaused && migration.state !== 'completed',
      ).length
    );
  }

  get historyIssueCount(): number {
    return (
      this.archiveSyncs.filter((sync) => sync.state === 'failed').length +
      this.migrations.filter((migration) => migration.state === 'failed').length
    );
  }

  get activeAccountsWithoutSync(): readonly BiliAccount[] {
    const synced = new Set(this.archiveSyncs.map((sync) => sync.accountId));
    return this.accounts.filter(
      (account) => account.state === 'active' && !synced.has(account.id),
    );
  }

  get historyQueueSources(): readonly { key: string; label: string }[] {
    return [
      ...this.archiveSyncs.map((sync) => ({
        key: `archive:${sync.accountId}`,
        label: `历史接入 · ${this.accountLabel(sync.accountId)}`,
      })),
      ...this.migrations.map((migration) => ({
        key: `migration:${migration.id}`,
        label: `历史搬运 · ${migration.sourceName || `UID ${migration.sourceUid}`}`,
      })),
    ];
  }

  selectTab(index: number): void {
    this.activeTab = this.tabs[index]?.key ?? 'tasks';
  }

  openProcessingQueue(): void {
    this.processingQueueVisible = true;
    this.processingQueuePage = 1;
    this.loadProcessingQueue();
  }

  changeProcessingQueuePage(page: number): void {
    this.processingQueuePage = page;
    this.loadProcessingQueue();
  }

  openHistoryQueue(): void {
    this.historyQueueVisible = true;
    this.historyQueuePage = 1;
    const sources = this.historyQueueSources;
    if (!sources.some((source) => source.key === this.historyQueueSource)) {
      this.historyQueueSource = sources[0]?.key ?? '';
    }
    this.loadHistoryQueue();
  }

  selectHistoryQueueSource(source: string): void {
    this.historyQueueSource = source;
    this.historyQueuePage = 1;
    this.loadHistoryQueue();
  }

  changeHistoryQueuePage(page: number): void {
    this.historyQueuePage = page;
    this.loadHistoryQueue();
  }

  queueCategoryLabel(category: VaingloryAnalysisQueueSummary['category']): string {
    return {
      realtime: '近期直播',
      manual: '人工任务',
      archive: '历史接入',
      migration: '历史搬运',
      backlog: '普通积压',
    }[category];
  }

  historyItemTitle(
    item: VaingloryArchiveBackfillItem | ArchiveMigrationItem,
  ): string {
    return item.title || item.bvid;
  }

  historyItemState(
    item: VaingloryArchiveBackfillItem | ArchiveMigrationItem,
  ): string {
    return 'stage' in item
      ? this.archiveStageLabel(item.stage)
      : this.migrationItemLabel(item);
  }

  historyItemDetail(
    item: VaingloryArchiveBackfillItem | ArchiveMigrationItem,
  ): string {
    if ('stage' in item) {
      return item.error || item.currentPartTitle || `已识别 ${item.matchCount} 局`;
    }
    return item.error || `分析：${item.analysisState || '等待'} · 投稿：${
      item.uploadState || '等待'
    }`;
  }

  tabIndex(): number {
    return Math.max(
      0,
      this.tabs.findIndex((tab) => tab.key === this.activeTab),
    );
  }

  accountLabel(accountId: number): string {
    const account = this.accounts.find((value) => value.id === accountId);
    return account?.displayName || `投稿账号 #${accountId}`;
  }

  archiveItems(
    accountId: number,
  ): readonly VaingloryArchiveBackfillItem[] {
    return this.archiveItemsByAccountId.get(accountId) ?? [];
  }

  currentArchiveItems(
    accountId: number,
  ): readonly VaingloryArchiveBackfillItem[] {
    return this.archiveItems(accountId)
      .filter(
        (item) =>
          item.stage !== 'completed' && item.stage !== 'managed_elsewhere',
      )
      .slice(0, 6);
  }

  archiveStageLabel(stage: ArchiveBackfillStage): string {
    const labels: Readonly<Record<ArchiveBackfillStage, string>> = {
      queued: '等待读取',
      reading_metadata: '读取稿件',
      download_pending: '等待下载',
      downloading: '获取媒体',
      analysis_pending: '等待分析',
      scanning_video: '扫描录像',
      locating_results: '定位结算',
      ocr_recognition: '识别战绩',
      publication_pending: '等待回填',
      publishing_description: '回填简介',
      publishing_comments: '回填评论',
      pinning_comment: '置顶评论',
      completed: '已完成',
      managed_elsewhere: '已有本地任务',
      failed: '需要处理',
    };
    return labels[stage];
  }

  archiveStateLabel(sync: VaingloryArchiveSync): string {
    if (sync.operatorPaused) {
      return '已暂停';
    }
    const labels: Readonly<Record<VaingloryArchiveSync['state'], string>> = {
      idle: '等待开始',
      discovering: '发现稿件',
      running: '持续接入',
      ready: '本轮完成',
      failed: '需要处理',
    };
    return labels[sync.state];
  }

  archiveStateColor(sync: VaingloryArchiveSync): string {
    if (sync.operatorPaused) {
      return 'orange';
    }
    if (sync.state === 'failed') {
      return 'red';
    }
    if (sync.state === 'ready') {
      return 'green';
    }
    return 'blue';
  }

  archiveDailyLimit(sync: VaingloryArchiveSync): number {
    return this.archiveDailyLimitDrafts.get(sync.accountId) ?? sync.dailyLimit;
  }

  setArchiveDailyLimit(accountId: number, value: number | null): void {
    if (value !== null) {
      this.archiveDailyLimitDrafts.set(accountId, value);
    }
  }

  saveArchiveDailyLimit(sync: VaingloryArchiveSync): void {
    this.updateArchiveControl(sync, {
      dailyLimit: this.archiveDailyLimit(sync),
    });
  }

  toggleArchiveSync(sync: VaingloryArchiveSync): void {
    this.updateArchiveControl(sync, { paused: !sync.operatorPaused });
  }

  setArchiveDownloadConcurrency(value: number | null): void {
    this.archiveDownloadConcurrencyDraft = value;
  }

  refreshArchiveDownloadQueue(): void {
    this.loadArchiveDownloadQueue();
  }

  openArchiveDownloadQueue(state: VaingloryArchiveDownloadQueueState): void {
    this.archiveDownloadQueueDialogState = state;
    this.archiveDownloadQueueDialogPage = 1;
    this.archiveDownloadQueueDialogItems = [];
    this.archiveDownloadQueueDialogTotal = 0;
    this.archiveDownloadQueueDialogArchiveCount = 0;
    this.archiveDownloadQueueDialogVisible = true;
    this.loadArchiveDownloadQueueItems(true, true);
  }

  closeArchiveDownloadQueue(): void {
    this.archiveDownloadQueueDialogVisible = false;
    this.archiveDownloadQueueDialogRequestId += 1;
    this.archiveDownloadQueueDialogLoading = false;
    this.archiveDownloadQueueDialogRefreshing = false;
  }

  changeArchiveDownloadQueuePage(page: number): void {
    this.archiveDownloadQueueDialogPage = page;
    this.archiveDownloadQueueDialogItems = [];
    this.loadArchiveDownloadQueueItems(true, true);
  }

  archiveDownloadQueueStateLabel(
    state: VaingloryArchiveDownloadQueueState,
  ): string {
    return {
      pending: '等待下载',
      downloading: '正在下载',
      downloaded_waiting_analysis: '已下载待分析',
      analyzing: '正在分析',
      failed: '下载失败',
    }[state];
  }

  retryArchiveDownload(item: VaingloryArchiveDownloadQueueItem): void {
    if (this.retryingDownloadPartIds.has(item.partId)) {
      return;
    }
    this.retryingDownloadPartIds.add(item.partId);
    this.vainglory
      .retryArchiveDownload(item.partId)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.retryingDownloadPartIds.delete(item.partId);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (queue) => {
          this.archiveDownloadQueue = queue;
          this.actionMessage = `${item.archiveTitle} · P${item.page} 已重新加入下载队列`;
          this.loadArchiveDownloadQueueItems(false, true);
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, '下载任务重试失败');
        },
      });
  }

  retryAllFailedArchiveDownloads(): void {
    if (this.archiveDownloadRetryAllLoading) {
      return;
    }
    this.archiveDownloadRetryAllLoading = true;
    this.vainglory
      .retryFailedArchiveDownloads()
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.archiveDownloadRetryAllLoading = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (result) => {
          this.archiveDownloadQueue = result.queue;
          this.actionMessage = `已将 ${result.retriedCount} 个失败分 P 重新加入下载队列`;
          if (result.failedCount > 0) {
            this.pageError = `${result.failedCount} 个分 P 暂时无法重试，请查看失败原因`;
          }
          this.loadArchiveDownloadQueueItems(false, true);
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, '批量重试下载任务失败');
        },
      });
  }

  archiveDownloadProgress(item: VaingloryArchiveDownloadQueueItem): string {
    if (item.totalBytes === null) {
      return item.downloadedBytes > 0
        ? this.formatBytes(item.downloadedBytes)
        : `${Math.round(item.progress * 100)}%`;
    }
    return `${this.formatBytes(item.downloadedBytes)} / ${this.formatBytes(
      item.totalBytes,
    )}`;
  }

  archiveDownloadRate(item: VaingloryArchiveDownloadQueueItem): string {
    return item.speedBytesPerSecond === null
      ? ''
      : `${this.formatBytes(item.speedBytesPerSecond)}/s`;
  }

  saveArchiveDownloadConcurrency(): void {
    if (this.archiveDownloadQueueSaving) {
      return;
    }
    const value = Number(
      this.archiveDownloadConcurrencyDraft ??
        this.archiveDownloadQueue?.downloadsPerInterface,
    );
    if (!Number.isInteger(value) || value < 1 || value > 8) {
      this.pageError = '每条线路的下载并发必须在 1 到 8 之间';
      return;
    }
    this.archiveDownloadQueueSaving = true;
    this.vainglory
      .updateArchiveDownloadQueue(value)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.archiveDownloadQueueSaving = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (queue) => {
          this.archiveDownloadQueue = queue;
          this.archiveDownloadConcurrencyDraft = queue.downloadsPerInterface;
          this.actionMessage = `下载并发已调整为每条线路 ${queue.downloadsPerInterface} 路，共 ${queue.totalConcurrency} 路`;
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, '下载并发更新失败');
        },
      });
  }

  requestArchiveSync(account: BiliAccount, rescan = false): void {
    if (this.archiveControlIds.has(account.id)) {
      return;
    }
    this.archiveControlIds.add(account.id);
    this.vainglory
      .requestArchiveSync(account.id, rescan)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.archiveControlIds.delete(account.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (sync) => {
          this.upsertArchiveSync(sync);
          this.actionMessage = rescan
            ? `${account.displayName} 已从第一页重新扫描`
            : `${account.displayName} 已恢复历史接入流水线`;
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, '历史接入启动失败');
        },
      });
  }

  rescanArchiveSync(sync: VaingloryArchiveSync): void {
    const account = this.accounts.find((value) => value.id === sync.accountId);
    if (account) {
      this.requestArchiveSync(account, true);
    }
  }

  migrationItems(
    migrationId: number,
  ): readonly ArchiveMigrationItem[] {
    return this.migrationItemsById.get(migrationId) ?? [];
  }

  currentMigrationItems(
    migrationId: number,
  ): readonly ArchiveMigrationItem[] {
    const visible = this.migrationItems(migrationId).filter(
      (item) => item.state !== 'task_created',
    );
    return [
      ...visible.filter((item) => item.state === 'failed'),
      ...visible.filter((item) => item.state !== 'failed'),
    ].slice(0, 6);
  }

  migrationStateLabel(migration: ArchiveMigrationStatus): string {
    if (migration.operatorPaused) {
      return '已暂停';
    }
    return {
      discovering: '发现稿件',
      running: '持续搬运',
      completed: '本轮完成',
      failed: '需要处理',
    }[migration.state];
  }

  migrationStateColor(migration: ArchiveMigrationStatus): string {
    if (migration.operatorPaused) {
      return 'orange';
    }
    if (migration.state === 'failed') {
      return 'red';
    }
    if (migration.state === 'completed') {
      return 'green';
    }
    return 'blue';
  }

  migrationItemLabel(item: ArchiveMigrationItem): string {
    return {
      queued: '等待下载',
      downloading: '下载稿件',
      creating_task: '创建投稿',
      task_created: '已移交投稿',
      failed: '需要处理',
    }[item.state];
  }

  migrationDailyLimit(migration: ArchiveMigrationStatus): number {
    return (
      this.migrationDailyLimitDrafts.get(migration.id) ?? migration.dailyLimit
    );
  }

  setMigrationDailyLimit(migrationId: number, value: number | null): void {
    if (value !== null) {
      this.migrationDailyLimitDrafts.set(migrationId, value);
    }
  }

  saveMigrationDailyLimit(migration: ArchiveMigrationStatus): void {
    this.updateMigrationControl(migration, {
      dailyLimit: this.migrationDailyLimit(migration),
    });
  }

  toggleMigration(migration: ArchiveMigrationStatus): void {
    this.updateMigrationControl(migration, {
      paused: !migration.operatorPaused,
    });
  }

  queuePublicationAudit(): void {
    if (this.auditQueuing || (this.publicationAudit?.staleCount ?? 0) === 0) {
      return;
    }
    this.auditQueuing = true;
    this.actionMessage = null;
    this.vainglory
      .queuePublicationAudit(168, 20)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.auditQueuing = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (result) => {
          this.publicationAudit = result;
          this.loadPublicationRecords();
          this.actionMessage =
            result.queuedCount > 0
              ? `已将 ${result.queuedCount} 条稿件加入低优先级远端验证`
              : '当前没有超过 7 天未验证的稿件';
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, '稿件远端验证排队失败');
        },
      });
  }

  selectPublicationFilter(filter: VaingloryPublicationRecordFilter): void {
    if (this.publicationFilter === filter) {
      return;
    }
    this.publicationFilter = filter;
    this.publicationRecordPage = 1;
    this.loadPublicationRecords();
  }

  changePublicationPage(page: number): void {
    this.publicationRecordPage = page;
    this.loadPublicationRecords();
  }

  retryPublication(record: VaingloryPublicationRecord): void {
    if (this.retryingPublicationIds.has(record.id) || record.state === 'running') {
      return;
    }
    this.retryingPublicationIds.add(record.id);
    this.vainglory
      .retryPublication(record.id)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.retryingPublicationIds.delete(record.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.actionMessage = `${record.title} 已加入重新回填与验证队列`;
          this.loadPublicationAudit();
          this.loadPublicationRecords();
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, '稿件重新回填失败');
        },
      });
  }

  reanalyzePublication(record: VaingloryPublicationRecord): void {
    if (this.retryingPublicationIds.has(record.id) || record.state === 'running') {
      return;
    }
    this.retryingPublicationIds.add(record.id);
    this.vainglory
      .requestScan(record.sessionId)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.retryingPublicationIds.delete(record.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.actionMessage = `${record.title} 已加入重新识别队列`;
          this.loadPublicationAudit();
          this.loadPublicationRecords();
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, '稿件重新识别失败');
        },
      });
  }

  retryPublicationChapter(record: VaingloryPublicationRecord): void {
    if (this.retryingPublicationIds.has(record.id) || record.state === 'running') {
      return;
    }
    this.retryingPublicationIds.add(record.id);
    this.vainglory
      .retryPublicationStep(record.sessionId, 'chapter')
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.retryingPublicationIds.delete(record.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: () => {
          this.actionMessage = `${record.title} 已加入视频分段重算队列`;
          this.loadPublicationAudit();
          this.loadPublicationRecords();
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, '视频分段重算失败');
        },
      });
  }

  publicationStatusColor(record: VaingloryPublicationRecord): string {
    if (record.state === 'failed') {
      return 'red';
    }
    if (record.state === 'confirmed') {
      return 'green';
    }
    if (record.state === 'running') {
      return 'blue';
    }
    if (record.state === 'paused') {
      return 'orange';
    }
    return 'default';
  }

  publicationVisibilityLabel(record: VaingloryPublicationRecord): string {
    return record.visibilityScope === 'owner'
      ? '仅自己可见'
      : record.visibilityScope === 'public'
        ? '公开稿件'
        : '待确认可见性';
  }

  biliArchiveUrl(record: VaingloryPublicationRecord): string {
    return `https://www.bilibili.com/video/${record.bvid}`;
  }

  openAddWorker(): void {
    this.workerDialogMode = 'create';
    this.editingWorker = null;
    this.workerIdDraft = '';
    this.workerNameDraft = '';
    this.workerDialogVisible = true;
  }

  openEditWorker(worker: VaingloryAnalysisWorkerNodeStatus): void {
    this.workerDialogMode = 'edit';
    this.editingWorker = worker;
    this.workerIdDraft = worker.workerId;
    this.workerNameDraft = worker.displayName;
    this.workerDialogVisible = true;
  }

  closeWorkerDialog(): void {
    if (!this.workerSaving) {
      this.workerDialogVisible = false;
    }
  }

  saveWorker(): void {
    const workerId = this.workerIdDraft.trim();
    if (!workerId || this.workerSaving) {
      return;
    }
    this.workerSaving = true;
    const request =
      this.workerDialogMode === 'create'
        ? this.vainglory.addAnalysisWorker(
            workerId,
            this.workerNameDraft.trim(),
          )
        : this.vainglory.updateAnalysisWorker(workerId, {
            displayName: this.workerNameDraft.trim(),
          });
    request
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.workerSaving = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (worker) => {
          this.upsertWorker(worker);
          this.workerDialogVisible = false;
          this.actionMessage =
            this.workerDialogMode === 'create'
              ? 'Worker 已登记，等待心跳'
              : 'Worker 名称已更新';
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, 'Worker 保存失败');
        },
      });
  }

  setWorkerEnabled(change: {
    readonly workerId: string;
    readonly enabled: boolean;
  }): void {
    if (this.updatingWorkerIds.has(change.workerId)) {
      return;
    }
    this.updatingWorkerIds.add(change.workerId);
    this.vainglory
      .updateAnalysisWorker(change.workerId, { enabled: change.enabled })
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.updatingWorkerIds.delete(change.workerId);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (worker) => this.upsertWorker(worker),
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, 'Worker 状态更新失败');
        },
      });
  }

  setWorkerConcurrency(change: {
    readonly workerId: string;
    readonly concurrency: number;
  }): void {
    if (this.updatingWorkerIds.has(change.workerId)) {
      return;
    }
    this.updatingWorkerIds.add(change.workerId);
    this.vainglory
      .updateAnalysisWorker(change.workerId, {
        desiredConcurrency: change.concurrency,
      })
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.updatingWorkerIds.delete(change.workerId);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (worker) => {
          this.upsertWorker(worker);
          this.actionMessage = `Worker 并发数已设为 ${change.concurrency}`;
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, 'Worker 并发数更新失败');
        },
      });
  }

  retryMigrationItem(
    migration: ArchiveMigrationStatus,
    item: ArchiveMigrationItem,
  ): void {
    if (item.state !== 'failed' || this.migrationControlIds.has(migration.id)) {
      return;
    }
    this.migrationControlIds.add(migration.id);
    this.accountsService
      .retryArchiveMigrationItem(migration.id, item.id)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.migrationControlIds.delete(migration.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (updated) => {
          this.migrations = [
            updated,
            ...this.migrations.filter((value) => value.id !== updated.id),
          ];
          this.migrationItemsById = new Map([
            ...this.migrationItemsById,
            [
              migration.id,
              this.migrationItems(migration.id).map((value) =>
                value.id === item.id
                  ? { ...value, state: 'queued', progress: 0, error: null }
                  : value,
              ),
            ],
          ]);
          this.actionMessage = `${item.title || item.bvid} 已重新加入搬运队列`;
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, '历史搬运任务重试失败');
        },
      });
  }

  openSession(sessionId: number): void {
    void this.router.navigate(['/vainglory'], { queryParams: { sessionId } });
  }

  openMatches(request: { readonly sessionId: number }): void {
    this.openSession(request.sessionId);
  }

  trackArchive(_index: number, item: VaingloryArchiveBackfillItem): number {
    return item.id;
  }

  trackMigration(_index: number, item: ArchiveMigrationItem): number {
    return item.id;
  }

  trackPublication(_index: number, item: VaingloryPublicationRecord): number {
    return item.id;
  }

  trackArchiveDownloadQueueItem(
    _index: number,
    item: VaingloryArchiveDownloadQueueItem,
  ): number {
    return item.partId;
  }

  private loadAccounts(showLoading = true): void {
    if (showLoading) {
      this.accountsLoading = true;
    }
    this.accountsService
      .listAccounts()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (accounts) => {
          this.accounts = accounts;
          this.accountsLoading = false;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.accountsLoading = false;
          this.pageError = this.errorMessage(error, '投稿账号读取失败');
          this.changeDetector.markForCheck();
        },
      });
  }

  private loadProcessingQueue(): void {
    this.processingQueueLoading = true;
    const offset = (this.processingQueuePage - 1) * this.queuePageSize;
    this.vainglory
      .listAnalysisQueueItems(this.queuePageSize, offset)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (page) => {
          this.processingQueueItems = page.items;
          this.processingQueueTotal = page.total;
          this.processingQueueLoading = false;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.processingQueueLoading = false;
          this.pageError = this.errorMessage(error, '处理队列读取失败');
          this.changeDetector.markForCheck();
        },
      });
  }

  private loadHistoryQueue(): void {
    if (!this.historyQueueSource) {
      this.historyQueueItems = [];
      this.historyQueueTotal = 0;
      return;
    }
    this.historyQueueLoading = true;
    const [kind, rawId] = this.historyQueueSource.split(':', 2);
    const id = Number(rawId);
    const offset = (this.historyQueuePage - 1) * this.queuePageSize;
    const request =
      kind === 'archive'
        ? this.vainglory.listArchiveSyncItemPage(
            id,
            this.queuePageSize,
            offset,
          ).pipe(map((page): HistoryQueuePage => page))
        : this.accountsService.listArchiveMigrationItemPage(
            id,
            this.queuePageSize,
            offset,
          ).pipe(map((page): HistoryQueuePage => page));
    request.pipe(takeUntil(this.destroy$)).subscribe({
      next: (page) => {
        this.historyQueueItems = page.items;
        this.historyQueueTotal = page.total;
        this.historyQueueLoading = false;
        this.changeDetector.markForCheck();
      },
      error: (error: unknown) => {
        this.historyQueueLoading = false;
        this.pageError = this.errorMessage(error, '历史队列读取失败');
        this.changeDetector.markForCheck();
      },
    });
  }

  private loadMigrations(showLoading = true): void {
    if (showLoading) {
      this.migrationLoading = true;
    }
    this.accountsService
      .listArchiveMigrations()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (migrations) => {
          this.migrations = migrations;
          this.migrationLoading = false;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.migrationLoading = false;
          this.pageError = this.errorMessage(error, '历史搬运状态读取失败');
          this.changeDetector.markForCheck();
        },
      });
  }

  private loadArchiveDownloadQueue(showLoading = true): void {
    if (showLoading) {
      this.archiveDownloadQueueLoading = true;
    }
    this.vainglory
      .getArchiveDownloadQueue()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (queue) => {
          this.archiveDownloadQueue = queue;
          this.archiveDownloadConcurrencyDraft = queue.downloadsPerInterface;
          this.archiveDownloadQueueLoading = false;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.archiveDownloadQueueLoading = false;
          this.pageError = this.errorMessage(error, '下载队列读取失败');
          this.changeDetector.markForCheck();
        },
      });
  }

  private loadArchiveDownloadQueueItems(
    showLoading = true,
    force = false,
  ): void {
    if (!this.archiveDownloadQueueDialogVisible) {
      return;
    }
    if (
      !force &&
      (this.archiveDownloadQueueDialogLoading ||
        this.archiveDownloadQueueDialogRefreshing)
    ) {
      return;
    }
    const requestId = ++this.archiveDownloadQueueDialogRequestId;
    if (showLoading) {
      this.archiveDownloadQueueDialogLoading = true;
    } else {
      this.archiveDownloadQueueDialogRefreshing = true;
    }
    const offset =
      (this.archiveDownloadQueueDialogPage - 1) *
      this.archiveDownloadQueuePageSize;
    this.vainglory
      .listArchiveDownloadQueueItems(
        this.archiveDownloadQueueDialogState,
        this.archiveDownloadQueuePageSize,
        offset,
      )
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (page) => {
          if (requestId !== this.archiveDownloadQueueDialogRequestId) {
            return;
          }
          this.archiveDownloadQueueDialogItems = page.items;
          this.archiveDownloadQueueDialogTotal = page.total;
          this.archiveDownloadQueueDialogArchiveCount = page.archiveCount;
          this.archiveDownloadQueueDialogLoading = false;
          this.archiveDownloadQueueDialogRefreshing = false;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          if (requestId !== this.archiveDownloadQueueDialogRequestId) {
            return;
          }
          this.archiveDownloadQueueDialogLoading = false;
          this.archiveDownloadQueueDialogRefreshing = false;
          this.pageError = this.errorMessage(error, '下载队列明细读取失败');
          this.changeDetector.markForCheck();
        },
      });
  }

  private loadPublicationAudit(showLoading = true): void {
    if (showLoading) {
      this.auditLoading = true;
    }
    this.vainglory
      .getPublicationAudit(168)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (audit) => {
          this.publicationAudit = audit;
          this.auditLoading = false;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.auditLoading = false;
          this.pageError = this.errorMessage(error, '稿件完成证据读取失败');
          this.changeDetector.markForCheck();
        },
      });
  }

  private loadPublicationRecords(showLoading = true): void {
    if (showLoading) {
      this.publicationRecordsLoading = true;
    }
    const offset =
      (this.publicationRecordPage - 1) * this.publicationRecordPageSize;
    this.vainglory
      .listPublicationRecords(
        this.publicationFilter,
        this.publicationRecordPageSize,
        offset,
      )
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (result) => {
          this.publicationRecords = result.items;
          this.publicationRecordTotal = result.total;
          this.publicationRecordsLoading = false;
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.publicationRecordsLoading = false;
          this.pageError = this.errorMessage(error, '稿件回填记录读取失败');
          this.changeDetector.markForCheck();
        },
      });
  }

  private applyIndexSnapshot(data: unknown): void {
    const snapshot = this.indexSnapshot(data);
    if (snapshot === null) {
      return;
    }
    this.queue = snapshot.analysisQueue;
    this.sampledAt = snapshot.sampledAt;
    this.changeDetector.markForCheck();
  }

  private applyArchiveSnapshot(data: unknown): void {
    const snapshot = this.archiveSnapshot(data);
    if (snapshot === null) {
      return;
    }
    this.archiveSyncs = snapshot.syncs;
    this.archiveItemsByAccountId = new Map(
      snapshot.syncs.map((sync) => [
        sync.accountId,
        snapshot.items[String(sync.accountId)] ?? [],
      ]),
    );
    if (snapshot.downloadQueue) {
      this.archiveDownloadQueue = snapshot.downloadQueue;
      this.archiveDownloadConcurrencyDraft =
        snapshot.downloadQueue.downloadsPerInterface;
      this.archiveDownloadQueueLoading = false;
    }
    if (this.archiveDownloadQueueDialogVisible) {
      this.loadArchiveDownloadQueueItems(false);
    }
    this.changeDetector.markForCheck();
  }

  private applyMigrationSnapshot(data: unknown): void {
    const snapshot = this.migrationSnapshot(data);
    if (snapshot === null) {
      this.loadMigrations();
      return;
    }
    this.migrations = snapshot.migrations;
    this.migrationItemsById = new Map(
      snapshot.migrations.map((migration) => [
        migration.id,
        snapshot.items[String(migration.id)] ?? [],
      ]),
    );
    this.migrationLoading = false;
    this.changeDetector.markForCheck();
  }

  private updateArchiveControl(
    sync: VaingloryArchiveSync,
    control: { readonly paused?: boolean; readonly dailyLimit?: number },
  ): void {
    if (this.archiveControlIds.has(sync.accountId)) {
      return;
    }
    this.archiveControlIds.add(sync.accountId);
    this.vainglory
      .updateArchiveSync(sync.accountId, control)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.archiveControlIds.delete(sync.accountId);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (updated) => {
          this.upsertArchiveSync(updated);
          this.actionMessage = updated.operatorPaused
            ? '历史接入已暂停，当前步骤完成后停止领取'
            : '历史接入已恢复';
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, '历史接入控制失败');
        },
      });
  }

  private updateMigrationControl(
    migration: ArchiveMigrationStatus,
    control: { readonly paused?: boolean; readonly dailyLimit?: number },
  ): void {
    if (this.migrationControlIds.has(migration.id)) {
      return;
    }
    this.migrationControlIds.add(migration.id);
    this.accountsService
      .updateArchiveMigration(migration.id, control)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => {
          this.migrationControlIds.delete(migration.id);
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (updated) => {
          this.migrations = [
            updated,
            ...this.migrations.filter((value) => value.id !== updated.id),
          ];
          this.actionMessage = updated.operatorPaused
            ? '历史搬运已暂停，当前步骤完成后停止领取'
            : '历史搬运已恢复';
        },
        error: (error: unknown) => {
          this.pageError = this.errorMessage(error, '历史搬运控制失败');
        },
      });
  }

  private upsertArchiveSync(sync: VaingloryArchiveSync): void {
    this.archiveSyncs = [
      sync,
      ...this.archiveSyncs.filter((value) => value.accountId !== sync.accountId),
    ];
    this.changeDetector.markForCheck();
  }

  private upsertWorker(worker: VaingloryAnalysisWorkerNodeStatus): void {
    if (this.queue === null) {
      return;
    }
    this.queue = {
      ...this.queue,
      workers: [
        worker,
        ...this.queue.workers.filter(
          (value) => value.workerId !== worker.workerId,
        ),
      ],
    };
    this.changeDetector.markForCheck();
  }

  private indexSnapshot(data: unknown): VaingloryIndexRealtimeSnapshot | null {
    if (typeof data !== 'object' || data === null) {
      return null;
    }
    const sampledAt = Reflect.get(data, 'sampledAt');
    const analysisQueue = Reflect.get(data, 'analysisQueue');
    const indexSummary = Reflect.get(data, 'indexSummary');
    if (
      typeof sampledAt !== 'number' ||
      typeof analysisQueue !== 'object' ||
      analysisQueue === null ||
      typeof indexSummary !== 'object' ||
      indexSummary === null
    ) {
      return null;
    }
    return data as VaingloryIndexRealtimeSnapshot;
  }

  private archiveSnapshot(
    data: unknown,
  ): VaingloryArchiveBackfillRealtimeSnapshot | null {
    if (typeof data !== 'object' || data === null) {
      return null;
    }
    const syncs = Reflect.get(data, 'syncs');
    const items = Reflect.get(data, 'items');
    if (
      !Array.isArray(syncs) ||
      typeof items !== 'object' ||
      items === null ||
      Array.isArray(items)
    ) {
      return null;
    }
    return data as VaingloryArchiveBackfillRealtimeSnapshot;
  }

  private migrationSnapshot(
    data: unknown,
  ): ArchiveMigrationRealtimeSnapshot | null {
    if (typeof data !== 'object' || data === null) {
      return null;
    }
    const migrations = Reflect.get(data, 'migrations');
    const items = Reflect.get(data, 'items');
    if (
      !Array.isArray(migrations) ||
      typeof items !== 'object' ||
      items === null ||
      Array.isArray(items)
    ) {
      return null;
    }
    return data as ArchiveMigrationRealtimeSnapshot;
  }

  private errorMessage(error: unknown, fallback: string): string {
    if (error instanceof HttpErrorResponse) {
      const detail = error.error?.detail;
      return typeof detail === 'string' && detail ? detail : fallback;
    }
    return error instanceof Error && error.message ? error.message : fallback;
  }

  private formatBytes(bytes: number): string {
    const value = Math.max(0, bytes);
    if (value < 1024) {
      return `${Math.round(value)} B`;
    }
    const units = ['KB', 'MB', 'GB', 'TB'];
    let scaled = value / 1024;
    let unit = units[0];
    for (let index = 1; index < units.length && scaled >= 1024; index += 1) {
      scaled /= 1024;
      unit = units[index];
    }
    return `${scaled >= 100 ? scaled.toFixed(0) : scaled.toFixed(1)} ${unit}`;
  }
}
