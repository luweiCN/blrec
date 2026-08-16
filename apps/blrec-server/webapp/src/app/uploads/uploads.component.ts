import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';

import { Subject, from, timer } from 'rxjs';
import { map, switchMap, takeUntil } from 'rxjs/operators';

import { RealtimeService } from '../core/services/realtime.service';

import {
  AccountRelationships,
  AccountRemovalRequest,
  AccountState,
  AccountsView,
  ArchiveMigrationItem,
  ArchiveMigrationRealtimeSnapshot,
  ArchiveMigrationState,
  ArchiveMigrationStatus,
  BiliAccount,
  LoginView,
  QrDisplay,
  QrSession,
  RemovalMode,
} from './shared/bili-account.model';
import { BiliAccountService } from './shared/bili-account.service';
import { QrCodeRenderer } from './shared/qr-code-renderer.service';

type AccountDialogState =
  | { state: 'closed' }
  | { state: 'loading'; account: BiliAccount }
  | {
      state: 'ready' | 'submitting';
      account: BiliAccount;
      relationships: AccountRelationships;
    }
  | { state: 'error'; account: BiliAccount; message: string };

@Component({
  selector: 'app-uploads',
  templateUrl: './uploads.component.html',
  styleUrls: ['./uploads.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class UploadsComponent implements OnInit, OnDestroy {
  accountsView: AccountsView = { state: 'loading' };
  loginView: LoginView = { state: 'idle' };
  loginDialogVisible = false;
  primaryDialog: AccountDialogState = { state: 'closed' };
  removalDialog: AccountDialogState = { state: 'closed' };
  removalMode: RemovalMode = 'follow_primary';
  replacementAccountId: number | null = null;
  newPrimaryAccountId: number | null = null;
  actionError: string | null = null;
  actionMessage: string | null = null;
  archiveMigrations: readonly ArchiveMigrationStatus[] = [];
  archiveMigrationItemsById: ReadonlyMap<
    number,
    readonly ArchiveMigrationItem[]
  > = new Map();
  archiveMigrationsLoading = true;
  archiveMigrationError: string | null = null;
  archiveMigrationSourceUid: number | null = null;
  archiveDownloadAccountId: number | null = null;
  archiveTargetAccountId: number | null = null;
  archiveMigrationSubmitting = false;
  readonly credentialVersionTip =
    '每次成功更换登录凭据后递增，用于防止旧任务覆盖新凭据；它不是账号等级或软件版本。';
  readonly credentialExpiryTip =
    '这是 B 站扫码接口返回的 TV access token 预计失效时间，不代表账号本身或 Web Cookie 会在此刻同时失效；系统会在接近过期或 B 站要求时自动续期。';
  readonly primaryAccountTip =
    '主账号的 Cookie 用于需要登录的房间信息和画质查询；后续投稿、评论和视频弹幕也固定使用这个账号，不会逐请求轮换。';

  private readonly destroy$ = new Subject<void>();
  private readonly stopQrPolling$ = new Subject<void>();
  private readonly checkingAccountIds = new Set<number>();
  private readonly failedAvatarUrls = new Set<string>();
  readonly archiveMigrationControlIds = new Set<number>();
  readonly archiveMigrationDailyLimitDrafts = new Map<number, number>();
  constructor(
    private accountService: BiliAccountService,
    private changeDetector: ChangeDetectorRef,
    private qrCodeRenderer: QrCodeRenderer,
    private realtime: RealtimeService
  ) {}

  ngOnInit(): void {
    this.loadAccounts();
    this.loadArchiveMigrations();
    this.realtime.events$
      .pipe(takeUntil(this.destroy$))
      .subscribe((event) => {
        if (event.type === 'resync') {
          this.loadArchiveMigrations();
          return;
        }
        if (event.type === 'archive_migration') {
          this.applyArchiveMigrationSnapshot(event.data);
        }
      });
  }

  ngOnDestroy(): void {
    this.stopQrPolling$.next();
    this.stopQrPolling$.complete();
    this.destroy$.next();
    this.destroy$.complete();
  }

  get accounts(): readonly BiliAccount[] {
    return this.accountsView.state === 'ready'
      ? this.accountsView.accounts
      : [];
  }

  get accountsError(): string | null {
    return this.accountsView.state === 'error'
      ? this.accountsView.message
      : null;
  }

  get visibleQr(): QrDisplay | null {
    switch (this.loginView.state) {
      case 'waiting':
      case 'scanned':
      case 'cancelling':
        return this.loginView;
      default:
        return null;
    }
  }

  get canCancelLogin(): boolean {
    return (
      this.loginView.state === 'waiting' || this.loginView.state === 'scanned'
    );
  }

  get primaryDialogVisible(): boolean {
    return this.primaryDialog.state !== 'closed';
  }

  get primaryDialogAccount(): BiliAccount | null {
    return this.primaryDialog.state === 'closed'
      ? null
      : this.primaryDialog.account;
  }

  get primaryRelationships(): AccountRelationships | null {
    return this.primaryDialog.state === 'ready' ||
      this.primaryDialog.state === 'submitting'
      ? this.primaryDialog.relationships
      : null;
  }

  get primaryDialogError(): string | null {
    return this.primaryDialog.state === 'error'
      ? this.primaryDialog.message
      : null;
  }

  get removalDialogVisible(): boolean {
    return this.removalDialog.state !== 'closed';
  }

  get removalDialogAccount(): BiliAccount | null {
    return this.removalDialog.state === 'closed'
      ? null
      : this.removalDialog.account;
  }

  get removalRelationships(): AccountRelationships | null {
    return this.removalDialog.state === 'ready' ||
      this.removalDialog.state === 'submitting'
      ? this.removalDialog.relationships
      : null;
  }

  get removalDialogError(): string | null {
    return this.removalDialog.state === 'error'
      ? this.removalDialog.message
      : null;
  }

  get activeReplacementAccounts(): readonly BiliAccount[] {
    const target = this.removalDialogAccount;
    if (!target) {
      return [];
    }
    return this.accounts.filter(
      (account) => account.id !== target.id && account.state === 'active'
    );
  }

  get activeAccounts(): readonly BiliAccount[] {
    return this.accounts.filter((account) => account.state === 'active');
  }

  get canRequestArchiveMigration(): boolean {
    const sourceUid = this.archiveMigrationSourceUid;
    const downloadAccountId = this.archiveDownloadAccountId;
    const targetAccountId = this.archiveTargetAccountId;
    if (
      this.archiveMigrationSubmitting ||
      sourceUid === null ||
      !Number.isInteger(sourceUid) ||
      sourceUid <= 0 ||
      downloadAccountId === null ||
      targetAccountId === null
    ) {
      return false;
    }
    const activeIds = new Set(this.activeAccounts.map((account) => account.id));
    const target = this.activeAccounts.find(
      (account) => account.id === targetAccountId
    );
    return (
      activeIds.has(downloadAccountId) &&
      target !== undefined &&
      target.uid !== sourceUid
    );
  }

  get canConfirmRemoval(): boolean {
    if (this.removalDialog.state !== 'ready') {
      return false;
    }
    const relationships = this.removalDialog.relationships;
    if (relationships.blockingJobs.length > 0) {
      return false;
    }
    const replacements = this.activeReplacementAccounts;
    if (relationships.isPrimary) {
      if (replacements.length === 0) {
        return this.removalMode === 'disable';
      }
      if (!this.isActiveReplacement(this.newPrimaryAccountId)) {
        return false;
      }
    }
    if (this.removalMode === 'fixed') {
      return this.isActiveReplacement(this.replacementAccountId);
    }
    if (this.removalMode === 'follow_primary' && !relationships.isPrimary) {
      return replacements.some((account) => account.isPrimary);
    }
    return true;
  }

  retryAccounts(): void {
    this.loadAccounts();
  }

  requestArchiveMigration(
    existing: ArchiveMigrationStatus | null = null
  ): void {
    const sourceUid = existing?.sourceUid ?? this.archiveMigrationSourceUid;
    const downloadAccountId =
      existing?.downloadAccountId ?? this.archiveDownloadAccountId;
    const targetAccountId =
      existing?.targetAccountId ?? this.archiveTargetAccountId;
    if (
      sourceUid === null ||
      !Number.isInteger(sourceUid) ||
      sourceUid <= 0 ||
      downloadAccountId === null ||
      targetAccountId === null
    ) {
      this.archiveMigrationError = '请填写源账号 UID，并选择下载账号和目标账号';
      this.changeDetector.markForCheck();
      return;
    }
    const target = this.activeAccounts.find(
      (account) => account.id === targetAccountId
    );
    if (!target || target.uid === sourceUid) {
      this.archiveMigrationError = '源账号和目标账号不能相同';
      this.changeDetector.markForCheck();
      return;
    }
    this.archiveMigrationSubmitting = true;
    this.archiveMigrationError = null;
    this.changeDetector.markForCheck();
    this.accountService
      .requestArchiveMigration({
        sourceUid,
        downloadAccountId,
        targetAccountId,
      })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (migration) => {
          this.archiveMigrationSubmitting = false;
          this.upsertArchiveMigration(migration);
          this.loadArchiveMigrationItems([migration]);
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.archiveMigrationSubmitting = false;
          this.archiveMigrationError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  refreshArchiveMigrations(): void {
    this.loadArchiveMigrations();
  }

  archiveMigrationDailyLimit(migration: ArchiveMigrationStatus): number {
    return (
      this.archiveMigrationDailyLimitDrafts.get(migration.id) ??
      migration.dailyLimit
    );
  }

  setArchiveMigrationDailyLimit(migrationId: number, value: number): void {
    this.archiveMigrationDailyLimitDrafts.set(migrationId, Number(value));
  }

  updateArchiveMigrationControl(
    migration: ArchiveMigrationStatus,
    paused?: boolean
  ): void {
    if (this.archiveMigrationControlIds.has(migration.id)) {
      return;
    }
    const dailyLimit = this.archiveMigrationDailyLimit(migration);
    if (!Number.isInteger(dailyLimit) || dailyLimit < 1 || dailyLimit > 1000) {
      this.archiveMigrationError = '每日处理上限必须是 1 到 1000 的整数';
      this.changeDetector.markForCheck();
      return;
    }
    this.archiveMigrationControlIds.add(migration.id);
    this.accountService
      .updateArchiveMigration(migration.id, { paused, dailyLimit })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (updated) => {
          this.archiveMigrationControlIds.delete(migration.id);
          this.archiveMigrationDailyLimitDrafts.delete(migration.id);
          this.upsertArchiveMigration(updated);
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.archiveMigrationControlIds.delete(migration.id);
          this.archiveMigrationError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  toggleArchiveMigration(migration: ArchiveMigrationStatus): void {
    this.updateArchiveMigrationControl(migration, !migration.operatorPaused);
  }

  archiveMigrationSourceLabel(migration: ArchiveMigrationStatus): string {
    const source = this.accounts.find(
      (account) => account.uid === migration.sourceUid
    );
    const displayName = source?.displayName ?? migration.sourceName;
    return displayName
      ? `${displayName}（UID ${migration.sourceUid}）`
      : `旧投稿账号（UID ${migration.sourceUid}）`;
  }

  archiveMigrationStateLabel(state: ArchiveMigrationState): string {
    switch (state) {
      case 'discovering':
        return '正在读取稿件列表';
      case 'running':
        return '正在逐稿处理';
      case 'completed':
        return '本轮处理完成';
      case 'failed':
        return '读取失败';
      default: {
        const exhaustive: never = state;
        throw new Error(`未知迁移状态：${exhaustive}`);
      }
    }
  }

  archiveMigrationItems(
    migration: ArchiveMigrationStatus
  ): readonly ArchiveMigrationItem[] {
    return this.archiveMigrationItemsById.get(migration.id) ?? [];
  }

  archiveMigrationCurrentItem(
    migration: ArchiveMigrationStatus
  ): ArchiveMigrationItem | null {
    const items = this.archiveMigrationItems(migration);
    return (
      items.find((item) => this.archiveMigrationItemIsActive(item)) ??
      items[0] ??
      null
    );
  }

  archiveMigrationItemStateLabel(item: ArchiveMigrationItem): string {
    switch (item.state) {
      case 'queued':
        return '等待迁移';
      case 'downloading':
        return item.pageCount > 1
          ? `正在下载源稿件（${item.downloadedPageCount}/${item.pageCount} P）`
          : '正在下载源稿件';
      case 'creating_task':
        return '正在校验并创建上传任务';
      case 'failed':
        return '迁移失败';
      case 'task_created':
        break;
      default: {
        const exhaustive: never = item.state;
        throw new Error(`未知稿件迁移状态：${exhaustive}`);
      }
    }
    if (item.uploadState === 'waiting_artifacts') {
      return '等待迁移文件就绪';
    }
    if (item.uploadState === 'ready' || item.uploadState === 'uploading') {
      return '正在上传';
    }
    if (item.uploadState === 'submitting') {
      return '正在投稿';
    }
    if (item.uploadState === 'waiting_review') {
      return '等待 B 站审核';
    }
    if (item.uploadState === 'paused') {
      return '上传已暂停';
    }
    if (item.uploadState === 'rejected') {
      return '稿件审核未通过';
    }
    if (
      item.danmakuBranchState === 'pending' ||
      item.danmakuBranchState === 'importing' ||
      item.danmakuBranchState === 'publishing'
    ) {
      return '正在回灌分 P 弹幕';
    }
    if (
      item.analysisState === 'pending' ||
      item.analysisState === 'analyzing'
    ) {
      return '正在识别对局';
    }
    if (item.analysisState === 'failed') {
      return '对局识别失败';
    }
    if (
      item.commentBranchState === 'pending' ||
      item.commentBranchState === 'running'
    ) {
      return '正在发布战绩';
    }
    return item.uploadState === 'approved' || item.uploadState === 'completed'
      ? '迁移完成'
      : '上传任务已创建';
  }

  archiveMigrationItemColor(
    item: ArchiveMigrationItem
  ): 'blue' | 'green' | 'gold' | 'red' | 'default' {
    if (item.state === 'failed' || item.uploadState === 'rejected') {
      return 'red';
    }
    if (item.uploadState === 'paused' || item.analysisState === 'failed') {
      return 'gold';
    }
    if (
      (item.uploadState === 'approved' || item.uploadState === 'completed') &&
      item.analysisState !== 'pending' &&
      item.analysisState !== 'analyzing'
    ) {
      return 'green';
    }
    return this.archiveMigrationItemIsActive(item) ? 'blue' : 'default';
  }

  archiveMigrationItemProgress(item: ArchiveMigrationItem): number {
    return Math.max(0, Math.min(100, Math.round(item.progress * 100)));
  }

  archiveSourceUrl(item: ArchiveMigrationItem): string {
    return `https://www.bilibili.com/video/${encodeURIComponent(item.bvid)}`;
  }

  archiveTargetUrl(item: ArchiveMigrationItem): string | null {
    return item.targetBvid
      ? `https://www.bilibili.com/video/${encodeURIComponent(item.targetBvid)}`
      : null;
  }

  trackArchiveMigrationItem(_index: number, item: ArchiveMigrationItem): number {
    return item.id;
  }


  archiveMigrationProgress(migration: ArchiveMigrationStatus): number {
    return Math.max(0, Math.min(100, Math.round(migration.progress * 100)));
  }

  archiveMigrationProgressStatus(
    migration: ArchiveMigrationStatus
  ): 'normal' | 'active' | 'success' | 'exception' {
    if (migration.state === 'failed' || migration.failedCount > 0) {
      return 'exception';
    }
    if (migration.state === 'completed') {
      return 'success';
    }
    return migration.state === 'running' ? 'active' : 'normal';
  }

  accountDisplayName(accountId: number): string {
    const account = this.accounts.find((value) => value.id === accountId);
    return account
      ? `${account.displayName}（UID ${account.uid}）`
      : `账号 #${accountId}`;
  }

  openLoginDialog(): void {
    this.stopQrPolling$.next();
    this.loginView = { state: 'idle' };
    this.loginDialogVisible = true;
    this.actionError = null;
    this.actionMessage = null;
    this.changeDetector.markForCheck();
  }

  closeLoginDialog(): void {
    const display = this.visibleQr;
    const shouldCancel = display !== null && this.canCancelLogin;
    this.stopQrPolling$.next();
    this.loginDialogVisible = false;
    this.loginView = { state: 'idle' };
    this.changeDetector.markForCheck();
    if (!display || !shouldCancel) {
      return;
    }
    this.accountService
      .cancelQrSession(display.session.id)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        error: (error: unknown) => {
          this.actionError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  startLogin(): void {
    this.stopQrPolling$.next();
    this.actionError = null;
    this.actionMessage = null;
    this.loginView = { state: 'creating' };
    this.changeDetector.markForCheck();
    this.accountService
      .createQrSession()
      .pipe(
        switchMap((session) => {
          if (!session.qrUrl) {
            throw new Error('B站没有返回可用的登录二维码');
          }
          return from(this.qrCodeRenderer.toDataUrl(session.qrUrl)).pipe(
            map((qrDataUrl) => ({ session, qrDataUrl }))
          );
        }),
        takeUntil(this.stopQrPolling$),
        takeUntil(this.destroy$)
      )
      .subscribe({
        next: (display) => {
          this.loginView = { state: 'waiting', ...display };
          this.changeDetector.markForCheck();
          this.pollQrSession(display);
        },
        error: (error: unknown) => {
          this.loginView = {
            state: 'error',
            message: this.errorMessage(error),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  cancelLogin(): void {
    const display = this.visibleQr;
    if (!display || !this.canCancelLogin) {
      return;
    }
    this.stopQrPolling$.next();
    this.loginView = { state: 'cancelling', ...display };
    this.changeDetector.markForCheck();
    this.accountService
      .cancelQrSession(display.session.id)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: () => {
          this.loginView = { state: 'cancelled' };
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.loginView = {
            state: 'error',
            message: this.errorMessage(error),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  checkRenewal(account: BiliAccount): void {
    if (this.checkingAccountIds.has(account.id)) {
      return;
    }
    this.checkingAccountIds.add(account.id);
    this.actionError = null;
    this.actionMessage = null;
    this.changeDetector.markForCheck();
    this.accountService
      .checkRenewal(account.id)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (result) => {
          this.checkingAccountIds.delete(account.id);
          this.actionMessage = result.refreshed
            ? `凭据已续期，当前版本 ${result.credentialVersion}`
            : '凭据当前有效，暂不需要续期';
          this.loadAccounts();
        },
        error: (error: unknown) => {
          this.checkingAccountIds.delete(account.id);
          this.actionError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  isChecking(accountId: number): boolean {
    return this.checkingAccountIds.has(accountId);
  }

  openPrimaryDialog(account: BiliAccount): void {
    if (account.state !== 'active' || account.isPrimary) {
      return;
    }
    this.actionError = null;
    this.actionMessage = null;
    this.primaryDialog = { state: 'loading', account };
    this.changeDetector.markForCheck();
    this.accountService
      .getRelationships(account.id)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (relationships) => {
          this.primaryDialog = { state: 'ready', account, relationships };
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.primaryDialog = {
            state: 'error',
            account,
            message: this.errorMessage(error),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  closePrimaryDialog(): void {
    if (this.primaryDialog.state !== 'submitting') {
      this.primaryDialog = { state: 'closed' };
      this.changeDetector.markForCheck();
    }
  }

  confirmPrimaryAccount(): void {
    if (this.primaryDialog.state !== 'ready') {
      return;
    }
    const { account, relationships } = this.primaryDialog;
    this.primaryDialog = { state: 'submitting', account, relationships };
    this.accountService
      .setPrimaryAccount(account.id)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: () => {
          this.primaryDialog = { state: 'closed' };
          this.actionMessage = `${account.displayName} 已设为主账号`;
          this.loadAccounts();
        },
        error: (error: unknown) => {
          this.primaryDialog = { state: 'ready', account, relationships };
          this.actionError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  openRemovalDialog(account: BiliAccount): void {
    this.actionError = null;
    this.actionMessage = null;
    this.removalMode = 'follow_primary';
    this.replacementAccountId = null;
    this.newPrimaryAccountId = null;
    this.removalDialog = { state: 'loading', account };
    this.changeDetector.markForCheck();
    this.accountService
      .getRelationships(account.id)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (relationships) => {
          this.removalDialog = { state: 'ready', account, relationships };
          if (
            relationships.isPrimary &&
            this.activeReplacementAccounts.length === 0
          ) {
            this.removalMode = 'disable';
          }
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.removalDialog = {
            state: 'error',
            account,
            message: this.errorMessage(error),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  closeRemovalDialog(): void {
    if (this.removalDialog.state !== 'submitting') {
      this.removalDialog = { state: 'closed' };
      this.changeDetector.markForCheck();
    }
  }

  removalModeChanged(mode: RemovalMode): void {
    this.removalMode = mode;
    if (mode !== 'fixed') {
      this.replacementAccountId = null;
    }
  }

  confirmRemoval(): void {
    if (!this.canConfirmRemoval || this.removalDialog.state !== 'ready') {
      return;
    }
    const { account, relationships } = this.removalDialog;
    const request: AccountRemovalRequest = { mode: this.removalMode };
    if (this.removalMode === 'fixed') {
      request.replacementAccountId = this.replacementAccountId!;
    }
    if (relationships.isPrimary && this.newPrimaryAccountId !== null) {
      request.newPrimaryAccountId = this.newPrimaryAccountId;
    }
    this.removalDialog = { state: 'submitting', account, relationships };
    this.accountService
      .removeAccount(account.id, request)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: () => {
          this.removalDialog = { state: 'closed' };
          this.actionMessage = `${account.displayName} 已移除`;
          this.loadAccounts();
        },
        error: (error: unknown) => {
          this.removalDialog = { state: 'ready', account, relationships };
          this.actionError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  accountInitial(displayName: string): string {
    return displayName.trim().charAt(0) || '?';
  }

  hasAvatarError(avatarUrl: string): boolean {
    return this.failedAvatarUrls.has(avatarUrl);
  }

  markAvatarError(avatarUrl: string): void {
    this.failedAvatarUrls.add(avatarUrl);
  }

  accountStateLabel(state: AccountState): string {
    switch (state) {
      case 'active':
        return '可用';
      case 'paused':
        return '已暂停';
      case 'refresh_unknown':
        return '续期结果待确认';
      case 'archived':
        return '已归档';
      default: {
        const exhaustive: never = state;
        throw new Error(`未知账号状态：${exhaustive}`);
      }
    }
  }

  accountStateColor(state: AccountState): string {
    switch (state) {
      case 'active':
        return 'green';
      case 'paused':
        return 'orange';
      case 'refresh_unknown':
        return 'red';
      case 'archived':
        return 'default';
      default: {
        const exhaustive: never = state;
        throw new Error(`未知账号状态：${exhaustive}`);
      }
    }
  }

  loginStatusText(): string {
    switch (this.loginView.state) {
      case 'idle':
        return '尚未开始扫码';
      case 'creating':
        return '正在向 B 站申请二维码';
      case 'waiting':
        return '等待扫码';
      case 'scanned':
        return '已扫码，请在手机确认';
      case 'cancelling':
        return '正在取消';
      case 'confirmed':
        return '登录成功';
      case 'expired':
        return '二维码已过期';
      case 'cancelled':
        return '已取消';
      case 'failed':
        return '登录失败';
      case 'error':
        return this.loginView.message;
      default: {
        const exhaustive: never = this.loginView;
        throw new Error(`未知登录状态：${exhaustive}`);
      }
    }
  }

  secondsRemaining(session: QrSession): number {
    return Math.max(0, session.expiresAt - Math.floor(Date.now() / 1000));
  }

  trackAccount(_index: number, account: BiliAccount): number {
    return account.id;
  }

  private isActiveReplacement(accountId: number | null): boolean {
    return this.activeReplacementAccounts.some(
      (account) => account.id === accountId
    );
  }

  private loadAccounts(): void {
    this.accountsView = { state: 'loading' };
    this.changeDetector.markForCheck();
    this.accountService
      .listAccounts()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (accounts) => {
          this.accountsView = { state: 'ready', accounts };
          this.ensureArchiveMigrationAccountSelection();
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.accountsView = {
            state: 'error',
            message: this.errorMessage(error),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  private loadArchiveMigrations(): void {
    this.accountService
      .listArchiveMigrations()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (migrations) => {
          this.archiveMigrations = migrations;
          this.archiveMigrationsLoading = false;
          this.archiveMigrationError = null;
          this.loadArchiveMigrationItems(migrations);
          this.changeDetector.markForCheck();
        },
        error: (error: unknown) => {
          this.archiveMigrationsLoading = false;
          this.archiveMigrationError = this.errorMessage(error);
          this.changeDetector.markForCheck();
        },
      });
  }

  private ensureArchiveMigrationAccountSelection(): void {
    const active = this.activeAccounts;
    if (
      !active.some((account) => account.id === this.archiveDownloadAccountId)
    ) {
      this.archiveDownloadAccountId =
        active.find((account) => account.isPrimary)?.id ?? active[0]?.id ?? null;
    }
    if (
      !active.some((account) => account.id === this.archiveTargetAccountId)
    ) {
      this.archiveTargetAccountId =
        active.find((account) => account.isPrimary)?.id ?? active[0]?.id ?? null;
    }
  }

  private upsertArchiveMigration(migration: ArchiveMigrationStatus): void {
    this.archiveMigrations = [
      migration,
      ...this.archiveMigrations.filter((value) => value.id !== migration.id),
    ];
  }

  private loadArchiveMigrationItems(
    migrations: readonly ArchiveMigrationStatus[]
  ): void {
    for (const migration of migrations) {
      this.accountService
        .listArchiveMigrationItems(migration.id)
        .pipe(takeUntil(this.destroy$))
        .subscribe({
          next: (items) => {
            const next = new Map(this.archiveMigrationItemsById);
            next.set(migration.id, items);
            this.archiveMigrationItemsById = next;
            this.changeDetector.markForCheck();
          },
          error: (error: unknown) => {
            this.archiveMigrationError = this.errorMessage(error);
            this.changeDetector.markForCheck();
          },
        });
    }
  }

  private applyArchiveMigrationSnapshot(data: unknown): void {
    const snapshot = this.archiveMigrationSnapshot(data);
    if (snapshot === null) {
      this.loadArchiveMigrations();
      return;
    }
    const items = new Map<number, readonly ArchiveMigrationItem[]>();
    for (const migration of snapshot.migrations) {
      items.set(migration.id, snapshot.items[String(migration.id)] ?? []);
    }
    this.archiveMigrations = snapshot.migrations;
    this.archiveMigrationItemsById = items;
    this.archiveMigrationsLoading = false;
    this.archiveMigrationError = null;
    this.changeDetector.markForCheck();
  }

  private archiveMigrationSnapshot(
    data: unknown
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
    return {
      migrations: migrations as ArchiveMigrationStatus[],
      items: items as Record<string, ArchiveMigrationItem[]>,
    };
  }

  private archiveMigrationItemIsActive(item: ArchiveMigrationItem): boolean {
    return (
      item.state === 'downloading' ||
      item.state === 'creating_task' ||
      item.uploadState === 'ready' ||
      item.uploadState === 'uploading' ||
      item.uploadState === 'submitting' ||
      item.uploadState === 'waiting_review' ||
      item.analysisState === 'pending' ||
      item.analysisState === 'analyzing' ||
      item.danmakuBranchState === 'pending' ||
      item.danmakuBranchState === 'importing' ||
      item.danmakuBranchState === 'publishing' ||
      item.commentBranchState === 'pending' ||
      item.commentBranchState === 'running'
    );
  }

  private pollQrSession(display: QrDisplay): void {
    timer(1000, 1000)
      .pipe(
        switchMap(() =>
          this.accountService.getQrSession(display.session.id)
        ),
        takeUntil(this.stopQrPolling$),
        takeUntil(this.destroy$)
      )
      .subscribe({
        next: (session) => this.applyQrStatus(session, display.qrDataUrl),
        error: (error: unknown) => {
          this.loginView = {
            state: 'error',
            message: this.errorMessage(error),
          };
          this.changeDetector.markForCheck();
        },
      });
  }

  private applyQrStatus(session: QrSession, qrDataUrl: string): void {
    switch (session.state) {
      case 'created':
      case 'pending':
        this.loginView = { state: 'waiting', session, qrDataUrl };
        break;
      case 'scanned':
        this.loginView = { state: 'scanned', session, qrDataUrl };
        break;
      case 'confirmed':
        this.stopQrPolling$.next();
        this.loginView = { state: 'confirmed', accountId: session.accountId };
        this.loginDialogVisible = false;
        this.actionMessage = '账号添加成功';
        this.loadAccounts();
        break;
      case 'expired':
      case 'cancelled':
      case 'failed':
        this.stopQrPolling$.next();
        this.loginView = { state: session.state };
        break;
      default: {
        const exhaustive: never = session.state;
        throw new Error(`未知二维码状态：${exhaustive}`);
      }
    }
    this.changeDetector.markForCheck();
  }

  private errorMessage(error: unknown): string {
    if (error instanceof HttpErrorResponse) {
      const body: unknown = error.error;
      if (
        typeof body === 'object' &&
        body !== null &&
        'detail' in body &&
        typeof (body as { detail?: unknown }).detail === 'string'
      ) {
        return (body as { detail: string }).detail;
      }
      return error.message;
    }
    return error instanceof Error ? error.message : '请求失败';
  }
}
