import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';

export interface RecordingRetentionStatus {
  managedVideoBytes: number;
  capacityBytes: number;
  remainingBytes: number;
  warningThresholdBytes: number;
  warning: boolean;
}

@Injectable({ providedIn: 'root' })
export class RecordingRetentionService {
  constructor(private http: HttpClient, private url: UrlService) {}

  status(): Observable<RecordingRetentionStatus> {
    const url = this.url.makeApiUrl('/api/v1/recording-retention/status');
    return this.http.get<RecordingRetentionStatus>(url);
  }
}
