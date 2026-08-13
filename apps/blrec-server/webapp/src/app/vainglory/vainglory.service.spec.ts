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

  it('loads completed zero-match sessions for manual review', () => {
    service.listZeroMatchSessions(10, 20).subscribe();

    const request = http.expectOne(
      '/api/v1/vainglory/zero-match-sessions?limit=10&offset=20',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ total: 0, items: [] });

    service.listZeroMatchSessions(10, 20, true).subscribe();
    const suppressed = http.expectOne(
      '/api/v1/vainglory/zero-match-sessions?limit=10&offset=20&suppressed=true',
    );
    expect(suppressed.request.method).toBe('GET');
    suppressed.flush({ total: 0, items: [] });

    service.suppressZeroMatchSession(12).subscribe();
    const suppress = http.expectOne(
      '/api/v1/vainglory/sessions/12/scan-suppression',
    );
    expect(suppress.request.method).toBe('PUT');
    suppress.flush(null);

    service.restoreZeroMatchSession(12).subscribe();
    const restore = http.expectOne(
      '/api/v1/vainglory/sessions/12/scan-suppression',
    );
    expect(restore.request.method).toBe('DELETE');
    restore.flush(null);
  });

  it('loads unresolved heroes and saves a manual hero', () => {
    service.listHeroReviews(20, 40).subscribe();
    const reviews = http.expectOne(
      '/api/v1/vainglory/hero-reviews?limit=20&offset=40',
    );
    expect(reviews.request.method).toBe('GET');
    reviews.flush({ total: 0, items: [] });

    service.setPlayerHero(9, 'right', 2, 7).subscribe();
    const saved = http.expectOne(
      '/api/v1/vainglory/matches/9/players/right/2/hero',
    );
    expect(saved.request.method).toBe('PATCH');
    expect(saved.request.body).toEqual({ heroId: 7 });
    saved.flush({});

    service.suppressMatchReview(9, 'hero').subscribe();
    const ignored = http.expectOne(
      '/api/v1/vainglory/matches/9/review-suppressions/hero',
    );
    expect(ignored.request.method).toBe('PUT');
    expect(ignored.request.body).toBeNull();
    ignored.flush(null);
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

  it('retries one failed publication step', () => {
    service.retryPublicationStep(9, 'pin').subscribe();

    const request = http.expectOne(
      '/api/v1/vainglory/sessions/9/publication/pin/retry',
    );
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toBeNull();
    request.flush(null);
  });

  it('registers and safely pauses an analysis worker', () => {
    service.addAnalysisWorker('mac-studio', 'Mac Studio').subscribe();
    const created = http.expectOne('/api/v1/vainglory/workers');
    expect(created.request.method).toBe('POST');
    expect(created.request.body).toEqual({
      workerId: 'mac-studio',
      displayName: 'Mac Studio',
    });
    created.flush({ workerId: 'mac-studio' });

    service.updateAnalysisWorker('mac-studio', { enabled: false }).subscribe();
    const paused = http.expectOne('/api/v1/vainglory/workers/mac-studio');
    expect(paused.request.method).toBe('PATCH');
    expect(paused.request.body).toEqual({ enabled: false });
    paused.flush({ workerId: 'mac-studio', enabled: false });
  });

  it('manages players and loads player-centred rankings', () => {
    service.listPlayers().subscribe();
    const listed = http.expectOne('/api/v1/vainglory/players');
    expect(listed.request.method).toBe('GET');
    listed.flush([]);

    service.createPlayer('游戏名').subscribe();
    const created = http.expectOne('/api/v1/vainglory/players');
    expect(created.request.method).toBe('POST');
    expect(created.request.body).toEqual({ name: '游戏名' });
    created.flush({});

    service.renamePlayer(5, '新游戏名').subscribe();
    const renamed = http.expectOne('/api/v1/vainglory/players/5');
    expect(renamed.request.method).toBe('PATCH');
    expect(renamed.request.body).toEqual({ name: '新游戏名' });
    renamed.flush({});

    service.bindPlayerRoom(5, 100).subscribe();
    const bound = http.expectOne('/api/v1/vainglory/players/5/rooms/100');
    expect(bound.request.method).toBe('PUT');
    expect(bound.request.body).toBeNull();
    bound.flush({});

    service.unbindPlayerRoom(5, 100).subscribe();
    const unbound = http.expectOne('/api/v1/vainglory/players/5/rooms/100');
    expect(unbound.request.method).toBe('DELETE');
    unbound.flush({});

    service.listPlayerStats().subscribe();
    const playerStats = http.expectOne('/api/v1/vainglory/stats/players');
    expect(playerStats.request.method).toBe('GET');
    playerStats.flush([]);

    service.listHeroStats('3v3').subscribe();
    const heroStats = http.expectOne(
      '/api/v1/vainglory/stats/heroes?gameMode=3v3',
    );
    expect(heroStats.request.method).toBe('GET');
    heroStats.flush([]);
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
