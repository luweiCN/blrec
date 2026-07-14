import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import {
  RoomUploadPolicy,
  RoomUploadPolicyRequest,
  UploadAccountMode,
  UploadCategoryCatalog,
} from './room-upload-policy.model';

@Injectable({ providedIn: 'root' })
export class RoomUploadPolicyService {
  constructor(
    private http: HttpClient,
    private url: UrlService,
  ) {}

  get(roomId: number): Observable<RoomUploadPolicy> {
    const url = this.url.makeApiUrl(
      `/api/v1/room-upload-policies/${roomId}`,
    );
    return this.http.get<RoomUploadPolicy>(url);
  }

  save(
    roomId: number,
    request: RoomUploadPolicyRequest,
  ): Observable<RoomUploadPolicy> {
    const url = this.url.makeApiUrl(
      `/api/v1/room-upload-policies/${roomId}`,
    );
    return this.http.put<RoomUploadPolicy>(url, request);
  }

  delete(roomId: number): Observable<void> {
    const url = this.url.makeApiUrl(
      `/api/v1/room-upload-policies/${roomId}`,
    );
    return this.http.delete<void>(url);
  }

  categories(
    accountMode: UploadAccountMode,
    accountId: number | null,
    refresh = false,
  ): Observable<UploadCategoryCatalog> {
    const url = this.url.makeApiUrl(
      '/api/v1/room-upload-policies/categories',
    );
    let params = new HttpParams()
      .set('accountMode', accountMode)
      .set('refresh', String(refresh));
    if (accountMode === 'fixed' && accountId !== null) {
      params = params.set('accountId', String(accountId));
    }
    return this.http.get<UploadCategoryCatalog>(url, { params });
  }
}
