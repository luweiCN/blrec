import { Injectable } from '@angular/core';
import { BehaviorSubject } from 'rxjs';

import { environment } from '../../environments/environment';

export const DASHBOARD_OWNER_TOKEN_STORAGE_KEY =
  'vainglory-dashboard-owner-token';

function ownerToken(): string {
  try {
    return window.sessionStorage.getItem(DASHBOARD_OWNER_TOKEN_STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

function saveOwnerToken(token: string): void {
  window.sessionStorage.setItem(DASHBOARD_OWNER_TOKEN_STORAGE_KEY, token);
}

function removeOwnerToken(): void {
  try {
    window.sessionStorage.removeItem(DASHBOARD_OWNER_TOKEN_STORAGE_KEY);
  } catch {
    return;
  }
}

export function dashboardRequestInit(cache: RequestCache): RequestInit {
  const token = ownerToken();
  if (token === '') {
    return { cache };
  }
  return {
    cache,
    headers: { Authorization: `Bearer ${token}` },
  };
}

@Injectable({ providedIn: 'root' })
export class DashboardOwnerAccessService {
  private readonly activeSubject = new BehaviorSubject<boolean>(
    ownerToken() !== '',
  );

  readonly active$ = this.activeSubject.asObservable();

  get active(): boolean {
    return this.activeSubject.value;
  }

  async unlock(rawToken: string): Promise<boolean> {
    const token = rawToken.trim();
    if (!/^\S{32,512}$/u.test(token)) {
      return false;
    }
    if (!(await this.validate(token))) {
      return false;
    }
    saveOwnerToken(token);
    this.activeSubject.next(true);
    return true;
  }

  async validateStored(): Promise<boolean> {
    const token = ownerToken();
    if (token === '' || !(await this.validate(token))) {
      this.lock();
      return false;
    }
    this.activeSubject.next(true);
    return true;
  }

  lock(): void {
    removeOwnerToken();
    this.activeSubject.next(false);
  }

  private async validate(token: string): Promise<boolean> {
    const apiBaseUrl = environment.apiBaseUrl.replace(/\/+$/u, '');
    if (apiBaseUrl === '') {
      return false;
    }
    const response = await fetch(`${apiBaseUrl}/owner/session`, {
      cache: 'no-store',
      headers: { Authorization: `Bearer ${token}` },
    });
    return response.ok;
  }
}
