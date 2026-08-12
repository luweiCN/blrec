import { DOCUMENT } from '@angular/common';
import {
  Inject,
  Injectable,
  InjectionToken,
  OnDestroy,
} from '@angular/core';
import { NavigationEnd, Router } from '@angular/router';
import { filter, Subscription } from 'rxjs';

import { environment } from '../../environments/environment';

export interface SiteAnalyticsConfig {
  readonly enabled: boolean;
  readonly endpoint: string;
  readonly heartbeatIntervalMs: number;
}

export interface SiteAnalyticsStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export const SITE_ANALYTICS_CONFIG =
  new InjectionToken<SiteAnalyticsConfig>('SITE_ANALYTICS_CONFIG', {
    providedIn: 'root',
    factory: () => ({
      enabled:
        environment.production &&
        window.location.hostname === 'vg.luwei.host',
      endpoint: '/analytics/pixel.svg',
      heartbeatIntervalMs: 120_000,
    }),
  });

export const SITE_ANALYTICS_STORAGE =
  new InjectionToken<SiteAnalyticsStorage>('SITE_ANALYTICS_STORAGE', {
    providedIn: 'root',
    factory: () => window.localStorage,
  });

export const SITE_ANALYTICS_STORAGE_KEY =
  'vainglory-dashboard-anonymous-visitor';

type AnalyticsEvent = 'pageview' | 'heartbeat';

@Injectable({ providedIn: 'root' })
export class SiteAnalyticsService implements OnDestroy {
  private heartbeatTimer: number | null = null;
  private lastTrackedUrl: string | null = null;
  private currentPage = 'overview';
  private routerSubscription: Subscription | null = null;
  private visitorId: string | null = null;

  constructor(
    private readonly router: Router,
    @Inject(DOCUMENT) private readonly document: Document,
    @Inject(SITE_ANALYTICS_CONFIG)
    private readonly config: SiteAnalyticsConfig,
    @Inject(SITE_ANALYTICS_STORAGE)
    private readonly storage: SiteAnalyticsStorage,
  ) {}

  start(): void {
    if (!this.config.enabled || this.routerSubscription !== null) {
      return;
    }

    this.trackPageView(this.router.url);
    this.routerSubscription = this.router.events
      .pipe(filter((event): event is NavigationEnd => event instanceof NavigationEnd))
      .subscribe((event) => this.trackPageView(event.urlAfterRedirects));
    this.document.addEventListener(
      'visibilitychange',
      this.handleVisibilityChange,
    );
    this.heartbeatTimer = window.setInterval(() => {
      if (this.document.visibilityState === 'visible') {
        this.send('heartbeat');
      }
    }, this.config.heartbeatIntervalMs);
  }

  stop(): void {
    this.routerSubscription?.unsubscribe();
    this.routerSubscription = null;
    this.document.removeEventListener(
      'visibilitychange',
      this.handleVisibilityChange,
    );
    if (this.heartbeatTimer !== null) {
      window.clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  ngOnDestroy(): void {
    this.stop();
  }

  private readonly handleVisibilityChange = (): void => {
    if (this.document.visibilityState === 'visible') {
      this.send('heartbeat');
    }
  };

  private trackPageView(url: string): void {
    if (url === this.lastTrackedUrl) {
      return;
    }
    this.lastTrackedUrl = url;
    this.currentPage = analyticsPage(url);
    this.send('pageview');
  }

  private send(event: AnalyticsEvent): void {
    const visitor = this.getVisitorId();
    this.sendRequest([
      ['event', event],
      ['visitor', visitor],
    ]);
    this.sendRequest([
      ['event', 'detail'],
      ['kind', event],
      ['visitor', visitor],
      ['page', this.currentPage],
      ['source', analyticsSource(this.document)],
      ['device', analyticsDevice(window.innerWidth)],
    ]);
  }

  private sendRequest(parameters: ReadonlyArray<readonly [string, string]>): void {
    const requestUrl = new URL(this.config.endpoint, this.document.location.origin);
    for (const [name, value] of parameters) {
      requestUrl.searchParams.set(name, value);
    }

    void window
      .fetch(requestUrl.toString(), {
        cache: 'no-store',
        credentials: 'omit',
        keepalive: true,
        referrerPolicy: 'no-referrer',
      })
      .catch(() => undefined);
  }

  private getVisitorId(): string {
    if (this.visitorId !== null) {
      return this.visitorId;
    }

    try {
      const stored = this.storage.getItem(SITE_ANALYTICS_STORAGE_KEY);
      if (stored !== null && /^[0-9a-f-]{16,64}$/u.test(stored)) {
        this.visitorId = stored;
        return stored;
      }
    } catch {
      // 浏览器禁止本地存储时，仍可用当前页面内的匿名标识完成统计。
    }

    const created = createVisitorId();
    this.visitorId = created;
    try {
      this.storage.setItem(SITE_ANALYTICS_STORAGE_KEY, created);
    } catch {
      // 本地存储不可用不会影响页面和本次访问统计。
    }
    return created;
  }
}

export function analyticsPage(url: string): string {
  const path = url.split(/[?#]/u, 1)[0];
  const segments = path.split('/').filter(Boolean);
  if (segments.length === 0) {
    return 'overview';
  }
  if (segments[0] === 'players') {
    return segments.length > 1 ? 'player-detail' : 'players';
  }
  if (segments[0] === 'heroes') {
    return segments.length > 1 ? 'hero-detail' : 'heroes';
  }
  if (segments[0] === 'matches') {
    return 'matches';
  }
  if (segments[0] === 'guide' && segments[1]) {
    return `guide-${segments[1]}`.slice(0, 64);
  }
  return 'other';
}

export function analyticsSource(document: Document): string {
  if (!document.referrer) {
    return 'direct';
  }
  try {
    const source = new URL(document.referrer, document.location.origin);
    return source.hostname === document.location.hostname
      ? 'internal'
      : source.hostname.toLowerCase().slice(0, 128);
  } catch {
    return 'unknown';
  }
}

export function analyticsDevice(width: number): string {
  if (width < 768) {
    return 'mobile';
  }
  if (width < 1100) {
    return 'tablet';
  }
  return 'desktop';
}

function createVisitorId(): string {
  if (typeof window.crypto.randomUUID === 'function') {
    return window.crypto.randomUUID();
  }
  const bytes = window.crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join(
    '',
  );
}
