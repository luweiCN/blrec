import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import {
  RoomUploadPolicy,
  RoomUploadPolicyRequest,
} from './room-upload-policy.model';

@Injectable({ providedIn: 'root' })
export class RoomUploadPolicyService {
  constructor(
    private http: HttpClient,
    private url: UrlService,
  ) {}

  list(): Observable<RoomUploadPolicy[]> {
    const url = this.url.makeApiUrl('/api/v1/room-upload-policies');
    return this.http.get<RoomUploadPolicy[]>(url);
  }

  save(
    roomId: number,
    request: RoomUploadPolicyRequest,
  ): Observable<RoomUploadPolicy> {
    const url = this.url.makeApiUrl(`/api/v1/room-upload-policies/${roomId}`);
    return this.http.put<RoomUploadPolicy>(url, request);
  }

  delete(roomId: number): Observable<void> {
    const url = this.url.makeApiUrl(`/api/v1/room-upload-policies/${roomId}`);
    return this.http.delete<void>(url);
  }
}
