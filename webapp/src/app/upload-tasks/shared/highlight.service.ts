import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import { RoomUploadPolicyRequest } from 'src/app/tasks/upload-policy-dialog/room-upload-policy.model';
import {
  CreateHighlightClipRequest,
  HighlightClip,
  HighlightClipList,
  HighlightClipInspection,
  HighlightMarker,
  HighlightMediaAccess,
  HighlightTimeline,
  HighlightUploadSessionResponse,
  HighlightUploadTaskResponse,
} from './highlight.model';

@Injectable({ providedIn: 'root' })
export class HighlightService {
  constructor(
    private http: HttpClient,
    private url: UrlService,
  ) {}

  getTimeline(sessionId: number): Observable<HighlightTimeline> {
    return this.http.get<HighlightTimeline>(
      this.url.makeApiUrl(`/api/v1/highlights/sessions/${sessionId}/timeline`),
    );
  }

  inspectClip(
    sessionId: number,
    startMs: number,
    endMs: number,
  ): Observable<HighlightClipInspection> {
    const path = `/api/v1/highlights/sessions/${sessionId}/clips/inspect`;
    return this.http.post<HighlightClipInspection>(this.url.makeApiUrl(path), {
      startMs,
      endMs,
    });
  }

  createClip(
    sessionId: number,
    request: CreateHighlightClipRequest,
  ): Observable<HighlightClip> {
    const path = `/api/v1/highlights/sessions/${sessionId}/clips`;
    return this.http.post<HighlightClip>(this.url.makeApiUrl(path), request);
  }

  listClips(sessionId: number): Observable<readonly HighlightClip[]> {
    return this.http.get<readonly HighlightClip[]>(
      this.url.makeApiUrl(`/api/v1/highlights/sessions/${sessionId}/clips`),
    );
  }

  listAllClips(limit: number, offset: number): Observable<HighlightClipList> {
    const path = `/api/v1/highlights/clips?limit=${limit}&offset=${offset}`;
    return this.http.get<HighlightClipList>(this.url.makeApiUrl(path));
  }

  getClip(clipId: number): Observable<HighlightClip> {
    return this.http.get<HighlightClip>(
      this.url.makeApiUrl(`/api/v1/highlights/clips/${clipId}`),
    );
  }

  retryClip(clipId: number): Observable<HighlightClip> {
    return this.http.post<HighlightClip>(
      this.url.makeApiUrl(`/api/v1/highlights/clips/${clipId}/retry`),
      null,
    );
  }

  deleteClip(clipId: number): Observable<void> {
    return this.http.delete<void>(
      this.url.makeApiUrl(`/api/v1/highlights/clips/${clipId}`),
    );
  }

  prepareUploadSession(
    clipId: number,
  ): Observable<HighlightUploadSessionResponse> {
    const path = `/api/v1/highlights/clips/${clipId}/upload-session`;
    return this.http.post<HighlightUploadSessionResponse>(
      this.url.makeApiUrl(path),
      null,
    );
  }

  createUploadTask(
    clipId: number,
    settings: RoomUploadPolicyRequest,
  ): Observable<HighlightUploadTaskResponse> {
    const path = `/api/v1/highlights/clips/${clipId}/upload-task`;
    return this.http.post<HighlightUploadTaskResponse>(
      this.url.makeApiUrl(path),
      settings,
    );
  }

  createMediaAccess(clipId: number): Observable<HighlightMediaAccess> {
    const path = `/api/v1/highlights/clips/${clipId}/media-access`;
    return this.http.post<HighlightMediaAccess>(
      this.url.makeApiUrl(path),
      null,
    );
  }

  mediaUrl(clipId: number, access: HighlightMediaAccess): string {
    const token = encodeURIComponent(access.token);
    const path =
      `/api/v1/highlights/clips/${clipId}/media` +
      `?media_token=${token}&media_expires=${access.expiresAt}`;
    return this.url.makeApiUrl(path);
  }

  downloadUrl(clipId: number, access: HighlightMediaAccess): string {
    return `${this.mediaUrl(clipId, access)}&download=1`;
  }

  updateMarker(
    markerId: number,
    name: string,
    note: string,
  ): Observable<HighlightMarker> {
    return this.http.patch<HighlightMarker>(
      this.url.makeApiUrl(`/api/v1/highlights/${markerId}`),
      { name, note },
    );
  }

  deleteMarker(markerId: number): Observable<void> {
    return this.http.delete<void>(
      this.url.makeApiUrl(`/api/v1/highlights/${markerId}`),
    );
  }
}
