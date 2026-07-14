import { HttpErrorResponse } from '@angular/common/http';
import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { FormsModule } from '@angular/forms';

import { NzMessageService } from 'ng-zorro-antd/message';
import { of, throwError } from 'rxjs';

import { BiliAccountService } from '../../uploads/shared/bili-account.service';
import {
  RoomUploadPolicy,
  UploadCategoryCatalog,
} from './room-upload-policy.model';
import { RoomUploadPolicyService } from './room-upload-policy.service';
import { UploadPolicyDialogComponent } from './upload-policy-dialog.component';

describe('UploadPolicyDialogComponent', () => {
  let fixture: ComponentFixture<UploadPolicyDialogComponent>;
  let component: UploadPolicyDialogComponent;
  let policyService: jasmine.SpyObj<RoomUploadPolicyService>;

  const account = {
    id: 7,
    uid: 42,
    displayName: '投稿账号',
    avatarUrl: '',
    credentialVersion: 3,
    credentialExpiresAt: 2_000_000,
    createdAt: 1000,
    state: 'active' as const,
    isPrimary: true,
  };

  const categories: UploadCategoryCatalog = {
    accountId: 7,
    credentialVersion: 3,
    fetchedAt: 1000,
    stale: false,
    categories: [
      {
        id: 4,
        name: '游戏',
        description: '',
        children: [
          {
            id: 17,
            name: '单机游戏',
            description: '单机内容',
            children: [],
          },
        ],
      },
    ],
  };

  const existingPolicy: RoomUploadPolicy = {
    roomId: 100,
    accountMode: 'fixed',
    accountId: 7,
    resolvedAccountId: 7,
    resolvedAccountName: '投稿账号',
    enabled: false,
    titleTemplate: '旧标题',
    descriptionTemplate: '旧简介',
    partTitleTemplate: '第 {{ part_index }} P',
    dynamicTemplate: '旧动态',
    tid: 17,
    tags: '旧标签',
    copyright: 1,
    source: '',
    isOnlySelf: true,
    publishDynamic: false,
    noReprint: false,
    upSelectionReply: true,
    upCloseReply: false,
    upCloseDanmu: false,
    autoComment: false,
    danmakuBackfill: false,
    filters: {},
    blockedReason: null,
    createdAt: 1000,
    updatedAt: 1000,
  };

  beforeEach(async () => {
    policyService = jasmine.createSpyObj<RoomUploadPolicyService>(
      'RoomUploadPolicyService',
      ['get', 'save', 'delete', 'categories'],
    );
    policyService.get.and.returnValue(
      throwError(() => new HttpErrorResponse({ status: 404 })),
    );
    policyService.categories.and.returnValue(of(categories));
    policyService.save.and.returnValue(of(existingPolicy));
    policyService.delete.and.returnValue(of(undefined));

    await TestBed.configureTestingModule({
      declarations: [UploadPolicyDialogComponent],
      imports: [FormsModule],
      providers: [
        { provide: RoomUploadPolicyService, useValue: policyService },
        {
          provide: BiliAccountService,
          useValue: {
            listAccounts: () => of([account]),
          },
        },
        {
          provide: NzMessageService,
          useValue: jasmine.createSpyObj<NzMessageService>('NzMessageService', [
            'success',
          ]),
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();
  });

  function create(): void {
    fixture = TestBed.createComponent(UploadPolicyDialogComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('roomId', 100);
    fixture.componentRef.setInput('roomName', '测试主播');
    fixture.detectChanges();
  }

  it('uses safe defaults for a new room and loads categories lazily', () => {
    create();

    expect(policyService.get).toHaveBeenCalledOnceWith(100);
    expect(policyService.categories).toHaveBeenCalledOnceWith(
      'primary',
      null,
      false,
    );
    expect(component.draft.partTitleTemplate).toBe('P{{ part_index }}');
    expect(component.draft.dynamicTemplate).toBe('{{ title }} 录播');
    expect(component.draft.publishDynamic).toBeTrue();
    expect(component.draft.noReprint).toBeTrue();
    expect(component.draft.autoComment).toBeTrue();
    expect(component.draft.danmakuBackfill).toBeTrue();
    expect(component.draft.upCloseReply).toBeFalse();
    expect(component.draft.upCloseDanmu).toBeFalse();
    expect(component.draft.tid).toBeNull();
  });

  it('preserves every value from an existing room policy', () => {
    policyService.get.and.returnValue(of(existingPolicy));

    create();

    expect(component.draft.titleTemplate).toBe('旧标题');
    expect(component.draft.partTitleTemplate).toBe('第 {{ part_index }} P');
    expect(component.draft.publishDynamic).toBeFalse();
    expect(component.draft.noReprint).toBeFalse();
    expect(component.draft.isOnlySelf).toBeTrue();
    expect(component.categoryPath).toEqual([4, 17]);
  });

  it('maps a category path and rejects conflicting interaction switches', () => {
    create();
    component.categoryChanged([4, 17]);
    component.draft.upCloseReply = true;

    expect(component.draft.tid).toBe(17);
    expect(component.interactionError).toContain('自动索引评论');
    expect(component.canSave).toBeFalse();
  });

  it('submits the complete policy and deletes an existing policy', () => {
    policyService.get.and.returnValue(of(existingPolicy));
    create();
    component.save();

    expect(policyService.save).toHaveBeenCalledOnceWith(
      100,
      jasmine.objectContaining({
        partTitleTemplate: '第 {{ part_index }} P',
        publishDynamic: false,
        noReprint: false,
        upSelectionReply: true,
      }),
    );

    component.deletePolicy();
    expect(policyService.delete).toHaveBeenCalledOnceWith(100);
  });
});
