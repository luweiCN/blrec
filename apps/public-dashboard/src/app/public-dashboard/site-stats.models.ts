export interface SiteStats {
  readonly schemaVersion: 1;
  readonly generatedAt: string;
  readonly timezone: 'Asia/Shanghai';
  readonly trackingStartedAt: string;
  readonly activeWindowMinutes: 5;
  readonly today: {
    readonly date: string;
    readonly visitors: number;
    readonly pageViews: number;
  };
  readonly activeVisitors: number;
  readonly totalPageViews: number;
}

export type SiteStatsState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly stats: SiteStats }
  | { readonly kind: 'unavailable' };
