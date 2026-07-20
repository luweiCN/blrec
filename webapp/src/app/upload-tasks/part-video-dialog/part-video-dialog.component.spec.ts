import { CommonModule } from '@angular/common';
import { OverlayContainer } from '@angular/cdk/overlay';
import {
  ComponentFixture,
  TestBed,
  fakeAsync,
  flushMicrotasks,
  tick,
} from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { of, Subject } from 'rxjs';
import { NzAlertModule } from 'ng-zorro-antd/alert';
import { NzModalModule } from 'ng-zorro-antd/modal';

import {
  RecordingMediaAccess,
  RecordingPart,
  RecordingSession,
} from '../shared/recording-session.model';
import { RecordingSessionService } from '../shared/recording-session.service';
import { PART_PLAYER_LOADER } from './part-player.loader';
import type {
  PartPlayer,
  PartPlayerEventHandler,
  PartPlayerFactoryLike,
  PartPlayerLoader,
} from './part-player.loader';
import { PartVideoDialogComponent } from './part-video-dialog.component';

interface Deferred<T> {
  readonly promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe('PartVideoDialogComponent', () => {
  let fixture: ComponentFixture<PartVideoDialogComponent>;
  let service: jasmine.SpyObj<RecordingSessionService>;
  let playerLoader: jasmine.Spy<PartPlayerLoader>;
  let playerFactory: jasmine.SpyObj<PartPlayerFactoryLike>;
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
      ['createMediaAccess', 'mediaUrl', 'listDanmaku'],
    );
    service.createMediaAccess.and.returnValue(
      of({
        token: 'signed',
        expiresAt: 123,
        snapshotId: 'snapshot-id',
        durationMs: 12_500,
        fileSizeBytes: 2_048,
        recording: true,
        playbackMode: 'active_snapshot',
        indexState: 'pending',
        retryAfterMs: null,
        requestId: 'request-1',
      }),
    );
    service.listDanmaku.and.returnValue(
      of({
        items: [
          {
            index: 0,
            progressMs: 1_250,
            mode: 1,
            fontSize: 25,
            color: 16_777_215,
            user: '观众甲',
            uid: 42,
            content: '第一条弹幕',
          },
          {
            index: 1,
            progressMs: 2_500,
            mode: 1,
            fontSize: 25,
            color: 16_777_215,
            user: null,
            uid: null,
            content: '第二条弹幕',
          },
        ],
        nextCursor: null,
      }),
    );
    service.mediaUrl.and.returnValue('/api/media?signed');
    player = jasmine.createSpyObj<PartPlayer>('PartPlayer', [
      'pause',
      'unload',
      'detachMediaElement',
      'destroy',
    ]);
    playerFactory = jasmine.createSpyObj<PartPlayerFactoryLike>(
      'PartPlayerFactoryLike',
      ['attachFlv'],
    );
    playerFactory.attachFlv.and.returnValue(player);
    playerLoader = jasmine
      .createSpy<PartPlayerLoader>('partPlayerLoader')
      .and.callFake(() => Promise.resolve(playerFactory));

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
        { provide: PART_PLAYER_LOADER, useValue: playerLoader },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(PartVideoDialogComponent);
    overlayContainer = TestBed.inject(OverlayContainer);
    fixture.componentRef.setInput('session', session);
    fixture.componentRef.setInput('part', part);
    fixture.componentRef.setInput('visible', true);
  });

  it('plays video and loads its danmaku in the same dialog', fakeAsync(() => {
    fixture.detectChanges();
    flushMicrotasks();

    expect(playerFactory.attachFlv).toHaveBeenCalledWith(
      jasmine.any(HTMLVideoElement),
      '/api/media?signed',
      {
        playbackMode: 'active_snapshot',
        durationMs: 12_500,
        fileSizeBytes: 2_048,
      },
      jasmine.any(Function),
    );
    expect(service.listDanmaku).toHaveBeenCalledOnceWith(2, 0, 500);
    expect(fixture.nativeElement.querySelector('[role="tablist"]')).toBeNull();
    const overlay = overlayContainer.getContainerElement();
    expect(overlay.textContent).toContain('第一条弹幕');
    expect(
      overlay.querySelector('[data-testid="danmaku-list"]'),
    ).not.toBeNull();
    fixture.componentInstance.handleClose();
  }));

  it('does not load the FLV runtime for a closed dialog', () => {
    fixture.componentRef.setInput('visible', false);

    fixture.detectChanges();

    expect(playerLoader).not.toHaveBeenCalled();
    expect(playerFactory.attachFlv).not.toHaveBeenCalled();
  });

  it('single-flights the FLV loader only after media access succeeds', fakeAsync(() => {
    const access = new Subject<RecordingMediaAccess>();
    service.createMediaAccess.and.returnValue(access);
    fixture.detectChanges();

    expect(playerLoader).not.toHaveBeenCalled();

    access.next({
      token: 'signed',
      expiresAt: 123,
      snapshotId: 'snapshot-id',
      durationMs: 12_500,
      fileSizeBytes: 2_048,
      recording: true,
      playbackMode: 'active_snapshot',
      indexState: 'pending',
      retryAfterMs: null,
      requestId: 'request-loader',
    });
    fixture.detectChanges();
    fixture.detectChanges();

    expect(playerLoader).toHaveBeenCalledTimes(1);

    flushMicrotasks();
    expect(playerFactory.attachFlv).toHaveBeenCalledTimes(1);
    fixture.componentInstance.handleClose();
  }));

  it('does not load the FLV runtime for native media', () => {
    fixture.componentRef.setInput('part', {
      ...part,
      finalPath: '/rec/p1.mp4',
      finalExists: true,
    });

    fixture.detectChanges();

    expect(playerLoader).not.toHaveBeenCalled();
    expect(playerFactory.attachFlv).not.toHaveBeenCalled();
  });

  it('shows an actionable error when the current FLV loader rejects', fakeAsync(() => {
    playerLoader.and.returnValue(
      Promise.reject(new Error('FLV 播放器代码加载失败')),
    );

    fixture.detectChanges();
    flushMicrotasks();
    fixture.detectChanges();

    expect(fixture.componentInstance.error).toBe('FLV 播放器代码加载失败');
    expect(playerFactory.attachFlv).not.toHaveBeenCalled();
  }));

  it('ignores a pending FLV loader after the dialog closes', fakeAsync(() => {
    const pending = deferred<PartPlayerFactoryLike>();
    playerLoader.and.returnValue(pending.promise);
    fixture.detectChanges();

    expect(playerLoader).toHaveBeenCalledTimes(1);

    fixture.componentInstance.handleClose();
    pending.resolve(playerFactory);
    flushMicrotasks();

    expect(playerFactory.attachFlv).not.toHaveBeenCalled();
  }));

  it('disposes a player invalidated by a synchronous attach event', fakeAsync(() => {
    playerFactory.attachFlv.and.callFake((_element, _url, _source, onEvent) => {
      onEvent({ type: 'error', message: '播放器同步失败' });
      return player;
    });

    fixture.detectChanges();
    flushMicrotasks();

    expect(fixture.componentInstance.error).toBe('播放器同步失败');
    expect(player.pause).toHaveBeenCalled();
    expect(player.unload).toHaveBeenCalled();
    expect(player.detachMediaElement).toHaveBeenCalled();
    expect(player.destroy).toHaveBeenCalled();
  }));

  it('bounds the rendered danmaku window for long recordings', () => {
    service.listDanmaku.and.returnValue(
      of({
        items: Array.from({ length: 1_001 }, (_value, index) => ({
          index,
          progressMs: index * 1_000,
          mode: 1,
          fontSize: 25,
          color: 16_777_215,
          user: null,
          uid: null,
          content: `弹幕 ${index}`,
        })),
        nextCursor: 1_001,
      }),
    );

    fixture.detectChanges();

    expect(fixture.componentInstance.danmakuItems.length).toBe(1_000);
    expect(fixture.componentInstance.danmakuItems[0].index).toBe(1);
    expect(
      overlayContainer
        .getContainerElement()
        .querySelectorAll('[data-testid="danmaku-line"]').length,
    ).toBe(1_000);
  });

  it('keeps manually paged danmaku at the requested window', () => {
    service.listDanmaku.and.callFake((_partId, cursor) => {
      const start = cursor ?? 0;
      return of({
        items: Array.from({ length: 500 }, (_value, offset) => ({
          index: start + offset,
          progressMs: (start + offset) * 1_000,
          mode: 1,
          fontSize: 25,
          color: 16_777_215,
          user: null,
          uid: null,
          content: `弹幕 ${start + offset}`,
        })),
        nextCursor: start + 500,
      });
    });
    fixture.detectChanges();

    fixture.componentInstance.loadMoreDanmaku();
    fixture.componentInstance.loadMoreDanmaku();
    const video = overlayContainer
      .getContainerElement()
      .querySelector('[data-testid="part-video"]') as HTMLVideoElement;
    video.currentTime = 0;
    video.dispatchEvent(new Event('timeupdate'));

    expect(service.listDanmaku.calls.count()).toBe(3);
    expect(fixture.componentInstance.danmakuItems.length).toBe(1_000);
    expect(fixture.componentInstance.danmakuItems[0].index).toBe(500);
    expect(fixture.componentInstance.danmakuItems[999].index).toBe(1_499);
  });

  it('highlights the danmaku nearest to the current video time', () => {
    fixture.detectChanges();
    const video = overlayContainer
      .getContainerElement()
      .querySelector('[data-testid="part-video"]') as HTMLVideoElement;

    video.currentTime = 2.6;
    video.dispatchEvent(new Event('timeupdate'));
    fixture.detectChanges();

    expect(fixture.componentInstance.activeDanmakuIndex).toBe(1);
    const active = overlayContainer
      .getContainerElement()
      .querySelector('[data-testid="danmaku-line"].active');
    expect(active?.textContent).toContain('第二条弹幕');
  });

  it('seeks the video when a danmaku line is selected', () => {
    fixture.detectChanges();
    const video = overlayContainer
      .getContainerElement()
      .querySelector('[data-testid="part-video"]') as HTMLVideoElement;
    const lines = overlayContainer
      .getContainerElement()
      .querySelectorAll('[data-testid="danmaku-line"]');

    (lines[0] as HTMLElement).click();

    expect(video.currentTime).toBeCloseTo(1.25, 2);
    expect(fixture.componentInstance.followDanmaku).toBeTrue();
  });

  it('uses finite player options when a growing FLV has no duration index', fakeAsync(() => {
    service.createMediaAccess.and.returnValue(
      of({
        token: 'signed',
        expiresAt: 123,
        snapshotId: null,
        durationMs: null,
        fileSizeBytes: 1_024,
        recording: true,
        playbackMode: 'sequential',
        indexState: 'pending',
        retryAfterMs: null,
        requestId: 'request-2',
      }),
    );

    fixture.detectChanges();
    flushMicrotasks();

    expect(playerFactory.attachFlv).toHaveBeenCalledWith(
      jasmine.any(HTMLVideoElement),
      '/api/media?signed',
      {
        playbackMode: 'sequential',
        durationMs: null,
        fileSizeBytes: 1_024,
      },
      jasmine.any(Function),
    );
    fixture.componentInstance.handleClose();
  }));

  it('marks the view after asynchronous media access completes', () => {
    const access = new Subject<{
      token: string;
      expiresAt: number;
      snapshotId: string;
      durationMs: number;
      fileSizeBytes: number;
      recording: boolean;
      playbackMode: 'active_snapshot';
      indexState: string;
      retryAfterMs: number | null;
      requestId: string;
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
      playbackMode: 'active_snapshot',
      indexState: 'pending',
      retryAfterMs: null,
      requestId: 'request-3',
    });

    expect(changeDetector.markForCheck).toHaveBeenCalled();
  });

  it('ends player loading after the first frame event', fakeAsync(() => {
    const callbacks: { report?: PartPlayerEventHandler } = {};
    playerFactory.attachFlv.and.callFake((_element, _url, _source, handler) => {
      callbacks.report = handler;
      return player;
    });
    fixture.detectChanges();
    flushMicrotasks();

    expect(fixture.componentInstance.playbackState.kind).toBe('player_loading');
    callbacks.report?.({ type: 'first_frame' });

    expect(fixture.componentInstance.playbackState.kind).toBe('playing');
  }));

  it('turns an endless player load into an actionable error', fakeAsync(() => {
    fixture.detectChanges();
    flushMicrotasks();

    tick(10_001);
    fixture.detectChanges();

    expect(fixture.componentInstance.playbackState.kind).toBe('error');
    expect(overlayContainer.getContainerElement().textContent).toContain(
      '本地视频打开超时',
    );
  }));

  it('destroys the FLV player when closed', fakeAsync(() => {
    fixture.detectChanges();
    flushMicrotasks();

    fixture.componentInstance.handleClose();

    expect(player.pause).toHaveBeenCalled();
    expect(player.unload).toHaveBeenCalled();
    expect(player.detachMediaElement).toHaveBeenCalled();
    expect(player.destroy).toHaveBeenCalled();
  }));

  it('surfaces native MP4 playback errors', () => {
    fixture.componentRef.setInput('part', {
      ...part,
      finalPath: '/rec/p1.mp4',
      finalExists: true,
    });
    fixture.detectChanges();

    const video = overlayContainer
      .getContainerElement()
      .querySelector('[data-testid="part-video"]') as HTMLVideoElement;
    video.dispatchEvent(new Event('error'));
    fixture.detectChanges();

    expect(overlayContainer.getContainerElement().textContent).toContain(
      '本地视频播放失败，请重新打开后再试',
    );
  });

  it('surfaces stalled native MP4 playback', () => {
    fixture.componentRef.setInput('part', {
      ...part,
      finalPath: '/rec/p1.mp4',
      finalExists: true,
    });
    fixture.detectChanges();

    const video = overlayContainer
      .getContainerElement()
      .querySelector('[data-testid="part-video"]') as HTMLVideoElement;
    video.dispatchEvent(new Event('stalled'));
    fixture.detectChanges();

    expect(overlayContainer.getContainerElement().textContent).toContain(
      '本地视频加载停滞，请检查连接后重试',
    );
  });
});
