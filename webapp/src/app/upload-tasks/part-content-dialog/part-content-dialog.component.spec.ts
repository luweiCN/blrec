import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzButtonModule } from 'ng-zorro-antd/button';
import { NzModalModule } from 'ng-zorro-antd/modal';

import {
  RecordingPart,
  RecordingSession,
} from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';
import {
  PartPlayer,
  PartPlayerFactory,
} from './part-player.factory';
import { PartContentDialogComponent } from './part-content-dialog.component';

describe('PartContentDialogComponent', () => {
  let fixture: ComponentFixture<PartContentDialogComponent>;
  let service: jasmine.SpyObj<RecordingSessionService>;
  let playerFactory: jasmine.SpyObj<PartPlayerFactory>;
  let player: jasmine.SpyObj<PartPlayer>;

  const part: RecordingPart = {
    id: 2,
    runId: 'run-1',
    partIndex: 1,
    sourcePath: '/rec/p1.flv',
    finalPath: null,
    xmlPath: '/rec/p1.xml',
    recordStartTime: 901,
    recordEndTime: null,
    recordDurationSeconds: 59,
    fileSizeBytes: 1_024,
    danmakuCount: 2,
    artifactState: 'recording',
    xmlCompleted: true,
    sourceExists: true,
    finalExists: false,
    errorMessage: null,
  };

  const session: RecordingSession = {
    id: 1,
    roomId: 100,
    broadcastSessionKey: '100:900',
    liveStartTime: 900,
    state: 'open',
    startedAt: 900,
    endedAt: null,
    title: '正在直播',
    coverUrl: '',
    coverPath: null,
    anchorUid: 42,
    anchorName: '主播',
    areaId: 1,
    areaName: '分区',
    parentAreaId: 2,
    parentAreaName: '父分区',
    liveEndTime: null,
    partCount: 1,
    danmakuCount: 2,
    totalFileSizeBytes: 1_024,
    recordDurationSeconds: 59,
    uploadJob: null,
    parts: [part],
  };

  beforeEach(async () => {
    service = jasmine.createSpyObj<RecordingSessionService>(
      'RecordingSessionService',
      ['createMediaAccess', 'mediaUrl', 'listDanmaku']
    );
    service.createMediaAccess.and.returnValue(
      of({ token: 'signed', expiresAt: 123 })
    );
    service.mediaUrl.and.returnValue('/api/media?signed');
    service.listDanmaku.and.returnValue(
      of({
        items: [
          {
            index: 0,
            progressMs: 1_250,
            mode: 1,
            fontSize: 25,
            color: 16_777_215,
            content: '<script>不会执行</script>',
          },
        ],
        nextCursor: 1,
      })
    );
    player = jasmine.createSpyObj<PartPlayer>('PartPlayer', [
      'pause',
      'unload',
      'detachMediaElement',
      'destroy',
    ]);
    playerFactory = jasmine.createSpyObj<PartPlayerFactory>(
      'PartPlayerFactory',
      ['attachFlv']
    );
    playerFactory.attachFlv.and.returnValue(player);

    await TestBed.configureTestingModule({
      declarations: [PartContentDialogComponent],
      imports: [
        CommonModule,
        NoopAnimationsModule,
        NzAlertModule,
        NzButtonModule,
        NzModalModule,
      ],
      providers: [
        { provide: RecordingSessionService, useValue: service },
        { provide: PartPlayerFactory, useValue: playerFactory },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(PartContentDialogComponent);
    fixture.componentRef.setInput('session', session);
    fixture.componentRef.setInput('part', part);
    fixture.componentRef.setInput('visible', true);
  });

  it('plays a growing FLV through a scoped media URL and tears it down', () => {
    fixture.componentInstance.focus = 'video';
    fixture.detectChanges();

    expect(service.createMediaAccess).toHaveBeenCalledOnceWith(2);
    expect(service.mediaUrl).toHaveBeenCalledOnceWith(2, {
      token: 'signed',
      expiresAt: 123,
    });
    expect(playerFactory.attachFlv).toHaveBeenCalledWith(
      jasmine.any(HTMLVideoElement),
      '/api/media?signed',
      true
    );

    fixture.componentInstance.handleClose();

    expect(player.pause).toHaveBeenCalled();
    expect(player.unload).toHaveBeenCalled();
    expect(player.detachMediaElement).toHaveBeenCalled();
    expect(player.destroy).toHaveBeenCalled();
  });

  it('uses native video playback for a completed MP4', () => {
    fixture.componentRef.setInput('part', {
      ...part,
      sourcePath: '/rec/p1.flv',
      finalPath: '/rec/p1.mp4',
      artifactState: 'ready',
      finalExists: true,
    });
    fixture.componentRef.setInput('focus', 'video');
    fixture.detectChanges();

    const video = document.body.querySelector(
      '[data-testid="part-video"]'
    ) as HTMLVideoElement | null;
    expect(video?.src).toContain('/api/media?signed');
    expect(playerFactory.attachFlv).not.toHaveBeenCalled();
  });

  it('pages text-only danmaku without rendering embedded markup', () => {
    fixture.componentRef.setInput('focus', 'danmaku');
    fixture.detectChanges();

    expect(service.listDanmaku).toHaveBeenCalledOnceWith(2, 0, 100);
    expect(document.body.textContent).toContain('<script>不会执行</script>');
    expect(document.body.querySelector('.danmaku-content script')).toBeNull();

    service.listDanmaku.and.returnValue(
      of({
        items: [
          {
            index: 1,
            progressMs: 2_500,
            mode: 1,
            fontSize: 25,
            color: 255,
            content: '第二条',
          },
        ],
        nextCursor: null,
      })
    );
    fixture.componentInstance.loadMoreDanmaku();
    fixture.detectChanges();

    expect(service.listDanmaku).toHaveBeenCalledWith(2, 1, 100);
    expect(document.body.textContent).toContain('第二条');
  });

  it('falls back to the exact approved Bilibili part when local media is gone', () => {
    fixture.componentRef.setInput('part', {
      ...part,
      sourceExists: false,
      finalExists: false,
    });
    fixture.componentRef.setInput('session', {
      ...session,
      state: 'closed',
      uploadJob: {
        id: 9,
        accountId: 7,
        accountUid: 42,
        accountDisplayName: '投稿账号',
        state: 'approved',
        submitState: 'confirmed',
        commentBranchState: 'completed',
        danmakuBranchState: 'completed',
        aid: 123,
        bvid: 'BV1test',
        reviewReason: null,
        attempt: 1,
        nextAttemptAt: 0,
        createdAt: 1_000,
        updatedAt: 1_100,
        danmakuTotal: 0,
        danmakuConfirmed: 0,
        danmakuPending: 0,
        danmakuUnknown: 0,
        danmakuFailed: 0,
        unknownDanmakuItems: [],
        parts: [],
      },
    });
    fixture.componentRef.setInput('focus', 'video');
    fixture.detectChanges();

    const link = document.body.querySelector(
      '[data-testid="remote-part-link"]'
    ) as HTMLAnchorElement | null;
    expect(service.createMediaAccess).not.toHaveBeenCalled();
    expect(link?.href).toBe('https://www.bilibili.com/video/BV1test?p=1');
  });
});
