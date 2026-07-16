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
import { PartPlayer, PartPlayerFactory } from '../part-video-dialog/part-player.factory';
import {
  HighlightClip,
  HighlightClipInspection,
  HighlightTimeline,
} from '../shared/highlight.model';
import { HighlightService } from '../shared/highlight.service';
import { RecordingSessionService } from '../shared/recording-session.service';
import { HighlightEditorComponent } from './highlight-editor.component';

@Component({ selector: 'app-task-edit-dialog', template: '' })
class TaskEditDialogStubComponent {
  @Input() visible = false;
  @Input() jobIds: readonly number[] = [];
  @Output() readonly closed = new EventEmitter<void>();
  @Output() readonly saved = new EventEmitter<void>();
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
        stableEndMs: 80_000,
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
      'deleteClip',
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
      })
    );
    highlights.updateMarker.and.callFake((id, name, note) =>
      of({ ...timeline.markers[0].marker, id, name, note })
    );
    highlights.deleteMarker.and.returnValue(of(void 0));
    highlights.createUploadTask.and.returnValue(of({ jobId: 44 }));
    highlights.createMediaAccess.and.returnValue(
      of({ token: 'clip-token', expiresAt: 123, fileSizeBytes: 4096 })
    );
    highlights.mediaUrl.and.returnValue('/api/highlight-media');

    recordings = jasmine.createSpyObj<RecordingSessionService>(
      'RecordingSessionService',
      ['createMediaAccess', 'mediaUrl', 'runJobAction']
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
      })
    );
    recordings.mediaUrl.and.callFake((partId) => `/media/${partId}`);
    recordings.runJobAction.and.returnValue(
      of({ results: [{ jobId: 44, accepted: true, message: '已继续上传' }] })
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
    realtime = new Subject<RealtimeEvent>();

    await TestBed.configureTestingModule({
      declarations: [HighlightEditorComponent, TaskEditDialogStubComponent],
      imports: [CommonModule, FormsModule, RouterTestingModule],
      providers: [
        { provide: HighlightService, useValue: highlights },
        { provide: RecordingSessionService, useValue: recordings },
        { provide: PartPlayerFactory, useValue: playerFactory },
        { provide: RealtimeService, useValue: { events$: realtime } },
        {
          provide: ActivatedRoute,
          useValue: {
            snapshot: { paramMap: { get: () => '9' } },
          },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(HighlightEditorComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('switches to the marker part and seeks to its local position', () => {
    component.selectMarker(timeline.markers[0]);
    fixture.detectChanges();

    expect(component.selectedPart?.partId).toBe(12);
    expect(component.playheadMs).toBe(115_000);
    expect(recordings.createMediaAccess).toHaveBeenCalledWith(12);
    expect(component.selectedMarkerId).toBe(7);
  });

  it('opens the first local recording and restores clips automatically', () => {
    expect(recordings.createMediaAccess).toHaveBeenCalledWith(11);
    expect(highlights.listClips).toHaveBeenCalledOnceWith(9);
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
    component.startMs = 160_000;
    component.endMs = 175_000;
    component.selectionChanged();
    fixture.detectChanges();

    const add = fixture.nativeElement.querySelector(
      '[data-testid="add-draft"]'
    ) as HTMLButtonElement;
    expect(add.disabled).toBeTrue();
    expect(fixture.nativeElement.textContent).toContain(
      '结束位置仍在录制安全区之外'
    );
  });

  it('adds multiple ranges from the current playhead', () => {
    component.playheadMs = 10_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 20_000;
    component.setSelectionEndFromPlayhead();
    component.addDraft();

    component.playheadMs = 30_000;
    component.setSelectionStartFromPlayhead();
    component.playheadMs = 45_000;
    component.setSelectionEndFromPlayhead();
    component.addDraft();

    expect(component.drafts.map((draft) => [draft.startMs, draft.endMs])).toEqual([
      [10_000, 20_000],
      [30_000, 45_000],
    ]);
  });

  it('checks a range automatically and asks only when keyframes need confirmation', () => {
    component.startMs = 110_000;
    component.endMs = 130_000;
    component.selectionChanged();
    component.addDraft();
    const draft = component.drafts[0];
    component.createDraft(draft);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('选择范围 01:50–02:10');
    expect(fixture.nativeElement.textContent).toContain('实际范围 01:38–02:10');
    expect(fixture.nativeElement.textContent).not.toContain('检查裁剪范围');
    expect(highlights.createClip).not.toHaveBeenCalled();

    component.confirmDraft(draft);

    expect(highlights.createClip).toHaveBeenCalledWith(9, {
      markerId: null,
      name: '高光片段 01:50',
      startMs: 110_000,
      endMs: 130_000,
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

  it('opens task settings and resumes only after settings are saved', () => {
    const readyClip = {
      ...processingClip,
      state: 'ready' as const,
      outputVideoPath: '/rec/highlight-3.mp4',
    };
    component.clips = [readyClip];

    component.createUploadTask(readyClip);

    expect(component.taskEditVisible).toBeTrue();
    expect(component.taskEditJobIds).toEqual([44]);
    expect(recordings.runJobAction).not.toHaveBeenCalled();

    component.taskEditSaved();

    expect(recordings.runJobAction).toHaveBeenCalledOnceWith(
      'resume_upload',
      [44]
    );
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

    expect(highlights.updateMarker).toHaveBeenCalledWith(
      7,
      '新的名称',
      '备注'
    );

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
