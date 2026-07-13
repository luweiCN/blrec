import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { Subject, of, throwError } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCardModule } from 'ng-zorro-antd/card';
import { NzEmptyModule } from 'ng-zorro-antd/empty';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzSpinModule } from 'ng-zorro-antd/spin';
import { NzTagModule } from 'ng-zorro-antd/tag';

import { BiliAccountService } from './shared/bili-account.service';
import { QrCodeRenderer } from './shared/qr-code-renderer.service';
import { QrSession } from './shared/bili-account.model';
import { UploadsComponent } from './uploads.component';

describe('UploadsComponent', () => {
  let fixture: ComponentFixture<UploadsComponent>;
  let component: UploadsComponent;
  let accountService: jasmine.SpyObj<BiliAccountService>;
  let qrRenderer: jasmine.SpyObj<QrCodeRenderer>;

  const pending: QrSession = {
    id: 'session-1',
    state: 'pending',
    qrUrl: 'https://passport.example.invalid/secret-auth-code',
    expiresAt: 4_102_444_800,
    accountId: null,
  };

  beforeEach(async () => {
    accountService = jasmine.createSpyObj<BiliAccountService>(
      'BiliAccountService',
      [
        'listAccounts',
        'createQrSession',
        'getQrSession',
        'cancelQrSession',
        'refreshAccount',
      ]
    );
    qrRenderer = jasmine.createSpyObj<QrCodeRenderer>('QrCodeRenderer', [
      'toDataUrl',
    ]);
    accountService.listAccounts.and.returnValue(of([]));
    accountService.createQrSession.and.returnValue(of(pending));
    accountService.getQrSession.and.returnValue(of(pending));
    accountService.cancelQrSession.and.returnValue(
      of({ ...pending, state: 'cancelled', qrUrl: null })
    );
    accountService.refreshAccount.and.returnValue(
      of({ credentialVersion: 2 })
    );
    qrRenderer.toDataUrl.and.resolveTo('data:image/png;base64,fixture');

    await TestBed.configureTestingModule({
      declarations: [UploadsComponent],
      imports: [
        CommonModule,
        NoopAnimationsModule,
        NzAlertModule,
        NzButtonModule,
        NzCardModule,
        NzEmptyModule,
        NzPageHeaderModule,
        NzSpinModule,
        NzTagModule,
      ],
      providers: [
        { provide: BiliAccountService, useValue: accountService },
        { provide: QrCodeRenderer, useValue: qrRenderer },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(UploadsComponent);
    component = fixture.componentInstance;
  });

  it('loads redacted account metadata and explains credential scope', () => {
    accountService.listAccounts.and.returnValue(
      of([
        {
          id: 7,
          uid: 42,
          displayName: 'fixture',
          credentialVersion: 3,
          state: 'active',
        },
      ])
    );

    fixture.detectChanges();

    const text = fixture.nativeElement.textContent;
    expect(text).toContain('fixture');
    expect(text).toContain('UID 42');
    expect(text).toContain('不会替代直播间匿名录制');
    expect(text).not.toContain('access_token');
    expect(text).not.toContain('Cookie=');
  });

  it('renders the QR locally and announces scan confirmation', fakeAsync(() => {
    accountService.getQrSession.and.returnValues(
      of({ ...pending, state: 'scanned' }),
      of({
        ...pending,
        state: 'confirmed',
        qrUrl: null,
        accountId: 7,
      })
    );
    fixture.detectChanges();

    component.startLogin();
    tick();
    fixture.detectChanges();

    expect(qrRenderer.toDataUrl).toHaveBeenCalledOnceWith(pending.qrUrl!);
    const image: HTMLImageElement = fixture.nativeElement.querySelector(
      '[data-testid="login-qr"]'
    );
    expect(image.src).toContain('data:image/png;base64,fixture');
    expect(image.alt).toContain('B站扫码登录二维码');

    tick(1000);
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('已扫码，请在手机确认');

    tick(1000);
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('登录成功');
    expect(accountService.listAccounts).toHaveBeenCalledTimes(2);
  }));

  it('cancels the server poller from a semantic button', fakeAsync(() => {
    fixture.detectChanges();
    component.startLogin();
    tick();
    fixture.detectChanges();

    const cancelButton = fixture.debugElement.query(
      By.css('[data-testid="cancel-login"]')
    ).nativeElement as HTMLButtonElement;
    cancelButton.click();
    tick();
    fixture.detectChanges();

    expect(accountService.cancelQrSession).toHaveBeenCalledOnceWith(
      'session-1'
    );
    expect(fixture.nativeElement.textContent).toContain('已取消');
  }));

  it('shows a fail-closed configuration error and offers retry', () => {
    accountService.listAccounts.and.returnValue(
      throwError(() => new Error('credential key is required'))
    );

    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain(
      'credential key is required'
    );
    expect(
      fixture.nativeElement.querySelector('[data-testid="retry-accounts"]')
    ).not.toBeNull();
  });

  it('stops local status polling on destroy', fakeAsync(() => {
    const status = new Subject<QrSession>();
    accountService.getQrSession.and.returnValue(status);
    fixture.detectChanges();
    component.startLogin();
    tick();
    tick(1000);

    fixture.destroy();
    status.next({ ...pending, state: 'scanned' });

    expect(component.loginView.state).not.toBe('scanned');
  }));
});
