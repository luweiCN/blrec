import { CommonModule } from '@angular/common';
import { HttpResponse } from '@angular/common/http';
import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { FormsModule } from '@angular/forms';
import {
  ActivatedRoute,
  Router,
  convertToParamMap,
} from '@angular/router';

import { NzMessageService } from 'ng-zorro-antd/message';
import { NzModalService } from 'ng-zorro-antd/modal';
import { of, throwError } from 'rxjs';

import { RecordingSubmissionService } from '../tasks/upload-policy-dialog/recording-submission.service';
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

describe('MediaLibraryComponent', () => {
  let fixture: ComponentFixture<MediaLibraryComponent>;
  let service: jasmine.SpyObj<MediaLibraryService>;
  let recordingSessions: jasmine.SpyObj<RecordingSessionService>;
  let submissions: jasmine.SpyObj<RecordingSubmissionService>;
  let message: jasmine.SpyObj<NzMessageService>;

  beforeEach(async () => {
    service = jasmine.createSpyObj<MediaLibraryService>(
      'MediaLibraryService',
      ['list', 'createImport', 'uploadPart', 'completeImport', 'update', 'delete'],
    );
    service.list.and.returnValue(of({ total: 1, items: [item()] }));
    recordingSessions = jasmine.createSpyObj<RecordingSessionService>(
      'RecordingSessionService',
      ['createMediaAccess', 'mediaUrl', 'runSessionAction'],
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
      imports: [CommonModule, FormsModule],
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
        {
          provide: Router,
          useValue: jasmine.createSpyObj<Router>('Router', ['navigate']),
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
