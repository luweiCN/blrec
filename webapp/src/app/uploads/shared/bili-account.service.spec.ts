import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { UrlService } from 'src/app/core/services/url.service';
import { BiliAccountService } from './bili-account.service';

describe('BiliAccountService', () => {
  let service: BiliAccountService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
    service = TestBed.inject(BiliAccountService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('uses the redacted account endpoint', () => {
    service.listAccounts().subscribe();

    const request = http.expectOne('/api/v1/bili-accounts');
    expect(request.request.method).toBe('GET');
    request.flush([]);
  });

  it('creates, polls, and cancels one QR session', () => {
    service.createQrSession().subscribe();
    let request = http.expectOne('/api/v1/bili-accounts/qr-sessions');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toBeNull();
    request.flush({});

    service.getQrSession('session-1').subscribe();
    request = http.expectOne(
      '/api/v1/bili-accounts/qr-sessions/session-1'
    );
    expect(request.request.method).toBe('GET');
    request.flush({});

    service.cancelQrSession('session-1').subscribe();
    request = http.expectOne(
      '/api/v1/bili-accounts/qr-sessions/session-1'
    );
    expect(request.request.method).toBe('DELETE');
    request.flush({});
  });

  it('requests a bounded credential renewal check', () => {
    service.checkRenewal(7).subscribe();

    const request = http.expectOne('/api/v1/bili-accounts/7/refresh');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toBeNull();
    request.flush({ credentialVersion: 4, refreshed: true });
  });

  it('selects one account as the primary account', () => {
    service.setPrimaryAccount(7).subscribe();

    const request = http.expectOne('/api/v1/bili-accounts/7/primary');
    expect(request.request.method).toBe('PUT');
    expect(request.request.body).toBeNull();
    request.flush({ id: 7, isPrimary: true });
  });

  it('loads account relationships before a destructive action', () => {
    service.getRelationships(7).subscribe();

    const request = http.expectOne(
      '/api/v1/bili-accounts/7/relationships'
    );
    expect(request.request.method).toBe('GET');
    request.flush({ accountId: 7, blockingJobs: [] });
  });

  it('removes an account with one explicit relationship policy', () => {
    service
      .removeAccount(7, {
        mode: 'fixed',
        replacementAccountId: 8,
        newPrimaryAccountId: 9,
      })
      .subscribe();

    const request = http.expectOne('/api/v1/bili-accounts/7/removal');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      mode: 'fixed',
      replacementAccountId: 8,
      newPrimaryAccountId: 9,
    });
    request.flush({ accountId: 7, state: 'archived' });
  });
});
