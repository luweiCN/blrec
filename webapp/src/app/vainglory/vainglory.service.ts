import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from '../core/services/url.service';
import {
  GameMode,
  VaingloryAnchorStats,
  VaingloryArchiveBackfillItem,
  VaingloryHero,
  VaingloryHeroStats,
  VaingloryArchiveSync,
  VaingloryArchiveSyncControl,
  VaingloryArchiveContentReviewList,
  VaingloryMatch,
  VaingloryMatchFilters,
  VaingloryMatchList,
  VaingloryMatchUpdate,
  VaingloryMatchSession,
  VaingloryMatchSessionList,
  VaingloryMatchSessionSort,
  VaingloryPlayer,
  VaingloryPlayerStats,
  VaingloryPublicationRetryStep,
  VaingloryScanJob,
  VaingloryZeroMatchSessionList,
} from './vainglory.model';

@Injectable({ providedIn: 'root' })
export class VaingloryService {
  constructor(
    private http: HttpClient,
    private url: UrlService,
  ) {}

  listMatchSessions(
    filters: VaingloryMatchFilters,
    limit = 20,
    offset = 0,
    sort: VaingloryMatchSessionSort = 'analyzed',
  ): Observable<VaingloryMatchSessionList> {
    return this.http.get<VaingloryMatchSessionList>(
      this.url.makeApiUrl('/api/v1/vainglory/sessions'),
      { params: this.matchParams(filters, limit, offset).set('sort', sort) },
    );
  }

  listZeroMatchSessions(
    limit = 20,
    offset = 0,
    suppressed = false,
  ): Observable<VaingloryZeroMatchSessionList> {
    let params = new HttpParams().set('limit', limit).set('offset', offset);
    if (suppressed) {
      params = params.set('suppressed', true);
    }
    return this.http.get<VaingloryZeroMatchSessionList>(
      this.url.makeApiUrl('/api/v1/vainglory/zero-match-sessions'),
      { params },
    );
  }

  suppressZeroMatchSession(sessionId: number): Observable<void> {
    return this.http.put<void>(
      this.url.makeApiUrl(
        `/api/v1/vainglory/sessions/${sessionId}/scan-suppression`,
      ),
      null,
    );
  }

  restoreZeroMatchSession(sessionId: number): Observable<void> {
    return this.http.delete<void>(
      this.url.makeApiUrl(
        `/api/v1/vainglory/sessions/${sessionId}/scan-suppression`,
      ),
    );
  }

  listMatches(
    filters: VaingloryMatchFilters,
    limit = 20,
    offset = 0,
  ): Observable<VaingloryMatchList> {
    return this.http.get<VaingloryMatchList>(
      this.url.makeApiUrl('/api/v1/vainglory/matches'),
      { params: this.matchParams(filters, limit, offset) },
    );
  }

  listRecordedPlayerReviews(
    limit = 100,
    offset = 0,
  ): Observable<VaingloryMatchList> {
    return this.http.get<VaingloryMatchList>(
      this.url.makeApiUrl('/api/v1/vainglory/recorded-player-reviews'),
      { params: new HttpParams().set('limit', limit).set('offset', offset) },
    );
  }

  listHeroReviews(limit = 100, offset = 0): Observable<VaingloryMatchList> {
    return this.http.get<VaingloryMatchList>(
      this.url.makeApiUrl('/api/v1/vainglory/hero-reviews'),
      { params: new HttpParams().set('limit', limit).set('offset', offset) },
    );
  }

  setRecordedPlayer(
    matchId: number,
    side: 'left' | 'right',
    slot: number,
  ): Observable<VaingloryMatch> {
    return this.http.patch<VaingloryMatch>(
      this.url.makeApiUrl(
        `/api/v1/vainglory/matches/${matchId}/recorded-player`,
      ),
      { side, slot },
    );
  }

  setPlayerHero(
    matchId: number,
    side: 'left' | 'right',
    slot: number,
    heroId: number,
  ): Observable<VaingloryMatch> {
    return this.http.patch<VaingloryMatch>(
      this.url.makeApiUrl(
        `/api/v1/vainglory/matches/${matchId}/players/${side}/${slot}/hero`,
      ),
      { heroId },
    );
  }

  private matchParams(
    filters: VaingloryMatchFilters,
    limit: number,
    offset: number,
  ): HttpParams {
    let params = new HttpParams().set('limit', limit).set('offset', offset);
    if (filters.playerName.trim()) {
      params = params.set('playerName', filters.playerName.trim());
    }
    for (const heroId of filters.heroIds) {
      params = params.append('heroId', heroId);
    }
    if (filters.winnerColor !== null) {
      params = params.set('winnerColor', filters.winnerColor);
    }
    if (filters.gameMode !== null) {
      params = params.set('gameMode', filters.gameMode);
    }
    if (filters.sessionId !== null) {
      params = params.set('sessionId', filters.sessionId);
    }
    if (filters.sourceTitle?.trim()) {
      params = params.set('sourceTitle', filters.sourceTitle.trim());
    }
    if (filters.anchorName !== undefined && filters.anchorName !== null) {
      params = params.set('anchorName', filters.anchorName);
    }
    if (filters.statsIncluded !== undefined && filters.statsIncluded !== null) {
      params = params.set('statsIncluded', filters.statsIncluded);
    }
    return params;
  }

  requestScan(sessionId: number): Observable<VaingloryScanJob> {
    return this.http.post<VaingloryScanJob>(
      this.url.makeApiUrl(`/api/v1/vainglory/sessions/${sessionId}/scan`),
      null,
    );
  }

  markSessionMatch(
    sessionId: number,
    partIndex: number,
    atMs: number,
  ): Observable<{
    readonly id: number;
    readonly sessionId: number;
    readonly partId: number;
    readonly partIndex: number;
    readonly atMs: number;
  }> {
    return this.http.post<{
      readonly id: number;
      readonly sessionId: number;
      readonly partId: number;
      readonly partIndex: number;
      readonly atMs: number;
    }>(
      this.url.makeApiUrl(
        `/api/v1/vainglory/sessions/${sessionId}/match-markers`,
      ),
      { partIndex, atMs },
    );
  }

  getScan(sessionId: number): Observable<VaingloryScanJob> {
    return this.http.get<VaingloryScanJob>(
      this.url.makeApiUrl(`/api/v1/vainglory/sessions/${sessionId}/scan`),
    );
  }

  retryPublicationStep(
    sessionId: number,
    step: VaingloryPublicationRetryStep,
  ): Observable<void> {
    return this.http.post<void>(
      this.url.makeApiUrl(
        `/api/v1/vainglory/sessions/${sessionId}/publication/${step}/retry`,
      ),
      null,
    );
  }

  listAnchorStats(): Observable<readonly VaingloryAnchorStats[]> {
    return this.http.get<readonly VaingloryAnchorStats[]>(
      this.url.makeApiUrl('/api/v1/vainglory/stats/anchors'),
    );
  }

  listPlayers(): Observable<readonly VaingloryPlayer[]> {
    return this.http.get<readonly VaingloryPlayer[]>(
      this.url.makeApiUrl('/api/v1/vainglory/players'),
    );
  }

  createPlayer(name: string): Observable<VaingloryPlayer> {
    return this.http.post<VaingloryPlayer>(
      this.url.makeApiUrl('/api/v1/vainglory/players'),
      { name },
    );
  }

  syncPlayerRooms(
    rooms: readonly { readonly roomId: number; readonly name: string }[],
  ): Observable<readonly VaingloryPlayer[]> {
    return this.http.post<readonly VaingloryPlayer[]>(
      this.url.makeApiUrl('/api/v1/vainglory/players/sync-rooms'),
      { rooms },
    );
  }

  renamePlayer(playerId: number, name: string): Observable<VaingloryPlayer> {
    return this.http.patch<VaingloryPlayer>(
      this.url.makeApiUrl(`/api/v1/vainglory/players/${playerId}`),
      { name },
    );
  }

  deletePlayer(playerId: number): Observable<void> {
    return this.http.delete<void>(
      this.url.makeApiUrl(`/api/v1/vainglory/players/${playerId}`),
    );
  }

  bindPlayerRoom(
    playerId: number,
    roomId: number,
  ): Observable<VaingloryPlayer> {
    return this.http.put<VaingloryPlayer>(
      this.url.makeApiUrl(
        `/api/v1/vainglory/players/${playerId}/rooms/${roomId}`,
      ),
      null,
    );
  }

  unbindPlayerRoom(
    playerId: number,
    roomId: number,
  ): Observable<VaingloryPlayer> {
    return this.http.delete<VaingloryPlayer>(
      this.url.makeApiUrl(
        `/api/v1/vainglory/players/${playerId}/rooms/${roomId}`,
      ),
    );
  }

  listPlayerStats(): Observable<readonly VaingloryPlayerStats[]> {
    return this.http.get<readonly VaingloryPlayerStats[]>(
      this.url.makeApiUrl('/api/v1/vainglory/stats/players'),
    );
  }

  listHeroStats(
    gameMode: GameMode | '' = '',
  ): Observable<readonly VaingloryHeroStats[]> {
    const options = gameMode
      ? { params: new HttpParams().set('gameMode', gameMode) }
      : {};
    return this.http.get<readonly VaingloryHeroStats[]>(
      this.url.makeApiUrl('/api/v1/vainglory/stats/heroes'),
      options,
    );
  }

  listHeroes(): Observable<readonly VaingloryHero[]> {
    return this.http.get<readonly VaingloryHero[]>(
      this.url.makeApiUrl('/api/v1/vainglory/heroes'),
    );
  }

  labelHero(heroId: number, label: string): Observable<VaingloryHero> {
    return this.http.patch<VaingloryHero>(
      this.url.makeApiUrl(`/api/v1/vainglory/heroes/${heroId}`),
      { label },
    );
  }

  updateMatchTitle(matchId: number, title: string): Observable<VaingloryMatch> {
    return this.updateMatch(matchId, { title });
  }

  updateMatch(
    matchId: number,
    update: VaingloryMatchUpdate,
  ): Observable<VaingloryMatch> {
    return this.http.patch<VaingloryMatch>(
      this.url.makeApiUrl(`/api/v1/vainglory/matches/${matchId}`),
      update,
    );
  }

  reanalyzeMatch(matchId: number): Observable<void> {
    return this.http.post<void>(
      this.url.makeApiUrl(`/api/v1/vainglory/matches/${matchId}/reanalyze`),
      null,
    );
  }

  suppressMatchReview(
    matchId: number,
    reviewType: 'hero' | 'recorded_player',
  ): Observable<void> {
    return this.http.put<void>(
      this.url.makeApiUrl(
        `/api/v1/vainglory/matches/${matchId}/review-suppressions/${reviewType}`,
      ),
      null,
    );
  }

  deleteMatch(matchId: number): Observable<void> {
    return this.http.delete<void>(
      this.url.makeApiUrl(`/api/v1/vainglory/matches/${matchId}`),
    );
  }

  updateSessionTitle(
    sessionId: number,
    title: string,
  ): Observable<VaingloryMatchSession> {
    return this.http.patch<VaingloryMatchSession>(
      this.url.makeApiUrl(`/api/v1/vainglory/sessions/${sessionId}`),
      { title },
    );
  }

  updateSessionAnchor(
    sessionId: number,
    anchorName: string,
  ): Observable<VaingloryMatchSession> {
    return this.http.patch<VaingloryMatchSession>(
      this.url.makeApiUrl(`/api/v1/vainglory/sessions/${sessionId}/anchor`),
      { anchorName },
    );
  }

  bulkUpdateSessions(
    sessionIds: readonly number[],
    update: { readonly anchorName?: string; readonly statsIncluded?: boolean },
  ): Observable<{ readonly updatedCount: number }> {
    return this.http.patch<{ readonly updatedCount: number }>(
      this.url.makeApiUrl('/api/v1/vainglory/sessions/bulk-update'),
      { sessionIds, ...update },
    );
  }

  requestArchiveSync(accountId: number): Observable<VaingloryArchiveSync> {
    return this.http.post<VaingloryArchiveSync>(
      this.url.makeApiUrl(`/api/v1/vainglory/archive-syncs/${accountId}`),
      null,
    );
  }

  getArchiveSync(accountId: number): Observable<VaingloryArchiveSync> {
    return this.http.get<VaingloryArchiveSync>(
      this.url.makeApiUrl(`/api/v1/vainglory/archive-syncs/${accountId}`),
    );
  }

  listArchiveSyncItems(
    accountId: number,
    limit = 30,
  ): Observable<readonly VaingloryArchiveBackfillItem[]> {
    return this.http.get<readonly VaingloryArchiveBackfillItem[]>(
      this.url.makeApiUrl(
        `/api/v1/vainglory/archive-syncs/${accountId}/items`,
      ),
      { params: new HttpParams().set('limit', limit) },
    );
  }

  updateArchiveSync(
    accountId: number,
    control: VaingloryArchiveSyncControl,
  ): Observable<VaingloryArchiveSync> {
    return this.http.patch<VaingloryArchiveSync>(
      this.url.makeApiUrl(`/api/v1/vainglory/archive-syncs/${accountId}`),
      control,
    );
  }

  listArchiveContentReviews(
    limit = 20,
    offset = 0,
  ): Observable<VaingloryArchiveContentReviewList> {
    return this.http.get<VaingloryArchiveContentReviewList>(
      this.url.makeApiUrl('/api/v1/vainglory/archive-content-reviews'),
      {
        params: new HttpParams().set('limit', limit).set('offset', offset),
      },
    );
  }
}
