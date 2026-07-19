import { HttpErrorResponse } from '@angular/common/http';
import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { FormsModule } from '@angular/forms';

import { NzMessageService } from 'ng-zorro-antd/message';
import { Subject, of, throwError } from 'rxjs';

import { BiliAccountService } from '../../uploads/shared/bili-account.service';
import {
  BiliCollectionCatalog,
  CoverAsset,
  RoomUploadPolicy,
  UploadCategoryCatalog,
} from './room-upload-policy.model';
import { RoomUploadPolicyService } from './room-upload-policy.service';
import { UploadPolicyDialogComponent } from './upload-policy-dialog.component';
import { RecordingSubmissionService } from './recording-submission.service';

describe('UploadPolicyDialogComponent', () => {
  let fixture: ComponentFixture<UploadPolicyDialogComponent>;
  let component: UploadPolicyDialogComponent;
  let policyService: jasmine.SpyObj<RoomUploadPolicyService>;
  let submissionService: jasmine.SpyObj<RecordingSubmissionService>;

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
          {
            id: 21,
            name: '日常',
            description: '日常生活内容',
            children: [],
          },
        ],
      },
      {
        id: 36,
        name: '知识',
        description: '',
        children: [
          {
            id: 208,
            name: '校园学习',
            description: '学习内容',
            children: [],
          },
        ],
      },
    ],
    creationStatements: [
      { id: -1, content: '内容无需标注' },
      { id: 1, content: '含 AI 生成内容' },
      { id: -2, content: '内容为转载' },
    ],
    creationStatementTip: '请根据内容选择',
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
    creationStatementId: -1,
    originalAuthorization: false,
    source: '',
    isOnlySelf: true,
    publishDynamic: false,
    upSelectionReply: true,
    upCloseReply: false,
    upCloseDanmu: false,
    autoComment: false,
    danmakuBackfill: false,
    filters: {},
    collectionSeasonId: 20,
    collectionSectionId: 21,
    coverMode: 'custom',
    coverAssetId: 3,
    publishDelaySeconds: 21_600,
    retentionMode: 'approved',
    retentionDays: 14,
    blockedReason: null,
    createdAt: 1000,
    updatedAt: 1000,
  };

  const covers: CoverAsset[] = [
    {
      id: 3,
      filename: '主播封面.png',
      mimeType: 'image/png',
      width: 1600,
      height: 1000,
      byteSize: 100,
      createdAt: 1000,
      contentUrl: '/api/v1/upload-covers/3/content',
    },
  ];

  const collectionCatalog: BiliCollectionCatalog = {
    accountId: 7,
    collections: [
      {
        id: 20,
        title: '主播录播',
        description: '',
        coverUrl: '',
        state: 0,
        rejectReason: '',
        selectable: true,
        sections: [{ id: 21, title: '正片' }],
      },
    ],
  };

  const submissionResponse = {
    sessionId: 7,
    roomId: 100,
    decision: 'follow_room' as const,
    inherited: true,
    settingsSource: 'room' as const,
    resolutionState: 'pending' as const,
    resolutionError: null,
    settings: existingPolicy,
  };

  beforeEach(async () => {
    policyService = jasmine.createSpyObj<RoomUploadPolicyService>(
      'RoomUploadPolicyService',
      [
        'get',
        'save',
        'delete',
        'categories',
        'covers',
        'coverContent',
        'uploadCover',
        'collections',
        'createCollection',
      ],
    );
    policyService.get.and.returnValue(
      throwError(() => new HttpErrorResponse({ status: 404 })),
    );
    policyService.categories.and.returnValue(of(categories));
    policyService.covers.and.returnValue(of(covers));
    policyService.coverContent.and.returnValue(
      of(new Blob(['image'], { type: 'image/png' })),
    );
    policyService.collections.and.returnValue(of(collectionCatalog));
    policyService.uploadCover.and.returnValue(of(covers[0]));
    policyService.createCollection.and.returnValue(
      of({ accountId: 7, collection: collectionCatalog.collections[0] }),
    );
    policyService.save.and.returnValue(of(existingPolicy));
    policyService.delete.and.returnValue(of(undefined));
    submissionService = jasmine.createSpyObj<RecordingSubmissionService>(
      'RecordingSubmissionService',
      ['get', 'save', 'clear', 'setDecision'],
    );
    submissionService.get.and.returnValue(of(submissionResponse));
    submissionService.save.and.returnValue(of(submissionResponse));
    submissionService.clear.and.returnValue(of(submissionResponse));

    await TestBed.configureTestingModule({
      declarations: [UploadPolicyDialogComponent],
      imports: [FormsModule],
      providers: [
        { provide: RoomUploadPolicyService, useValue: policyService },
        { provide: RecordingSubmissionService, useValue: submissionService },
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

  it('uses the reference project templates and repost defaults for a new room', () => {
    create();

    expect(policyService.get).toHaveBeenCalledOnceWith(100);
    expect(policyService.categories).toHaveBeenCalledOnceWith(
      'primary',
      null,
      false,
    );
    expect(component.draft.titleTemplate).toBe(
      '【直播回放】【{{ anchor_name }}】{{ title }} {{ live_start_time | date: "%Y年%m月%d日%H点%M分" }}',
    );
    expect(component.draft.descriptionTemplate).toBe(
      '直播录像\n{{ anchor_name }}直播间：https://live.bilibili.com/{{ room_id }}',
    );
    expect(component.draft.partTitleTemplate).toBe(
      'P{{ part_index }}-{{ area_name }}-{{ live_start_time | date: "%m月%d日%H点%M分" }}',
    );
    expect(component.draft.dynamicTemplate).toBe(
      '直播录像\n{{ anchor_name }}直播间：https://live.bilibili.com/{{ room_id }}',
    );
    expect(component.draft.tags).toBe(
      '直播回放,{{ anchor_name }},{{ area_name }}',
    );
    expect(component.draft.publishDynamic).toBeTrue();
    expect(component.draft.creationStatementId).toBe(-2);
    expect(component.draft.originalAuthorization).toBeFalse();
    expect(component.draft.source).toBe(
      'https://live.bilibili.com/{{ room_id }}',
    );
    expect(component.draft.autoComment).toBeTrue();
    expect(component.draft.danmakuBackfill).toBeTrue();
    expect(component.draft.upCloseReply).toBeFalse();
    expect(component.draft.upCloseDanmu).toBeFalse();
    expect(component.draft.tid).toBe(21);
    expect(component.categoryPath).toEqual([4, 21]);
    expect(component.draft.coverMode).toBe('live');
    expect(component.draft.coverAssetId).toBeNull();
    expect(component.draft.collectionSeasonId).toBeNull();
    expect(component.draft.collectionSectionId).toBeNull();
    expect(component.publishMode).toBe('immediate');
  });

  it('shows the form while the category request is still running', () => {
    const pendingCategories = new Subject<UploadCategoryCatalog>();
    policyService.categories.and.returnValue(pendingCategories);

    create();

    expect(component.loading).toBeFalse();
    expect(component.categoryLoading).toBeTrue();
  });

  it('preserves every value from an existing room policy', () => {
    policyService.get.and.returnValue(of(existingPolicy));

    create();

    expect(component.draft.titleTemplate).toBe('旧标题');
    expect(component.draft.partTitleTemplate).toBe('第 {{ part_index }} P');
    expect(component.draft.publishDynamic).toBeFalse();
    expect(component.draft.creationStatementId).toBe(-1);
    expect(component.draft.originalAuthorization).toBeFalse();
    expect(component.draft.isOnlySelf).toBeTrue();
    expect(component.categoryPath).toEqual([4, 17]);
    expect(component.draft.coverMode).toBe('custom');
    expect(component.draft.coverAssetId).toBe(3);
    expect(component.collectionSelection).toBe('20:21');
    expect(component.publishMode).toBe('scheduled');
    expect(component.publishDelayHours).toBe(6);
  });

  it('marks parent categories as expandable and child categories as selectable', () => {
    create();

    expect(component.categoryOptions[0].isLeaf).toBeFalse();
    expect(component.categoryOptions[0].children?.[0].isLeaf).toBeTrue();

    component.categoryChanged([4, 17]);

    expect(component.draft.tid).toBe(17);
    expect(component.categoryPath).toEqual([4, 17]);
  });

  it('keeps category options stable while the catalog is unchanged', () => {
    create();
    const options = component.categoryOptions;

    fixture.detectChanges();

    expect(component.categoryOptions).toBe(options);
  });

  it('recommends but does not automatically apply a matching live category', () => {
    create();
    component.liveParentAreaName = '知识';
    component.liveAreaName = '教育学习';
    component.draft.tid = 17;
    component.categoryPath = [4, 17];

    expect(component.categoryRecommendation?.label).toBe('知识 / 校园学习');
    expect(component.draft.tid).toBe(17);

    component.applyCategoryRecommendation();

    expect(component.draft.tid).toBe(208);
    expect(component.categoryPath).toEqual([36, 208]);
  });

  it('keeps save available and reports missing fields after it is clicked', () => {
    create();
    expect(component.saveDisabled).toBeFalse();
    component.categoryChanged(null);

    component.save();

    expect(policyService.save).not.toHaveBeenCalled();
    expect(component.validationErrors.category).toBe('请选择投稿分区');
  });

  it('maps positive interaction switches and clears dependent options', () => {
    create();
    component.draft.upSelectionReply = true;
    component.draft.autoComment = true;
    component.draft.danmakuBackfill = true;

    component.allowRepliesChanged(false);
    component.allowDanmakuChanged(false);

    expect(component.draft.upCloseReply).toBeTrue();
    expect(component.draft.upSelectionReply).toBeFalse();
    expect(component.draft.autoComment).toBeFalse();
    expect(component.draft.upCloseDanmu).toBeTrue();
    expect(component.draft.danmakuBackfill).toBeFalse();
  });

  it('keeps repost and original authorization mutually exclusive', () => {
    create();
    component.draft.originalAuthorization = true;

    component.creationStatementChanged(-2);

    expect(component.isRepost).toBeTrue();
    expect(component.draft.originalAuthorization).toBeFalse();

    component.creationStatementChanged(-1);
    component.draft.originalAuthorization = true;
    expect(component.isRepost).toBeFalse();
    expect(component.draft.originalAuthorization).toBeTrue();
  });

  it('submits the complete policy without exposing a destructive delete action', () => {
    policyService.get.and.returnValue(of(existingPolicy));
    create();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).not.toContain('删除投稿设置');
    component.save();

    expect(policyService.save).toHaveBeenCalledOnceWith(
      100,
      jasmine.objectContaining({
        partTitleTemplate: '第 {{ part_index }} P',
        publishDynamic: false,
        creationStatementId: -1,
        originalAuthorization: false,
        upSelectionReply: true,
        collectionSeasonId: 20,
        collectionSectionId: 21,
        coverMode: 'custom',
        coverAssetId: 3,
        publishDelaySeconds: 21_600,
        retentionMode: 'approved',
        retentionDays: 14,
      }),
    );

    expect(policyService.delete).not.toHaveBeenCalled();
  });

  it('reuses the complete form for a recording-session override', () => {
    fixture = TestBed.createComponent(UploadPolicyDialogComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('roomId', 100);
    fixture.componentRef.setInput('roomName', '测试主播');
    fixture.componentRef.setInput('sessionId', 7);
    fixture.detectChanges();

    expect(submissionService.get).toHaveBeenCalledOnceWith(7);
    expect(component.modalTitle).toContain('本场投稿设置');
    const saved = jasmine.createSpy('saved');
    component.saved.subscribe(saved);
    component.save();
    expect(submissionService.save).toHaveBeenCalledWith(
      7,
      jasmine.objectContaining({ titleTemplate: '旧标题' }),
    );
    expect(policyService.save).not.toHaveBeenCalled();
    expect(saved).toHaveBeenCalled();
  });

  it('uses a clip-specific form and returns the final submission title', () => {
    policyService.get.and.returnValue(of(existingPolicy));
    fixture = TestBed.createComponent(UploadPolicyDialogComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('roomId', 100);
    fixture.componentRef.setInput('roomName', '精彩片段');
    fixture.componentRef.setInput('deferredSave', true);
    const confirmed = jasmine.createSpy('confirmed');
    component.settingsConfirmed.subscribe(confirmed);
    fixture.detectChanges();

    expect(component.draft.titleTemplate).toBe('精彩片段');
    component.draft.titleTemplate = '最终投稿标题';
    component.save();

    expect(component.modalTitle).toContain('片段投稿设置');
    expect(confirmed).toHaveBeenCalledWith(
      jasmine.objectContaining({
        enabled: true,
        titleTemplate: '最终投稿标题',
        partTitleTemplate: '最终投稿标题',
        collectionSeasonId: 20,
        collectionSectionId: 21,
        coverMode: 'custom',
        publishDelaySeconds: 21_600,
        retentionMode: 'never',
        retentionDays: 0,
      }),
    );
    expect(policyService.save).not.toHaveBeenCalled();
    expect(submissionService.save).not.toHaveBeenCalled();
  });

  it('uploads and selects a collection cover without changing the manuscript cover', () => {
    const uploadedCover: CoverAsset = {
      ...covers[0],
      id: 8,
      filename: '新合集封面.png',
    };
    policyService.get.and.returnValue(of(existingPolicy));
    policyService.uploadCover.and.returnValue(of(uploadedCover));
    create();
    component.openCreateCollection();
    const file = new File(['image'], '新合集封面.png', { type: 'image/png' });

    component.collectionCoverFileSelected({
      target: { files: { item: () => file }, value: 'selected' },
    } as unknown as Event);

    expect(policyService.uploadCover).toHaveBeenCalledOnceWith(file);
    expect(component.newCollectionCoverAssetId).toBe(8);
    expect(component.draft.coverAssetId).toBe(3);
  });

  it('can restore a recording session to inherited room settings', () => {
    submissionService.get.and.returnValue(
      of({
        ...submissionResponse,
        inherited: false,
        settingsSource: 'session',
      }),
    );
    fixture = TestBed.createComponent(UploadPolicyDialogComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('roomId', 100);
    fixture.componentRef.setInput('roomName', '测试主播');
    fixture.componentRef.setInput('sessionId', 7);
    fixture.detectChanges();

    component.restoreInherited();

    expect(submissionService.clear).toHaveBeenCalledOnceWith(7);
  });

  it('hides restore-to-inherited when explicit session settings are required', () => {
    submissionService.get.and.returnValue(
      of({
        ...submissionResponse,
        inherited: false,
        settingsSource: 'session',
      }),
    );
    fixture = TestBed.createComponent(UploadPolicyDialogComponent);
    fixture.componentRef.setInput('roomId', 100);
    fixture.componentRef.setInput('sessionId', 7);
    fixture.componentRef.setInput('allowRestoreInherited', false);
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector(
        '[data-testid="restore-inherited-policy"]',
      ),
    ).toBeNull();
  });

  it('clears account-specific collection selection when the account changes', () => {
    policyService.get.and.returnValue(of(existingPolicy));
    create();

    component.accountModeChanged('primary');

    expect(component.collectionSelection).toBeNull();
    expect(component.draft.collectionSeasonId).toBeNull();
    expect(component.draft.collectionSectionId).toBeNull();
    expect(policyService.collections).toHaveBeenCalledWith('primary', null);
  });

  it('submits a newly selected cover collection and native publish delay', () => {
    create();
    component.coverModeChanged('custom');
    component.customCoverChanged(3);
    component.collectionChanged('20:21');
    component.publishModeChanged('scheduled');
    component.publishDelayHours = 4;
    component.draft.retentionMode = 'approved';
    component.draft.retentionDays = 14;

    component.save();

    expect(policyService.save).toHaveBeenCalledOnceWith(
      100,
      jasmine.objectContaining({
        coverMode: 'custom',
        coverAssetId: 3,
        collectionSeasonId: 20,
        collectionSectionId: 21,
        publishDelaySeconds: 14_400,
        retentionMode: 'approved',
        retentionDays: 14,
      }),
    );
  });

  it('creates a collection for the currently selected upload account', () => {
    create();
    component.openCreateCollection();
    component.newCollectionTitle = '主播录播合集';
    component.newCollectionDescription = '直播录像';
    component.newCollectionCoverAssetId = 3;

    component.createCollection();

    expect(policyService.createCollection).toHaveBeenCalledOnceWith({
      accountMode: 'primary',
      accountId: null,
      title: '主播录播合集',
      description: '直播录像',
      coverAssetId: 3,
    });
    expect(component.newCollectionVisible).toBeFalse();
  });
});
