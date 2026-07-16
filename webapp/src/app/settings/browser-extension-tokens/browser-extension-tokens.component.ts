import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnInit,
} from '@angular/core';

import { NzModalService } from 'ng-zorro-antd/modal';

import {
  BrowserExtensionToken,
  BrowserExtensionTokenService,
} from './browser-extension-token.service';

@Component({
  selector: 'app-browser-extension-tokens',
  templateUrl: './browser-extension-tokens.component.html',
  styleUrls: ['./browser-extension-tokens.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BrowserExtensionTokensComponent implements OnInit {
  tokens: readonly BrowserExtensionToken[] = [];
  loading = true;
  error: string | null = null;

  constructor(
    private service: BrowserExtensionTokenService,
    private modal: NzModalService,
    private changeDetector: ChangeDetectorRef
  ) {}

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.loading = true;
    this.error = null;
    this.service.list().subscribe({
      next: (tokens) => {
        this.tokens = tokens;
        this.loading = false;
        this.changeDetector.markForCheck();
      },
      error: (error: unknown) => {
        this.loading = false;
        this.error = this.describeError(error, '无法加载插件授权');
        this.changeDetector.markForCheck();
      },
    });
  }

  confirmRevoke(token: BrowserExtensionToken): void {
    if (token.revokedAt !== null) {
      return;
    }
    this.modal.confirm({
      nzTitle: `撤销浏览器插件授权 #${token.id}？`,
      nzContent: '撤销后，使用该授权的浏览器需要重新连接。',
      nzOkText: '撤销',
      nzOkDanger: true,
      nzOnOk: () =>
        new Promise<void>((resolve, reject) => {
          this.service.revoke(token.id).subscribe({
            next: () => {
              const revokedAt = Math.floor(Date.now() / 1000);
              this.tokens = this.tokens.map((item) =>
                item.id === token.id ? { ...item, revokedAt } : item
              );
              this.changeDetector.markForCheck();
              resolve();
            },
            error: (error: unknown) => {
              this.error = this.describeError(error, '撤销插件授权失败');
              this.changeDetector.markForCheck();
              reject(error);
            },
          });
        }),
    });
  }

  trackToken(_index: number, token: BrowserExtensionToken): number {
    return token.id;
  }

  private describeError(error: unknown, fallback: string): string {
    const value = error as { error?: { detail?: unknown }; message?: unknown };
    if (typeof value?.error?.detail === 'string') {
      return value.error.detail;
    }
    return typeof value?.message === 'string' ? value.message : fallback;
  }
}
