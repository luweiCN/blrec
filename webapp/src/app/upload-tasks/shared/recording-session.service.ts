import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import {
  DanmakuDecisionRequest,
  RecordingSessionsResponse,
} from './recording-session.model';

@Injectable({ providedIn: 'root' })
export class RecordingSessionService {
  constructor(private http: HttpClient, private url: UrlService) {}

  listSessions(limit = 50): Observable<RecordingSessionsResponse> {
    const path = `/api/v1/recording-sessions?limit=${limit}`;
    return this.http.get<RecordingSessionsResponse>(this.url.makeApiUrl(path));
  }

  decideDanmakuItem(
    itemId: number,
    request: DanmakuDecisionRequest
  ): Observable<void> {
    const path = `/api/v1/recording-sessions/danmaku-items/${itemId}/decision`;
    return this.http.post<void>(this.url.makeApiUrl(path), request);
  }
}
