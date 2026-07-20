import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from 'src/app/core/services/url.service';
import { RecordingSessionService } from './recording-session.service';

describe('RecordingSessionService', () => {
  let service: RecordingSessionService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        RecordingSessionService,
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
    service = TestBed.inject(RecordingSessionService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('loads the newest recording sessions without a write request', () => {
    service.listSessions(20, 40).subscribe();

    const request = http.expectOne(
      '/api/v1/recording-sessions?limit=20&offset=40&scope=recordings',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ degradedReason: null, total: 0, sessions: [] });
  });

  it('loads one complete recording-session detail on demand', () => {
    service.getSession(7).subscribe();

    const request = http.expectOne('/api/v1/recording-sessions/7');
    expect(request.request.method).toBe('GET');
    request.flush({ id: 7, parts: [] });
  });

  it('sends upload-task filters as query parameters', () => {
    service
      .listSessions(20, 0, {
        scope: 'uploads',
        query: '主播 名',
        recordingState: 'closed',
        uploadState: 'approved',
        startedFrom: 100,
        startedTo: 200,
        sort: 'oldest',
      })
      .subscribe();

    const request = http.expectOne(
      (candidate) =>
        candidate.url === '/api/v1/recording-sessions' &&
        candidate.params.get('q') === '主播 名',
    );
    expect(request.request.params.get('recordingState')).toBe('closed');
    expect(request.request.params.get('scope')).toBe('uploads');
    expect(request.request.params.get('uploadState')).toBe('approved');
    expect(request.request.params.get('startedFrom')).toBe('100');
    expect(request.request.params.get('startedTo')).toBe('200');
    expect(request.request.params.get('sort')).toBe('oldest');
    request.flush({ degradedReason: null, total: 0, sessions: [] });
  });

  it('uses one endpoint for single and batch upload-job actions', () => {
    service.runJobAction('repair_transcode', [9, 10]).subscribe();

    const request = http.expectOne(
      '/api/v1/recording-sessions/upload-jobs/actions',
    );
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      action: 'repair_transcode',
      jobIds: [9, 10],
    });
    request.flush({ results: [] });
  });

  it('uses recording-session IDs for actions that also support sessions without jobs', () => {
    service.runSessionAction('set_upload', [7, 8]).subscribe();

    const request = http.expectOne('/api/v1/recording-sessions/actions');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      action: 'set_upload',
      sessionIds: [7, 8],
    });
    request.flush({ results: [] });
  });

  it('retries all server-selected failed upload jobs', () => {
    service.retryFailedJobs().subscribe();

    const request = http.expectOne(
      '/api/v1/recording-sessions/upload-jobs/retry-failed',
    );
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toBeNull();
    request.flush({ results: [] });
  });

  it('creates a scoped media access URL and pages danmaku', () => {
    service.createMediaAccess(7).subscribe();
    const accessRequest = http.expectOne(
      '/api/v1/recording-sessions/parts/7/media-access',
    );
    expect(accessRequest.request.method).toBe('POST');
    accessRequest.flush({
      token: 'signed token',
      expiresAt: 123,
      snapshotId: 'snapshot-id',
      durationMs: 12_500,
      fileSizeBytes: 2_048,
      recording: true,
      playbackMode: 'active_snapshot',
      indexState: 'pending',
      retryAfterMs: null,
      requestId: 'request-service',
    });

    expect(
      service.mediaUrl(7, {
        token: 'signed token',
        expiresAt: 123,
        snapshotId: 'snapshot-id',
        durationMs: 12_500,
        fileSizeBytes: 2_048,
        recording: true,
        playbackMode: 'active_snapshot',
        indexState: 'pending',
        retryAfterMs: null,
        requestId: 'request-service',
      }),
    ).toBe(
      '/api/v1/recording-sessions/parts/7/media?media_token=signed%20token&media_expires=123&media_snapshot=snapshot-id',
    );

    service.listDanmaku(7, 100, 50).subscribe();
    const danmakuRequest = http.expectOne(
      '/api/v1/recording-sessions/parts/7/danmaku?cursor=100&limit=50',
    );
    expect(danmakuRequest.request.method).toBe('GET');
    danmakuRequest.flush({ items: [], nextCursor: null });
  });
});
