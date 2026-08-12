import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';

import { UrlService } from '../core/services/url.service';
import {
  VisitorAnalyticsQuery,
  VisitorAnalyticsSummary,
} from './visitor-analytics.model';

@Injectable({ providedIn: 'root' })
export class VisitorAnalyticsService {
  constructor(
    private readonly http: HttpClient,
    private readonly url: UrlService,
  ) {}

  summary(
    query: VisitorAnalyticsQuery,
    refresh = false,
  ): Observable<VisitorAnalyticsSummary> {
    let params = new HttpParams()
      .set('startAt', query.startAt.toISOString())
      .set('endAt', query.endAt.toISOString())
      .set('event', query.event)
      .set('refresh', refresh);
    const filters: Array<[string, string]> = [
      ['page', query.page],
      ['country', query.country],
      ['province', query.province],
      ['city', query.city],
      ['provider', query.provider],
      ['source', query.source],
      ['device', query.device],
      ['browser', query.browser],
    ];
    for (const [key, value] of filters) {
      if (value.trim()) {
        params = params.set(key, value.trim());
      }
    }
    return this.http.get<VisitorAnalyticsSummary>(
      this.url.makeApiUrl('/api/v1/visitor-analytics/summary'),
      { params },
    );
  }
}
