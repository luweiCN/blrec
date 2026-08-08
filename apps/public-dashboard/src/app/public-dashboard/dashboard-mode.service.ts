import { Inject, Injectable, InjectionToken } from '@angular/core';
import { BehaviorSubject, Observable } from 'rxjs';

import { isModeFilter, ModeFilter } from './public-dashboard.models';

export interface DashboardModeStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export const DASHBOARD_MODE_STORAGE =
  new InjectionToken<DashboardModeStorage>('DASHBOARD_MODE_STORAGE', {
    providedIn: 'root',
    factory: () => window.localStorage,
  });

export const DEFAULT_DASHBOARD_MODE: ModeFilter = '3v3';
export const DASHBOARD_MODE_STORAGE_KEY = 'vainglory-dashboard-mode';

@Injectable({ providedIn: 'root' })
export class DashboardModeService {
  private readonly modeSubject: BehaviorSubject<ModeFilter>;

  readonly mode$: Observable<ModeFilter>;

  constructor(
    @Inject(DASHBOARD_MODE_STORAGE)
    private readonly storage: DashboardModeStorage,
  ) {
    this.modeSubject = new BehaviorSubject(this.readStoredMode());
    this.mode$ = this.modeSubject.asObservable();
  }

  get mode(): ModeFilter {
    return this.modeSubject.value;
  }

  selectMode(mode: ModeFilter): void {
    if (mode === this.mode) {
      return;
    }

    this.modeSubject.next(mode);
    try {
      this.storage.setItem(DASHBOARD_MODE_STORAGE_KEY, mode);
    } catch {
      // 浏览器禁止本地存储时，当前页面仍然可以正常切换模式。
    }
  }

  private readStoredMode(): ModeFilter {
    try {
      const storedMode = this.storage.getItem(DASHBOARD_MODE_STORAGE_KEY);
      return storedMode !== null && isModeFilter(storedMode)
        ? storedMode
        : DEFAULT_DASHBOARD_MODE;
    } catch {
      return DEFAULT_DASHBOARD_MODE;
    }
  }
}
