import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnInit,
} from '@angular/core';
import { Router } from '@angular/router';

import { finalize } from 'rxjs/operators';

import { AuthService } from '../core/services/auth.service';

type AuthMode = 'loading' | 'setup' | 'login' | 'recover';

@Component({
  selector: 'app-auth',
  templateUrl: './auth.component.html',
  styleUrls: ['./auth.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AuthComponent implements OnInit {
  mode: AuthMode = 'loading';
  username = '';
  apiKey = '';
  password = '';
  submitting = false;
  errorMessage = '';
  successMessage = '';

  constructor(
    private auth: AuthService,
    private router: Router,
    private changeDetector: ChangeDetectorRef
  ) {}

  ngOnInit(): void {
    this.auth.getStatus().subscribe({
      next: (status) => {
        if (status.authenticated) {
          this.auth.ensureSession().subscribe((authenticated) => {
            if (authenticated) {
              void this.router.navigateByUrl('/tasks');
            } else {
              this.mode = status.setupRequired ? 'setup' : 'login';
              this.changeDetector.markForCheck();
            }
          });
          return;
        }
        this.mode = status.setupRequired ? 'setup' : 'login';
        this.changeDetector.markForCheck();
      },
      error: () => {
        this.mode = 'login';
        this.errorMessage = '无法连接到服务，请确认后端已经启动。';
        this.changeDetector.markForCheck();
      },
    });
  }

  submit(): void {
    if (this.submitting || !this.username || this.password.length < 10) {
      return;
    }
    this.submitting = true;
    this.errorMessage = '';
    const request =
      this.mode === 'setup'
        ? this.auth.setup(this.username, this.apiKey, this.password)
        : this.auth.login(this.username, this.password);
    request
      .pipe(
        finalize(() => {
          this.submitting = false;
          this.changeDetector.markForCheck();
        })
      )
      .subscribe({
        next: () => void this.router.navigateByUrl('/tasks'),
        error: (error) => {
          this.errorMessage = this.errorDetail(error);
        },
      });
  }

  submitRecovery(): void {
    if (this.submitting || !this.username || this.password.length < 10) {
      return;
    }
    this.submitting = true;
    this.errorMessage = '';
    this.auth
      .recover(this.username, this.apiKey, this.password)
      .pipe(
        finalize(() => {
          this.submitting = false;
          this.changeDetector.markForCheck();
        })
      )
      .subscribe({
        next: () => {
          this.mode = 'login';
          this.username = '';
          this.apiKey = '';
          this.password = '';
          this.successMessage = '管理员密码已重置，请使用新密码登录。';
        },
        error: (error) => {
          this.errorMessage = this.errorDetail(error);
        },
      });
  }

  showRecovery(): void {
    this.mode = 'recover';
    this.username = '';
    this.apiKey = '';
    this.password = '';
    this.errorMessage = '';
    this.successMessage = '';
  }

  showLogin(): void {
    this.mode = 'login';
    this.username = '';
    this.apiKey = '';
    this.password = '';
    this.errorMessage = '';
  }

  private errorDetail(error: unknown): string {
    const value = error as {
      status?: number;
      headers?: { get(name: string): string | null };
      error?: { detail?: unknown };
      message?: unknown;
    };
    if (value?.status === 429) {
      const retryAfter = Number(value.headers?.get('Retry-After'));
      if (Number.isFinite(retryAfter) && retryAfter > 0) {
        return `尝试次数过多，请在 ${Math.ceil(retryAfter / 60)} 分钟后重试。`;
      }
      return '尝试次数过多，请稍后重试。';
    }
    if (typeof value?.error?.detail === 'string') {
      return value.error.detail;
    }
    if (typeof value?.message === 'string') {
      return value.message;
    }
    return '操作失败，请稍后重试。';
  }
}
