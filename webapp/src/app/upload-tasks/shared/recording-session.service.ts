import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import {
  DanmakuDecisionRequest,
  RecordingDanmakuPage,
  RecordingMediaAccess,
  RecordingSessionFilters,
  RecordingSessionsResponse,
  UploadJobAction,
  UploadJobActionResponse,
} from './recording-session.model';

@Injectable({ providedIn: 'root' })
export class RecordingSessionService {
  constructor(private http: HttpClient, private url: UrlService) {}

  listSessions(
    limit = 20,
    offset = 0,
    filters?: RecordingSessionFilters
  ): Observable<RecordingSessionsResponse> {
    const path = '/api/v1/recording-sessions';
    let params = new HttpParams()
      .set('limit', limit)
      .set('offset', offset);
    if (filters) {
      if (filters.query.trim()) {
        params = params.set('q', filters.query.trim());
      }
      if (filters.recordingState) {
        params = params.set('recordingState', filters.recordingState);
      }
      if (filters.uploadState) {
        params = params.set('uploadState', filters.uploadState);
      }
      if (filters.startedFrom !== null) {
        params = params.set('startedFrom', filters.startedFrom);
      }
      if (filters.startedTo !== null) {
        params = params.set('startedTo', filters.startedTo);
      }
      params = params.set('sort', filters.sort);
    }
    return this.http.get<RecordingSessionsResponse>(
      this.url.makeApiUrl(path),
      { params }
    );
  }

  decideDanmakuItem(
    itemId: number,
    request: DanmakuDecisionRequest
  ): Observable<void> {
    const path = `/api/v1/recording-sessions/danmaku-items/${itemId}/decision`;
    return this.http.post<void>(this.url.makeApiUrl(path), request);
  }

  runJobAction(
    action: UploadJobAction,
    jobIds: readonly number[]
  ): Observable<UploadJobActionResponse> {
    const path = '/api/v1/recording-sessions/upload-jobs/actions';
    return this.http.post<UploadJobActionResponse>(
      this.url.makeApiUrl(path),
      { action, jobIds }
    );
  }

  retryFailedJobs(): Observable<UploadJobActionResponse> {
    const path = '/api/v1/recording-sessions/upload-jobs/retry-failed';
    return this.http.post<UploadJobActionResponse>(
      this.url.makeApiUrl(path),
      null
    );
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
