import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';
import {
  AccountRelationships,
  AccountRemovalRequest,
  AccountRemovalResult,
  ArchiveMigrationItem,
  ArchiveMigrationItemPage,
  ArchiveMigrationControl,
  ArchiveMigrationRequest,
  ArchiveMigrationStatus,
  BiliAccount,
  QrSession,
  RefreshResult,
} from './bili-account.model';

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

  checkRenewal(accountId: number): Observable<RefreshResult> {
    const url = this.url.makeApiUrl(
      `/api/v1/bili-accounts/${accountId}/refresh`
    );
    return this.http.post<RefreshResult>(url, null);
  }

  setPrimaryAccount(accountId: number): Observable<BiliAccount> {
    const url = this.url.makeApiUrl(
      `/api/v1/bili-accounts/${accountId}/primary`
    );
    return this.http.put<BiliAccount>(url, null);
  }

  getRelationships(accountId: number): Observable<AccountRelationships> {
    const url = this.url.makeApiUrl(
      `/api/v1/bili-accounts/${accountId}/relationships`
    );
    return this.http.get<AccountRelationships>(url);
  }

  removeAccount(
    accountId: number,
    request: AccountRemovalRequest
  ): Observable<AccountRemovalResult> {
    const url = this.url.makeApiUrl(
      `/api/v1/bili-accounts/${accountId}/removal`
    );
    return this.http.post<AccountRemovalResult>(url, request);
  }

  listArchiveMigrations(): Observable<ArchiveMigrationStatus[]> {
    const url = this.url.makeApiUrl(
      '/api/v1/bili-accounts/archive-migrations'
    );
    return this.http.get<ArchiveMigrationStatus[]>(url);
  }

  requestArchiveMigration(
    request: ArchiveMigrationRequest
  ): Observable<ArchiveMigrationStatus> {
    const url = this.url.makeApiUrl(
      '/api/v1/bili-accounts/archive-migrations'
    );
    return this.http.post<ArchiveMigrationStatus>(url, request);
  }

  listArchiveMigrationItems(
    migrationId: number
  ): Observable<ArchiveMigrationItem[]> {
    const url = this.url.makeApiUrl(
      `/api/v1/bili-accounts/archive-migrations/${migrationId}/items`
    );
    return this.http.get<ArchiveMigrationItem[]>(url);
  }

  listArchiveMigrationItemPage(
    migrationId: number,
    limit = 20,
    offset = 0
  ): Observable<ArchiveMigrationItemPage> {
    const url = this.url.makeApiUrl(
      `/api/v1/bili-accounts/archive-migrations/${migrationId}/item-page`
    );
    return this.http.get<ArchiveMigrationItemPage>(url, {
      params: { limit, offset },
    });
  }

  retryArchiveMigrationItem(
    migrationId: number,
    itemId: number
  ): Observable<ArchiveMigrationStatus> {
    const url = this.url.makeApiUrl(
      `/api/v1/bili-accounts/archive-migrations/${migrationId}/items/${itemId}/retry`
    );
    return this.http.post<ArchiveMigrationStatus>(url, null);
  }

  updateArchiveMigration(
    migrationId: number,
    control: ArchiveMigrationControl
  ): Observable<ArchiveMigrationStatus> {
    const url = this.url.makeApiUrl(
      `/api/v1/bili-accounts/archive-migrations/${migrationId}`
    );
    return this.http.patch<ArchiveMigrationStatus>(url, control);
  }
}
