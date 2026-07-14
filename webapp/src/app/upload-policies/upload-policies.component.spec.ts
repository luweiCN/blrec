import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { FormsModule } from '@angular/forms';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCardModule } from 'ng-zorro-antd/card';
import { NzCheckboxModule } from 'ng-zorro-antd/checkbox';
import { NzEmptyModule } from 'ng-zorro-antd/empty';
import { NzFormModule } from 'ng-zorro-antd/form';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzInputNumberModule } from 'ng-zorro-antd/input-number';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzPopconfirmModule } from 'ng-zorro-antd/popconfirm';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzSpinModule } from 'ng-zorro-antd/spin';
import { NzSwitchModule } from 'ng-zorro-antd/switch';
import { NzTagModule } from 'ng-zorro-antd/tag';
import { NzToolTipModule } from 'ng-zorro-antd/tooltip';

import { TaskData } from '../tasks/shared/task.model';
import { TaskService } from '../tasks/shared/services/task.service';
import { BiliAccountService } from '../uploads/shared/bili-account.service';
import { RoomUploadPolicy } from './shared/room-upload-policy.model';
import { RoomUploadPolicyService } from './shared/room-upload-policy.service';
import { UploadPoliciesComponent } from './upload-policies.component';

describe('UploadPoliciesComponent', () => {
  let fixture: ComponentFixture<UploadPoliciesComponent>;
  let component: UploadPoliciesComponent;
  let policyService: jasmine.SpyObj<RoomUploadPolicyService>;
  let accountService: jasmine.SpyObj<BiliAccountService>;
  let taskService: jasmine.SpyObj<TaskService>;

  const policy: RoomUploadPolicy = {
    roomId: 100,
    accountMode: 'primary',
    accountId: null,
    resolvedAccountId: 7,
    resolvedAccountName: '投稿账号',
    enabled: true,
    titleTemplate: '{{ title }} 录播',
    descriptionTemplate: '主播：{{ anchor_name }}',
    tid: 17,
    tags: '直播,录播',
    copyright: 1,
    source: '',
    autoComment: false,
    danmakuBackfill: true,
    filters: {},
    blockedReason: null,
    createdAt: 1000,
    updatedAt: 1000,
  };

  const account = {
    id: 7,
    uid: 42,
    displayName: '投稿账号',
    avatarUrl: '',
    credentialVersion: 1,
    credentialExpiresAt: 2_000_000,
    createdAt: 1000,
    state: 'active' as const,
    isPrimary: true,
  };

  const task = {
    user_info: { name: '测试主播' },
    room_info: { room_id: 100, title: '测试直播' },
    task_status: {},
  } as TaskData;

  beforeEach(async () => {
    policyService = jasmine.createSpyObj<RoomUploadPolicyService>(
      'RoomUploadPolicyService',
      ['list', 'save', 'delete'],
    );
    accountService = jasmine.createSpyObj<BiliAccountService>(
      'BiliAccountService',
      ['listAccounts'],
    );
    taskService = jasmine.createSpyObj<TaskService>('TaskService', [
      'getAllTaskData',
    ]);
    policyService.list.and.returnValue(of([policy]));
    policyService.save.and.returnValue(of(policy));
    policyService.delete.and.returnValue(of(undefined));
    accountService.listAccounts.and.returnValue(of([account]));
    taskService.getAllTaskData.and.returnValue(of([task]));

    await TestBed.configureTestingModule({
      declarations: [UploadPoliciesComponent],
      imports: [
        CommonModule,
        FormsModule,
        NoopAnimationsModule,
        NzAlertModule,
        NzButtonModule,
        NzCardModule,
        NzCheckboxModule,
        NzEmptyModule,
        NzFormModule,
        NzInputModule,
        NzInputNumberModule,
        NzModalModule,
        NzPageHeaderModule,
        NzPopconfirmModule,
        NzSelectModule,
        NzSpinModule,
        NzSwitchModule,
        NzTagModule,
        NzToolTipModule,
      ],
      providers: [
        { provide: RoomUploadPolicyService, useValue: policyService },
        { provide: BiliAccountService, useValue: accountService },
        { provide: TaskService, useValue: taskService },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(UploadPoliciesComponent);
    component = fixture.componentInstance;
  });

  it('shows account binding and explains that existing jobs stay unchanged', () => {
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent;
    expect(text).toContain('投稿规则');
    expect(text).toContain('测试主播 · 100');
    expect(text).toContain('跟随主账号 · 投稿账号');
    expect(text).toContain('{{ title }} 录播');
    expect(text).toContain('弹幕回灌');
    expect(text).toContain('不会改绑已经创建的任务');
  });

  it('submits a fixed account only for the selected room', () => {
    policyService.list.and.returnValue(of([]));
    fixture.detectChanges();
    component.openCreate();
    component.accountModeChanged('fixed');

    component.save();

    expect(policyService.save).toHaveBeenCalledOnceWith(
      100,
      jasmine.objectContaining({
        accountMode: 'fixed',
        accountId: 7,
        titleTemplate: '{{ title }} 录播',
      }),
    );
  });

  it('marks the OnPush view after all three resources load', () => {
    const markForCheck = spyOn(component['changeDetector'], 'markForCheck');

    fixture.detectChanges();

    expect(markForCheck).toHaveBeenCalled();
  });
});
