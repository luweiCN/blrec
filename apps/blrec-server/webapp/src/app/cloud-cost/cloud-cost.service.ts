import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';

import { UrlService } from '../core/services/url.service';
import { CloudCostSummary } from './cloud-cost.model';

@Injectable({ providedIn: 'root' })
export class CloudCostService {
  constructor(
    private readonly http: HttpClient,
    private readonly url: UrlService,
  ) {}

  summary(refresh = false): Observable<CloudCostSummary> {
    const params = new HttpParams().set('refresh', refresh);
    return this.http.get<CloudCostSummary>(
      this.url.makeApiUrl('/api/v1/cloud-cost/summary'),
      { params },
    );
  }
}
