import { CommonModule } from '@angular/common';
import { Clipboard } from '@angular/cdk/clipboard';
import { Component, EventEmitter, Input, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { FormsModule } from '@angular/forms';
import { RouterTestingModule } from '@angular/router/testing';

import { of, Subject, throwError } from 'rxjs';
import {
  CopyOutline,
  MoreOutline,
  QuestionCircleOutline,
  RedoOutline,
  ReloadOutline,
  SearchOutline,
} from '@ant-design/icons-angular/icons';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCheckboxModule } from 'ng-zorro-antd/checkbox';
import { NzDatePickerModule } from 'ng-zorro-antd/date-picker';
import { NzDrawerComponent, NzDrawerModule } from 'ng-zorro-antd/drawer';
import { NzDropDownDirective, NzDropDownModule } from 'ng-zorro-antd/dropdown';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzMenuModule } from 'ng-zorro-antd/menu';
import { NZ_ICONS, NzIconModule } from 'ng-zorro-antd/icon';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzMessageService } from 'ng-zorro-antd/message';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzPaginationModule } from 'ng-zorro-antd/pagination';
import { NzSelectModule } from 'ng-zorro-antd/select';
import { NzTableModule } from 'ng-zorro-antd/table';
import { NzTagModule } from 'ng-zorro-antd/tag';
import { NzToolTipModule } from 'ng-zorro-antd/tooltip';
import { NzProgressModule } from 'ng-zorro-antd/progress';

import {
  RealtimeEvent,
  RealtimeService,
} from '../../core/services/realtime.service';
import { RecordingSession } from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';
import { RecordingSessionsComponent } from './recording-sessions.component';

@Component({ selector: 'app-task-edit-dialog', template: '' })
class TaskEditDialogStubComponent {
  @Input() visible = false;
  @Input() jobIds: readonly number[] = [];
  @Output() readonly closed = new EventEmitter<void>();
  @Output() readonly saved = new EventEmitter<void>();
}

@Component({ selector: 'app-upload-policy-dialog', template: '' })
class UploadPolicyDialogStubComponent {
  @Input() sessionId: number | null = null;
  @Input() roomId = 0;
  @Input() roomName = '';
  @Input() liveAreaName = '';
  @Input() liveParentAreaName = '';
  @Output() readonly closed = new EventEmitter<void>();
}

describe('RecordingSessionsComponent', () => {
  let fixture: ComponentFixture<RecordingSessionsComponent>;
  let service: jasmine.SpyObj<RecordingSessionService>;
  let clipboard: jasmine.SpyObj<Clipboard>;
  let message: jasmine.SpyObj<NzMessageService>;
  let realtimeEvents: Subject<RealtimeEvent>;

  beforeEach(async () => {
    service = jasmine.createSpyObj<RecordingSessionService>(
      'RecordingSessionService',
      [
        'listSessions',
        'runJobAction',
        'runSessionAction',
        'retryFailedJobs',
        'previewRetryFailedJobs',
      ],
    );
    clipboard = jasmine.createSpyObj<Clipboard>('Clipboard', ['copy']);
    message = jasmine.createSpyObj<NzMessageService>('NzMessageService', [
      'success',
      'error',
      'warning',
      'info',
    ]);
    realtimeEvents = new Subject<RealtimeEvent>();
    service.runJobAction.and.returnValue(of({ results: [] }));
    service.runSessionAction.and.returnValue(of({ results: [] }));
    service.retryFailedJobs.and.returnValue(of({ results: [] }));
    service.previewRetryFailedJobs.and.returnValue(of({ items: [] }));
    service.listSessions.and.returnValue(
      of({
        degradedReason: null,
        total: 41,
        sessions: [
          {
            id: 1,
            roomId: 100,
            broadcastSessionKey: '100:900',
            liveStartTime: 900,
            state: 'closed',
            startedAt: 900,
            endedAt: 1_000,
            title: '今晚挑战通关',
            coverUrl: 'https://example.invalid/cover.jpg',
            coverPath: '/rec/cover.jpg',
            anchorUid: 42,
            anchorName: '主播名',
            areaId: 1,
            areaName: '单机游戏',
            parentAreaId: 2,
            parentAreaName: '游戏',
            liveEndTime: 1_000,
            partCount: 1,
            danmakuCount: 321,
            totalFileSizeBytes: 1_048_576,
            recordDurationSeconds: 59,
            uploadIntent: 'none',
            uploadDecision: 'follow_room',
            submissionInherited: true,
            uploadResolutionState: 'job_created',
            uploadResolutionError: null,
            uploadSuppressed: false,
            deletionState: 'none',
            deletionError: null,
            sourceKind: 'live',
            highlightClipId: null,
            displayState: 'waiting_review',
            availableActions: ['delete_local'],
            uploadJob: {
              id: 9,
              accountId: 7,
              accountUid: 42,
              accountDisplayName: '投稿账号',
              state: 'waiting_review',
              submitState: 'confirmed',
              commentBranchState: 'pending',
              danmakuBranchState: 'pending',
              aid: 123,
              bvid: 'BV1test',
              reviewReason: '等待 B 站审核',
              attempt: 2,
              nextAttemptAt: 1_100,
              createdAt: 1_001,
              updatedAt: 1_050,
              danmakuTotal: 1,
              danmakuConfirmed: 0,
              danmakuPending: 0,
              danmakuUnknown: 1,
              danmakuFailed: 0,
              repairState: 'idle',
              repairMessage: null,
              repairError: null,
              canRetry: false,
              canRepair: false,
              canSkip: false,
              canRepost: false,
              canDelete: true,
              operatorPaused: false,
              scheduledPublishAt: null,
              collectionBranchState: 'disabled',
              collectionError: null,
              submissionVerificationState: 'partial',
              submissionVerifiedAt: 1_040,
              submissionVerification: {
                state: 'partial',
                checked: ['title'],
                missing: ['up_selection_reply'],
                mismatches: [],
                differences: {},
                unverifiable: ['cover', 'collection'],
                error: null,
              },
              commentError: null,
              danmakuError: null,
              canPause: false,
              canResume: false,
              canEdit: false,
              confirmedBytes: 4,
              totalBytes: 8,
              percent: 50,
              bytesPerSecond: 2,
              etaSeconds: 2,
              currentPartIndex: 1,
              unknownDanmakuItems: [
                {
                  id: 11,
                  partIndex: 1,
                  progressMs: 12_000,
                  content: '需要确认的弹幕',
                  errorMessage: '远端结果未知',
                },
              ],
              parts: [
                {
                  id: 10,
                  partIndex: 1,
                  uploadState: 'confirmed',
                  danmakuImportState: 'pending',
                  remoteFilename: 'remote-p1',
                  cid: null,
                  transcodeState: 'unknown',
                  transcodeFailCode: null,
                  transcodeFailDesc: null,
                  confirmedBytes: 4,
                  totalBytes: 8,
                },
              ],
            },
            parts: [
              {
                id: 2,
                runId: 'run-1',
                partIndex: 1,
                sourcePath: '/rec/p1.flv',
                finalPath: '/rec/p1.mp4',
                xmlPath: '/rec/p1.xml',
                recordStartTime: 901,
                recordEndTime: 960,
                recordDurationSeconds: 59,
                fileSizeBytes: 1_048_576,
                danmakuCount: 321,
                artifactState: 'ready',
                xmlCompleted: true,
                sourceExists: false,
                finalExists: true,
                errorMessage: null,
              },
            ],
          },
        ],
      }),
    );

    await TestBed.configureTestingModule({
      declarations: [
        RecordingSessionsComponent,
        TaskEditDialogStubComponent,
        UploadPolicyDialogStubComponent,
      ],
      imports: [
        CommonModule,
        FormsModule,
        NoopAnimationsModule,
        RouterTestingModule,
        NzAlertModule,
        NzButtonModule,
        NzCheckboxModule,
        NzDatePickerModule,
        NzDrawerModule,
        NzDropDownModule,
        NzInputModule,
        NzMenuModule,
        NzIconModule,
        NzModalModule,
        NzPageHeaderModule,
        NzPaginationModule,
        NzProgressModule,
        NzSelectModule,
        NzTableModule,
        NzTagModule,
        NzToolTipModule,
      ],
      providers: [
        { provide: RecordingSessionService, useValue: service },
        { provide: Clipboard, useValue: clipboard },
        { provide: NzMessageService, useValue: message },
        {
          provide: RealtimeService,
          useValue: { events$: realtimeEvents.asObservable() },
        },
        {
          provide: NZ_ICONS,
          useValue: [
            CopyOutline,
            MoreOutline,
            QuestionCircleOutline,
            RedoOutline,
            ReloadOutline,
            SearchOutline,
          ],
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(RecordingSessionsComponent);
  });

  it('shows a compact paginated upload-task table', () => {
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent;
    expect(service.listSessions).toHaveBeenCalledOnceWith(20, 0, {
      scope: 'uploads',
      query: '',
      recordingState: null,
      uploadState: null,
      startedFrom: null,
      startedTo: null,
      sort: 'newest',
    });
    expect(text).toContain('上传任务');
    expect(text).not.toContain('上传任务列表');
    expect(text).toContain('直播与房间');
    expect(text).toContain('录制概要');
    expect(text).toContain('投稿状态');
    expect(text).toContain('房间 100');
    expect(
      fixture.nativeElement.querySelector('thead').textContent,
    ).not.toContain('录制状态');
    expect(text).toContain('今晚挑战通关');
    expect(text).toContain('主播名');
    expect(text).toContain('59 秒');
    expect(text).toContain('1 MB');
    expect(text).toContain('等待审核');
    expect(text).toContain('投稿账号');
    expect(text).not.toContain('UID 42');
    expect(text).not.toContain('/rec/p1.mp4');
    expect(
      fixture.nativeElement.querySelector('.pagination-bar'),
    ).not.toBeNull();
  });

  it('shows the derived upload intent while a recording is active', () => {
    fixture.detectChanges();
    const session = {
      ...fixture.componentInstance.sessions[0],
      state: 'open',
      displayState: 'recording',
      uploadJob: null,
    } as RecordingSession;

    expect(
      fixture.componentInstance.displayStateDetail({
        ...session,
        uploadIntent: 'auto',
      }),
    ).toBe('本场结束后上传');
    expect(
      fixture.componentInstance.displayStateDetail({
        ...session,
        uploadIntent: 'skip',
      }),
    ).toBe('本场不上传');
  });

  it('offers highlight editing from a concrete recording part', () => {
    fixture.componentInstance.scope = 'recordings';
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector('[data-testid="edit-highlight"]'),
    ).toBeNull();

    fixture.componentInstance.openDetails(
      fixture.componentInstance.sessions[0],
    );
    fixture.detectChanges();
    const link = document.body.querySelector(
      '[data-testid="edit-highlight-part"]',
    ) as HTMLAnchorElement | null;
    expect(link).not.toBeNull();
    expect(link?.getAttribute('href')).toContain('/recordings/highlights/1');
    expect(link?.getAttribute('href')).toContain(
      `partId=${fixture.componentInstance.sessions[0].parts[0].id}`,
    );
  });

  it('does not offer highlight editing when a part has no local video', () => {
    fixture.componentInstance.scope = 'recordings';
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];
    if (fixture.componentInstance.view.state !== 'ready') {
      throw new Error('expected a ready recording-session view');
    }
    fixture.componentInstance.view = {
      state: 'ready',
      response: {
        ...fixture.componentInstance.view.response,
        sessions: [
          {
            ...session,
            parts: session.parts.map((part) => ({
              ...part,
              sourceExists: false,
              finalExists: false,
            })),
          },
        ],
      },
    };
    fixture.componentInstance.openDetails(
      fixture.componentInstance.sessions[0],
    );
    fixture.detectChanges();

    expect(
      document.body.querySelector('[data-testid="edit-highlight-part"]'),
    ).toBeNull();
  });

  it('labels derived highlight tasks without offering another cut', () => {
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];
    if (fixture.componentInstance.view.state !== 'ready') {
      throw new Error('expected a ready recording-session view');
    }
    fixture.componentInstance.view = {
      state: 'ready',
      response: {
        ...fixture.componentInstance.view.response,
        sessions: [
          {
            ...session,
            sourceKind: 'highlight',
            highlightClipId: 3,
          },
        ],
      },
    };
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('高光');
    expect(
      fixture.nativeElement.querySelector('[data-testid="edit-highlight"]'),
    ).toBeNull();
  });

  it('renders more actions with the shared ellipsis dropdown', () => {
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];
    if (fixture.componentInstance.view.state !== 'ready') {
      throw new Error('expected a ready recording-session view');
    }
    fixture.componentInstance.view = {
      state: 'ready',
      response: {
        ...fixture.componentInstance.view.response,
        sessions: [
          { ...session, availableActions: ['retry_failed', 'delete_local'] },
        ],
      },
    };
    fixture.detectChanges();

    const trigger = fixture.nativeElement.querySelector(
      '[data-testid="session-actions-trigger"]',
    ) as HTMLButtonElement | null;
    const dropdown = fixture.debugElement
      .query(By.directive(NzDropDownDirective))
      .injector.get(NzDropDownDirective);

    expect(trigger).not.toBeNull();
    expect(trigger?.classList).toContain('ant-btn-text');
    expect(trigger?.querySelector('i[nz-icon][nztype="more"]')).not.toBeNull();
    expect(trigger?.textContent?.trim()).toBe('');
    expect(dropdown.nzOverlayClassName).toBe('action-dropdown-overlay');
  });

  it('hides the more menu when delete is the only available row action', () => {
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector('[data-testid="delete-session"]'),
    ).not.toBeNull();
    expect(
      fixture.nativeElement.querySelector(
        '[data-testid="session-actions-trigger"]',
      ),
    ).toBeNull();
  });

  it('opens the shared complete submission form for one recording', () => {
    fixture.componentInstance.scope = 'recordings';
    fixture.detectChanges();

    fixture.componentInstance.openSubmissionSettings(
      fixture.componentInstance.sessions[0],
    );
    fixture.detectChanges();

    const dialog = fixture.debugElement.query(
      By.directive(UploadPolicyDialogStubComponent),
    );
    expect(dialog).not.toBeNull();
    expect(dialog.componentInstance as UploadPolicyDialogStubComponent).toEqual(
      jasmine.objectContaining({ sessionId: 1, roomId: 100 }),
    );
  });

  it('keeps delete visible and uses concise operation names', () => {
    fixture.detectChanges();

    const deleteButton = fixture.nativeElement.querySelector(
      '[data-testid="delete-session"]',
    ) as HTMLButtonElement | null;
    expect(deleteButton).not.toBeNull();
    expect(deleteButton?.textContent?.trim()).toBe('删除');

    fixture.componentInstance.openSessionAction('retry_failed', [1]);
    expect(fixture.componentInstance.uploadActionTitle()).toBe('重试上传');
    fixture.componentInstance.openSessionAction('repair_transcode', [1]);
    expect(fixture.componentInstance.uploadActionTitle()).toBe('修复转码');
    fixture.componentInstance.openSessionAction('backfill_danmaku', [1]);
    expect(fixture.componentInstance.uploadActionTitle()).toBe('回灌弹幕');
    fixture.componentInstance.openSessionAction('repost_as_new', [1]);
    expect(fixture.componentInstance.uploadActionTitle()).toBe('重新投稿');
    fixture.componentInstance.openSessionAction('delete_local', [1]);
    expect(fixture.componentInstance.uploadActionTitle()).toBe('删除');
  });

  it('shows one completed status and links an approved archive title', () => {
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];
    if (fixture.componentInstance.view.state !== 'ready') {
      throw new Error('expected a ready recording-session view');
    }
    const response = fixture.componentInstance.view.response;
    fixture.componentInstance.view = {
      state: 'ready',
      response: {
        ...response,
        sessions: [
          {
            ...session,
            displayState: 'completed',
            uploadJob: {
              ...session.uploadJob!,
              state: 'approved',
            },
          },
        ],
      },
    };

    fixture.detectChanges();

    const archiveLink = fixture.nativeElement.querySelector(
      '[data-testid="archive-link"]',
    ) as HTMLAnchorElement | null;
    expect(fixture.nativeElement.textContent).toContain('审核通过');
    expect(fixture.nativeElement.textContent).not.toContain('投稿：已确认');
    expect(archiveLink?.textContent).toContain('今晚挑战通关');
    expect(archiveLink?.href).toBe('https://www.bilibili.com/video/BV1test');
  });

  it('links an approved part only after its cid is available', () => {
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];
    const approved = {
      ...session,
      uploadJob: {
        ...session.uploadJob!,
        state: 'approved' as const,
        parts: [
          {
            ...session.uploadJob!.parts[0],
            cid: 456,
          },
        ],
      },
    };

    expect(fixture.componentInstance.remotePartUrl(approved, 1)).toBe(
      'https://www.bilibili.com/video/BV1test?p=1',
    );
    expect(fixture.componentInstance.remotePartUrl(session, 1)).toBeNull();
  });

  it('requests the selected server page and page size', () => {
    fixture.detectChanges();

    fixture.componentInstance.pageIndexChanged(2);
    expect(service.listSessions).toHaveBeenCalledWith(
      20,
      20,
      jasmine.any(Object),
    );

    fixture.componentInstance.pageSizeChanged(50);
    expect(fixture.componentInstance.pageIndex).toBe(1);
    expect(service.listSessions).toHaveBeenCalledWith(
      50,
      0,
      jasmine.any(Object),
    );
  });

  it('reloads from page one with server-side filters', () => {
    fixture.detectChanges();
    service.listSessions.calls.reset();
    fixture.componentInstance.pageIndex = 3;
    fixture.componentInstance.keyword = '主播';
    fixture.componentInstance.recordingState = 'closed';
    fixture.componentInstance.uploadState = 'approved';
    fixture.componentInstance.sortOrder = 'oldest';

    fixture.componentInstance.applyFilters();

    expect(fixture.componentInstance.pageIndex).toBe(1);
    expect(service.listSessions).toHaveBeenCalledOnceWith(20, 0, {
      scope: 'uploads',
      query: '主播',
      recordingState: 'closed',
      uploadState: 'approved',
      startedFrom: null,
      startedTo: null,
      sort: 'oldest',
    });
  });

  it('previews every safe failed job before retrying', () => {
    service.previewRetryFailedJobs.and.returnValue(
      of({
        items: [
          {
            jobId: 9,
            roomId: 100,
            title: '失败录像',
            accountDisplayName: '投稿账号',
            reason: '网络失败',
          },
        ],
      }),
    );
    service.retryFailedJobs.and.returnValue(
      of({
        results: [
          { jobId: 9, accepted: true, message: '失败任务已重新排队' },
          { jobId: 10, accepted: false, message: '本地视频不可用' },
        ],
      }),
    );
    fixture.detectChanges();

    fixture.componentInstance.retryAllFailedJobs();

    expect(service.previewRetryFailedJobs).toHaveBeenCalledTimes(1);
    expect(fixture.componentInstance.retryPreviewVisible).toBeTrue();
    expect(service.retryFailedJobs).not.toHaveBeenCalled();

    fixture.componentInstance.submitRetryAllFailedJobs();

    expect(service.retryFailedJobs).toHaveBeenCalledTimes(1);
    expect(message.warning).toHaveBeenCalledWith(
      '已重新排队 1 个任务，跳过 1 个：本地视频不可用',
    );
  });

  it('selects current-page sessions and exposes batch actions', () => {
    fixture.detectChanges();

    fixture.componentInstance.setSessionSelected(1, true);
    fixture.detectChanges();

    expect(fixture.componentInstance.selectedSessionCount).toBe(1);
    expect(
      fixture.nativeElement.querySelector('[data-testid="batch-action-bar"]'),
    ).not.toBeNull();
  });

  it('keeps the operation header and cells fixed together', () => {
    fixture.detectChanges();

    const fixedHeader = fixture.nativeElement.querySelector(
      'thead th.ant-table-cell-fix-right',
    );
    const fixedCell = fixture.nativeElement.querySelector(
      'tbody td.ant-table-cell-fix-right',
    );

    expect(fixedHeader?.textContent).toContain('操作');
    expect(fixedCell?.textContent).toContain('详情');
  });

  it('submits row actions through the session batch endpoint', () => {
    service.runSessionAction.and.returnValue(
      of({
        results: [
          { sessionId: 1, accepted: true, message: '已排队检查 B 站转码状态' },
        ],
      }),
    );
    fixture.detectChanges();

    fixture.componentInstance.openSessionAction('repair_transcode', [1]);
    fixture.componentInstance.submitUploadAction();

    expect(service.runSessionAction).toHaveBeenCalledOnceWith(
      'repair_transcode',
      [1],
    );
    expect(message.success).toHaveBeenCalledWith('已排队检查 B 站转码状态');
    expect(fixture.componentInstance.uploadActionVisible).toBeFalse();
  });

  it('keeps a rejected upload action visible with its exact reason', () => {
    service.runSessionAction.and.returnValue(
      of({
        results: [
          {
            sessionId: 1,
            accepted: false,
            message: '投稿结果未知，自动重试可能产生重复稿件',
          },
        ],
      }),
    );
    fixture.detectChanges();

    fixture.componentInstance.openSessionAction('retry_failed', [1]);
    fixture.componentInstance.submitUploadAction();

    expect(fixture.componentInstance.uploadActionVisible).toBeTrue();
    expect(fixture.componentInstance.uploadActionError).toContain(
      '投稿结果未知',
    );
  });

  it('allows a session without an upload job to be uploaded or deleted', () => {
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];
    if (fixture.componentInstance.view.state !== 'ready') {
      throw new Error('expected a ready recording-session view');
    }
    fixture.componentInstance.view = {
      state: 'ready',
      response: {
        ...fixture.componentInstance.view.response,
        sessions: [
          {
            ...session,
            uploadJob: null,
            displayState: 'not_uploading',
            availableActions: ['set_upload', 'delete_local'],
          },
        ],
      },
    };

    fixture.detectChanges();

    expect(fixture.componentInstance.pageSessionIds).toEqual([1]);
    expect(
      fixture.componentInstance.hasAction(session.id, 'set_upload'),
    ).toBeTrue();
    expect(
      fixture.nativeElement.querySelector('[data-testid="session-select"]'),
    ).not.toBeNull();
    expect(
      fixture.nativeElement.querySelector(
        '[data-testid="session-actions-trigger"]',
      ),
    ).not.toBeNull();
  });

  it('opens full session details in a right drawer', () => {
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];
    const drawer = fixture.debugElement
      .query(By.directive(NzDrawerComponent))
      .injector.get(NzDrawerComponent);

    fixture.componentInstance.openDetails(session);
    fixture.detectChanges();

    expect(fixture.componentInstance.detailVisible).toBeTrue();
    expect(fixture.componentInstance.selectedSession).toBe(session);
    expect(drawer.nzWidth).toBe('1180px');
    expect(document.body.textContent).not.toContain('投稿配置核验');
    expect(document.body.textContent).not.toContain('可核验设置未返回');
    expect(document.body.textContent).not.toContain(
      '2 项设置暂时无法从 B 站稿件详情核验',
    );
    expect(document.body.textContent).not.toContain('视为已发送');
    expect(document.body.textContent).not.toContain('接受重复风险并重试');
    expect(document.body.textContent).not.toContain('需要确认的弹幕');
    expect(document.body.textContent).not.toContain('remote-p1');
    fixture.componentInstance.closeDetails();
    expect(fixture.componentInstance.detailVisible).toBeFalse();
    expect(fixture.componentInstance.selectedSession).toBeNull();
  });

  it('opens and clears video and danmaku in separate dialogs', () => {
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];
    const part = session.parts[0];

    fixture.componentInstance.openPartVideo(session, part);

    expect(fixture.componentInstance.videoVisible).toBeTrue();
    expect(fixture.componentInstance.videoSession).toBe(session);
    expect(fixture.componentInstance.videoPart).toBe(part);
    expect(fixture.componentInstance.danmakuVisible).toBeFalse();

    fixture.componentInstance.videoVisibilityChanged(false);

    expect(fixture.componentInstance.videoVisible).toBeFalse();
    expect(fixture.componentInstance.videoSession).toBeNull();
    expect(fixture.componentInstance.videoPart).toBeNull();

    fixture.componentInstance.openPartDanmaku(session, part);

    expect(fixture.componentInstance.danmakuVisible).toBeTrue();
    expect(fixture.componentInstance.danmakuSession).toBe(session);
    expect(fixture.componentInstance.danmakuPart).toBe(part);
    expect(fixture.componentInstance.videoVisible).toBeFalse();

    fixture.componentInstance.danmakuVisibilityChanged(false);

    expect(fixture.componentInstance.danmakuVisible).toBeFalse();
    expect(fixture.componentInstance.danmakuSession).toBeNull();
    expect(fixture.componentInstance.danmakuPart).toBeNull();
  });

  it('does not reopen a closed detail drawer when the list refreshes', () => {
    fixture.detectChanges();
    fixture.componentInstance.openDetails(
      fixture.componentInstance.sessions[0],
    );
    fixture.componentInstance.closeDetails();

    fixture.componentInstance.load();

    expect(fixture.componentInstance.detailVisible).toBeFalse();
    expect(fixture.componentInstance.selectedSession).toBeNull();
  });

  it('shows only file names while retaining automatic recovery labels', () => {
    fixture.detectChanges();

    expect(fixture.componentInstance.fileName('/rec/path/very-long.flv')).toBe(
      'very-long.flv',
    );
    expect(fixture.componentInstance.sessionStateLabel('manual_review')).toBe(
      '自动恢复中',
    );
    expect(fixture.componentInstance.artifactStateLabel('manual_review')).toBe(
      '自动恢复中',
    );
  });

  it('copies the exact full path and reports success', () => {
    clipboard.copy.and.returnValue(true);

    fixture.componentInstance.copyPath('/rec/path/very-long.flv');

    expect(clipboard.copy).toHaveBeenCalledOnceWith('/rec/path/very-long.flv');
    expect(message.success).toHaveBeenCalledOnceWith('已复制完整路径');
    expect(message.error).not.toHaveBeenCalled();
  });

  it('shows explicit copy controls beside every visible file path', () => {
    fixture.componentInstance.scope = 'recordings';
    fixture.detectChanges();
    fixture.componentInstance.openDetails(
      fixture.componentInstance.sessions[0],
    );
    fixture.detectChanges();

    const finalButton = document.body.querySelector(
      '[data-testid="copy-final-path"]',
    );
    const xmlButton = document.body.querySelector(
      '[data-testid="copy-xml-path"]',
    );

    expect(finalButton?.getAttribute('aria-label')).toBe('复制完整路径');
    expect(xmlButton?.getAttribute('aria-label')).toBe('复制完整路径');
    expect(
      document.body.querySelector('[data-testid="copy-source-path"]'),
    ).toBeNull();
  });

  it('reports a clipboard failure instead of hiding it', () => {
    clipboard.copy.and.returnValue(false);

    fixture.componentInstance.copyPath('/rec/path/very-long.xml');

    expect(clipboard.copy).toHaveBeenCalledOnceWith('/rec/path/very-long.xml');
    expect(message.error).toHaveBeenCalledOnceWith('复制失败，请重试');
    expect(message.success).not.toHaveBeenCalled();
  });

  it('marks the OnPush application tree after sessions load', () => {
    const changeDetector = fixture.componentInstance['changeDetector'];
    const markForCheck = spyOn(changeDetector, 'markForCheck');

    fixture.detectChanges();

    expect(markForCheck).toHaveBeenCalled();
  });

  it('patches upload byte progress from SSE without reloading the page', () => {
    fixture.detectChanges();
    expect(service.listSessions).toHaveBeenCalledTimes(1);

    realtimeEvents.next({
      type: 'upload_progress',
      data: {
        jobs: [
          {
            jobId: 9,
            sessionId: 1,
            state: 'waiting_review',
            submitState: 'confirmed',
            aid: 123,
            bvid: 'BV1test',
            confirmedBytes: 6,
            totalBytes: 8,
            percent: 75,
            bytesPerSecond: 2,
            etaSeconds: 1,
            currentPartIndex: 1,
          },
        ],
      },
    });

    expect(fixture.componentInstance.sessions[0].uploadJob?.percent).toBe(75);
    expect(service.listSessions).toHaveBeenCalledTimes(1);
  });

  it('shows a retry action when session loading fails', () => {
    service.listSessions.and.returnValue(
      throwError(() => new Error('upload database is unavailable')),
    );

    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain(
      'upload database is unavailable',
    );
    expect(
      fixture.nativeElement.querySelector('[data-testid="retry-sessions"]'),
    ).not.toBeNull();
  });

});
