import { CommonModule } from '@angular/common';
import { CdkConnectedOverlay, OverlayModule } from '@angular/cdk/overlay';
import { Component, EventEmitter, Input, Output } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';

import { of, Subject } from 'rxjs';
import { NzToolTipModule, NzTooltipDirective } from 'ng-zorro-antd/tooltip';

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
      imports: [
        CommonModule,
        FormsModule,
        OverlayModule,
        RouterTestingModule,
        NzToolTipModule,
      ],
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

    component.handleTimelinePointerMove(
      { pointerId: 1, clientX: 140 } as PointerEvent,
      track,
    );
    expect(component.playheadMs).toBe(70_000);

    component.endTimelineDrag({ pointerId: 1 } as PointerEvent, track);
    expect(component.draggingPlayhead).toBeFalse();
  });

  it('ignores pointer actions in the unstable recording tail', () => {
    component.selectPart(timeline.parts[1]);
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
    const initialPlayhead = component.playheadMs;

    component.startTimelineDrag(
      {
        button: 0,
        pointerId: 2,
        clientX: 178,
        target: track,
        preventDefault: jasmine.createSpy('preventDefault'),
      } as unknown as PointerEvent,
      track,
    );

    expect(component.draggingPlayhead).toBeFalse();
    expect(component.playheadMs).toBe(initialPlayhead);
    expect(component.timelinePopover.kind).toBe('none');
  });

  it('does not snap a stable pointer position to an unstable highpoint', () => {
    component.timeline = {
      ...timeline,
      markers: [
        {
          ...timeline.markers[0],
          localOffsetMs: 81_000,
          timelineOffsetMs: 171_000,
        },
      ],
    };
    component.selectPart(timeline.parts[1]);
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
        pointerId: 4,
        clientX: 156,
        target: track,
        preventDefault: jasmine.createSpy('preventDefault'),
      } as unknown as PointerEvent,
      track,
    );

    expect(component.playheadMs).toBe(168_000);
  });

  it('does not carry an unstable highpoint into a later valid range', () => {
    const unstableMarker = {
      ...timeline.markers[0],
      localOffsetMs: 81_000,
      timelineOffsetMs: 171_000,
    };
    component.timeline = { ...timeline, markers: [unstableMarker] };
    component.selectPart(timeline.parts[1]);
    component.selectMarker(unstableMarker);

    component.setPointAsBoundary('start');

    expect(component.selectionActive).toBeFalse();
    expect(component.startBoundarySet).toBeFalse();

    component.playheadMs = 150_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 160_000;
    component.setSelectionEndFromPlayhead();

    expect(component.drafts[0].markerId).toBeNull();
  });

  it('adjusts the selected boundaries in whole seconds', () => {
    component.startMs = 10_000;
    component.endMs = 20_000;
    component.selectionActive = true;
    component.startBoundarySet = true;
    component.endBoundarySet = true;
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

  it('adjusts only a boundary that has already been set', () => {
    component.startMs = 10_000;
    component.endMs = 0;
    component.selectionActive = true;
    component.startBoundarySet = true;
    component.endBoundarySet = false;

    component.adjustSelection('start', 1);

    expect(component.startMs).toBe(11_000);
    expect(component.endBoundarySet).toBeFalse();
    expect(component.drafts).toEqual([]);
  });

  it('keeps a valid boundary when a one-second adjustment would cross it', () => {
    component.startMs = 10_000;
    component.endMs = 10_500;
    component.selectionActive = true;
    component.startBoundarySet = true;
    component.endBoundarySet = true;

    component.adjustSelection('start', 1);

    expect(component.startMs).toBe(10_000);
    expect(component.endMs).toBe(10_500);
    expect(component.actionError).toBe('开始位置必须早于结束位置');
  });

  it('does not overwrite a valid draft with an unstable playback position', () => {
    component.selectPart(timeline.parts[1]);
    component.playheadMs = 110_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 120_000;
    component.setSelectionEndFromPlayhead();
    const draft = component.drafts[0];

    component.playheadMs = 175_000;
    component.setSelectionEndFromPlayhead();
    component.clearTimelineSelection();

    expect(draft.endMs).toBe(120_000);
    expect(component.actionError).toBe('该位置仍在录制安全区之外，请稍后再试');
  });

  it('reports an adjustment beyond the stable endpoint instead of clamping', () => {
    component.selectPart(timeline.parts[1]);
    component.startMs = 160_000;
    component.endMs = 170_000;
    component.selectionActive = true;
    component.startBoundarySet = true;
    component.endBoundarySet = true;

    component.adjustSelection('end', 1);

    expect(component.endMs).toBe(170_000);
    expect(component.actionError).toBe('该位置仍在录制安全区之外，请稍后再试');
  });

  it('keeps the selected P when previewing its exact end boundary', () => {
    component.startMs = 80_000;
    component.endMs = 89_000;
    component.selectionActive = true;
    component.startBoundarySet = true;
    component.endBoundarySet = true;

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
      fixture.nativeElement.querySelectorAll('.timeline-track').length,
    ).toBe(1);
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

  it('shows handles only for the pending range clicked in the timeline', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    const first = component.drafts[0];
    component.clearTimelineSelection();
    component.playheadMs = 30_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 40_000;
    component.setSelectionEndFromPlayhead();
    fixture.detectChanges();

    const ranges = fixture.nativeElement.querySelectorAll(
      '.draft-range',
    ) as NodeListOf<HTMLButtonElement>;
    ranges[0].click();
    fixture.detectChanges();

    expect(component.editingDraftId).toBe(first.id);
    expect(
      fixture.nativeElement.querySelectorAll('.draft-range.active').length,
    ).toBe(1);
    expect(
      fixture.nativeElement.querySelectorAll('.selection-boundary').length,
    ).toBe(2);
  });

  it('persists a pending range name before selecting another range', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    const first = component.drafts[0];
    component.clearTimelineSelection();
    component.playheadMs = 30_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 40_000;
    component.setSelectionEndFromPlayhead();
    const second = component.drafts[1];
    component.selectDraftForEditing(first);
    component.clipName = '重新命名的片段';

    component.selectDraftForEditing(second);

    expect(component.drafts[0].name).toBe('重新命名的片段');
    expect(component.editingDraftId).toBe(second.id);
  });

  it('keeps an edited name when the selected pending range is clicked again', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    const draft = component.drafts[0];
    component.clipName = '不要丢失的名称';

    component.selectDraftForEditing(draft);

    expect(component.clipName).toBe('不要丢失的名称');
    expect(component.drafts[0].name).toBe('不要丢失的名称');
  });

  it('locks a pending range while it is being created', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    const draft = component.drafts[0];
    draft.state = 'creating';
    fixture.detectChanges();
    component.playheadMs = 12_000;

    component.setSelectionStartFromPlayhead();
    component.adjustSelection('end', 1);
    fixture.detectChanges();

    expect(draft.startMs).toBe(10_000);
    expect(draft.endMs).toBe(20_000);
    expect(component.selectedDraftLocked).toBeTrue();
    expect(
      (
        document.body.querySelector(
          '.timeline-popover input',
        ) as HTMLInputElement
      ).readOnly,
    ).toBeTrue();
    expect(
      Array.from<HTMLButtonElement>(
        fixture.nativeElement.querySelectorAll(
          '.timeline-capture-actions button, .selection-boundary',
        ),
      ).every((button) => button.disabled),
    ).toBeTrue();
  });

  it('keeps the submitted range stable while inspection is in flight', () => {
    const inspectionResponse = new Subject<HighlightClipInspection>();
    highlights.inspectClip.and.returnValue(inspectionResponse);
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    const draft = component.drafts[0];

    component.createSelectedDraft();
    component.playheadMs = 12_000;
    component.setSelectionStartFromPlayhead();
    component.adjustSelection('end', 1);

    expect(component.selectedDraftLocked).toBeTrue();
    expect([draft.startMs, draft.endMs]).toEqual([10_000, 20_000]);

    inspectionResponse.next({
      ...inspection,
      requestedStartMs: 10_000,
      requestedEndMs: 20_000,
      actualStartMs: 10_000,
      actualEndMs: 20_000,
      extraLeadMs: 0,
      confirmationRequired: false,
      sources: [
        {
          partId: 11,
          actualStartMs: 10_000,
          actualEndMs: 20_000,
          outputOffsetMs: 0,
        },
      ],
    });

    expect(highlights.createClip).toHaveBeenCalledWith(
      9,
      jasmine.objectContaining({ startMs: 10_000, endMs: 20_000 }),
    );
  });

  it('turns a blue pending range green after the clip is created', () => {
    highlights.inspectClip.and.returnValue(
      of({
        ...inspection,
        requestedStartMs: 10_000,
        requestedEndMs: 20_000,
        actualStartMs: 10_000,
        actualEndMs: 20_000,
        extraLeadMs: 0,
        confirmationRequired: false,
        sources: [
          {
            partId: 11,
            actualStartMs: 10_000,
            actualEndMs: 20_000,
            outputOffsetMs: 0,
          },
        ],
      }),
    );
    highlights.createClip.and.returnValue(
      of({
        ...processingClip,
        requestedStartMs: 10_000,
        requestedEndMs: 20_000,
        actualStartMs: 10_000,
        actualEndMs: 20_000,
        sources: [
          {
            partId: 11,
            ordinal: 0,
            requestedStartMs: 10_000,
            requestedEndMs: 20_000,
            actualStartMs: 10_000,
            actualEndMs: 20_000,
          },
        ],
      }),
    );
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('.draft-range').length).toBe(
      1,
    );

    component.createSelectedDraft();
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelectorAll('.draft-range').length).toBe(
      0,
    );
    expect(fixture.nativeElement.querySelectorAll('.clip-range').length).toBe(
      1,
    );
  });

  it('keeps pending ranges clickable above overlapping created clips', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    component.clips = [
      {
        ...processingClip,
        state: 'ready',
        requestedStartMs: 10_000,
        requestedEndMs: 25_000,
        actualStartMs: 10_000,
        actualEndMs: 25_000,
        sources: [
          {
            partId: 11,
            ordinal: 0,
            requestedStartMs: 10_000,
            requestedEndMs: 25_000,
            actualStartMs: 10_000,
            actualEndMs: 25_000,
          },
        ],
      },
    ];
    fixture.detectChanges();

    const draft = fixture.nativeElement.querySelector(
      '.draft-range',
    ) as HTMLElement;
    const clip = fixture.nativeElement.querySelector(
      '.clip-range',
    ) as HTMLElement;

    expect(Number(getComputedStyle(draft).zIndex)).toBeGreaterThan(
      Number(getComputedStyle(clip).zIndex),
    );
  });

  it('keeps timeline item pointer events from falling through to the track', () => {
    component.selectPart(timeline.parts[1]);
    component.playheadMs = 110_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 120_000;
    component.setSelectionEndFromPlayhead();
    component.clips = [
      {
        ...processingClip,
        state: 'ready',
        requestedStartMs: 110_000,
        requestedEndMs: 125_000,
        actualStartMs: 110_000,
        actualEndMs: 125_000,
        sources: [
          {
            partId: 12,
            ordinal: 0,
            requestedStartMs: 20_000,
            requestedEndMs: 35_000,
            actualStartMs: 20_000,
            actualEndMs: 35_000,
          },
        ],
      },
    ];
    fixture.detectChanges();
    const startDrag = spyOn(component, 'startTimelineDrag');
    const items = fixture.nativeElement.querySelectorAll(
      '.draft-range, .clip-range, .selection-boundary',
    ) as NodeListOf<HTMLElement>;

    items.forEach((item, index) =>
      item.dispatchEvent(
        new PointerEvent('pointerdown', {
          bubbles: true,
          button: 0,
          pointerId: index + 10,
        }),
      ),
    );

    expect(items.length).toBe(4);
    expect(startDrag).not.toHaveBeenCalled();
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

  it('does not create a pending range with an empty inline name', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    component.clipName = '';
    fixture.detectChanges();

    const create = Array.from<HTMLButtonElement>(
      document.body.querySelectorAll('.timeline-popover button'),
    ).find((button) => button.textContent?.trim() === '创建片段');

    expect(create?.disabled).toBeTrue();

    component.createSelectedDraft();

    expect(highlights.inspectClip).not.toHaveBeenCalled();
    expect(component.actionError).toBe('请输入片段名称');
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

  it('clicks a highpoint at its exact time without starting a track drag', () => {
    component.selectPart(timeline.parts[1]);
    fixture.detectChanges();
    const video = fixture.nativeElement.querySelector(
      '[data-testid="editor-video"]',
    ) as HTMLVideoElement;
    const pause = spyOn(video, 'pause');
    const startDrag = spyOn(component, 'startTimelineDrag');
    const marker = fixture.nativeElement.querySelector(
      '.marker-pin',
    ) as HTMLButtonElement;

    marker.dispatchEvent(
      new PointerEvent('pointerdown', {
        bubbles: true,
        button: 0,
        pointerId: 3,
      }),
    );
    marker.click();

    expect(startDrag).not.toHaveBeenCalled();
    expect(pause).toHaveBeenCalled();
    expect(component.playheadMs).toBe(115_000);
    expect(component.timelinePopover).toEqual({
      kind: 'point',
      timeMs: 115_000,
      markerId: 7,
    });
  });

  it('keeps the valid boundary when the other endpoint is invalid', () => {
    component.playheadMs = 20_000;
    component.setSelectionStartFromPlayhead();
    component.timelinePopover = {
      kind: 'point',
      timeMs: 10_000,
      markerId: null,
    };

    component.setPointAsBoundary('end');

    expect(component.startMs).toBe(20_000);
    expect(component.endBoundarySet).toBeFalse();
    expect(component.drafts).toEqual([]);
    expect(component.actionError).toBe('结束位置必须晚于开始位置');
    expect(component.timelinePopover.kind).toBe('point');

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

    component.handleTimelinePointerMove(
      { clientX: 90, pointerId: 9 } as PointerEvent,
      track,
    );

    expect(component.hoverTimeMs).toBe(45_000);
    expect(component.playheadMs).toBe(10_000);
  });

  it('keeps hover guidance after a drag until the pointer leaves the track', () => {
    const track = fixture.nativeElement.querySelector(
      '.timeline-track',
    ) as HTMLElement;
    spyOn(track, 'getBoundingClientRect').and.returnValue({
      left: 0,
      width: 180,
      right: 180,
      top: 0,
      bottom: 92,
      height: 92,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    component.startTimelineDrag(
      {
        button: 0,
        pointerId: 5,
        clientX: 40,
        target: track,
        preventDefault: jasmine.createSpy('preventDefault'),
      } as unknown as PointerEvent,
      track,
    );
    component.endTimelineDrag({ pointerId: 5 } as PointerEvent, track);

    component.handleTimelinePointerMove(
      { pointerId: 5, clientX: 100 } as PointerEvent,
      track,
    );
    expect(component.hoverTimeMs).toBe(50_000);

    component.clearTimelineHover();
    expect(component.hoverTimeMs).toBeNull();
  });

  it('aligns pending and created ranges with their explicit lanes', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    component.clips = [
      {
        ...processingClip,
        state: 'ready',
        requestedStartMs: 30_000,
        requestedEndMs: 45_000,
        actualStartMs: 30_000,
        actualEndMs: 45_000,
        sources: [
          {
            partId: 11,
            ordinal: 0,
            requestedStartMs: 30_000,
            requestedEndMs: 45_000,
            actualStartMs: 30_000,
            actualEndMs: 45_000,
          },
        ],
      },
    ];
    fixture.detectChanges();

    const base = fixture.nativeElement.querySelector(
      '.track-base',
    ) as HTMLElement;
    const draft = fixture.nativeElement.querySelector(
      '.draft-range',
    ) as HTMLElement;
    const createdLane = fixture.nativeElement.querySelector(
      '.created-lane',
    ) as HTMLElement;
    const clip = fixture.nativeElement.querySelector(
      '.clip-range',
    ) as HTMLElement;

    expect(getComputedStyle(draft).top).toBe(getComputedStyle(base).top);
    expect(getComputedStyle(draft).height).toBe(getComputedStyle(base).height);
    expect(getComputedStyle(clip).top).toBe(getComputedStyle(createdLane).top);
    expect(getComputedStyle(clip).height).toBe(
      getComputedStyle(createdLane).height,
    );
    const boundary = fixture.nativeElement.querySelector(
      '.selection-boundary',
    ) as HTMLElement;
    const boundaryStyle = getComputedStyle(boundary);
    const createdStyle = getComputedStyle(createdLane);
    expect(
      parseFloat(boundaryStyle.top) + parseFloat(boundaryStyle.height),
    ).toBeGreaterThanOrEqual(
      parseFloat(createdStyle.top) + parseFloat(createdStyle.height),
    );
  });

  it('renders timeline actions and marker help through global overlays', () => {
    component.selectPart(timeline.parts[1]);
    component.timelinePopover = {
      kind: 'point',
      timeMs: 115_000,
      markerId: 7,
    };
    fixture.detectChanges();

    const workbench = fixture.nativeElement.querySelector(
      '.editor-workbench',
    ) as HTMLElement;
    const marker = fixture.debugElement.query(By.css('.marker-pin'));

    expect(workbench.querySelector('.timeline-popover')).toBeNull();
    expect(
      document.body.querySelector('.cdk-overlay-container .timeline-popover'),
    ).not.toBeNull();
    expect(marker.injector.get(NzTooltipDirective, null)).not.toBeNull();
  });

  it('repositions the open timeline overlay when its anchor moves', () => {
    component.selectPart(timeline.parts[1]);
    component.selectMarker(timeline.markers[0]);
    fixture.detectChanges();
    const overlay = (
      component as unknown as {
        timelineOverlay?: CdkConnectedOverlay;
      }
    ).timelineOverlay;
    if (!overlay) {
      throw new Error('expected a connected timeline overlay');
    }
    if (!overlay.overlayRef) {
      throw new Error('expected an open timeline overlay');
    }
    const updatePosition = spyOn(overlay.overlayRef, 'updatePosition');

    component.selectMarker({
      ...timeline.markers[0],
      localOffsetMs: 35_000,
      timelineOffsetMs: 125_000,
    });

    expect(updatePosition).toHaveBeenCalled();
  });

  it('keeps the custom playhead synchronized with video playback', () => {
    const video = fixture.nativeElement.querySelector(
      '[data-testid="editor-video"]',
    ) as HTMLVideoElement;
    video.currentTime = 12;

    component.handleTimeUpdate();

    expect(component.playheadMs).toBe(12_000);
  });

  it('pauses playback once from the custom control', () => {
    const video = fixture.nativeElement.querySelector(
      '[data-testid="editor-video"]',
    ) as HTMLVideoElement;
    Object.defineProperty(video, 'paused', {
      configurable: true,
      value: false,
    });
    const pause = spyOn(video, 'pause');

    component.togglePlayback();

    expect(pause).toHaveBeenCalledTimes(1);
  });

  it('keeps the custom timeline visible in fullscreen mode', () => {
    const workbench = fixture.nativeElement.querySelector(
      '.editor-workbench',
    ) as HTMLElement;
    const requestFullscreen = jasmine
      .createSpy('requestFullscreen')
      .and.returnValue(Promise.resolve());
    Object.defineProperty(workbench, 'requestFullscreen', {
      value: requestFullscreen,
    });

    component.toggleFullscreen();

    expect(requestFullscreen).toHaveBeenCalled();
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

    const popoverText = document.body.querySelector(
      '.timeline-popover',
    )?.textContent;
    expect(popoverText).toContain('实际会从 00:00 开始');
    expect(popoverText).not.toContain('检查裁剪范围');
    expect(highlights.createClip).not.toHaveBeenCalled();

    component.clipName = '';
    component.confirmDraft(draft);

    expect(highlights.createClip).not.toHaveBeenCalled();
    expect(component.actionError).toBe('请输入片段名称');

    component.clipName = '重命名片段';
    component.confirmDraft(draft);

    expect(highlights.createClip).toHaveBeenCalledWith(9, {
      markerId: null,
      name: '重命名片段',
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

  it('removes a deleted marker from pending and created clip associations', () => {
    component.selectPart(timeline.parts[1]);
    component.selectMarker(timeline.markers[0]);
    component.setPointAsBoundary('start');
    component.playheadMs = 130_000;
    component.setSelectionEndFromPlayhead();
    component.clips = [
      {
        ...processingClip,
        markerId: 7,
        state: 'ready',
        requestedStartMs: 115_000,
        requestedEndMs: 130_000,
        actualStartMs: 115_000,
        actualEndMs: 130_000,
        sources: [
          {
            partId: 12,
            ordinal: 0,
            requestedStartMs: 25_000,
            requestedEndMs: 40_000,
            actualStartMs: 25_000,
            actualEndMs: 40_000,
          },
        ],
      },
    ];

    component.deleteMarker(timeline.markers[0]);

    expect(component.drafts[0].markerId).toBeNull();
    expect(component.clips[0].markerId).toBeNull();
    expect(component.selectedMarkerId).toBeNull();
  });

  it('does not reuse a deleted highpoint from its open timeline popover', () => {
    component.selectPart(timeline.parts[1]);
    component.selectMarker(timeline.markers[0]);

    component.deleteMarker(timeline.markers[0]);

    expect(component.timelinePopover).toEqual({
      kind: 'point',
      timeMs: 115_000,
      markerId: null,
    });

    component.setPointAsBoundary('start');
    component.playheadMs = 130_000;
    component.setSelectionEndFromPlayhead();

    expect(component.drafts[0].markerId).toBeNull();
  });

  it('destroys the FLV player with the page', () => {
    fixture.destroy();

    expect(player.pause).toHaveBeenCalled();
    expect(player.unload).toHaveBeenCalled();
    expect(player.detachMediaElement).toHaveBeenCalled();
    expect(player.destroy).toHaveBeenCalled();
  });
});
