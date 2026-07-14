import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { FormsModule } from '@angular/forms';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { Subject, of, throwError } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzAvatarModule } from 'ng-zorro-antd/avatar';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCardModule } from 'ng-zorro-antd/card';
import { NzCollapseModule } from 'ng-zorro-antd/collapse';
import { NzEmptyModule } from 'ng-zorro-antd/empty';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzRadioModule } from 'ng-zorro-antd/radio';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzSpinModule } from 'ng-zorro-antd/spin';
import { NzTagModule } from 'ng-zorro-antd/tag';
import { NzToolTipModule } from 'ng-zorro-antd/tooltip';

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
        'checkRenewal',
        'setPrimaryAccount',
        'getRelationships',
        'removeAccount',
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
    accountService.checkRenewal.and.returnValue(
      of({ credentialVersion: 2, refreshed: false })
    );
    accountService.setPrimaryAccount.and.returnValue(
      of({
        id: 7,
        uid: 42,
        displayName: 'fixture',
        avatarUrl: '',
        credentialVersion: 1,
        credentialExpiresAt: 1_800_000_000,
        createdAt: 1_700_000_000,
        state: 'active',
        isPrimary: true,
      })
    );
    accountService.getRelationships.and.returnValue(
      of({
        accountId: 7,
        isPrimary: false,
        followPrimaryRoomIds: [100],
        fixedRoomIds: [],
        reassignableJobs: [],
        blockingJobs: [],
        historicalJobCount: 0,
      })
    );
    accountService.removeAccount.and.returnValue(
      of({ accountId: 7, state: 'archived' })
    );
    qrRenderer.toDataUrl.and.resolveTo('data:image/png;base64,fixture');

    await TestBed.configureTestingModule({
      declarations: [UploadsComponent],
      imports: [
        CommonModule,
        FormsModule,
        NoopAnimationsModule,
        NzAlertModule,
        NzAvatarModule,
        NzButtonModule,
        NzCardModule,
        NzCollapseModule,
        NzEmptyModule,
        NzModalModule,
        NzPageHeaderModule,
        NzRadioModule,
        NzSelectModule,
        NzSpinModule,
        NzTagModule,
        NzToolTipModule,
      ],
      providers: [
        { provide: BiliAccountService, useValue: accountService },
        { provide: QrCodeRenderer, useValue: qrRenderer },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(UploadsComponent);
    component = fixture.componentInstance;
  });

  it('does not render upload tasks inside account management', () => {
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector('app-recording-sessions')
    ).toBeNull();
  });

  it('loads redacted account metadata and explains credential scope', () => {
    accountService.listAccounts.and.returnValue(
      of([
        {
          id: 7,
          uid: 42,
          displayName: 'fixture',
          avatarUrl: 'https://i0.hdslb.com/face.jpg',
          credentialVersion: 3,
          credentialExpiresAt: 1_800_000_000,
          createdAt: 1_700_000_000,
          state: 'active',
          isPrimary: true,
        },
      ])
    );

    fixture.detectChanges();

    const text = fixture.nativeElement.textContent;
    expect(text).toContain('投稿账号管理');
    expect(text).toContain('fixture');
    expect(text).toContain('UID 42');
    expect(text).toContain('添加时间');
    expect(text).toContain('TV Token 预计过期时间');
    expect(text).toContain('凭据版本 3');
    expect(text).toContain('检查并按需续期');
    expect(
      fixture.nativeElement.querySelector('[data-testid="add-account"]')
    ).not.toBeNull();
    expect(component.credentialVersionTip).toContain(
      '每次成功更换登录凭据后递增'
    );
    expect(component.credentialExpiryTip).toContain(
      '不代表账号本身或 Web Cookie'
    );
    expect(
      fixture.nativeElement.querySelector(
        '[data-testid="credential-expiry-help"]'
      )
    ).not.toBeNull();
    const avatar = fixture.nativeElement.querySelector(
      '[data-testid="account-avatar"]'
    );
    expect(avatar).not.toBeNull();
    const avatarImage = avatar.querySelector('img') as HTMLImageElement;
    expect(avatarImage.src).toContain('i0.hdslb.com/face.jpg');
    expect(avatarImage.referrerPolicy).toBe('no-referrer');
    const heading = fixture.nativeElement.querySelector('.account-heading');
    expect(heading.querySelector('strong').textContent).toContain('fixture');
    expect(heading.querySelector('nz-tag').textContent).toContain('可用');
    expect(text).toContain('主账号');
    expect(
      fixture.nativeElement.querySelector('.account-actions nz-tag')
    ).toBeNull();
    expect(text).toContain('批量开播状态仍匿名查询');
    expect(text).not.toContain('access_token');
    expect(text).not.toContain('Cookie=');
  });

  it('falls back to the account initial when the avatar fails', () => {
    accountService.listAccounts.and.returnValue(
      of([
        {
          id: 7,
          uid: 42,
          displayName: 'fixture',
          avatarUrl: 'https://i0.hdslb.com/missing.jpg',
          credentialVersion: 1,
          credentialExpiresAt: 0,
          createdAt: 1_700_000_000,
          state: 'active',
          isPrimary: false,
        },
      ])
    );
    fixture.detectChanges();

    const avatar = fixture.nativeElement.querySelector(
      '[data-testid="account-avatar"]'
    );
    avatar.querySelector('img').dispatchEvent(new Event('error'));
    fixture.detectChanges();

    expect(avatar.textContent).toContain('f');
    expect(fixture.nativeElement.textContent).toContain('暂未获取');
  });

  it('checks renewal without replacing a still-valid credential', fakeAsync(() => {
    const account = {
      id: 7,
      uid: 42,
      displayName: 'fixture',
      avatarUrl: '',
      credentialVersion: 1,
      credentialExpiresAt: 1_800_000_000,
      createdAt: 1_700_000_000,
      state: 'active' as const,
      isPrimary: true,
    };
    accountService.listAccounts.and.returnValue(of([account]));
    fixture.detectChanges();

    const button = fixture.nativeElement.querySelector(
      '[data-testid="check-renewal"]'
    ) as HTMLButtonElement;
    button.click();
    tick();
    fixture.detectChanges();

    expect(accountService.checkRenewal).toHaveBeenCalledOnceWith(7);
    expect(fixture.nativeElement.textContent).toContain(
      '凭据当前有效，暂不需要续期'
    );
  }));

  it('previews relationships before selecting the primary account', fakeAsync(() => {
    const first = {
      id: 7,
      uid: 42,
      displayName: 'first',
      avatarUrl: '',
      credentialVersion: 1,
      credentialExpiresAt: 1_800_000_000,
      createdAt: 1_700_000_000,
      state: 'active' as const,
      isPrimary: true,
    };
    const second = {
      ...first,
      id: 8,
      uid: 43,
      displayName: 'second',
      isPrimary: false,
    };
    accountService.listAccounts.and.returnValues(
      of([first, second]),
      of([
        { ...first, isPrimary: false },
        { ...second, isPrimary: true },
      ])
    );
    accountService.setPrimaryAccount.and.returnValue(
      of({ ...second, isPrimary: true })
    );
    fixture.detectChanges();

    const buttons = Array.from(
      fixture.nativeElement.querySelectorAll('[data-testid="set-primary"]')
    ) as HTMLButtonElement[];
    expect(buttons.length).toBe(1);
    buttons[0].click();
    tick();
    fixture.detectChanges();

    expect(accountService.getRelationships).toHaveBeenCalledOnceWith(8);
    expect(accountService.setPrimaryAccount).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain(
      '已创建的上传任务不会改绑'
    );
    expect(document.body.textContent).toContain(
      '不会断开正在工作的弹幕连接'
    );

    component.confirmPrimaryAccount();
    tick();
    fixture.detectChanges();

    expect(accountService.setPrimaryAccount).toHaveBeenCalledOnceWith(8);
    expect(fixture.nativeElement.textContent).toContain(
      'second 已设为主账号'
    );
    expect(component.primaryAccountTip).toContain('房间信息和画质查询');
  }));

  it('shows relationship handling choices before removing an account', fakeAsync(() => {
    const primary = {
      id: 7,
      uid: 42,
      displayName: 'primary',
      avatarUrl: '',
      credentialVersion: 1,
      credentialExpiresAt: 1_800_000_000,
      createdAt: 1_700_000_000,
      state: 'active' as const,
      isPrimary: true,
    };
    const standby = {
      ...primary,
      id: 8,
      uid: 43,
      displayName: 'standby',
      isPrimary: false,
    };
    accountService.listAccounts.and.returnValue(of([primary, standby]));
    accountService.getRelationships.and.returnValue(
      of({
        accountId: 7,
        isPrimary: true,
        followPrimaryRoomIds: [100, 200],
        fixedRoomIds: [300],
        reassignableJobs: [{ id: 1, roomId: 300, state: 'ready' }],
        blockingJobs: [],
        historicalJobCount: 4,
      })
    );
    fixture.detectChanges();

    const removeButtons = Array.from(
      fixture.nativeElement.querySelectorAll('[data-testid="remove-account"]')
    ) as HTMLButtonElement[];
    removeButtons[0].click();
    tick();
    fixture.detectChanges();

    const text = document.body.textContent;
    expect(accountService.getRelationships).toHaveBeenCalledOnceWith(7);
    expect(text).toContain('改为跟随新主账号');
    expect(text).toContain('固定切换到指定账号');
    expect(text).toContain('不迁移，关闭房间并暂停任务');
    expect(text).toContain('固定绑定房间（1）');
    expect(text).toContain('可迁移上传任务（1）');
    expect(accountService.removeAccount).not.toHaveBeenCalled();
  }));

  it('blocks account removal after an upload has remote side effects', fakeAsync(() => {
    const account = {
      id: 7,
      uid: 42,
      displayName: 'fixture',
      avatarUrl: '',
      credentialVersion: 1,
      credentialExpiresAt: 1_800_000_000,
      createdAt: 1_700_000_000,
      state: 'active' as const,
      isPrimary: false,
    };
    accountService.listAccounts.and.returnValue(of([account]));
    accountService.getRelationships.and.returnValue(
      of({
        accountId: 7,
        isPrimary: false,
        followPrimaryRoomIds: [],
        fixedRoomIds: [],
        reassignableJobs: [],
        blockingJobs: [{ id: 9, roomId: 100, state: 'uploading' }],
        historicalJobCount: 0,
      })
    );
    fixture.detectChanges();

    const removeButton = fixture.nativeElement.querySelector(
      '[data-testid="remove-account"]'
    ) as HTMLButtonElement;
    removeButton.click();
    tick();
    fixture.detectChanges();

    expect(document.body.textContent).toContain('必须先处理以下任务');
    expect(component.canConfirmRemoval).toBeFalse();
    component.confirmRemoval();
    expect(accountService.removeAccount).not.toHaveBeenCalled();
  }));

  it('submits explicit replacement and new primary accounts', fakeAsync(() => {
    const primary = {
      id: 7,
      uid: 42,
      displayName: 'primary',
      avatarUrl: '',
      credentialVersion: 1,
      credentialExpiresAt: 1_800_000_000,
      createdAt: 1_700_000_000,
      state: 'active' as const,
      isPrimary: true,
    };
    const replacement = {
      ...primary,
      id: 8,
      uid: 43,
      displayName: 'replacement',
      isPrimary: false,
    };
    const nextPrimary = {
      ...replacement,
      id: 9,
      uid: 44,
      displayName: 'next-primary',
    };
    accountService.listAccounts.and.returnValue(
      of([primary, replacement, nextPrimary])
    );
    accountService.getRelationships.and.returnValue(
      of({
        accountId: 7,
        isPrimary: true,
        followPrimaryRoomIds: [],
        fixedRoomIds: [],
        reassignableJobs: [],
        blockingJobs: [],
        historicalJobCount: 0,
      })
    );
    fixture.detectChanges();

    component.openRemovalDialog(primary);
    tick();
    component.removalMode = 'fixed';
    component.replacementAccountId = 8;
    component.newPrimaryAccountId = 9;
    component.confirmRemoval();
    tick();

    expect(accountService.removeAccount).toHaveBeenCalledOnceWith(7, {
      mode: 'fixed',
      replacementAccountId: 8,
      newPrimaryAccountId: 9,
    });
    expect(component.actionMessage).toBe('primary 已移除');
  }));

  it('opens account login without creating a QR code automatically', () => {
    fixture.detectChanges();

    const addButton = fixture.nativeElement.querySelector(
      '[data-testid="add-account"]'
    ) as HTMLButtonElement;
    addButton.click();
    fixture.detectChanges();

    expect(component.loginDialogVisible).toBeTrue();
    expect(accountService.createQrSession).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain('生成登录二维码');
  });

  it('cancels a pending login when the dialog closes', fakeAsync(() => {
    fixture.detectChanges();
    component.openLoginDialog();
    component.startLogin();
    tick();

    component.closeLoginDialog();
    tick();

    expect(accountService.cancelQrSession).toHaveBeenCalledOnceWith(
      'session-1'
    );
    expect(component.loginDialogVisible).toBeFalse();
  }));

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

    component.openLoginDialog();
    component.startLogin();
    tick();
    fixture.detectChanges();

    expect(qrRenderer.toDataUrl).toHaveBeenCalledOnceWith(pending.qrUrl!);
    const image = document.body.querySelector(
      '[data-testid="login-qr"]'
    ) as HTMLImageElement;
    expect(image.src).toContain('data:image/png;base64,fixture');
    expect(image.alt).toContain('B站扫码登录二维码');

    tick(1000);
    fixture.detectChanges();
    expect(document.body.textContent).toContain('已扫码，请在手机确认');

    tick(1000);
    fixture.detectChanges();
    expect(component.loginDialogVisible).toBeFalse();
    expect(component.actionMessage).toBe('账号添加成功');
    expect(accountService.listAccounts).toHaveBeenCalledTimes(2);
  }));

  it('cancels the server poller from a semantic button', fakeAsync(() => {
    fixture.detectChanges();
    component.openLoginDialog();
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
    expect(document.body.textContent).toContain('已取消');
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
