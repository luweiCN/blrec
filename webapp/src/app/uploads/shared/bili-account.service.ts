import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import { BiliAccount, QrSession, RefreshResult } from './bili-account.model';

@Injectable({ providedIn: 'root' })
export class BiliAccountService {
  constructor(private http: HttpClient, private url: UrlService) {}

  listAccounts(): Observable<BiliAccount[]> {
    const url = this.url.makeApiUrl('/api/v1/bili-accounts');
    return this.http.get<BiliAccount[]>(url);
  }

  createQrSession(): Observable<QrSession> {
    const url = this.url.makeApiUrl('/api/v1/bili-accounts/qr-sessions');
    return this.http.post<QrSession>(url, null);
  }

  getQrSession(sessionId: string): Observable<QrSession> {
    const url = this.url.makeApiUrl(
      `/api/v1/bili-accounts/qr-sessions/${encodeURIComponent(sessionId)}`
    );
    return this.http.get<QrSession>(url);
  }

  cancelQrSession(sessionId: string): Observable<QrSession> {
    const url = this.url.makeApiUrl(
      `/api/v1/bili-accounts/qr-sessions/${encodeURIComponent(sessionId)}`
    );
    return this.http.delete<QrSession>(url);
  }

  refreshAccount(accountId: number): Observable<RefreshResult> {
    const url = this.url.makeApiUrl(
      `/api/v1/bili-accounts/${accountId}/refresh`
    );
    return this.http.post<RefreshResult>(url, null);
  }
}
