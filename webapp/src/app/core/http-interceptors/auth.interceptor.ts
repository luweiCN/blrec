import { Injectable } from '@angular/core';
import {
  HttpRequest,
  HttpHandler,
  HttpEvent,
  HttpInterceptor,
  HttpErrorResponse,
} from '@angular/common/http';
import { Observable, throwError } from 'rxjs';
import { catchError } from 'rxjs/operators';

import { AuthService } from '../services/auth.service';

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

@Injectable()
export class AuthInterceptor implements HttpInterceptor {
  constructor(private auth: AuthService) {}

  intercept(
    request: HttpRequest<unknown>,
    next: HttpHandler
  ): Observable<HttpEvent<unknown>> {
    let authenticatedRequest = request.clone({ withCredentials: true });
    const csrfToken = this.auth.csrfToken;
    if (!SAFE_METHODS.has(request.method) && csrfToken) {
      authenticatedRequest = authenticatedRequest.clone({
        setHeaders: { 'X-CSRF-Token': csrfToken },
      });
    }
    return next.handle(authenticatedRequest).pipe(
      catchError((error: HttpErrorResponse) => {
        if (error.status === 401) {
          this.auth.handleUnauthorized();
        }
        return throwError(() => error);
      })
    );
  }
}
