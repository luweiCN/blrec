import { CommonModule } from '@angular/common';
import { Clipboard } from '@angular/cdk/clipboard';
import { Component, EventEmitter, Input, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';

import { NEVER, of, Subject, throwError } from 'rxjs';
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
  ControlOperation,
  ControlOperationService,
} from '../../core/services/control-operation.service';
import {
  EVENT_SOURCE_FACTORY,
  EventSourceLike,
  RealtimeEvent,
  RealtimeService,
} from '../../core/services/realtime.service';
import { UrlService } from '../../core/services/url.service';
import {
  RecordingSession,
  RecordingSessionDetail,
  RecordingSessionSummary,
  RecordingSessionsResponse,
} from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';
import { HighlightService } from '../shared/highlight.service';
import { RecordingSessionsComponent } from './recording-sessions.component';
import {
  RecordingSessionRowComponent,
  RecordingSessionRowAction,
} from './recording-session-row.component';
import { TaskManagerService } from '../../tasks/shared/services/task-manager.service';

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

class FakeRealtimeSource implements EventSourceLike {
  private readonly listeners = new Map<string, EventListener[]>();

  addEventListener(type: string, listener: EventListener): void {
    const values = this.listeners.get(type) ?? [];
    values.push(listener);
    this.listeners.set(type, values);
  }

  removeEventListener(type: string, listener: EventListener): void {
    this.listeners.set(
      type,
      (this.listeners.get(type) ?? []).filter((value) => value !== listener),
    );
  }

  close(): void {}

  next(event: RealtimeEvent): void {
    const message = new MessageEvent(event.type, {
      data: JSON.stringify(event.data),
    });
    for (const listener of this.listeners.get(event.type) ?? []) {
      listener(message);
    }
  }
}

function realtimeUploadJob(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    jobId: 9,
    sessionId: 1,
    state: 'waiting_review',
    submitState: 'confirmed',
    preuploadFinalized: true,
    displayState: 'standard',
    aid: 123,
    bvid: 'BV1test',
    confirmedBytes: 4,
    totalBytes: 8,
    percent: 50,
    bytesPerSecond: 2,
    etaSeconds: 2,
    currentPartIndex: 1,
    confirmedPartCount: 1,
    discoveredPartCount: 1,
    ...overrides,
  };
}

function realtimeUploadJobFor(
  session: RecordingSessionSummary,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const job = session.uploadJob;
  if (!job) {
    throw new Error('expected a summary upload job');
  }
  return realtimeUploadJob({
    jobId: job.id,
    sessionId: session.id,
    state: job.state,
    submitState: job.submitState,
    preuploadFinalized: job.preuploadFinalized,
    displayState: job.displayState,
    aid: job.aid,
    bvid: job.bvid,
    confirmedBytes: job.confirmedBytes,
    totalBytes: job.totalBytes,
    percent: job.percent,
    bytesPerSecond: job.bytesPerSecond,
    etaSeconds: job.etaSeconds,
    currentPartIndex: job.currentPartIndex,
    confirmedPartCount: job.confirmedPartCount,
    discoveredPartCount: job.discoveredPartCount,
    ...overrides,
  });
}

function isOnPush(component: unknown): boolean {
  const definition = Reflect.get(component as object, 'ɵcmp') as
    { readonly onPush?: boolean } | undefined;
  return definition?.onPush === true;
}

function retryOperation(
  status: 'running' | 'succeeded' | 'failed',
  processed: number,
  total: number,
  succeeded: number,
  rejected: number,
): ControlOperation {
  return {
    id: 'upload-retry-1',
    lane: 'upload-retry',
    kind: 'retry-failed',
    targetKey: 'upload-retry-1',
    attempt: 1,
    generation: 1,
    status,
    result: { processed, total, succeeded, rejected },
    errorCode: null,
    createdAt: 1,
    updatedAt: 2,
    steps: [],
  };
}

describe('RecordingSessionsComponent', () => {
  let fixture: ComponentFixture<RecordingSessionsComponent>;
  let service: jasmine.SpyObj<RecordingSessionService>;
  let clipboard: jasmine.SpyObj<Clipboard>;
  let message: jasmine.SpyObj<NzMessageService>;
  let taskManager: jasmine.SpyObj<TaskManagerService>;
  let highlights: jasmine.SpyObj<HighlightService>;
  let controlOperations: jasmine.SpyObj<ControlOperationService>;
  let realtimeEvents: FakeRealtimeSource;
  let detailSession: RecordingSessionDetail;
  let summarySession: RecordingSessionSummary;

  beforeEach(async () => {
    service = jasmine.createSpyObj<RecordingSessionService>(
      'RecordingSessionService',
      [
        'listSessions',
        'getSession',
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
      'loading',
      'remove',
    ]);
    message.loading.and.returnValue({
      messageId: 'retry-progress',
      onClose: new Subject<boolean>(),
    });
    controlOperations = jasmine.createSpyObj<ControlOperationService>(
      'ControlOperationService',
      ['poll'],
    );
    controlOperations.poll.and.returnValue(NEVER);
    taskManager = jasmine.createSpyObj<TaskManagerService>(
      'TaskManagerService',
      ['canCutStream', 'cutStream'],
    );
    taskManager.canCutStream.and.returnValue(of(true));
    taskManager.cutStream.and.returnValue(of(null));
    highlights = jasmine.createSpyObj<HighlightService>('HighlightService', [
      'getTimeline',
      'getMarkerCounts',
    ]);
    highlights.getTimeline.and.returnValue(
      of({
        sessionId: 1,
        roomId: 100,
        durationMs: 59_000,
        stableEndMs: 59_000,
        parts: [
          {
            partId: 2,
            partIndex: 1,
            timelineStartMs: 0,
            durationMs: 59_000,
            stableEndMs: 59_000,
            recording: false,
            mediaKind: 'native',
          },
        ],
        markers: [],
      }),
    );
    highlights.getMarkerCounts.and.returnValue(of([{ partId: 2, count: 0 }]));
    realtimeEvents = new FakeRealtimeSource();
    service.runJobAction.and.returnValue(of({ results: [] }));
    service.runSessionAction.and.returnValue(of({ results: [] }));
    service.retryFailedJobs.and.returnValue(
      of({ operationId: 'upload-retry-1', status: 'accepted', total: 0 }),
    );
    service.previewRetryFailedJobs.and.returnValue(of({ items: [] }));
    detailSession = {
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
        preuploadFinalized: true,
        displayState: 'standard',
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
        confirmedPartCount: 1,
        discoveredPartCount: 1,
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
    };
    const {
      broadcastSessionKey: _broadcastSessionKey,
      coverPath: _coverPath,
      parts: _parts,
      uploadJob: detailUploadJob,
      ...summaryFields
    } = detailSession;
    const {
      submissionVerification: _submissionVerification,
      unknownDanmakuItems: _unknownDanmakuItems,
      parts: _uploadParts,
      ...uploadSummary
    } = detailUploadJob!;
    summarySession = { ...summaryFields, uploadJob: uploadSummary };
    service.getSession.and.returnValue(of(detailSession));
    service.listSessions.and.returnValue(
      of({ degradedReason: null, total: 41, sessions: [summarySession] }),
    );

    await TestBed.configureTestingModule({
      declarations: [
        RecordingSessionsComponent,
        RecordingSessionRowComponent,
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
        { provide: TaskManagerService, useValue: taskManager },
        { provide: HighlightService, useValue: highlights },
        { provide: ControlOperationService, useValue: controlOperations },
        RealtimeService,
        { provide: EVENT_SOURCE_FACTORY, useValue: () => realtimeEvents },
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
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

    spyOnProperty(TestBed.inject(Router), 'url', 'get').and.returnValue(
      '/upload-tasks',
    );
    localStorage.removeItem('blrec-upload-retry-operation-id');
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
    expect(text).toContain('任务');
    expect(text).toContain('录像');
    expect(text).toContain('处理进度');
    expect(text).toContain('投稿配置');
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
    expect(
      fixture.nativeElement.querySelector('[data-testid="danmaku-summary"]')
        .textContent,
    ).toContain('采集 321 条');
    expect(
      fixture.nativeElement.querySelector('[data-testid="danmaku-summary"]')
        .textContent,
    ).toContain('已回灌 0 / 1');
    expect(text).not.toContain('UID 42');
    expect(text).not.toContain('/rec/p1.mp4');
    expect('parts' in fixture.componentInstance.sessions[0]).toBeFalse();
    expect(
      'submissionVerification' in
        (fixture.componentInstance.sessions[0].uploadJob as object),
    ).toBeFalse();
    expect(
      'parts' in (fixture.componentInstance.sessions[0].uploadJob as object),
    ).toBeFalse();
    expect(
      fixture.nativeElement.querySelector('.pagination-bar'),
    ).not.toBeNull();
  });

  it('uses OnPush for the parent and delegates six cells to native row hosts', () => {
    fixture.detectChanges();

    expect(isOnPush(RecordingSessionsComponent)).toBeTrue();
    const rows = fixture.nativeElement.querySelectorAll(
      'tbody > tr[app-recording-session-row]',
    ) as NodeListOf<HTMLTableRowElement>;
    expect(rows.length).toBe(1);
    expect(rows[0].children.length).toBe(6);
    expect(fixture.componentInstance.trackSession(99, summarySession)).toBe(
      summarySession.id,
    );
  });

  it('routes closed row events through the parent using current summaries', () => {
    const openSession: RecordingSessionSummary = {
      ...summarySession,
      state: 'open',
      displayState: 'recording',
      availableActions: [
        'edit_submission',
        'edit_task',
        'retry_failed',
        'delete_local',
      ],
    };
    service.listSessions.and.returnValue(
      of({ degradedReason: null, total: 1, sessions: [openSession] }),
    );
    fixture.componentInstance.scope = 'recordings';
    fixture.detectChanges();

    const rowDebug = fixture.debugElement.query(
      By.directive(RecordingSessionRowComponent),
    );
    expect(rowDebug).not.toBeNull();
    if (!rowDebug) {
      return;
    }
    const row = rowDebug.componentInstance as RecordingSessionRowComponent;
    const emit = (action: RecordingSessionRowAction): void => {
      row.rowAction.emit(action);
    };

    emit({ type: 'selected', sessionId: openSession.id, selected: true });
    expect(
      fixture.componentInstance.isSessionSelected(openSession.id),
    ).toBeTrue();

    emit({ type: 'details', sessionId: openSession.id });
    expect(service.getSession).toHaveBeenCalledOnceWith(openSession.id);
    expect(fixture.componentInstance.detailVisible).toBeTrue();

    emit({ type: 'edit-submission', sessionId: openSession.id });
    expect(fixture.componentInstance.submissionSession).toBe(openSession);

    emit({
      type: 'session-action',
      sessionId: openSession.id,
      action: 'retry_failed',
    });
    expect(fixture.componentInstance.uploadAction).toBe('retry_failed');
    expect(fixture.componentInstance.uploadActionSessionIds).toEqual([
      openSession.id,
    ]);

    emit({ type: 'edit-task', jobId: openSession.uploadJob!.id });
    expect(fixture.componentInstance.taskEditVisible).toBeTrue();
    expect(fixture.componentInstance.taskEditJobIds).toEqual([
      openSession.uploadJob!.id,
    ]);

    emit({ type: 'cut-current', sessionId: openSession.id });
    expect(taskManager.canCutStream).toHaveBeenCalledOnceWith(
      openSession.roomId,
    );
    expect(taskManager.cutStream).toHaveBeenCalledOnceWith(openSession.roomId);
  });

  it('shows preupload phases instead of generic internal job states', () => {
    fixture.detectChanges();
    const job = fixture.componentInstance.sessions[0].uploadJob!;

    expect(
      fixture.componentInstance.uploadDisplayStateLabel({
        ...job,
        preuploadFinalized: false,
        displayState: 'preuploading',
      }),
    ).toBe('录制中 · 正在预上传');
    expect(
      fixture.componentInstance.uploadDisplayStateLabel({
        ...job,
        preuploadFinalized: false,
        displayState: 'preuploaded_waiting',
      }),
    ).toBe('录制中 · 已预上传，等待新分 P');
    expect(
      fixture.componentInstance.uploadDisplayStateLabel({
        ...job,
        preuploadFinalized: false,
        displayState: 'preupload_paused',
      }),
    ).toBe('录制中 · 预上传已暂停');
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
      `partId=${detailSession.parts[0].id}`,
    );
  });

  it('shows the highlight count for each part in recording details', () => {
    highlights.getMarkerCounts.and.returnValue(of([{ partId: 2, count: 2 }]));
    fixture.componentInstance.scope = 'recordings';
    fixture.detectChanges();

    fixture.componentInstance.openDetails(
      fixture.componentInstance.sessions[0],
    );
    fixture.detectChanges();

    expect(service.getSession).toHaveBeenCalledOnceWith(1);
    expect(highlights.getMarkerCounts).toHaveBeenCalledOnceWith(1);
    expect(highlights.getTimeline).not.toHaveBeenCalled();
    expect(
      document.body.querySelector('[data-testid="part-highlight-count"]')
        ?.textContent,
    ).toContain('高光 2');
  });

  it('does not offer highlight editing when a part has no local video', () => {
    fixture.componentInstance.scope = 'recordings';
    fixture.detectChanges();
    service.getSession.and.returnValue(
      of({
        ...detailSession,
        parts: detailSession.parts.map((part) => ({
          ...part,
          sourceExists: false,
          finalExists: false,
        })),
      }),
    );
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
    fixture.componentInstance['changeDetector'].markForCheck();
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
    fixture.componentInstance['changeDetector'].markForCheck();
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

  it('offers current-file cutting only for an active live recording', () => {
    fixture.componentInstance.scope = 'recordings';
    fixture.detectChanges();
    const closedSession = fixture.componentInstance.sessions[0];
    const openSession = {
      ...closedSession,
      state: 'open',
      displayState: 'recording',
      uploadJob: null,
    } as RecordingSession;
    const cutActions =
      fixture.componentInstance as RecordingSessionsComponent & {
        canCutCurrentFile?: (session: RecordingSession) => boolean;
      };

    expect(cutActions.canCutCurrentFile?.(openSession)).toBeTrue();
    expect(cutActions.canCutCurrentFile?.(closedSession)).toBeFalse();
    expect(
      cutActions.canCutCurrentFile?.({
        ...openSession,
        sourceKind: 'highlight',
      }),
    ).toBeFalse();
    fixture.componentInstance.scope = 'uploads';
    expect(cutActions.canCutCurrentFile?.(openSession)).toBeFalse();
  });

  it('checks capability once before cutting the active recording file', () => {
    fixture.componentInstance.scope = 'recordings';
    fixture.detectChanges();
    const openSession = {
      ...fixture.componentInstance.sessions[0],
      state: 'open',
      displayState: 'recording',
      uploadJob: null,
    } as RecordingSession;
    const capability = new Subject<boolean>();
    taskManager.canCutStream.and.returnValue(capability);
    const cutActions =
      fixture.componentInstance as RecordingSessionsComponent & {
        cutCurrentFile?: (session: RecordingSession) => void;
      };

    cutActions.cutCurrentFile?.(openSession);
    cutActions.cutCurrentFile?.(openSession);
    expect(taskManager.canCutStream).toHaveBeenCalledOnceWith(100);

    capability.next(true);
    capability.complete();
    expect(taskManager.cutStream).toHaveBeenCalledOnceWith(100);
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
    fixture.componentInstance['changeDetector'].markForCheck();

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
    const session = detailSession;
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

  it('links sparse original parts by their submitted page order', () => {
    fixture.detectChanges();
    const session = detailSession;
    const sparse = {
      ...session,
      uploadJob: {
        ...session.uploadJob!,
        state: 'approved' as const,
        parts: [
          { ...session.uploadJob!.parts[0], partIndex: 2, cid: 202 },
          { ...session.uploadJob!.parts[0], id: 12, partIndex: 12, cid: 1212 },
        ],
      },
    };

    expect(fixture.componentInstance.remotePartUrl(sparse, 2)).toBe(
      'https://www.bilibili.com/video/BV1test?p=1',
    );
    expect(fixture.componentInstance.remotePartUrl(sparse, 12)).toBe(
      'https://www.bilibili.com/video/BV1test?p=2',
    );
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

  it('ignores an older list response after a newer page request', () => {
    const older = new Subject<RecordingSessionsResponse>();
    const newer = new Subject<RecordingSessionsResponse>();
    const newerSummary = { ...summarySession, id: 2, roomId: 200 };
    service.listSessions.and.returnValues(older, newer);

    fixture.detectChanges();
    fixture.componentInstance.pageIndexChanged(2);
    newer.next({ degradedReason: null, total: 1, sessions: [newerSummary] });
    newer.complete();
    older.next({ degradedReason: null, total: 1, sessions: [summarySession] });
    older.complete();

    expect(fixture.componentInstance.sessions).toEqual([newerSummary]);
  });

  it('keeps the list request pipeline alive after an error', () => {
    service.listSessions.and.returnValues(
      throwError(() => new Error('first request failed')),
      of({ degradedReason: null, total: 1, sessions: [summarySession] }),
    );

    fixture.detectChanges();
    expect(fixture.componentInstance.errorMessage).toBe('first request failed');

    fixture.componentInstance.load();

    expect(fixture.componentInstance.sessions).toEqual([summarySession]);
    expect(service.listSessions).toHaveBeenCalledTimes(2);
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

  it('previews every safe failed job before starting its durable retry operation', () => {
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
        operationId: 'upload-retry-1',
        status: 'accepted',
        total: 2,
      }),
    );
    fixture.detectChanges();

    fixture.componentInstance.retryAllFailedJobs();

    expect(service.previewRetryFailedJobs).toHaveBeenCalledTimes(1);
    expect(fixture.componentInstance.retryPreviewVisible).toBeTrue();
    expect(service.retryFailedJobs).not.toHaveBeenCalled();

    fixture.componentInstance.submitRetryAllFailedJobs();

    expect(service.retryFailedJobs).toHaveBeenCalledTimes(1);
    expect(controlOperations.poll).toHaveBeenCalledOnceWith('upload-retry-1');
    expect(localStorage.getItem('blrec-upload-retry-operation-id')).toBe(
      'upload-retry-1',
    );
  });

  it('shows cumulative retry progress and reloads the list at terminal state', () => {
    const operation = new Subject<ControlOperation>();
    controlOperations.poll.and.returnValue(operation);
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
      of({ operationId: 'upload-retry-1', status: 'accepted', total: 201 }),
    );
    fixture.detectChanges();
    service.listSessions.calls.reset();

    fixture.componentInstance.retryAllFailedJobs();
    fixture.componentInstance.submitRetryAllFailedJobs();
    operation.next(retryOperation('running', 100, 201, 99, 1));

    expect(fixture.componentInstance.retryProgress).toEqual({
      processed: 100,
      total: 201,
      succeeded: 99,
      rejected: 1,
    });
    expect(message.loading).toHaveBeenCalledWith(
      '失败任务重试中：已处理 100/201，成功 99，跳过 1',
      { nzDuration: 0 },
    );

    operation.next(retryOperation('failed', 201, 201, 199, 2));
    operation.complete();

    expect(message.warning).toHaveBeenCalledWith(
      '失败任务重试完成：已处理 201/201，成功 199，跳过 2',
    );
    expect(service.listSessions).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem('blrec-upload-retry-operation-id')).toBeNull();
  });

  it('only stops local polling on destroy and resumes a stored operation on re-entry', () => {
    const operation = new Subject<ControlOperation>();
    controlOperations.poll.and.returnValue(operation);
    localStorage.setItem(
      'blrec-upload-retry-operation-id',
      'upload-retry-resume',
    );

    fixture.detectChanges();

    expect(controlOperations.poll).toHaveBeenCalledOnceWith(
      'upload-retry-resume',
    );
    expect(operation.observers.length).toBe(1);

    fixture.destroy();

    expect(operation.observers.length).toBe(0);
    expect(localStorage.getItem('blrec-upload-retry-operation-id')).toBe(
      'upload-retry-resume',
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

  it('fits its core columns without a horizontal-scroll table', () => {
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector('.ant-table-scroll-horizontal'),
    ).toBeNull();
    expect(fixture.nativeElement.querySelector('thead').textContent).toContain(
      '操作',
    );
    expect(fixture.nativeElement.querySelector('tbody').textContent).toContain(
      '详情',
    );
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
    fixture.componentInstance['changeDetector'].markForCheck();

    fixture.detectChanges();

    expect(fixture.componentInstance.pageSessionIds).toEqual([1]);
    const row = fixture.debugElement.query(
      By.directive(RecordingSessionRowComponent),
    ).componentInstance as RecordingSessionRowComponent;
    expect(row.hasAction('set_upload')).toBeTrue();
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
    expect(service.getSession).toHaveBeenCalledOnceWith(1);
    expect(highlights.getMarkerCounts).toHaveBeenCalledOnceWith(1);
    expect(highlights.getTimeline).not.toHaveBeenCalled();
    expect(fixture.componentInstance.selectedSession).toBe(detailSession);
    expect(drawer.nzWidth).toBe('1180px');
    expect(document.body.textContent).not.toContain('投稿配置核验');
    expect(document.body.textContent).not.toContain('可核验设置未返回');
    expect(document.body.textContent).not.toContain(
      '2 项设置暂时无法从 B 站稿件详情核验',
    );
    expect(document.body.textContent).not.toContain('视为已发送');
    expect(document.body.textContent).not.toContain('接受重复风险并重试');
    expect(document.body.textContent).toContain('发送结果待确认 1 条');
    expect(document.body.textContent).toContain('P1 · 12 秒');
    expect(document.body.textContent).toContain('需要确认的弹幕');
    expect(document.body.textContent).toContain('远端结果未知');
    expect(document.body.textContent).not.toContain('remote-p1');
    fixture.componentInstance.closeDetails();
    expect(fixture.componentInstance.detailVisible).toBeFalse();
    expect(fixture.componentInstance.selectedSession).toBeNull();
  });

  it('loads non-live detail without requesting marker counts', () => {
    const clipSummary: RecordingSessionSummary = {
      ...summarySession,
      sourceKind: 'highlight',
      highlightClipId: 3,
    };
    const clipDetail: RecordingSessionDetail = {
      ...detailSession,
      sourceKind: 'highlight',
      highlightClipId: 3,
    };
    service.getSession.and.returnValue(of(clipDetail));
    fixture.detectChanges();
    service.getSession.calls.reset();
    highlights.getMarkerCounts.calls.reset();

    fixture.componentInstance.openDetails(clipSummary);

    expect(service.getSession).toHaveBeenCalledOnceWith(1);
    expect(highlights.getMarkerCounts).not.toHaveBeenCalled();
    expect(fixture.componentInstance.selectedSession).toBe(clipDetail);
  });

  it('keeps loaded detail fields when the summary list refreshes', () => {
    fixture.detectChanges();
    fixture.componentInstance.openDetails(summarySession);
    expect(fixture.componentInstance.selectedSession?.parts).toEqual(
      detailSession.parts,
    );

    fixture.componentInstance.load();

    expect(fixture.componentInstance.selectedSession?.parts).toEqual(
      detailSession.parts,
    );
  });

  it('does not apply a detail request after the drawer closes', () => {
    const detail = new Subject<RecordingSessionDetail>();
    const counts = new Subject<readonly { partId: number; count: number }[]>();
    service.getSession.and.returnValue(detail);
    highlights.getMarkerCounts.and.returnValue(counts);
    fixture.detectChanges();

    fixture.componentInstance.openDetails(summarySession);
    fixture.componentInstance.closeDetails();
    detail.next(detailSession);
    detail.complete();
    counts.next([{ partId: 2, count: 1 }]);
    counts.complete();

    expect(fixture.componentInstance.detailVisible).toBeFalse();
    expect(fixture.componentInstance.selectedSession).toBeNull();
  });

  it('opens video and danmaku in one combined dialog', () => {
    fixture.detectChanges();
    const session = detailSession;
    const part = session.parts[0];

    fixture.componentInstance.openPartVideo(session, part);

    expect(fixture.componentInstance.videoVisible).toBeTrue();
    expect(fixture.componentInstance.videoSession).toBe(session);
    expect(fixture.componentInstance.videoPart).toBe(part);

    fixture.componentInstance.videoVisibilityChanged(false);

    expect(fixture.componentInstance.videoVisible).toBeFalse();
    expect(fixture.componentInstance.videoSession).toBeNull();
    expect(fixture.componentInstance.videoPart).toBeNull();

    fixture.componentInstance.openPartDanmaku(session, part);

    expect(fixture.componentInstance.videoVisible).toBeTrue();
    expect(fixture.componentInstance.videoSession).toBe(session);
    expect(fixture.componentInstance.videoPart).toBe(part);
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
          realtimeUploadJob({
            confirmedBytes: 6,
            percent: 75,
            etaSeconds: 1,
          }),
        ],
      },
    });

    expect(fixture.componentInstance.sessions[0].uploadJob?.percent).toBe(75);
    expect(service.listSessions).toHaveBeenCalledTimes(1);
  });

  it('preserves the ready view when an SSE snapshot has no scalar changes', () => {
    fixture.detectChanges();
    const beforeView = fixture.componentInstance.view;
    if (beforeView.state !== 'ready') {
      throw new Error('expected a ready upload-task view');
    }
    const beforeResponse = beforeView.response;
    const beforeSessions = beforeResponse.sessions;
    const beforeSession = beforeSessions[0];
    const beforeJob = beforeSession.uploadJob;
    const markForCheck = spyOn(
      fixture.componentInstance['changeDetector'],
      'markForCheck',
    );

    realtimeEvents.next({
      type: 'upload_progress',
      data: { jobs: [realtimeUploadJobFor(beforeSession)] },
    });

    expect(fixture.componentInstance.view).toBe(beforeView);
    expect(fixture.componentInstance.view.state).toBe('ready');
    if (fixture.componentInstance.view.state !== 'ready') {
      return;
    }
    expect(fixture.componentInstance.view.response).toBe(beforeResponse);
    expect(fixture.componentInstance.view.response.sessions).toBe(
      beforeSessions,
    );
    expect(fixture.componentInstance.sessions[0]).toBe(beforeSession);
    expect(fixture.componentInstance.sessions[0].uploadJob).toBe(beforeJob);
    expect(markForCheck).not.toHaveBeenCalled();
    expect(service.listSessions).toHaveBeenCalledTimes(1);
  });

  it('reuses 19 row inputs and instances for one pure SSE progress change', () => {
    const sessions = Array.from({ length: 20 }, (_, index) => ({
      ...summarySession,
      id: index + 1,
      roomId: 100 + index,
      title: `场次 ${index + 1}`,
      uploadJob: {
        ...summarySession.uploadJob!,
        id: 1000 + index,
        state: 'uploading' as const,
        percent: index,
      },
    }));
    service.listSessions.and.returnValue(
      of({ degradedReason: null, total: sessions.length, sessions }),
    );
    fixture.detectChanges();

    const baseline = sessions.map((session) => realtimeUploadJobFor(session));
    realtimeEvents.next({
      type: 'upload_progress',
      data: { jobs: baseline },
    });
    fixture.detectChanges();

    const beforeRefs = [...fixture.componentInstance.sessions];
    const beforeRows = fixture.debugElement.queryAll(
      By.directive(RecordingSessionRowComponent),
    );
    expect(beforeRows.length).toBe(20);
    const nativeRows = fixture.nativeElement.querySelectorAll(
      'tbody > tr[app-recording-session-row]',
    ) as NodeListOf<HTMLTableRowElement>;
    expect(nativeRows.length).toBe(20);
    expect(
      Array.from(nativeRows).every(
        (nativeRow) => nativeRow.children.length === 6,
      ),
    ).toBeTrue();
    const beforeInstances = beforeRows.map(
      (row) => row.componentInstance as RecordingSessionRowComponent,
    );
    const changedIndex = 7;

    realtimeEvents.next({
      type: 'upload_progress',
      data: {
        jobs: baseline.map((job, index) =>
          index === changedIndex ? { ...job, percent: 99 } : job,
        ),
      },
    });
    fixture.detectChanges();

    const afterRefs = fixture.componentInstance.sessions;
    const afterRows = fixture.debugElement.queryAll(
      By.directive(RecordingSessionRowComponent),
    );
    const afterInstances = afterRows.map(
      (row) => row.componentInstance as RecordingSessionRowComponent,
    );
    expect(
      afterRefs
        .map((session, index) => session !== beforeRefs[index])
        .filter(Boolean),
    ).toHaveSize(1);
    expect(afterRefs[changedIndex]).not.toBe(beforeRefs[changedIndex]);
    expect(afterRefs[changedIndex].uploadJob?.percent).toBe(99);
    beforeRefs.forEach((session, index) => {
      if (index !== changedIndex) {
        expect(afterRefs[index]).toBe(session);
      }
    });
    expect(afterInstances).toEqual(beforeInstances);
    expect(afterRows[changedIndex].nativeElement.textContent).toContain('99%');
    expect(service.listSessions).toHaveBeenCalledTimes(1);
  });

  it('skips the bootstrap resync and reloads for the next resync', () => {
    fixture.detectChanges();
    expect(service.listSessions).toHaveBeenCalledTimes(1);

    realtimeEvents.next({ type: 'resync', data: {} });
    expect(service.listSessions).toHaveBeenCalledTimes(1);

    realtimeEvents.next({ type: 'resync', data: {} });
    expect(service.listSessions).toHaveBeenCalledTimes(2);
  });

  it('reloads when SSE announces a new visible preupload task', () => {
    fixture.detectChanges();
    const existingJob = realtimeUploadJob();
    realtimeEvents.next({
      type: 'upload_progress',
      data: { jobs: [existingJob] },
    });
    expect(service.listSessions).toHaveBeenCalledTimes(1);

    realtimeEvents.next({
      type: 'upload_progress',
      data: {
        jobs: [
          existingJob,
          realtimeUploadJob({
            jobId: 10,
            sessionId: 2,
            state: 'waiting_artifacts',
            submitState: 'prepared',
            preuploadFinalized: false,
            displayState: 'preuploading',
            aid: null,
            bvid: null,
            confirmedBytes: 0,
            percent: 0,
            bytesPerSecond: null,
            etaSeconds: null,
            confirmedPartCount: 0,
          }),
        ],
      },
    });

    expect(service.listSessions).toHaveBeenCalledTimes(2);
  });

  it('reloads when the current provisional task disappears from SSE', () => {
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];
    if (
      fixture.componentInstance.view.state !== 'ready' ||
      !session.uploadJob
    ) {
      throw new Error('expected a ready upload-task view');
    }
    fixture.componentInstance.view = {
      state: 'ready',
      response: {
        ...fixture.componentInstance.view.response,
        sessions: [
          {
            ...session,
            uploadJob: {
              ...session.uploadJob,
              preuploadFinalized: false,
              displayState: 'preuploaded_waiting',
            },
          },
        ],
      },
    };
    fixture.componentInstance['changeDetector'].markForCheck();

    realtimeEvents.next({
      type: 'upload_progress',
      data: {
        jobs: [
          realtimeUploadJob({
            preuploadFinalized: false,
            displayState: 'preuploaded_waiting',
          }),
        ],
      },
    });
    expect(service.listSessions).toHaveBeenCalledTimes(1);

    realtimeEvents.next({ type: 'upload_progress', data: { jobs: [] } });

    expect(service.listSessions).toHaveBeenCalledTimes(2);
  });

  it('shows completed and discovered part counts during preupload', () => {
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];
    if (
      fixture.componentInstance.view.state !== 'ready' ||
      !session.uploadJob
    ) {
      throw new Error('expected a ready upload-task view');
    }
    fixture.componentInstance.view = {
      state: 'ready',
      response: {
        ...fixture.componentInstance.view.response,
        sessions: [
          {
            ...session,
            uploadJob: {
              ...session.uploadJob,
              preuploadFinalized: false,
              displayState: 'preuploaded_waiting',
            },
          },
        ],
      },
    };
    fixture.componentInstance['changeDetector'].markForCheck();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain(
      '已预上传 1 / 1 个已封口分 P',
    );
  });

  it('updates preupload part counts from SSE without a state change', () => {
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];
    if (
      fixture.componentInstance.view.state !== 'ready' ||
      !session.uploadJob
    ) {
      throw new Error('expected a ready upload-task view');
    }
    fixture.componentInstance.view = {
      state: 'ready',
      response: {
        ...fixture.componentInstance.view.response,
        sessions: [
          {
            ...session,
            uploadJob: {
              ...session.uploadJob,
              preuploadFinalized: false,
              displayState: 'preuploaded_waiting',
            },
          },
        ],
      },
    };
    fixture.componentInstance['changeDetector'].markForCheck();
    const progress = realtimeUploadJob({
      preuploadFinalized: false,
      displayState: 'preuploaded_waiting',
    });
    realtimeEvents.next({
      type: 'upload_progress',
      data: { jobs: [progress] },
    });

    realtimeEvents.next({
      type: 'upload_progress',
      data: {
        jobs: [
          realtimeUploadJob({
            ...progress,
            confirmedPartCount: 2,
            discoveredPartCount: 2,
          }),
        ],
      },
    });
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain(
      '已预上传 2 / 2 个已封口分 P',
    );
    expect(service.listSessions).toHaveBeenCalledTimes(1);
  });

  it('reloads when a paginated-out job disappears from the SSE snapshot', () => {
    fixture.detectChanges();
    const currentJob = realtimeUploadJob();
    realtimeEvents.next({
      type: 'upload_progress',
      data: {
        jobs: [currentJob, realtimeUploadJob({ jobId: 10, sessionId: 2 })],
      },
    });
    service.listSessions.calls.reset();

    realtimeEvents.next({
      type: 'upload_progress',
      data: { jobs: [currentJob] },
    });

    expect(service.listSessions).toHaveBeenCalledTimes(1);
  });

  it('labels open-session parts as discovered rather than final', () => {
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];
    if (fixture.componentInstance.view.state !== 'ready') {
      throw new Error('expected a ready upload-task view');
    }
    fixture.componentInstance.view = {
      state: 'ready',
      response: {
        ...fixture.componentInstance.view.response,
        sessions: [{ ...session, state: 'open' }],
      },
    };
    fixture.componentInstance['changeDetector'].markForCheck();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('1 个已发现分 P');
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
