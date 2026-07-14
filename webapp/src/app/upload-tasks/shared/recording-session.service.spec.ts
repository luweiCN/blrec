import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
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
        { provide: UrlService, useValue: { makeApiUrl: (path: string) => path } },
      ],
    });
    service = TestBed.inject(RecordingSessionService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('loads the newest recording sessions without a write request', () => {
    service.listSessions(20).subscribe();

    const request = http.expectOne('/api/v1/recording-sessions?limit=20');
    expect(request.request.method).toBe('GET');
    request.flush({ degradedReason: null, sessions: [] });
  });
});
