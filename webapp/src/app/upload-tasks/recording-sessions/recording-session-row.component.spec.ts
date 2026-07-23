import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { FormsModule } from '@angular/forms';

import { MoreOutline } from '@ant-design/icons-angular/icons';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCheckboxModule } from 'ng-zorro-antd/checkbox';
import { NzDropDownDirective, NzDropDownModule } from 'ng-zorro-antd/dropdown';
import { NZ_ICONS, NzIconModule } from 'ng-zorro-antd/icon';
import { NzMenuModule } from 'ng-zorro-antd/menu';
import { NzProgressModule } from 'ng-zorro-antd/progress';
import { NzTagModule } from 'ng-zorro-antd/tag';

import { RecordingSessionSummary } from '../shared/recording-session.model';
import {
  RecordingSessionRowAction,
  RecordingSessionRowComponent,
  RecordingSessionServerAction,
} from './recording-session-row.component';

function summary(): RecordingSessionSummary {
  return {
    id: 1,
    roomId: 100,
    liveStartTime: 900,
    state: 'closed',
    startedAt: 900,
    endedAt: 1_000,
    title: '今晚挑战通关',
    coverUrl: 'https://example.invalid/cover.jpg',
    anchorUid: 42,
    anchorName: '主播名',
    areaId: 1,
    areaName: '单机游戏',
    parentAreaId: 2,
    parentAreaName: '游戏',
    liveEndTime: 1_000,
    partCount: 2,
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
    mediaLibraryItemId: null,
    displayState: 'waiting_review',
    availableActions: [
      'edit_submission',
      'edit_task',
      'retry_failed',
      'delete_local',
    ],
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
      danmakuTotal: 10,
      danmakuConfirmed: 6,
      danmakuPending: 3,
      danmakuUnknown: 0,
      danmakuFailed: 1,
      repairState: 'idle',
      repairMessage: null,
      repairError: null,
      canRetry: true,
      canRepair: false,
      canSkip: false,
      canRepost: false,
      canDelete: true,
      operatorPaused: false,
      scheduledPublishAt: 1_700_000_000,
      collectionBranchState: 'pending',
      collectionError: null,
      submissionVerificationState: 'pending',
      submissionVerifiedAt: null,
      commentError: null,
      danmakuError: null,
      canPause: false,
      canResume: false,
      canEdit: true,
      confirmedBytes: 4,
      totalBytes: 8,
      percent: 50,
      bytesPerSecond: 2,
      etaSeconds: 2,
      currentPartIndex: 1,
      confirmedPartCount: 1,
      discoveredPartCount: 2,
    },
  };
}

function isOnPush(component: unknown): boolean {
  const definition = Reflect.get(component as object, 'ɵcmp') as
    { readonly onPush?: boolean } | undefined;
  return definition?.onPush === true;
}

@Component({
  template: `
    <table>
      <tbody>
        <tr
          app-recording-session-row
          [session]="session"
          [selected]="selected"
          [scope]="scope"
          [cutting]="cutting"
          [favoriting]="favoriting"
        ></tr>
      </tbody>
    </table>
  `,
})
class HostComponent {
  session = summary();
  selected = false;
  scope: 'recordings' | 'uploads' = 'uploads';
  cutting = false;
  favoriting = false;
}

describe('RecordingSessionRowComponent', () => {
  let fixture: ComponentFixture<HostComponent>;
  let host: HostComponent;
  let row: RecordingSessionRowComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [HostComponent, RecordingSessionRowComponent],
      imports: [
        CommonModule,
        FormsModule,
        NoopAnimationsModule,
        NzButtonModule,
        NzCheckboxModule,
        NzDropDownModule,
        NzIconModule,
        NzMenuModule,
        NzProgressModule,
        NzTagModule,
      ],
      providers: [{ provide: NZ_ICONS, useValue: [MoreOutline] }],
    }).compileComponents();

    fixture = TestBed.createComponent(HostComponent);
    host = fixture.componentInstance;
    fixture.detectChanges();
    row = fixture.debugElement.query(By.directive(RecordingSessionRowComponent))
      .componentInstance as RecordingSessionRowComponent;
  });

  it('uses an OnPush native table-row host with exactly six cells', () => {
    const element = fixture.nativeElement.querySelector(
      'tbody > tr[app-recording-session-row]',
    ) as HTMLTableRowElement | null;

    expect(isOnPush(RecordingSessionRowComponent)).toBeTrue();
    expect(element).not.toBeNull();
    expect(element?.tagName).toBe('TR');
    expect(element?.children.length).toBe(6);
    expect(
      Array.from(element?.children ?? []).every(
        (child) => child.tagName === 'TD',
      ),
    ).toBeTrue();
    expect(element?.querySelector('tr')).toBeNull();
    expect(element?.querySelector('app-recording-session-row')).toBeNull();
  });

  it('renders row-only identity, metrics, progress, danmaku and submission data', () => {
    const text = fixture.nativeElement.textContent;

    expect(text).toContain('今晚挑战通关');
    expect(text).toContain('主播名 · 房间 100');
    expect(text).toContain('2 个分 P');
    expect(text).toContain('59 秒 · 1 MB');
    expect(text).toContain('等待审核');
    expect(text).toContain('采集 321 条');
    expect(text).toContain('已回灌 6 / 10');
    expect(text).toContain('待处理 3');
    expect(text).toContain('失败 1');
    expect(text).toContain('投稿账号');
    expect(text).toContain('定时');
    expect(text).toContain('合集：待处理');
    expect(
      fixture.debugElement.query(By.directive(NzDropDownDirective)),
    ).not.toBeNull();
  });

  it('keeps archive links and highlight identity in the row presenter', () => {
    host.session = {
      ...host.session,
      sourceKind: 'highlight',
      highlightClipId: 3,
      uploadJob: {
        ...host.session.uploadJob!,
        state: 'approved',
      },
    };
    fixture.detectChanges();

    const link = fixture.nativeElement.querySelector(
      '[data-testid="archive-link"]',
    ) as HTMLAnchorElement | null;
    expect(fixture.nativeElement.textContent).toContain('高光');
    expect(link?.href).toBe('https://www.bilibili.com/video/BV1test');
    expect(link?.target).toBe('_blank');
    expect(link?.rel).toBe('noopener noreferrer');
  });

  it('emits only closed action variants with stable identifiers', () => {
    const events: RecordingSessionRowAction[] = [];
    const serverAction: RecordingSessionServerAction = 'retry_failed';
    row.rowAction.subscribe((event: RecordingSessionRowAction) =>
      events.push(event),
    );
    const controls = row as unknown as {
      selectionChanged(selected: boolean): void;
      showDetails(): void;
      cutCurrent(): void;
      favorite(): void;
      editSubmission(): void;
      runSessionAction(action: RecordingSessionServerAction): void;
      editTask(jobId: number): void;
    };

    controls.selectionChanged(true);
    controls.showDetails();
    controls.cutCurrent();
    controls.favorite();
    controls.editSubmission();
    controls.runSessionAction(serverAction);
    controls.editTask(9);
    const deleteButton = fixture.nativeElement.querySelector(
      '[data-testid="delete-session"]',
    ) as HTMLButtonElement;
    deleteButton.click();

    expect(events).toEqual([
      { type: 'selected', sessionId: 1, selected: true },
      { type: 'details', sessionId: 1 },
      { type: 'cut-current', sessionId: 1 },
      { type: 'favorite', sessionId: 1 },
      { type: 'edit-submission', sessionId: 1 },
      { type: 'session-action', sessionId: 1, action: 'retry_failed' },
      { type: 'edit-task', jobId: 9 },
      { type: 'session-action', sessionId: 1, action: 'delete_local' },
    ]);
    expect(events.every((event) => !('session' in event))).toBeTrue();
    expect(
      events.some((event) => (event as { type: string }).type === 'play'),
    ).toBeFalse();
  });

  it('derives cut availability from primitive inputs and exposes cutting state', () => {
    host.scope = 'recordings';
    host.session = {
      ...host.session,
      state: 'open',
      displayState: 'recording',
      uploadJob: null,
    };
    fixture.detectChanges();

    expect(row.canCutCurrentFile()).toBeTrue();
    expect(row.hasMoreActions()).toBeTrue();

    host.cutting = true;
    fixture.detectChanges();
    expect(row.cutting).toBeTrue();
  });

  it('offers permanent collection only for closed recording sessions', () => {
    host.scope = 'recordings';
    fixture.detectChanges();

    expect(row.canFavorite()).toBeTrue();

    host.session = { ...host.session, state: 'open' };
    fixture.detectChanges();
    expect(row.canFavorite()).toBeFalse();

    host.session = {
      ...host.session,
      state: 'closed',
      mediaLibraryItemId: 8,
    };
    fixture.detectChanges();
    expect(row.canFavorite()).toBeFalse();
    expect(fixture.nativeElement.textContent).toContain('已收藏');
  });

  it('renders the active recording upload intent', () => {
    host.session = {
      ...host.session,
      state: 'open',
      displayState: 'recording',
      uploadIntent: 'auto',
      uploadJob: null,
    };
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('本场结束后上传');

    host.session = { ...host.session, uploadIntent: 'skip' };
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('本场不上传');
  });
});
