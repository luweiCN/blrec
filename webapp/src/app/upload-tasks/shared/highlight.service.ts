import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import {
  CreateHighlightClipRequest,
  HighlightClip,
  HighlightClipInspection,
  HighlightMarker,
  HighlightTimeline,
  HighlightUploadTaskResponse,
} from './highlight.model';

@Injectable({ providedIn: 'root' })
export class HighlightService {
  constructor(private http: HttpClient, private url: UrlService) {}

  getTimeline(sessionId: number): Observable<HighlightTimeline> {
    return this.http.get<HighlightTimeline>(
      this.url.makeApiUrl(
        `/api/v1/highlights/sessions/${sessionId}/timeline`
      )
    );
  }

  inspectClip(
    sessionId: number,
    startMs: number,
    endMs: number
  ): Observable<HighlightClipInspection> {
    const path = `/api/v1/highlights/sessions/${sessionId}/clips/inspect`;
    return this.http.post<HighlightClipInspection>(
      this.url.makeApiUrl(path),
      { startMs, endMs }
    );
  }

  createClip(
    sessionId: number,
    request: CreateHighlightClipRequest
  ): Observable<HighlightClip> {
    const path = `/api/v1/highlights/sessions/${sessionId}/clips`;
    return this.http.post<HighlightClip>(this.url.makeApiUrl(path), request);
  }

  getClip(clipId: number): Observable<HighlightClip> {
    return this.http.get<HighlightClip>(
      this.url.makeApiUrl(`/api/v1/highlights/clips/${clipId}`)
    );
  }

  deleteClip(clipId: number): Observable<void> {
    return this.http.delete<void>(
      this.url.makeApiUrl(`/api/v1/highlights/clips/${clipId}`)
    );
  }

  createUploadTask(clipId: number): Observable<HighlightUploadTaskResponse> {
    const path = `/api/v1/highlights/clips/${clipId}/upload-task`;
    return this.http.post<HighlightUploadTaskResponse>(
      this.url.makeApiUrl(path),
      null
    );
  }

  updateMarker(
    markerId: number,
    name: string,
    note: string
  ): Observable<HighlightMarker> {
    return this.http.patch<HighlightMarker>(
      this.url.makeApiUrl(`/api/v1/highlights/${markerId}`),
      { name, note }
    );
  }

  deleteMarker(markerId: number): Observable<void> {
    return this.http.delete<void>(
      this.url.makeApiUrl(`/api/v1/highlights/${markerId}`)
    );
  }
}
