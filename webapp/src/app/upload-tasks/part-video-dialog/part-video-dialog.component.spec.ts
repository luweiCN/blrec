import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of } from 'rxjs';
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
    uploadSuppressed: false,
    deletionState: 'none',
    deletionError: null,
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

  it('destroys the FLV player when closed', () => {
    fixture.detectChanges();

    fixture.componentInstance.handleClose();

    expect(player.pause).toHaveBeenCalled();
    expect(player.unload).toHaveBeenCalled();
    expect(player.detachMediaElement).toHaveBeenCalled();
    expect(player.destroy).toHaveBeenCalled();
  });
});
