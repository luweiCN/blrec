import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import {
  RecordingSessionAction,
  RecordingSessionActionResponse,
  RecordingDanmakuPage,
  RecordingMediaAccess,
  UploadJobRetryPreviewResponse,
  RecordingSessionFilters,
  RecordingSessionsResponse,
  UploadJobAction,
  UploadJobActionResponse,
  UploadTaskSettings,
  UploadTaskSettingsUpdateResponse,
} from './recording-session.model';

@Injectable({ providedIn: 'root' })
export class RecordingSessionService {
  constructor(
    private http: HttpClient,
    private url: UrlService,
  ) {}

  listSessions(
    limit = 20,
    offset = 0,
    filters?: RecordingSessionFilters,
  ): Observable<RecordingSessionsResponse> {
    const path = '/api/v1/recording-sessions';
    let params = new HttpParams()
      .set('limit', limit)
      .set('offset', offset)
      .set('scope', filters?.scope ?? 'recordings');
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
    return this.http.get<RecordingSessionsResponse>(this.url.makeApiUrl(path), {
      params,
    });
  }

  runJobAction(
    action: UploadJobAction,
    jobIds: readonly number[],
  ): Observable<UploadJobActionResponse> {
    const path = '/api/v1/recording-sessions/upload-jobs/actions';
    return this.http.post<UploadJobActionResponse>(this.url.makeApiUrl(path), {
      action,
      jobIds,
    });
  }

  runSessionAction(
    action: RecordingSessionAction,
    sessionIds: readonly number[],
  ): Observable<RecordingSessionActionResponse> {
    const path = '/api/v1/recording-sessions/actions';
    return this.http.post<RecordingSessionActionResponse>(
      this.url.makeApiUrl(path),
      { action, sessionIds },
    );
  }

  retryFailedJobs(): Observable<UploadJobActionResponse> {
    const path = '/api/v1/recording-sessions/upload-jobs/retry-failed';
    return this.http.post<UploadJobActionResponse>(
      this.url.makeApiUrl(path),
      null,
    );
  }

  previewRetryFailedJobs(): Observable<UploadJobRetryPreviewResponse> {
    const path = '/api/v1/recording-sessions/upload-jobs/retry-failed-preview';
    return this.http.get<UploadJobRetryPreviewResponse>(
      this.url.makeApiUrl(path),
    );
  }

  getTaskSettings(jobId: number): Observable<UploadTaskSettings> {
    const path = `/api/v1/recording-sessions/upload-jobs/${jobId}/settings`;
    return this.http.get<UploadTaskSettings>(this.url.makeApiUrl(path));
  }

  updateTaskSettings(
    jobId: number,
    accountId: number,
    changes: Readonly<Record<string, unknown>>,
  ): Observable<UploadTaskSettingsUpdateResponse> {
    const path = `/api/v1/recording-sessions/upload-jobs/${jobId}/settings`;
    return this.http.put<UploadTaskSettingsUpdateResponse>(
      this.url.makeApiUrl(path),
      { accountId, changes },
    );
  }

  createMediaAccess(partId: number): Observable<RecordingMediaAccess> {
    const path = `/api/v1/recording-sessions/parts/${partId}/media-access`;
    return this.http.post<RecordingMediaAccess>(
      this.url.makeApiUrl(path),
      null,
    );
  }

  mediaUrl(partId: number, access: RecordingMediaAccess): string {
    const token = encodeURIComponent(access.token);
    let path =
      `/api/v1/recording-sessions/parts/${partId}/media` +
      `?media_token=${token}&media_expires=${access.expiresAt}`;
    if (access.snapshotId !== null) {
      path += `&media_snapshot=${encodeURIComponent(access.snapshotId)}`;
    }
    return this.url.makeApiUrl(path);
  }

  listDanmaku(
    partId: number,
    cursor = 0,
    limit = 100,
  ): Observable<RecordingDanmakuPage> {
    const path =
      `/api/v1/recording-sessions/parts/${partId}/danmaku` +
      `?cursor=${cursor}&limit=${limit}`;
    return this.http.get<RecordingDanmakuPage>(this.url.makeApiUrl(path));
  }
}
