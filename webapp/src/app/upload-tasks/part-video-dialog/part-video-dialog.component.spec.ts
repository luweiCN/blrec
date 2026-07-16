import { CommonModule } from '@angular/common';
import { OverlayContainer } from '@angular/cdk/overlay';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of, Subject } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzModalModule } from 'ng-zorro-antd/modal';

import { RecordingPart, RecordingSession } from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';
import { PartPlayer, PartPlayerFactory } from './part-player.factory';
import { PartVideoDialogComponent } from './part-video-dialog.component';

describe('PartVideoDialogComponent', () => {
  let fixture: ComponentFixture<PartVideoDialogComponent>;
  let service: jasmine.SpyObj<RecordingSessionService>;
  let playerFactory: jasmine.SpyObj<PartPlayerFactory>;
  let player: jasmine.SpyObj<PartPlayer>;
  let overlayContainer: OverlayContainer;

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
  const session = {
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
    uploadIntent: 'auto',
    uploadDecision: 'follow_room',
    submissionInherited: true,
    uploadResolutionState: 'pending',
    uploadResolutionError: null,
    uploadSuppressed: false,
    deletionState: 'none',
    deletionError: null,
    sourceKind: 'live',
    highlightClipId: null,
    displayState: 'recording',
    availableActions: ['set_skip', 'delete_local'],
    uploadJob: null,
    parts: [part],
  } as RecordingSession;

  beforeEach(async () => {
    service = jasmine.createSpyObj<RecordingSessionService>(
      'RecordingSessionService',
      ['createMediaAccess', 'mediaUrl', 'listDanmaku']
    );
    service.createMediaAccess.and.returnValue(
      of({
        token: 'signed',
        expiresAt: 123,
        snapshotId: 'snapshot-id',
        durationMs: 12_500,
        fileSizeBytes: 2_048,
        recording: true,
      })
    );
    service.mediaUrl.and.returnValue('/api/media?signed');
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
      declarations: [PartVideoDialogComponent],
      imports: [
        CommonModule,
        NoopAnimationsModule,
        NzAlertModule,
        NzModalModule,
      ],
      providers: [
        { provide: RecordingSessionService, useValue: service },
        { provide: PartPlayerFactory, useValue: playerFactory },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(PartVideoDialogComponent);
    overlayContainer = TestBed.inject(OverlayContainer);
    fixture.componentRef.setInput('session', session);
    fixture.componentRef.setInput('part', part);
    fixture.componentRef.setInput('visible', true);
  });

  it('plays a growing FLV as a finite seekable snapshot', () => {
    fixture.detectChanges();

    expect(playerFactory.attachFlv).toHaveBeenCalledWith(
      jasmine.any(HTMLVideoElement),
      '/api/media?signed',
      {
        isLive: false,
        durationMs: 12_500,
        fileSizeBytes: 2_048,
      },
      jasmine.any(Function)
    );
    expect(service.listDanmaku).not.toHaveBeenCalled();
    expect(fixture.nativeElement.querySelector('[role="tablist"]')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('查看弹幕');
  });

  it('uses finite player options when a growing FLV has no duration index', () => {
    service.createMediaAccess.and.returnValue(
      of({
        token: 'signed',
        expiresAt: 123,
        snapshotId: null,
        durationMs: null,
        fileSizeBytes: 1_024,
        recording: true,
      })
    );

    fixture.detectChanges();

    expect(playerFactory.attachFlv).toHaveBeenCalledWith(
      jasmine.any(HTMLVideoElement),
      '/api/media?signed',
      {
        isLive: false,
        durationMs: null,
        fileSizeBytes: 1_024,
      },
      jasmine.any(Function)
    );
  });

  it('marks the view after asynchronous media access completes', () => {
    const access = new Subject<{
      token: string;
      expiresAt: number;
      snapshotId: string;
      durationMs: number;
      fileSizeBytes: number;
      recording: boolean;
    }>();
    service.createMediaAccess.and.returnValue(access);
    const changeDetector = (fixture.componentInstance as any).changeDetector;
    spyOn(changeDetector, 'markForCheck');
    fixture.detectChanges();

    access.next({
      token: 'signed',
      expiresAt: 123,
      snapshotId: 'snapshot-id',
      durationMs: 12_500,
      fileSizeBytes: 2_048,
      recording: true,
    });

    expect(changeDetector.markForCheck).toHaveBeenCalled();
  });

  it('destroys the FLV player when closed', () => {
    fixture.detectChanges();

    fixture.componentInstance.handleClose();

    expect(player.pause).toHaveBeenCalled();
    expect(player.unload).toHaveBeenCalled();
    expect(player.detachMediaElement).toHaveBeenCalled();
    expect(player.destroy).toHaveBeenCalled();
  });

  it('surfaces native MP4 playback errors', () => {
    fixture.componentRef.setInput('part', {
      ...part,
      finalPath: '/rec/p1.mp4',
      finalExists: true,
    });
    fixture.detectChanges();

    const video = overlayContainer.getContainerElement().querySelector(
      '[data-testid="part-video"]'
    ) as HTMLVideoElement;
    video.dispatchEvent(new Event('error'));
    fixture.detectChanges();

    expect(overlayContainer.getContainerElement().textContent).toContain(
      '本地视频播放失败，请重新打开后再试'
    );
  });

  it('surfaces stalled native MP4 playback', () => {
    fixture.componentRef.setInput('part', {
      ...part,
      finalPath: '/rec/p1.mp4',
      finalExists: true,
    });
    fixture.detectChanges();

    const video = overlayContainer.getContainerElement().querySelector(
      '[data-testid="part-video"]'
    ) as HTMLVideoElement;
    video.dispatchEvent(new Event('stalled'));
    fixture.detectChanges();

    expect(overlayContainer.getContainerElement().textContent).toContain(
      '本地视频加载停滞，请检查连接后重试'
    );
  });
});
