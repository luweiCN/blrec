import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import {
  BiliCollectionCatalog,
  BiliCollectionCreateRequest,
  BiliCollectionCreation,
  CoverAsset,
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

  covers(): Observable<CoverAsset[]> {
    const url = this.url.makeApiUrl('/api/v1/upload-covers');
    return this.http.get<CoverAsset[]>(url);
  }

  coverContent(assetId: number): Observable<Blob> {
    const url = this.url.makeApiUrl(
      `/api/v1/upload-covers/${assetId}/content`,
    );
    return this.http.get(url, { responseType: 'blob' });
  }

  uploadCover(file: File): Observable<CoverAsset> {
    const url = this.url.makeApiUrl('/api/v1/upload-covers');
    const params = new HttpParams().set('filename', file.name);
    return this.http.post<CoverAsset>(url, file, { params });
  }

  collections(
    accountMode: UploadAccountMode,
    accountId: number | null,
  ): Observable<BiliCollectionCatalog> {
    const url = this.url.makeApiUrl('/api/v1/bili-collections');
    let params = new HttpParams().set('accountMode', accountMode);
    if (accountMode === 'fixed' && accountId !== null) {
      params = params.set('accountId', String(accountId));
    }
    return this.http.get<BiliCollectionCatalog>(url, { params });
  }

  createCollection(
    request: BiliCollectionCreateRequest,
  ): Observable<BiliCollectionCreation> {
    const url = this.url.makeApiUrl('/api/v1/bili-collections');
    return this.http.post<BiliCollectionCreation>(url, request);
  }
}
