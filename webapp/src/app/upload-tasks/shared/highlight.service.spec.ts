import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from 'src/app/core/services/url.service';
import { HighlightService } from './highlight.service';

describe('HighlightService', () => {
  let service: HighlightService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        HighlightService,
        { provide: UrlService, useValue: { makeApiUrl: (path: string) => path } },
      ],
    });
    service = TestBed.inject(HighlightService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('uses the session timeline and inspection endpoints', () => {
    service.getTimeline(9).subscribe();
    const timeline = http.expectOne(
      '/api/v1/highlights/sessions/9/timeline'
    );
    expect(timeline.request.method).toBe('GET');
    timeline.flush({ parts: [], markers: [] });

    service.inspectClip(9, 20_000, 70_000).subscribe();
    const inspection = http.expectOne(
      '/api/v1/highlights/sessions/9/clips/inspect'
    );
    expect(inspection.request.method).toBe('POST');
    expect(inspection.request.body).toEqual({
      startMs: 20_000,
      endMs: 70_000,
    });
    inspection.flush({});
  });

  it('creates, loads, deletes and submits one independent clip', () => {
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
      'GET'
    );

    service.deleteClip(3).subscribe();
    const remove = http.expectOne('/api/v1/highlights/clips/3');
    expect(remove.request.method).toBe('DELETE');
    remove.flush(null, { status: 204, statusText: 'No Content' });

    service.createUploadTask(3).subscribe();
    const upload = http.expectOne('/api/v1/highlights/clips/3/upload-task');
    expect(upload.request.method).toBe('POST');
    expect(upload.request.body).toBeNull();
    upload.flush({ jobId: 17 });
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
      })
    ).toBe(
      '/api/v1/highlights/clips/3/media' +
        '?media_token=signed%20token&media_expires=123'
    );
  });
});
