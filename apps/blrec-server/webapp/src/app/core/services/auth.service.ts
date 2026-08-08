import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Router } from '@angular/router';

import { Observable, of } from 'rxjs';
import { catchError, map, tap } from 'rxjs/operators';

import { UrlService } from './url.service';

export interface AuthStatus {
  setupRequired: boolean;
  authenticated: boolean;
}

export interface AuthSession {
  authenticated: true;
  csrfToken: string;
  expiresAt: number;
}

@Injectable({
  providedIn: 'root',
})
export class AuthService {
  private session: AuthSession | null = null;

  constructor(
    private http: HttpClient,
    private url: UrlService,
    private router: Router
  ) {}

  get csrfToken(): string {
    return this.session?.csrfToken ?? '';
  }

  getStatus(): Observable<AuthStatus> {
    return this.http.get<AuthStatus>(
      this.url.makeApiUrl('/api/v1/auth/status')
    );
  }

  ensureSession(): Observable<boolean> {
    if (this.session) {
      return of(true);
    }
    return this.http
      .get<AuthSession>(this.url.makeApiUrl('/api/v1/auth/session'))
      .pipe(
        tap((session) => (this.session = session)),
        map(() => true),
        catchError(() => {
          this.session = null;
          return of(false);
        })
      );
  }

  setup(
    username: string,
    apiKey: string,
    password: string
  ): Observable<AuthSession> {
    return this.http
      .post<AuthSession>(this.url.makeApiUrl('/api/v1/auth/setup'), {
        username,
        apiKey,
        password,
      })
      .pipe(tap((session) => (this.session = session)));
  }

  login(username: string, password: string): Observable<AuthSession> {
    return this.http
      .post<AuthSession>(this.url.makeApiUrl('/api/v1/auth/login'), {
        username,
        password,
      })
      .pipe(tap((session) => (this.session = session)));
  }

  logout(): Observable<void> {
    return this.http
      .post<void>(this.url.makeApiUrl('/api/v1/auth/logout'), null)
      .pipe(tap(() => this.handleUnauthorized()));
  }

  changePassword(
    currentPassword: string,
    newPassword: string
  ): Observable<void> {
    return this.http
      .post<void>(this.url.makeApiUrl('/api/v1/auth/change-password'), {
        currentPassword,
        newPassword,
      })
      .pipe(tap(() => this.handleUnauthorized()));
  }

  recover(
    username: string,
    apiKey: string,
    newPassword: string
  ): Observable<void> {
    return this.http.post<void>(
      this.url.makeApiUrl('/api/v1/auth/recover'),
      { username, apiKey, newPassword }
    );
  }

  handleUnauthorized(): void {
    this.session = null;
    if (this.router.url !== '/auth') {
      void this.router.navigateByUrl('/auth');
    }
  }
}
