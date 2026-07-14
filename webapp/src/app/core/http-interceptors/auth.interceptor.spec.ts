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
    auth = jasmine.createSpyObj<AuthService>('AuthService', [
      'getApiKey',
      'hasApiKey',
      'removeApiKey',
      'setApiKey',
    ]);
    auth.getApiKey.and.returnValue('old-api-key');
    auth.hasApiKey.and.returnValue(true);
    interceptor = new AuthInterceptor(auth);
  });

  it('should be created', () => {
    expect(interceptor).toBeTruthy();
  });

  it('does not retry failed write requests automatically', () => {
    let calls = 0;
    const next: HttpHandler = {
      handle: () =>
        defer(() => {
          calls += 1;
          return throwError(
            () =>
              new HttpErrorResponse({ status: 0, statusText: 'network error' })
          );
        }),
    };

    interceptor
      .intercept(
        new HttpRequest('POST', '/api/v1/bili-accounts/7/refresh', null),
        next
      )
      .subscribe({ error: () => undefined });

    expect(calls).toBe(1);
  });

  it('retries once with the newly entered key after a 401', () => {
    spyOn(window, 'prompt').and.returnValue('new-api-key');
    let calls = 0;
    const next: HttpHandler = {
      handle: (request) =>
        defer(() => {
          calls += 1;
          if (request.headers.get('X-API-KEY') === 'old-api-key') {
            return throwError(() => new HttpErrorResponse({ status: 401 }));
          }
          expect(request.headers.get('X-API-KEY')).toBe('new-api-key');
          return of(new HttpResponse({ status: 200 }));
        }),
    };

    interceptor
      .intercept(new HttpRequest('GET', '/api/v1/bili-accounts'), next)
      .subscribe();

    expect(calls).toBe(2);
    expect(auth.removeApiKey).toHaveBeenCalledTimes(1);
    expect(auth.setApiKey).toHaveBeenCalledOnceWith('new-api-key');
  });

  it('stores a replacement key but never replays a mutation after 401', () => {
    spyOn(window, 'prompt').and.returnValue('new-api-key');
    let calls = 0;
    const next: HttpHandler = {
      handle: () =>
        defer(() => {
          calls += 1;
          return throwError(() => new HttpErrorResponse({ status: 401 }));
        }),
    };

    interceptor
      .intercept(new HttpRequest('POST', '/api/v1/write', {}), next)
      .subscribe({ error: () => undefined });

    expect(calls).toBe(1);
    expect(auth.setApiKey).toHaveBeenCalledOnceWith('new-api-key');
  });
});
