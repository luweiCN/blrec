import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from 'src/app/core/services/url.service';
import { RecordingSubmissionService } from './recording-submission.service';

describe('RecordingSubmissionService', () => {
  let service: RecordingSubmissionService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        RecordingSubmissionService,
        { provide: UrlService, useValue: { makeApiUrl: (path: string) => path } },
      ],
    });
    service = TestBed.inject(RecordingSubmissionService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('loads and saves one complete recording override', () => {
    service.get(7).subscribe();
    const get = http.expectOne(
      '/api/v1/recording-sessions/7/submission-settings',
    );
    expect(get.request.method).toBe('GET');
    get.flush({});

    const settings = {
      accountMode: 'primary' as const,
      accountId: null,
      enabled: true,
      titleTemplate: '{{ title }}',
      descriptionTemplate: '',
      partTitleTemplate: 'P{{ part_index }}',
      dynamicTemplate: '',
      tid: 17,
      tags: '直播,录播',
      creationStatementId: -1,
      originalAuthorization: true,
      source: '',
      isOnlySelf: false,
      publishDynamic: true,
      upSelectionReply: false,
      upCloseReply: false,
      upCloseDanmu: false,
      autoComment: true,
      danmakuBackfill: true,
      filters: {},
      collectionSeasonId: null,
      collectionSectionId: null,
      coverMode: 'live' as const,
      coverAssetId: null,
      publishDelaySeconds: 0,
      retentionMode: 'submitted' as const,
      retentionDays: 5,
    };
    service.save(7, settings).subscribe();
    const put = http.expectOne(
      '/api/v1/recording-sessions/7/submission-settings',
    );
    expect(put.request.method).toBe('PUT');
    expect(put.request.body).toEqual(settings);
    put.flush({});
  });

  it('restores inherited settings and changes the decision independently', () => {
    service.clear(7).subscribe();
    const clear = http.expectOne(
      '/api/v1/recording-sessions/7/submission-settings',
    );
    expect(clear.request.method).toBe('DELETE');
    clear.flush({});

    service.setDecision(7, 'skip').subscribe();
    const decision = http.expectOne(
      '/api/v1/recording-sessions/7/submission-decision',
    );
    expect(decision.request.method).toBe('PATCH');
    expect(decision.request.body).toEqual({ decision: 'skip' });
    decision.flush({});
  });
});
