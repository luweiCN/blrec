import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { FormsModule } from '@angular/forms';

import { of, throwError } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzCardModule } from 'ng-zorro-antd/card';
import { NzCollapseModule } from 'ng-zorro-antd/collapse';
import { NzEmptyModule } from 'ng-zorro-antd/empty';
import { NzInputModule } from 'ng-zorro-antd/input';
import { NzModalModule } from 'ng-zorro-antd/modal';
import { NzSpinModule } from 'ng-zorro-antd/spin';
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
        NzCardModule,
        NzCollapseModule,
        NzEmptyModule,
        NzInputModule,
        NzModalModule,
        NzSpinModule,
        NzTagModule,
      ],
      providers: [{ provide: RecordingSessionService, useValue: service }],
    }).compileComponents();

    fixture = TestBed.createComponent(RecordingSessionsComponent);
  });

  it('shows persisted sessions, part order, final files, and XML state', () => {
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent;
    expect(service.listSessions).toHaveBeenCalledOnceWith(50);
    expect(text).toContain('上传任务列表');
    expect(text).toContain('房间 100');
    expect(text).toContain('已归集');
    expect(text).toContain('今晚挑战通关');
    expect(text).toContain('主播名');
    expect(text).toContain('游戏 / 单机游戏');
    expect(text).toContain('59 秒');
    expect(text).toContain('1 MB');
    expect(text).toContain('321 条');
    expect(text).toContain('等待审核');
    expect(text).toContain('投稿账号');
    expect(text).toContain('UID 42');
    expect(text).toContain('BV1test');
    expect(text).toContain('等待 B 站审核');
    expect(text).toContain('评论：待处理');
    expect(text).toContain('回灌：待处理');
    expect(text).toContain(
      '回灌弹幕会长期出现在投稿视频中，发送者显示为投稿账号，不是原直播观众账号。'
    );
    expect(text).toContain('已发送 0 / 1');
    expect(text).toContain('结果未知 1');
    expect(text).toContain('需要确认的弹幕');
    expect(text).toContain('P1');
    expect(text).toContain('上传已完成');
    expect(text).toContain('CID 待回填');
    expect(text).toContain('/rec/p1.mp4');
    expect(text).toContain('/rec/p1.xml');
    const cover = fixture.nativeElement.querySelector('.session-cover');
    expect(cover.getAttribute('src')).toBe(
      'https://example.invalid/cover.jpg'
    );
    expect(cover.getAttribute('referrerpolicy')).toBe('no-referrer');
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
