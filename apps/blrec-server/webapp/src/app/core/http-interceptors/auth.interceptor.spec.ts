import {
  HttpErrorResponse,
  HttpHandler,
  HttpRequest,
  HttpResponse,
} from '@angular/common/http';

import { defer, of, throwError } from 'rxjs';

import { AuthService } from '../services/auth.service';
import { AuthInterceptor } from './auth.interceptor';

describe('AuthInterceptor', () => {
  let auth: jasmine.SpyObj<AuthService>;
  let interceptor: AuthInterceptor;

  beforeEach(() => {
    auth = jasmine.createSpyObj<AuthService>(
      'AuthService',
      ['handleUnauthorized'],
      { csrfToken: 'csrf-token' }
    );
    interceptor = new AuthInterceptor(auth);
  });

  it('uses cookies and never sends an API key', () => {
    const next: HttpHandler = {
      handle: (request) => {
        expect(request.withCredentials).toBeTrue();
        expect(request.headers.has('X-API-KEY')).toBeFalse();
        expect(request.headers.has('X-CSRF-Token')).toBeFalse();
        return of(new HttpResponse({ status: 200 }));
      },
    };

    interceptor
      .intercept(new HttpRequest('GET', '/api/v1/bili-accounts'), next)
      .subscribe();
  });

  it('adds CSRF only to state-changing requests', () => {
    const next: HttpHandler = {
      handle: (request) => {
        expect(request.withCredentials).toBeTrue();
        expect(request.headers.get('X-CSRF-Token')).toBe('csrf-token');
        return of(new HttpResponse({ status: 200 }));
      },
    };

    interceptor
      .intercept(new HttpRequest('POST', '/api/v1/write', {}), next)
      .subscribe();
  });

  it('does not prompt or replay requests after a 401', () => {
    spyOn(window, 'prompt');
    let calls = 0;
    const next: HttpHandler = {
      handle: () =>
        defer(() => {
          calls += 1;
          return throwError(() => new HttpErrorResponse({ status: 401 }));
        }),
    };

    interceptor
      .intercept(new HttpRequest('GET', '/api/v1/bili-accounts'), next)
      .subscribe({ error: () => undefined });

    expect(calls).toBe(1);
    expect(window.prompt).not.toHaveBeenCalled();
    expect(auth.handleUnauthorized).toHaveBeenCalledTimes(1);
  });
});
