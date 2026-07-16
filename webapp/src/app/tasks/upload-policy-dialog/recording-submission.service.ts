import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import { RoomUploadPolicyRequest } from './room-upload-policy.model';

export type RecordingSubmissionDecision = 'follow_room' | 'upload' | 'skip';

export interface RecordingSubmissionSettings {
  readonly sessionId: number;
  readonly roomId: number;
  readonly decision: RecordingSubmissionDecision;
  readonly inherited: boolean;
  readonly settingsSource: 'session' | 'room' | 'default';
  readonly resolutionState:
    | 'pending'
    | 'not_requested'
    | 'configuration_required'
    | 'job_created';
  readonly resolutionError: string | null;
  readonly settings: RoomUploadPolicyRequest;
}

@Injectable({ providedIn: 'root' })
export class RecordingSubmissionService {
  constructor(
    private http: HttpClient,
    private url: UrlService,
  ) {}

  get(sessionId: number): Observable<RecordingSubmissionSettings> {
    return this.http.get<RecordingSubmissionSettings>(
      this.settingsUrl(sessionId),
    );
  }

  save(
    sessionId: number,
    settings: RoomUploadPolicyRequest,
  ): Observable<RecordingSubmissionSettings> {
    return this.http.put<RecordingSubmissionSettings>(
      this.settingsUrl(sessionId),
      settings,
    );
  }

  clear(sessionId: number): Observable<RecordingSubmissionSettings> {
    return this.http.delete<RecordingSubmissionSettings>(
      this.settingsUrl(sessionId),
    );
  }

  setDecision(
    sessionId: number,
    decision: RecordingSubmissionDecision,
  ): Observable<RecordingSubmissionSettings> {
    const url = this.url.makeApiUrl(
      `/api/v1/recording-sessions/${sessionId}/submission-decision`,
    );
    return this.http.patch<RecordingSubmissionSettings>(url, { decision });
  }

  private settingsUrl(sessionId: number): string {
    return this.url.makeApiUrl(
      `/api/v1/recording-sessions/${sessionId}/submission-settings`,
    );
  }
}
