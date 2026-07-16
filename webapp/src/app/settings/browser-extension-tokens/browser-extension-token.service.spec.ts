import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from 'src/app/core/services/url.service';
import { BrowserExtensionTokenService } from './browser-extension-token.service';

describe('BrowserExtensionTokenService', () => {
  let service: BrowserExtensionTokenService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        BrowserExtensionTokenService,
        { provide: UrlService, useValue: { makeApiUrl: (path: string) => path } },
      ],
    });
    service = TestBed.inject(BrowserExtensionTokenService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('lists extension authorizations without token material', () => {
    service.list().subscribe();

    const request = http.expectOne('/api/v1/auth/extensions');
    expect(request.request.method).toBe('GET');
    request.flush([]);
  });

  it('revokes one extension authorization', () => {
    service.revoke(7).subscribe();

    const request = http.expectOne('/api/v1/auth/extensions/7');
    expect(request.request.method).toBe('DELETE');
    request.flush(null, { status: 204, statusText: 'No Content' });
  });
});
