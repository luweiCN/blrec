import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { FormsModule } from '@angular/forms';

import { of, throwError } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzDrawerModule } from 'ng-zorro-antd/drawer';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzPageHeaderModule } from 'ng-zorro-antd/page-header';
import { NzTableModule } from 'ng-zorro-antd/table';
import { NzTagModule } from 'ng-zorro-antd/tag';

import { RecordingSessionService } from '../shared/recording-session.service';
import { RecordingSessionsComponent } from './recording-sessions.component';

describe('RecordingSessionsComponent', () => {
  let fixture: ComponentFixture<RecordingSessionsComponent>;
  let service: jasmine.SpyObj<RecordingSessionService>;

  beforeEach(async () => {
    service = jasmine.createSpyObj<RecordingSessionService>(
      'RecordingSessionService',
      ['listSessions', 'decideDanmakuItem']
    );
    service.decideDanmakuItem.and.returnValue(of(void 0));
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
      })
    );

    await TestBed.configureTestingModule({
      declarations: [RecordingSessionsComponent],
      imports: [
        CommonModule,
        FormsModule,
        NoopAnimationsModule,
        NzAlertModule,
        NzButtonModule,
        NzDrawerModule,
        NzInputModule,
        NzModalModule,
        NzPageHeaderModule,
        NzTableModule,
        NzTagModule,
      ],
      providers: [{ provide: RecordingSessionService, useValue: service }],
    }).compileComponents();

    fixture = TestBed.createComponent(RecordingSessionsComponent);
  });

  it('shows a compact paginated upload-task table', () => {
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent;
    expect(service.listSessions).toHaveBeenCalledOnceWith(20, 0);
    expect(text).toContain('上传任务');
    expect(text).not.toContain('上传任务列表');
    expect(text).toContain('直播与房间');
    expect(text).toContain('录制概要');
    expect(text).toContain('投稿状态');
    expect(text).toContain('房间 100');
    expect(text).toContain('已归集');
    expect(text).toContain('今晚挑战通关');
    expect(text).toContain('主播名');
    expect(text).toContain('59 秒');
    expect(text).toContain('1 MB');
    expect(text).toContain('等待审核');
    expect(text).toContain('投稿账号');
    expect(text).not.toContain('/rec/p1.mp4');
  });

  it('requests the selected server page and page size', () => {
    fixture.detectChanges();

    fixture.componentInstance.pageIndexChanged(2);
    expect(service.listSessions).toHaveBeenCalledWith(20, 20);

    fixture.componentInstance.pageSizeChanged(50);
    expect(fixture.componentInstance.pageIndex).toBe(1);
    expect(service.listSessions).toHaveBeenCalledWith(50, 0);
  });

  it('opens full session details in a right drawer', () => {
    fixture.detectChanges();
    const session = fixture.componentInstance.sessions[0];

    fixture.componentInstance.openDetails(session);

    expect(fixture.componentInstance.detailVisible).toBeTrue();
    expect(fixture.componentInstance.selectedSession).toBe(session);
    fixture.componentInstance.closeDetails();
    expect(fixture.componentInstance.detailVisible).toBeFalse();
  });

  it('marks the OnPush application tree after sessions load', () => {
    const changeDetector = fixture.componentInstance['changeDetector'];
    const markForCheck = spyOn(changeDetector, 'markForCheck');

    fixture.detectChanges();

    expect(markForCheck).toHaveBeenCalled();
  });

  it('shows a retry action when session loading fails', () => {
    service.listSessions.and.returnValue(
      throwError(() => new Error('upload database is unavailable'))
    );

    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain(
      'upload database is unavailable'
    );
    expect(
      fixture.nativeElement.querySelector('[data-testid="retry-sessions"]')
    ).not.toBeNull();
  });

  it('requires a reason before accepting duplicate danmaku risk', () => {
    fixture.detectChanges();
    const item = fixture.componentInstance.sessions[0].uploadJob!
      .unknownDanmakuItems[0];

    fixture.componentInstance.openDanmakuDecision(
      item,
      'retry_accept_duplicate_risk'
    );
    fixture.componentInstance.decisionReason = '';
    fixture.componentInstance.submitDanmakuDecision();
    expect(service.decideDanmakuItem).not.toHaveBeenCalled();

    fixture.componentInstance.decisionReason = '已人工核对，接受重复风险';
    fixture.componentInstance.submitDanmakuDecision();

    expect(service.decideDanmakuItem).toHaveBeenCalledOnceWith(11, {
      action: 'retry_accept_duplicate_risk',
      reason: '已人工核对，接受重复风险',
    });
  });
});
