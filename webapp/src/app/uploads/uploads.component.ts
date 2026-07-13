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

import {
  AccountState,
  AccountsView,
  BiliAccount,
  LoginView,
  QrDisplay,
  QrSession,
} from './shared/bili-account.model';
import { BiliAccountService } from './shared/bili-account.service';
import { QrCodeRenderer } from './shared/qr-code-renderer.service';

@Component({
  selector: 'app-uploads',
  templateUrl: './uploads.component.html',
  styleUrls: ['./uploads.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class UploadsComponent implements OnInit, OnDestroy {
  accountsView: AccountsView = { state: 'loading' };
  loginView: LoginView = { state: 'idle' };
  actionError: string | null = null;
  actionMessage: string | null = null;
  readonly credentialVersionTip =
    '每次成功更换登录凭据后递增，用于防止旧任务覆盖新凭据；它不是账号等级或软件版本。';

  private readonly destroy$ = new Subject<void>();
  private readonly stopQrPolling$ = new Subject<void>();
  private readonly checkingAccountIds = new Set<number>();

  constructor(
    private accountService: BiliAccountService,
    private changeDetector: ChangeDetectorRef,
    private qrCodeRenderer: QrCodeRenderer
  ) {}

  ngOnInit(): void {
    this.loadAccounts();
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

  retryAccounts(): void {
    this.loadAccounts();
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

  accountInitial(displayName: string): string {
    return displayName.trim().charAt(0) || '?';
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

  private loadAccounts(): void {
    this.accountsView = { state: 'loading' };
    this.changeDetector.markForCheck();
    this.accountService
      .listAccounts()
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (accounts) => {
          this.accountsView = { state: 'ready', accounts };
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
