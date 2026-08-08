import {
  HttpClient,
  HttpEvent,
  HttpParams,
} from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from '../core/services/url.service';
import {
  CreateMediaImportRequest,
  DeleteMediaLibraryItemResponse,
  MediaLibraryItem,
  MediaLibraryKind,
  MediaLibraryList,
  MediaLibraryPart,
  UpdateMediaLibraryItemRequest,
} from './media-library.model';

@Injectable({ providedIn: 'root' })
export class MediaLibraryService {
  constructor(
    private http: HttpClient,
    private url: UrlService,
  ) {}

  list(
    kind: MediaLibraryKind,
    limit: number,
    offset: number,
    query: string,
  ): Observable<MediaLibraryList> {
    let params = new HttpParams()
      .set('kind', kind)
      .set('limit', limit)
      .set('offset', offset);
    if (query.trim()) {
      params = params.set('q', query.trim());
    }
    return this.http.get<MediaLibraryList>(
      this.url.makeApiUrl('/api/v1/media-library'),
      { params },
    );
  }

  favorite(sessionId: number): Observable<MediaLibraryItem> {
    return this.http.post<MediaLibraryItem>(
      this.url.makeApiUrl(`/api/v1/media-library/favorites/${sessionId}`),
      null,
    );
  }

  createImport(
    request: CreateMediaImportRequest,
  ): Observable<MediaLibraryItem> {
    return this.http.post<MediaLibraryItem>(
      this.url.makeApiUrl('/api/v1/media-library/imports'),
      request,
    );
  }

  uploadPart(
    itemId: number,
    partIndex: number,
    file: File,
  ): Observable<HttpEvent<MediaLibraryPart>> {
    const path = `/api/v1/media-library/${itemId}/parts/${partIndex}/content`;
    return this.http.put<MediaLibraryPart>(this.url.makeApiUrl(path), file, {
      observe: 'events',
      reportProgress: true,
      headers: { 'Content-Type': 'application/octet-stream' },
    });
  }

  completeImport(itemId: number): Observable<MediaLibraryItem> {
    return this.http.post<MediaLibraryItem>(
      this.url.makeApiUrl(`/api/v1/media-library/${itemId}/complete`),
      null,
    );
  }

  update(
    itemId: number,
    request: UpdateMediaLibraryItemRequest,
  ): Observable<MediaLibraryItem> {
    return this.http.patch<MediaLibraryItem>(
      this.url.makeApiUrl(`/api/v1/media-library/${itemId}`),
      request,
    );
  }

  delete(itemId: number): Observable<DeleteMediaLibraryItemResponse> {
    return this.http.delete<DeleteMediaLibraryItemResponse>(
      this.url.makeApiUrl(`/api/v1/media-library/${itemId}`),
    );
  }
}
