import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from '../core/services/url.service';
import { VaingloryService } from './vainglory.service';

describe('VaingloryService', () => {
  let service: VaingloryService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        VaingloryService,
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
    service = TestBed.inject(VaingloryService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('sends every active match filter with pagination', () => {
    service
      .listMatchSessions(
        {
          playerName: ' 5555-2 ',
          heroIds: [7, 8],
          winnerColor: 'teal',
          gameMode: '3v3',
          sessionId: 9,
          sourceTitle: ' 神霸 ',
          anchorName: '',
          statsIncluded: false,
        },
        20,
        40,
      )
      .subscribe();

    const request = http.expectOne(
      (candidate) =>
        candidate.url === '/api/v1/vainglory/sessions' &&
        candidate.params.get('playerName') === '5555-2' &&
        candidate.params.getAll('heroId')?.join(',') === '7,8' &&
        candidate.params.get('winnerColor') === 'teal' &&
        candidate.params.get('gameMode') === '3v3' &&
        candidate.params.get('sessionId') === '9' &&
        candidate.params.get('sourceTitle') === '神霸' &&
        candidate.params.has('anchorName') &&
        candidate.params.get('anchorName') === '' &&
        candidate.params.get('statsIncluded') === 'false' &&
        candidate.params.get('limit') === '20' &&
        candidate.params.get('offset') === '40',
    );

    expect(request.request.method).toBe('GET');
    request.flush({ total: 0, items: [] });
  });

  it('loads all matches only when a recording session is opened', () => {
    service
      .listMatches(
        {
          playerName: '',
          heroIds: [],
          winnerColor: null,
          gameMode: null,
          sessionId: 9,
        },
        100,
        0,
      )
      .subscribe();

    const request = http.expectOne(
      '/api/v1/vainglory/matches?limit=100&offset=0&sessionId=9',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ total: 0, items: [] });
  });

  it('updates one title for a recording session', () => {
    service.updateSessionTitle(9, '整场标题').subscribe();

    const request = http.expectOne('/api/v1/vainglory/sessions/9');
    expect(request.request.method).toBe('PATCH');
    expect(request.request.body).toEqual({ title: '整场标题' });
    request.flush({});
  });

  it('updates one anchor and batches session statistics settings', () => {
    service.updateSessionAnchor(9, '玩不明白').subscribe();
    const anchor = http.expectOne('/api/v1/vainglory/sessions/9/anchor');
    expect(anchor.request.method).toBe('PATCH');
    expect(anchor.request.body).toEqual({ anchorName: '玩不明白' });
    anchor.flush({});

    service.bulkUpdateSessions([9, 10], { statsIncluded: false }).subscribe();
    const bulk = http.expectOne('/api/v1/vainglory/sessions/bulk-update');
    expect(bulk.request.method).toBe('PATCH');
    expect(bulk.request.body).toEqual({
      sessionIds: [9, 10],
      statsIncluded: false,
    });
    bulk.flush({ updatedCount: 2 });
  });

  it('loads anchor match statistics', () => {
    service.listAnchorStats().subscribe();

    const request = http.expectOne('/api/v1/vainglory/stats/anchors');
    expect(request.request.method).toBe('GET');
    request.flush([]);
  });

  it('starts and reads an account archive backfill', () => {
    service.requestArchiveSync(7).subscribe();
    const request = http.expectOne('/api/v1/vainglory/archive-syncs/7');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toBeNull();
    request.flush({ accountId: 7, state: 'discovering' });

    service.getArchiveSync(7).subscribe();
    const status = http.expectOne('/api/v1/vainglory/archive-syncs/7');
    expect(status.request.method).toBe('GET');
    status.flush({ accountId: 7, state: 'running' });
  });

  it('lists suspected non-Vainglory public archives', () => {
    service.listArchiveContentReviews(20, 40).subscribe();

    const request = http.expectOne(
      '/api/v1/vainglory/archive-content-reviews?limit=20&offset=40',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ total: 0, items: [] });
  });
});
