import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';

import { UrlService } from './url.service';
import { AuthService } from './auth.service';

describe('AuthService', () => {
  let service: AuthService;
  let http: HttpTestingController;
  let router: jasmine.SpyObj<Router>;

  beforeEach(() => {
    router = jasmine.createSpyObj<Router>('Router', ['navigateByUrl']);
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        { provide: Router, useValue: router },
        {
          provide: UrlService,
          useValue: { makeApiUrl: (path: string) => path },
        },
      ],
    });
    service = TestBed.inject(AuthService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('loads the existing session and keeps only the CSRF token in memory', () => {
    let authenticated = false;
    service.ensureSession().subscribe((value) => (authenticated = value));
    http.expectOne('/api/v1/auth/session').flush({
      authenticated: true,
      csrfToken: 'csrf-token',
      expiresAt: 123,
    });

    expect(authenticated).toBeTrue();
    expect(service.csrfToken).toBe('csrf-token');
    expect(localStorage.getItem('app-api-key')).toBeNull();
  });

  it('initializes the administrator with the one-time API key', () => {
    service
      .setup('owner', 'bootstrap-key', 'correct horse battery staple')
      .subscribe();
    const request = http.expectOne('/api/v1/auth/setup');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      username: 'owner',
      apiKey: 'bootstrap-key',
      password: 'correct horse battery staple',
    });
    request.flush({
      authenticated: true,
      csrfToken: 'csrf-token',
      expiresAt: 123,
    });
    expect(service.csrfToken).toBe('csrf-token');
  });

  it('logs in with a username and password without sending the API key', () => {
    service.login('owner', 'correct horse battery staple').subscribe();

    const request = http.expectOne('/api/v1/auth/login');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      username: 'owner',
      password: 'correct horse battery staple',
    });
    expect(request.request.body.apiKey).toBeUndefined();
    request.flush({
      authenticated: true,
      csrfToken: 'csrf-token',
      expiresAt: 123,
    });
  });

  it('recovers the password with all initialization credentials', () => {
    service
      .recover('owner', 'bootstrap-key', 'new correct password')
      .subscribe();

    const request = http.expectOne('/api/v1/auth/recover');
    expect(request.request.body).toEqual({
      username: 'owner',
      apiKey: 'bootstrap-key',
      newPassword: 'new correct password',
    });
    request.flush(null);
  });

  it('clears a rejected session and navigates to login', () => {
    service.handleUnauthorized();

    expect(service.csrfToken).toBe('');
    expect(router.navigateByUrl).toHaveBeenCalledOnceWith('/auth');
  });
});
