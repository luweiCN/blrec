import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import {
  DanmakuDecisionRequest,
  RecordingDanmakuPage,
  RecordingMediaAccess,
  RecordingSessionsResponse,
} from './recording-session.model';

@Injectable({ providedIn: 'root' })
export class RecordingSessionService {
  constructor(private http: HttpClient, private url: UrlService) {}

  listSessions(limit = 20, offset = 0): Observable<RecordingSessionsResponse> {
    const path = `/api/v1/recording-sessions?limit=${limit}&offset=${offset}`;
    return this.http.get<RecordingSessionsResponse>(this.url.makeApiUrl(path));
  }

  decideDanmakuItem(
    itemId: number,
    request: DanmakuDecisionRequest
  ): Observable<void> {
    const path = `/api/v1/recording-sessions/danmaku-items/${itemId}/decision`;
    return this.http.post<void>(this.url.makeApiUrl(path), request);
  }

  createMediaAccess(partId: number): Observable<RecordingMediaAccess> {
    const path = `/api/v1/recording-sessions/parts/${partId}/media-access`;
    return this.http.post<RecordingMediaAccess>(
      this.url.makeApiUrl(path),
      null
    );
  }

  mediaUrl(partId: number, access: RecordingMediaAccess): string {
    const token = encodeURIComponent(access.token);
    const path =
      `/api/v1/recording-sessions/parts/${partId}/media` +
      `?media_token=${token}&media_expires=${access.expiresAt}`;
    return this.url.makeApiUrl(path);
  }

  listDanmaku(
    partId: number,
    cursor = 0,
    limit = 100
  ): Observable<RecordingDanmakuPage> {
    const path =
      `/api/v1/recording-sessions/parts/${partId}/danmaku` +
      `?cursor=${cursor}&limit=${limit}`;
    return this.http.get<RecordingDanmakuPage>(this.url.makeApiUrl(path));
  }
}
