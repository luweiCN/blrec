import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';

import { of, Subject } from 'rxjs';

import {
  RealtimeEvent,
  RealtimeService,
} from 'src/app/core/services/realtime.service';
import { RoomUploadPolicyRequest } from 'src/app/tasks/upload-policy-dialog/room-upload-policy.model';
import {
  PartPlayer,
  PartPlayerFactory,
} from '../part-video-dialog/part-player.factory';
import {
  HighlightClip,
  HighlightClipInspection,
  HighlightTimeline,
} from '../shared/highlight.model';
import { HighlightService } from '../shared/highlight.service';
import { RecordingSessionService } from '../shared/recording-session.service';
import { HighlightEditorComponent } from './highlight-editor.component';

@Component({ selector: 'app-upload-policy-dialog', template: '' })
class UploadPolicyDialogStubComponent {
  @Input() sessionId: number | null = null;
  @Input() roomId = 0;
  @Input() roomName = '';
  @Input() liveAreaName = '';
  @Input() liveParentAreaName = '';
  @Input() allowRestoreInherited = true;
  @Input() deferredSave = false;
  @Output() readonly closed = new EventEmitter<void>();
  @Output() readonly saved = new EventEmitter<void>();
  @Output() readonly settingsConfirmed =
    new EventEmitter<RoomUploadPolicyRequest>();
}

describe('HighlightEditorComponent', () => {
  let fixture: ComponentFixture<HighlightEditorComponent>;
  let component: HighlightEditorComponent;
  let highlights: jasmine.SpyObj<HighlightService>;
  let recordings: jasmine.SpyObj<RecordingSessionService>;
  let playerFactory: jasmine.SpyObj<PartPlayerFactory>;
  let player: jasmine.SpyObj<PartPlayer>;
  let realtime: Subject<RealtimeEvent>;

  const timeline: HighlightTimeline = {
    sessionId: 9,
    roomId: 100,
    durationMs: 180_000,
    stableEndMs: 170_000,
    parts: [
      {
        partId: 11,
        partIndex: 1,
        timelineStartMs: 0,
        durationMs: 90_000,
        stableEndMs: 90_000,
        recording: false,
        mediaKind: 'flv',
      },
      {
        partId: 12,
        partIndex: 2,
        timelineStartMs: 90_000,
        durationMs: 90_000,
        stableEndMs: 170_000,
        recording: true,
        mediaKind: 'flv',
      },
    ],
    markers: [
      {
        marker: {
          id: 7,
          roomId: 100,
          observedAtMs: 1_100_000,
          playerDelayMs: 0,
          contentAtMs: 1_100_000,
          title: '直播标题',
          anchorName: '主播',
          name: '精彩操作',
          note: '',
          source: 'browser_extension',
          createdAt: 1,
          updatedAt: 1,
        },
        partId: 12,
        localOffsetMs: 25_000,
        timelineOffsetMs: 115_000,
      },
    ],
  };

  const submissionSettings: RoomUploadPolicyRequest = {
    accountMode: 'primary',
    accountId: null,
    enabled: true,
    titleTemplate: '{{ title }} 精选',
    descriptionTemplate: '高光片段',
    partTitleTemplate: 'P{{ part_index }}',
    dynamicTemplate: '高光片段',
    tid: 21,
    tags: '高光,直播',
    creationStatementId: -1,
    originalAuthorization: false,
    source: '',
    isOnlySelf: false,
    publishDynamic: true,
    upSelectionReply: false,
    upCloseReply: false,
    upCloseDanmu: false,
    autoComment: true,
    danmakuBackfill: true,
    filters: {},
    collectionSeasonId: 20,
    collectionSectionId: 21,
    coverMode: 'live',
    coverAssetId: null,
    publishDelaySeconds: 0,
    retentionMode: 'submitted',
    retentionDays: 5,
  };

  const inspection: HighlightClipInspection = {
    requestedStartMs: 110_000,
    requestedEndMs: 130_000,
    actualStartMs: 98_000,
    actualEndMs: 130_000,
    extraLeadMs: 12_000,
    confirmationRequired: true,
    compatible: true,
    sources: [
      {
        partId: 12,
        actualStartMs: 8_000,
        actualEndMs: 40_000,
        outputOffsetMs: 0,
      },
    ],
  };

  const processingClip: HighlightClip = {
    id: 3,
    markerId: 7,
    roomId: 100,
    sourceSessionId: 9,
    uploadSessionId: null,
    name: '精彩操作',
    requestedStartMs: 110_000,
    requestedEndMs: 130_000,
    actualStartMs: 98_000,
    actualEndMs: 130_000,
    outputVideoPath: null,
    outputXmlPath: null,
    state: 'processing',
    confirmationRequired: true,
    confirmed: true,
    errorMessage: null,
    attempt: 1,
    createdAt: 1,
    updatedAt: 1,
    sources: [],
  };

  beforeEach(async () => {
    highlights = jasmine.createSpyObj<HighlightService>('HighlightService', [
      'getTimeline',
      'listClips',
      'inspectClip',
      'createClip',
      'getClip',
      'retryClip',
      'deleteClip',
      'prepareUploadSession',
      'createUploadTask',
      'createMediaAccess',
      'mediaUrl',
      'updateMarker',
      'deleteMarker',
    ]);
    highlights.getTimeline.and.returnValue(of(timeline));
    highlights.listClips.and.returnValue(of([]));
    highlights.inspectClip.and.returnValue(of(inspection));
    highlights.createClip.and.returnValue(of(processingClip));
    highlights.getClip.and.returnValue(
      of({
        ...processingClip,
        state: 'ready',
        outputVideoPath: '/rec/highlight-3.mp4',
      }),
    );
    highlights.retryClip.and.returnValue(
      of({ ...processingClip, state: 'queued', errorMessage: null }),
    );
    highlights.updateMarker.and.callFake((id, name, note) =>
      of({ ...timeline.markers[0].marker, id, name, note }),
    );
    highlights.deleteMarker.and.returnValue(of(void 0));
    highlights.createUploadTask.and.returnValue(of({ jobId: 44 }));
    highlights.prepareUploadSession.and.returnValue(of({ sessionId: 77 }));
    highlights.createMediaAccess.and.returnValue(
      of({ token: 'clip-token', expiresAt: 123, fileSizeBytes: 4096 }),
    );
    highlights.mediaUrl.and.returnValue('/api/highlight-media');

    recordings = jasmine.createSpyObj<RecordingSessionService>(
      'RecordingSessionService',
      ['createMediaAccess', 'mediaUrl', 'runJobAction'],
    );
    recordings.createMediaAccess.and.returnValue(
      of({
        token: 'signed',
        expiresAt: 123,
        snapshotId: 'snapshot',
        durationMs: 80_000,
        fileSizeBytes: 2048,
        recording: true,
        playbackMode: 'active_snapshot',
        indexState: 'pending',
        retryAfterMs: null,
        requestId: 'request-editor',
      }),
    );
    recordings.mediaUrl.and.callFake((partId) => `/media/${partId}`);
    recordings.runJobAction.and.returnValue(
      of({ results: [{ jobId: 44, accepted: true, message: '已继续上传' }] }),
    );

    player = jasmine.createSpyObj<PartPlayer>('PartPlayer', [
      'pause',
      'unload',
      'detachMediaElement',
      'destroy',
    ]);
    playerFactory = jasmine.createSpyObj<PartPlayerFactory>(
      'PartPlayerFactory',
      ['attachFlv'],
    );
    playerFactory.attachFlv.and.returnValue(player);
    realtime = new Subject<RealtimeEvent>();

    await TestBed.configureTestingModule({
      declarations: [HighlightEditorComponent, UploadPolicyDialogStubComponent],
      imports: [CommonModule, FormsModule, RouterTestingModule],
      providers: [
        { provide: HighlightService, useValue: highlights },
        { provide: RecordingSessionService, useValue: recordings },
        { provide: PartPlayerFactory, useValue: playerFactory },
        { provide: RealtimeService, useValue: { events$: realtime } },
        {
          provide: ActivatedRoute,
          useValue: {
            snapshot: {
              paramMap: { get: () => '9' },
              queryParamMap: { get: () => '11' },
            },
          },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(HighlightEditorComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('keeps the editor scoped to the selected recording part', () => {
    fixture.detectChanges();

    expect(component.selectedPart?.partId).toBe(11);
    expect(component.positionPercent(45_000)).toBe(50);
    expect(fixture.nativeElement.querySelectorAll('.marker-pin').length).toBe(
      0,
    );
  });

  it('does not silently fall back when the requested recording part is missing', () => {
    Object.defineProperty(component, 'initialPartId', { value: 999 });

    (
      component as unknown as { loadTimeline(initial: boolean): void }
    ).loadTimeline(true);

    expect(component.selectedPart).toBeNull();
    expect(component.error).toContain('本地录像已不存在');
  });

  it('requires a concrete recording part in the editor URL', () => {
    Object.defineProperty(component, 'initialPartId', { value: null });

    (
      component as unknown as { loadTimeline(initial: boolean): void }
    ).loadTimeline(true);

    expect(component.selectedPart).toBeNull();
    expect(component.error).toContain('具体分段');
  });

  it('drags the playhead within the selected recording part', () => {
    const track = fixture.nativeElement.querySelector(
      '.timeline-track',
    ) as HTMLElement;
    spyOn(track, 'getBoundingClientRect').and.returnValue({
      left: 0,
      width: 180,
      right: 180,
      top: 0,
      bottom: 54,
      height: 54,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    const start = {
      button: 0,
      pointerId: 1,
      clientX: 112,
      target: track,
      preventDefault: jasmine.createSpy('preventDefault'),
    } as unknown as PointerEvent;

    component.startTimelineDrag(start, track);

    expect(component.draggingPlayhead).toBeTrue();
    expect(component.playheadMs).toBe(56_000);

    component.moveTimelineDrag(
      { pointerId: 1, clientX: 140 } as PointerEvent,
      track,
    );
    expect(component.playheadMs).toBe(70_000);

    component.endTimelineDrag({ pointerId: 1 } as PointerEvent, track);
    expect(component.draggingPlayhead).toBeFalse();
  });

  it('adjusts the selected boundaries in whole seconds', () => {
    component.startMs = 10_000;
    component.endMs = 20_000;
    const video = fixture.nativeElement.querySelector(
      '[data-testid="editor-video"]',
    ) as HTMLVideoElement;
    spyOn(video, 'pause');

    component.adjustSelection('start', -1);
    expect(video.pause).toHaveBeenCalled();
    expect(video.currentTime).toBe(9);

    component.adjustSelection('end', 1);

    expect(component.startMs).toBe(9_000);
    expect(component.endMs).toBe(21_000);
    expect(video.currentTime).toBe(21);
  });

  it('keeps the selected P when previewing its exact end boundary', () => {
    component.startMs = 80_000;
    component.endMs = 89_000;

    component.adjustSelection('end', 1);

    expect(component.selectedPart?.partId).toBe(11);
    expect(component.playheadMs).toBe(90_000);
  });

  it('opens an existing clip on the timeline without changing it', () => {
    const readyClip: HighlightClip = {
      ...processingClip,
      state: 'ready',
      requestedStartMs: 10_000,
      requestedEndMs: 25_000,
      actualStartMs: 8_000,
      actualEndMs: 25_000,
      sources: [
        {
          partId: 11,
          ordinal: 0,
          requestedStartMs: 10_000,
          requestedEndMs: 25_000,
          actualStartMs: 8_000,
          actualEndMs: 25_000,
        },
      ],
    };

    component.selectClipForEditing(readyClip);

    expect(component.sourceClipId).toBe(3);
    expect(component.editingDraftId).toBeNull();
    expect(component.drafts).toEqual([]);
    expect(component.playheadMs).toBe(10_000);
  });

  it('uses one custom timeline without native controls or separate editors', () => {
    const video = fixture.nativeElement.querySelector(
      '[data-testid="editor-video"]',
    ) as HTMLVideoElement;

    expect(video.hasAttribute('controls')).toBeFalse();
    expect(
      fixture.nativeElement.querySelector('.timeline-capture-actions'),
    ).not.toBeNull();
    expect(fixture.nativeElement.querySelector('.selection-editor')).toBeNull();
    expect(fixture.nativeElement.querySelector('.draft-panel')).toBeNull();
    expect(fixture.nativeElement.querySelector('.marker-panel')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('按 I');
    expect(fixture.nativeElement.textContent).not.toContain('按 O');
  });

  it('does not render recording thumbnails on the timeline', () => {
    expect(fixture.nativeElement.querySelector('.thumbnail-strip')).toBeNull();
  });

  it('opens the first local recording and restores clips automatically', () => {
    expect(recordings.createMediaAccess).toHaveBeenCalledWith(11);
    expect(highlights.listClips).toHaveBeenCalledOnceWith(9);
  });

  it('places the timeline directly below the video preview', () => {
    const videoStage = fixture.nativeElement.querySelector(
      '.video-stage',
    ) as HTMLElement;
    const timelinePanel = fixture.nativeElement.querySelector(
      '.timeline-panel',
    ) as HTMLElement;

    expect(videoStage.nextElementSibling).toBe(timelinePanel);
    expect(timelinePanel.querySelector('.timeline-track')).not.toBeNull();
  });

  it('marks the view after the timeline loads asynchronously', () => {
    const timelineResponse = new Subject<HighlightTimeline>();
    highlights.getTimeline.and.returnValue(timelineResponse);
    const asyncFixture = TestBed.createComponent(HighlightEditorComponent);
    const asyncComponent = asyncFixture.componentInstance;
    const changeDetector = (asyncComponent as any).changeDetector;
    spyOn(changeDetector, 'markForCheck');
    asyncFixture.detectChanges();

    timelineResponse.next(timeline);

    expect(changeDetector.markForCheck).toHaveBeenCalled();
    asyncFixture.destroy();
  });

  it('blocks a range that reaches beyond the stable recording boundary', () => {
    component.selectPart(timeline.parts[1]);
    component.startMs = 160_000;
    component.endMs = 175_000;
    component.selectionActive = true;
    component.startBoundarySet = true;
    component.endBoundarySet = true;
    fixture.detectChanges();

    expect(component.selectionError).toBe(
      '结束位置仍在录制安全区之外，请稍后再试',
    );
    expect(fixture.nativeElement.textContent).toContain(
      '结束位置仍在录制安全区之外',
    );
  });

  it('automatically keeps multiple pending ranges on the timeline', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();

    component.clearTimelineSelection();
    component.playheadMs = 30_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 45_000;
    component.setSelectionEndFromPlayhead();

    expect(
      component.drafts.map((draft) => [draft.startMs, draft.endMs]),
    ).toEqual([
      [10_000, 20_000],
      [30_000, 45_000],
    ]);
    expect(component.editingDraftId).toBe(component.drafts[1].id);
  });

  it('deselects a pending range without deleting it when the track is used', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    const draftId = component.drafts[0].id;

    component.clearTimelineSelection();

    expect(component.drafts.map((draft) => draft.id)).toEqual([draftId]);
    expect(component.editingDraftId).toBeNull();
    expect(component.selectionActive).toBeFalse();
  });

  it('selects any blue pending range and adjusts only that range', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    const first = component.drafts[0];
    component.clearTimelineSelection();
    component.playheadMs = 30_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 45_000;
    component.setSelectionEndFromPlayhead();

    component.selectDraftForEditing(first);
    component.adjustSelection('start', 1);

    expect(component.editingDraftId).toBe(first.id);
    expect(component.drafts[0].startMs).toBe(11_000);
    expect(component.drafts[1].startMs).toBe(30_000);
  });

  it('copies a green created clip into a new blue pending range', () => {
    const readyClip: HighlightClip = {
      ...processingClip,
      state: 'ready',
      name: '已创建片段',
      requestedStartMs: 10_000,
      requestedEndMs: 25_000,
      sources: [
        {
          partId: 11,
          ordinal: 0,
          requestedStartMs: 10_000,
          requestedEndMs: 25_000,
          actualStartMs: 8_000,
          actualEndMs: 25_000,
        },
      ],
    };

    component.copyClipToDraft(readyClip);

    expect(component.drafts.length).toBe(1);
    expect(component.drafts[0].name).toBe('已创建片段 副本');
    expect(component.drafts[0].startMs).toBe(10_000);
    expect(component.drafts[0].endMs).toBe(25_000);
    expect(component.editingDraftId).toBe(component.drafts[0].id);
  });

  it('shows point actions only after pointer interaction finishes', () => {
    const track = fixture.nativeElement.querySelector(
      '.timeline-track',
    ) as HTMLElement;
    spyOn(track, 'getBoundingClientRect').and.returnValue({
      left: 0,
      width: 180,
      right: 180,
      top: 0,
      bottom: 54,
      height: 54,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    component.startTimelineDrag(
      {
        button: 0,
        pointerId: 2,
        clientX: 40,
        target: track,
        preventDefault: () => undefined,
      } as unknown as PointerEvent,
      track,
    );

    expect(component.timelinePopover.kind).toBe('none');

    component.endTimelineDrag({ pointerId: 2 } as PointerEvent, track);

    expect(component.timelinePopover.kind).toBe('point');
  });

  it('keeps an unfinished boundary while seeking the other endpoint', () => {
    const track = fixture.nativeElement.querySelector(
      '.timeline-track',
    ) as HTMLElement;
    spyOn(track, 'getBoundingClientRect').and.returnValue({
      left: 0,
      width: 180,
      right: 180,
      top: 0,
      bottom: 54,
      height: 54,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();

    component.startTimelineDrag(
      {
        button: 0,
        pointerId: 3,
        clientX: 40,
        target: track,
        preventDefault: () => undefined,
      } as unknown as PointerEvent,
      track,
    );
    component.endTimelineDrag({ pointerId: 3 } as PointerEvent, track);
    component.setPointAsBoundary('end');

    expect(component.drafts.length).toBe(1);
    expect(component.drafts[0].startMs).toBe(10_000);
    expect(component.drafts[0].endMs).toBe(20_000);
  });

  it('fills the inline name editor when a pending range is completed', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();

    expect(component.clipName).toBe(component.drafts[0].name);
    expect(component.clipName).toBe('高光片段 00:10');
  });

  it('shows marker editing only while the marker point itself is selected', () => {
    component.selectPart(timeline.parts[1]);
    component.selectMarker(timeline.markers[0]);

    expect(component.selectedTimelineMarker?.marker.id).toBe(7);

    component.setPointAsBoundary('start');
    component.timelinePopover = {
      kind: 'point',
      timeMs: 140_000,
      markerId: null,
    };

    expect(component.selectedTimelineMarker).toBeNull();
  });

  it('keeps the valid boundary when the other endpoint is invalid', () => {
    component.playheadMs = 20_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 10_000;

    component.setSelectionEndFromPlayhead();

    expect(component.startMs).toBe(20_000);
    expect(component.endBoundarySet).toBeFalse();
    expect(component.drafts).toEqual([]);
    expect(component.actionError).toBe('结束位置必须晚于开始位置');

    component.playheadMs = 30_000;
    component.setSelectionEndFromPlayhead();

    expect(component.drafts.length).toBe(1);
    expect(component.drafts[0].startMs).toBe(20_000);
    expect(component.drafts[0].endMs).toBe(30_000);
  });

  it('previews hover time without moving the playhead', () => {
    const track = fixture.nativeElement.querySelector(
      '.timeline-track',
    ) as HTMLElement;
    spyOn(track, 'getBoundingClientRect').and.returnValue({
      left: 0,
      width: 180,
      right: 180,
      top: 0,
      bottom: 54,
      height: 54,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    component.playheadMs = 10_000;

    component.handleTimelineHover({ clientX: 90 } as MouseEvent, track);

    expect(component.hoverTimeMs).toBe(45_000);
    expect(component.playheadMs).toBe(10_000);
  });

  it('cancels only the selected pending range', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    const firstId = component.drafts[0].id;
    component.clearTimelineSelection();
    component.playheadMs = 30_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 40_000;
    component.setSelectionEndFromPlayhead();

    component.cancelSelectedDraft();

    expect(component.drafts.map((draft) => draft.id)).toEqual([firstId]);
    expect(component.editingDraftId).toBeNull();
  });

  it('checks a range automatically and asks only when keyframes need confirmation', () => {
    const partInspection: HighlightClipInspection = {
      ...inspection,
      requestedStartMs: 10_000,
      requestedEndMs: 30_000,
      actualStartMs: 0,
      actualEndMs: 30_000,
      extraLeadMs: 10_000,
      sources: [
        {
          partId: 11,
          actualStartMs: 0,
          actualEndMs: 30_000,
          outputOffsetMs: 0,
        },
      ],
    };
    highlights.inspectClip.and.returnValue(of(partInspection));
    component.startMs = 10_000;
    component.endMs = 30_000;
    component.clipName = '';
    component.selectionActive = true;
    component.startBoundarySet = true;
    component.endBoundarySet = true;
    component.addDraft();
    const draft = component.drafts[0];
    component.createDraft(draft);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('实际会从 00:00 开始');
    expect(fixture.nativeElement.textContent).not.toContain('检查裁剪范围');
    expect(highlights.createClip).not.toHaveBeenCalled();

    component.confirmDraft(draft);

    expect(highlights.createClip).toHaveBeenCalledWith(9, {
      markerId: null,
      name: '高光片段 00:10',
      startMs: 10_000,
      endMs: 30_000,
      confirmKeyframe: true,
    });
  });

  it('updates the active clip from the shared realtime stream', () => {
    component.clips = [processingClip];

    realtime.next({
      type: 'highlight_progress',
      data: {
        clips: [
          {
            id: 3,
            roomId: 100,
            name: '精彩操作',
            state: 'ready',
            attempt: 1,
            errorMessage: null,
            updatedAt: 2,
          },
        ],
      },
    });

    expect(highlights.getClip).toHaveBeenCalledOnceWith(3);
    expect(component.clip?.state).toBe('ready');
  });

  it('retries a failed clip without recreating its range', () => {
    const failed = {
      ...processingClip,
      state: 'failed' as const,
      errorMessage: 'ffprobe failed',
    };
    component.clips = [failed];

    component.retryClip(failed);

    expect(highlights.retryClip).toHaveBeenCalledOnceWith(3);
    expect(component.clips[0].state).toBe('queued');
  });

  it('selects the following part at an exact adjacent-part boundary', () => {
    const part = component['partAt'](90_000);

    expect(part?.partId).toBe(12);
  });

  it('updates a clip upload state from the shared realtime stream', () => {
    component.clips = [
      {
        ...processingClip,
        state: 'ready',
        uploadJobId: 44,
        uploadState: 'uploading',
        uploadPercent: 25,
      },
    ];

    realtime.next({
      type: 'upload_progress',
      data: {
        jobs: [
          {
            jobId: 44,
            state: 'waiting_review',
            percent: 100,
            bvid: 'BV1test',
          },
        ],
      },
    });

    expect(component.clips[0].uploadState).toBe('waiting_review');
    expect(component.clips[0].uploadPercent).toBe(100);
    expect(component.clips[0].uploadBvid).toBe('BV1test');
  });

  it('opens complete submission settings before creating an upload task', () => {
    const readyClip = {
      ...processingClip,
      state: 'ready' as const,
      outputVideoPath: '/rec/highlight-3.mp4',
    };
    component.clips = [readyClip];

    component.openClipSubmission(readyClip);

    expect(highlights.prepareUploadSession).not.toHaveBeenCalled();
    expect(component.submissionClip?.id).toBe(3);
    expect(highlights.createUploadTask).not.toHaveBeenCalled();

    component.clipSubmissionSaved(submissionSettings);

    expect(highlights.createUploadTask).toHaveBeenCalledOnceWith(
      3,
      submissionSettings,
    );
    expect(recordings.runJobAction).not.toHaveBeenCalled();
    expect(component.clips[0].uploadJobId).toBe(44);
    expect(component.clips[0].uploadState).toBe('ready');
  });

  it('previews a ready clip through its signed range URL', () => {
    const readyClip = {
      ...processingClip,
      state: 'ready' as const,
      outputVideoPath: '/rec/highlight-3.mp4',
    };

    component.openClipPreview(readyClip);

    expect(highlights.createMediaAccess).toHaveBeenCalledOnceWith(3);
    expect(highlights.mediaUrl).toHaveBeenCalledWith(3, {
      token: 'clip-token',
      expiresAt: 123,
      fileSizeBytes: 4096,
    });
    expect(component.clipPreviewUrl).toBe('/api/highlight-media');
  });

  it('renames and deletes a marker without changing the clip range', () => {
    component.beginMarkerEdit(timeline.markers[0]);
    component.markerName = '新的名称';
    component.markerNote = '备注';
    component.saveMarker();

    expect(highlights.updateMarker).toHaveBeenCalledWith(7, '新的名称', '备注');

    component.deleteMarker(timeline.markers[0]);
    expect(highlights.deleteMarker).toHaveBeenCalledOnceWith(7);
    expect(component.timeline?.markers.length).toBe(0);
  });

  it('destroys the FLV player with the page', () => {
    fixture.destroy();

    expect(player.pause).toHaveBeenCalled();
    expect(player.unload).toHaveBeenCalled();
    expect(player.detachMediaElement).toHaveBeenCalled();
    expect(player.destroy).toHaveBeenCalled();
  });
});
