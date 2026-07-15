import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from 'src/app/core/services/url.service';
import { LiveStatusMetrics } from '../setting.model';
import { LiveStatusService } from './live-status.service';

describe('LiveStatusService', () => {
  let http: HttpTestingController;
  let service: LiveStatusService;

  const metricsFixture: LiveStatusMetrics = {
    mode: 'batch',
    intervalSeconds: 30,
    batchSize: 29,
    registeredRooms: 58,
    activeWebsockets: 0,
    lastSuccessAt: 100,
    snapshotMaxAgeSeconds: 12,
    missingResults: 0,
    fallbackRequests: 0,
    breakerState: 'closed',
    breakerReason: null,
  };

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    service = TestBed.inject(LiveStatusService);
  });

  afterEach(() => http.verify());

  it('loads health and resumes a paused coordinator', () => {
    service.getMetrics().subscribe((value) => expect(value.mode).toBe('batch'));
    const getRequest = http.expectOne('/api/v1/live-status');
    expect(getRequest.request.method).toBe('GET');
    getRequest.flush(metricsFixture);

    service.resume().subscribe();
    const postRequest = http.expectOne('/api/v1/live-status/resume');
    expect(postRequest.request.method).toBe('POST');
    postRequest.flush(null);
  });
});
