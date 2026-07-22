import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NzMessageService } from 'ng-zorro-antd/message';
import { NzModalService } from 'ng-zorro-antd/modal';
import { of } from 'rxjs';

import {
  HighlightClip,
  HighlightClipSummary,
} from '../shared/highlight.model';
import { HighlightService } from '../shared/highlight.service';
import { ClipLibraryComponent } from './clip-library.component';

const clip: HighlightClipSummary = {
  id: 3,
  roomId: 100,
  sourceSessionId: 9,
  name: '五杀高光',
  state: 'ready',
  deletionState: 'none',
  deletionError: null,
  errorMessage: null,
  createdAt: 1_100,
  updatedAt: 1_100,
  uploadJobId: null,
  uploadState: null,
  uploadPercent: null,
  uploadBvid: null,
  sourceAnchorName: '主播名',
  sourceTitle: '排位赛',
  durationMs: 52_000,
  fileSizeBytes: 1_048_576,
};

const fullClip: HighlightClip = {
  ...clip,
  markerId: null,
  uploadSessionId: null,
  requestedStartMs: 20_000,
  requestedEndMs: 70_000,
  actualStartMs: 18_000,
  actualEndMs: 70_000,
  outputVideoPath: '/clips/100/highlight-3.mp4',
  outputXmlPath: '/clips/100/highlight-3.xml',
  confirmationRequired: false,
  confirmed: false,
  attempt: 1,
  sources: [],
};

describe('ClipLibraryComponent', () => {
  let fixture: ComponentFixture<ClipLibraryComponent>;
  let service: jasmine.SpyObj<HighlightService>;
  let message: jasmine.SpyObj<NzMessageService>;
  let modal: jasmine.SpyObj<NzModalService>;

  beforeEach(async () => {
    service = jasmine.createSpyObj<HighlightService>('HighlightService', [
      'listAllClips',
      'createMediaAccess',
      'mediaUrl',
      'downloadUrl',
      'retryClip',
      'deleteClip',
      'createUploadTask',
    ]);
    service.listAllClips.and.returnValue(of({ total: 1, items: [clip] }));
    service.createMediaAccess.and.returnValue(
      of({ token: 'signed', expiresAt: 2_000, fileSizeBytes: 1_048_576 }),
    );
    service.mediaUrl.and.returnValue('/api/clip.mp4');
    service.downloadUrl.and.returnValue('/api/clip.mp4?download=1');
    service.retryClip.and.returnValue(of(fullClip));
    service.deleteClip.and.returnValue(of(void 0));
    service.createUploadTask.and.returnValue(of({ jobId: 17 }));
    message = jasmine.createSpyObj<NzMessageService>('NzMessageService', [
      'success',
      'error',
    ]);
    modal = jasmine.createSpyObj<NzModalService>('NzModalService', ['confirm']);

    await TestBed.configureTestingModule({
      declarations: [ClipLibraryComponent],
      providers: [
        { provide: HighlightService, useValue: service },
        {
          provide: NzMessageService,
          useValue: message,
        },
        {
          provide: NzModalService,
          useValue: modal,
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();

    fixture = TestBed.createComponent(ClipLibraryComponent);
  });

  it('shows permanent clip assets and their source recording', () => {
    fixture.detectChanges();

    expect(service.listAllClips).toHaveBeenCalledOnceWith(20, 0);
    expect(fixture.nativeElement.textContent).toContain('五杀高光');
    expect(fixture.nativeElement.textContent).toContain('主播名');
    expect(fixture.nativeElement.textContent).toContain('排位赛');
    expect(fixture.nativeElement.textContent).toContain('52 秒');
    expect(fixture.nativeElement.textContent).toContain('1 MB');
  });

  it('owns vertical scrolling when the clip list exceeds the viewport', () => {
    fixture.detectChanges();

    expect(getComputedStyle(fixture.nativeElement).overflowY).toBe('auto');
  });

  it('does not create an upload task until submission settings are confirmed', () => {
    fixture.detectChanges();

    fixture.componentInstance.openUpload(clip);
    expect(service.createUploadTask).not.toHaveBeenCalled();

    fixture.componentInstance.submitUpload({} as never);
    expect(service.createUploadTask).toHaveBeenCalledOnceWith(3, {} as never);
  });

  it('shows a not-indexed label for a legacy clip without a persisted size', () => {
    service.listAllClips.and.returnValue(
      of({ total: 1, items: [{ ...clip, fileSizeBytes: null }] }),
    );

    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('大小待索引');
    expect(fixture.nativeElement.textContent).not.toContain('0 B');
  });

  it('reports deletion as queued instead of already completed', async () => {
    fixture.detectChanges();

    fixture.componentInstance.delete(clip);
    const options = modal.confirm.calls.mostRecent().args[0];
    expect(options).toBeDefined();
    const onOk = options!.nzOnOk as () => Promise<void>;
    await onOk();

    expect(service.deleteClip).toHaveBeenCalledOnceWith(3);
    expect(message.success).toHaveBeenCalledOnceWith('已提交删除，正在处理');
  });

  it('shows a failed deletion and offers deletion retry', () => {
    service.listAllClips.and.returnValue(
      of({
        total: 1,
        items: [
          {
            ...clip,
            deletionState: 'failed',
            deletionError: 'path_ownership_violation',
          },
        ],
      }),
    );

    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('删除失败');
    expect(fixture.nativeElement.textContent).toContain(
      'path_ownership_violation',
    );
    expect(fixture.nativeElement.textContent).toContain('重试删除');
  });

  it('does not retry a failed clip after its source recording was lost', () => {
    const unavailable = {
      ...clip,
      state: 'failed' as const,
      sourceSessionId: null,
    };

    fixture.componentInstance.retry(unavailable);

    expect(service.retryClip).not.toHaveBeenCalled();
    expect(message.error).toHaveBeenCalledOnceWith(
      '源录像关联已丢失，无法重试，请删除后重新创建片段',
    );
  });
});
