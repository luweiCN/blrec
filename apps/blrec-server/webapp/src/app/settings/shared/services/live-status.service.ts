import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import { LiveStatusMetrics } from '../setting.model';

@Injectable({
  providedIn: 'root',
})
export class LiveStatusService {
  constructor(private http: HttpClient, private url: UrlService) {}

  getMetrics(): Observable<LiveStatusMetrics> {
    const url = this.url.makeApiUrl('/api/v1/live-status');
    return this.http.get<LiveStatusMetrics>(url);
  }

  resume(): Observable<void> {
    const url = this.url.makeApiUrl('/api/v1/live-status/resume');
    return this.http.post<void>(url, null);
  }
}
