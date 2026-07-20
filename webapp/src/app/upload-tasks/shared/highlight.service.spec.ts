import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from 'src/app/core/services/url.service';
import { RoomUploadPolicyRequest } from 'src/app/tasks/upload-policy-dialog/room-upload-policy.model';
import { HighlightClipSummary } from './highlight.model';
import { HighlightService } from './highlight.service';

describe('HighlightService', () => {
  let service: HighlightService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        HighlightService,
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
    service = TestBed.inject(HighlightService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('uses the session timeline and inspection endpoints', () => {
    service.getTimeline(9).subscribe();
    const timeline = http.expectOne('/api/v1/highlights/sessions/9/timeline');
    expect(timeline.request.method).toBe('GET');
    timeline.flush({ parts: [], markers: [] });

    service.inspectClip(9, 20_000, 70_000).subscribe();
    const inspection = http.expectOne(
      '/api/v1/highlights/sessions/9/clips/inspect',
    );
    expect(inspection.request.method).toBe('POST');
    expect(inspection.request.body).toEqual({
      startMs: 20_000,
      endMs: 70_000,
    });
    inspection.flush({});
  });

  it('loads lightweight marker counts without the full timeline', () => {
    service.getMarkerCounts(7).subscribe();

    const request = http.expectOne(
      '/api/v1/highlights/sessions/7/marker-counts',
    );
    expect(request.request.method).toBe('GET');
    request.flush([{ partId: 2, count: 3 }]);
  });

  it('creates, loads, deletes and submits one independent clip', () => {
    service.listClips(9).subscribe();
    expect(
      http.expectOne('/api/v1/highlights/sessions/9/clips').request.method,
    ).toBe('GET');

    service
      .createClip(9, {
        markerId: 1,
        name: '第一段高光',
        startMs: 20_000,
        endMs: 70_000,
        confirmKeyframe: true,
      })
      .subscribe();
    const create = http.expectOne('/api/v1/highlights/sessions/9/clips');
    expect(create.request.method).toBe('POST');
    expect(create.request.body).toEqual({
      markerId: 1,
      name: '第一段高光',
      startMs: 20_000,
      endMs: 70_000,
      confirmKeyframe: true,
    });
    create.flush({});

    service.getClip(3).subscribe();
    expect(http.expectOne('/api/v1/highlights/clips/3').request.method).toBe(
      'GET',
    );

    service.retryClip(3).subscribe();
    const retry = http.expectOne('/api/v1/highlights/clips/3/retry');
    expect(retry.request.method).toBe('POST');
    expect(retry.request.body).toBeNull();
    retry.flush({});

    service.deleteClip(3).subscribe();
    const remove = http.expectOne('/api/v1/highlights/clips/3');
    expect(remove.request.method).toBe('DELETE');
    remove.flush(null, { status: 204, statusText: 'No Content' });

    service.prepareUploadSession(3).subscribe();
    const prepare = http.expectOne('/api/v1/highlights/clips/3/upload-session');
    expect(prepare.request.method).toBe('POST');
    expect(prepare.request.body).toBeNull();
    prepare.flush({ sessionId: 12 });

    const settings: RoomUploadPolicyRequest = {
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
    service.createUploadTask(3, settings).subscribe();
    const upload = http.expectOne('/api/v1/highlights/clips/3/upload-task');
    expect(upload.request.method).toBe('POST');
    expect(upload.request.body).toEqual(settings);
    upload.flush({ jobId: 17 });
  });

  it('loads the global clip library with pagination', () => {
    let item: HighlightClipSummary | undefined;
    service.listAllClips(20, 40).subscribe((response) => {
      item = response.items[0];
    });

    const request = http.expectOne(
      '/api/v1/highlights/clips?limit=20&offset=40',
    );
    expect(request.request.method).toBe('GET');
    request.flush({
      total: 1,
      items: [
        {
          id: 3,
          roomId: 100,
          sourceSessionId: 9,
          name: '五杀高光',
          state: 'ready',
          errorMessage: null,
          createdAt: 1_100,
          updatedAt: 1_100,
          sourceAnchorName: '主播名',
          sourceTitle: '排位赛',
          durationMs: 52_000,
          fileSizeBytes: null,
          uploadJobId: null,
          uploadState: null,
          uploadPercent: null,
          uploadBvid: null,
        },
      ],
    });
    expect(item?.fileSizeBytes).toBeNull();
    expect('outputVideoPath' in (item as object)).toBeFalse();
  });

  it('updates and deletes independent marker metadata', () => {
    service.updateMarker(1, '新名称', '备注').subscribe();
    const update = http.expectOne('/api/v1/highlights/1');
    expect(update.request.method).toBe('PATCH');
    expect(update.request.body).toEqual({ name: '新名称', note: '备注' });
    update.flush({});

    service.deleteMarker(1).subscribe();
    const remove = http.expectOne('/api/v1/highlights/1');
    expect(remove.request.method).toBe('DELETE');
    remove.flush(null, { status: 204, statusText: 'No Content' });
  });

  it('creates a signed streaming URL for a ready clip', () => {
    service.createMediaAccess(3).subscribe();
    const access = http.expectOne('/api/v1/highlights/clips/3/media-access');
    expect(access.request.method).toBe('POST');
    expect(access.request.body).toBeNull();
    access.flush({
      token: 'signed token',
      expiresAt: 123,
      fileSizeBytes: 2048,
    });

    expect(
      service.mediaUrl(3, {
        token: 'signed token',
        expiresAt: 123,
        fileSizeBytes: 2048,
      }),
    ).toBe(
      '/api/v1/highlights/clips/3/media' +
        '?media_token=signed%20token&media_expires=123',
    );
    expect(
      service.downloadUrl(3, {
        token: 'signed token',
        expiresAt: 123,
        fileSizeBytes: 2048,
      }),
    ).toBe(
      '/api/v1/highlights/clips/3/media' +
        '?media_token=signed%20token&media_expires=123&download=1',
    );
  });
});
