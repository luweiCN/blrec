import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

import { Observable } from 'rxjs';

import { UrlService } from 'src/app/core/services/url.service';

export interface BrowserExtensionToken {
  readonly id: number;
  readonly createdAt: number;
  readonly lastUsedAt: number;
  readonly revokedAt: number | null;
}

@Injectable({ providedIn: 'root' })
export class BrowserExtensionTokenService {
  constructor(private http: HttpClient, private url: UrlService) {}

  list(): Observable<readonly BrowserExtensionToken[]> {
    return this.http.get<readonly BrowserExtensionToken[]>(
      this.url.makeApiUrl('/api/v1/auth/extensions')
    );
  }

  revoke(tokenId: number): Observable<void> {
    return this.http.delete<void>(
      this.url.makeApiUrl(`/api/v1/auth/extensions/${tokenId}`)
    );
  }
}
