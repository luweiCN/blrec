import { CommonModule } from '@angular/common';
import { HttpResponse } from '@angular/common/http';
import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';

import { NzMessageService } from 'ng-zorro-antd/message';
import { NzModalService } from 'ng-zorro-antd/modal';
import { of, throwError } from 'rxjs';

import { RecordingSubmissionService } from '../tasks/upload-policy-dialog/recording-submission.service';
import { RecordingSessionDetail } from '../upload-tasks/shared/recording-session.model';
import { RecordingSessionService } from '../upload-tasks/shared/recording-session.service';
import { MediaLibraryItem } from './media-library.model';
import { MediaLibraryComponent } from './media-library.component';
import { MediaLibraryService } from './media-library.service';

function item(state: MediaLibraryItem['state'] = 'ready'): MediaLibraryItem {
  return {
    id: 9,
    sessionId: 7,
    kind: 'broadcast',
    origin: 'upload',
    displayName: '外部直播',
    note: '',
    state,
    error: null,
    createdAt: 100,
    updatedAt: 100,
    roomId: 0,
    sourceTitle: '外部直播',
    anchorName: '',
    startedAt: 100,
    tags: ['精选'],
    parts: [
      {
        itemId: 9,
        partIndex: 1,
        recordingPartId: state === 'ready' ? 11 : null,
        originalFilename: 'one.mp4',
        expectedSize: 3,
        receivedSize: state === 'ready' ? 3 : 0,
        state: state === 'ready' ? 'ready' : 'pending',
        error: null,
        durationSeconds: state === 'ready' ? 10 : null,
      },
    ],
    submissions: [],
  };
}

function sessionDetail(partId = 11): RecordingSessionDetail {
  return {
    id: 7,
    roomId: 100,
    broadcastSessionKey: '100:100',
    liveStartTime: 100,
    state: 'closed',
    startedAt: 100,
    endedAt: 200,
    title: '原直播标题',
    coverUrl: '',
    coverPath: null,
    anchorUid: null,
    anchorName: '主播',
    areaId: null,
    areaName: '',
    parentAreaId: null,
    parentAreaName: '',
    liveEndTime: 200,
    partCount: 1,
    danmakuCount: 0,
    totalFileSizeBytes: 3,
    recordDurationSeconds: 10,
    uploadIntent: 'none',
    uploadDecision: 'follow_room',
    submissionInherited: true,
    uploadResolutionState: 'not_requested',
    uploadResolutionError: null,
    uploadSuppressed: false,
    deletionState: 'none',
    deletionError: null,
    sourceKind: 'live',
    highlightClipId: null,
    mediaLibraryItemId: 9,
    displayState: 'not_uploading',
    availableActions: ['delete_local'],
    uploadJob: null,
    parts: [
      {
        id: partId,
        runId: 'run-1',
        partIndex: 1,
        sourcePath: '/favorites/key/part-0001.flv',
        finalPath: null,
        xmlPath: '/favorites/key/part-0001.xml',
        recordStartTime: 100,
        recordEndTime: 110,
        recordDurationSeconds: 10,
        fileSizeBytes: 3,
        danmakuCount: 0,
        artifactState: 'ready',
        xmlCompleted: true,
        sourceExists: true,
        finalExists: false,
        errorMessage: null,
        mediaIndexState: 'ready',
      },
    ],
  };
}

describe('MediaLibraryComponent', () => {
  let fixture: ComponentFixture<MediaLibraryComponent>;
  let service: jasmine.SpyObj<MediaLibraryService>;
  let recordingSessions: jasmine.SpyObj<RecordingSessionService>;
  let submissions: jasmine.SpyObj<RecordingSubmissionService>;
  let message: jasmine.SpyObj<NzMessageService>;

  beforeEach(async () => {
    service = jasmine.createSpyObj<MediaLibraryService>('MediaLibraryService', [
      'list',
      'createImport',
      'uploadPart',
      'completeImport',
      'update',
      'delete',
    ]);
    service.list.and.returnValue(of({ total: 1, items: [item()] }));
    recordingSessions = jasmine.createSpyObj<RecordingSessionService>(
      'RecordingSessionService',
      ['getSession', 'createMediaAccess', 'mediaUrl', 'runSessionAction'],
    );
    submissions = jasmine.createSpyObj<RecordingSubmissionService>(
      'RecordingSubmissionService',
      ['setDecision'],
    );
    message = jasmine.createSpyObj<NzMessageService>('NzMessageService', [
      'success',
      'error',
      'info',
    ]);

    await TestBed.configureTestingModule({
      declarations: [MediaLibraryComponent],
      imports: [CommonModule, FormsModule, RouterTestingModule],
      providers: [
        { provide: MediaLibraryService, useValue: service },
        { provide: RecordingSessionService, useValue: recordingSessions },
        { provide: RecordingSubmissionService, useValue: submissions },
        { provide: NzMessageService, useValue: message },
        {
          provide: NzModalService,
          useValue: jasmine.createSpyObj<NzModalService>('NzModalService', [
            'confirm',
          ]),
        },
        {
          provide: ActivatedRoute,
          useValue: {
            queryParamMap: of(convertToParamMap({ kind: 'broadcast' })),
          },
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();

    fixture = TestBed.createComponent(MediaLibraryComponent);
  });

  it('loads permanent broadcasts from the media library', () => {
    fixture.detectChanges();

    expect(service.list).toHaveBeenCalledOnceWith('broadcast', 20, 0, '');
    expect(fixture.componentInstance.items[0].displayName).toBe('外部直播');
  });

  it('opens a collected FLV in the shared recording preview dialog', () => {
    const favorite = {
      ...item(),
      origin: 'recording' as const,
      parts: [
        {
          ...item().parts[0],
          originalFilename: 'recording.flv',
        },
      ],
    };
    recordingSessions.getSession.and.returnValue(of(sessionDetail()));

    fixture.componentInstance.openPreview(favorite, favorite.parts[0]);
    fixture.detectChanges();

    expect(recordingSessions.getSession).toHaveBeenCalledOnceWith(7);
    expect(fixture.componentInstance.previewRecordingPart?.sourcePath).toBe(
      '/favorites/key/part-0001.flv',
    );
    expect(fixture.componentInstance.previewSession?.title).toBe('外部直播');
    expect(fixture.componentInstance.previewVisible).toBeTrue();
    expect(
      fixture.nativeElement.querySelector('app-part-video-dialog'),
    ).not.toBeNull();
  });

  it('does not open a preview when the selected part disappeared', () => {
    recordingSessions.getSession.and.returnValue(
      of({ ...sessionDetail(), parts: [] }),
    );
    const imported = item();

    fixture.componentInstance.openPreview(imported, imported.parts[0]);

    expect(fixture.componentInstance.previewVisible).toBeFalse();
    expect(message.error).toHaveBeenCalledOnceWith('该分 P 的本地录像已不存在');
  });

  it('offers clipping on each concrete broadcast part', () => {
    const broadcast = {
      ...item(),
      parts: [
        item().parts[0],
        {
          ...item().parts[0],
          partIndex: 2,
          recordingPartId: 12,
          originalFilename: 'two.mp4',
        },
      ],
    };
    service.list.and.returnValue(of({ total: 1, items: [broadcast] }));

    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.item-actions a')).toBeNull();
    const links = Array.from(
      fixture.nativeElement.querySelectorAll(
        '[data-testid="edit-media-part"]',
      ) as NodeListOf<HTMLAnchorElement>,
    );
    expect(links.length).toBe(2);
    expect(links[0].getAttribute('href')).toContain(
      '/recordings/highlights/7?partId=11',
    );
    expect(links[1].getAttribute('href')).toContain(
      '/recordings/highlights/7?partId=12',
    );
  });

  it('presents generated and imported clips in one media-library tab', () => {
    const component = fixture.componentInstance;
    fixture.detectChanges();
    component.kind = 'clip';
    service.list.and.returnValue(
      of({ total: 1, items: [{ ...item(), kind: 'clip' }] }),
    );

    component.load();
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector('[role="tablist"]'),
    ).not.toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('上传片段');
    expect(fixture.nativeElement.textContent).toContain('导入外部片段');
    expect(
      fixture.nativeElement.querySelector('app-clip-library'),
    ).not.toBeNull();
  });

  it('uploads selected files sequentially in the displayed part order', () => {
    service.createImport.and.returnValue(of(item('uploading')));
    service.uploadPart.and.returnValues(
      of(new HttpResponse({ body: item().parts[0] })),
      of(
        new HttpResponse({
          body: { ...item().parts[0], partIndex: 2 },
        }),
      ),
    );
    service.completeImport.and.returnValue(of(item()));
    const one = new File(['one'], 'one.mp4', { type: 'video/mp4' });
    const two = new File(['two'], 'two.mp4', { type: 'video/mp4' });
    const component = fixture.componentInstance;
    component.openImport('broadcast');
    component.importDisplayName = '外部直播';
    component.importFiles = [one, two].map((file) => ({
      file,
      progress: 0,
      state: 'pending' as const,
      error: null,
    }));

    component.submitImport();

    expect(service.createImport).toHaveBeenCalledWith(
      jasmine.objectContaining({
        kind: 'broadcast',
        parts: [
          { filename: 'one.mp4', sizeBytes: 3 },
          { filename: 'two.mp4', sizeBytes: 3 },
        ],
      }),
    );
    expect(service.uploadPart.calls.allArgs()).toEqual([
      [9, 1, one],
      [9, 2, two],
    ]);
    expect(service.completeImport).toHaveBeenCalledOnceWith(9);
    expect(message.success).toHaveBeenCalledWith('外部直播已永久保存');
  });

  it('reuploads completed browser files when server-side validation fails', () => {
    service.createImport.and.returnValue(of(item('uploading')));
    service.uploadPart.and.returnValue(
      of(new HttpResponse({ body: item().parts[0] })),
    );
    service.completeImport.and.returnValue(
      throwError(() => ({
        status: 409,
        error: { detail: '第 1 个分 P 视频文件无法识别，请重新上传' },
      })),
    );
    const file = new File(['one'], 'one.mp4', { type: 'video/mp4' });
    const component = fixture.componentInstance;
    component.openImport('clip');
    component.importDisplayName = '外部片段';
    component.importFiles = [
      { file, progress: 0, state: 'pending', error: null },
    ];

    component.submitImport();

    expect(component.importFiles[0].state).toBe('failed');
    expect(component.importFiles[0].progress).toBe(0);

    service.completeImport.and.returnValue(of({ ...item(), kind: 'clip' }));
    component.submitImport();

    expect(service.uploadPart.calls.count()).toBe(2);
  });
});
