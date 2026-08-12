export type VisitorAnalyticsStatus =
  | 'not_configured'
  | 'ready'
  | 'partial'
  | 'error';
export type VisitorAnalyticsEvent = 'all' | 'pageview' | 'heartbeat';

export interface VisitorAnalyticsFilters {
  startAt: string;
  endAt: string;
  event: VisitorAnalyticsEvent;
  page: string | null;
  country: string | null;
  province: string | null;
  city: string | null;
  provider: string | null;
  source: string | null;
  device: string | null;
  browser: string | null;
}

export interface VisitorAnalyticsQuery {
  startAt: Date;
  endAt: Date;
  event: VisitorAnalyticsEvent;
  page: string;
  country: string;
  province: string;
  city: string;
  provider: string;
  source: string;
  device: string;
  browser: string;
}

export interface VisitorAnalyticsTotals {
  visitors: number;
  events: number;
  pageViews: number;
  heartbeats: number;
}

export interface VisitorTrendPoint {
  bucket: string;
  visitors: number;
  events: number;
}

export interface VisitorDimensionPoint {
  value: string;
  visitors: number;
  events: number;
}

export interface RecentVisit {
  occurredAt: string;
  visitor: string;
  page: string;
  source: string;
  device: string;
  browser: string;
  country: string;
  province: string;
  city: string;
}

export interface VisitorAnalyticsSummary {
  provider: 'aliyun-sls';
  status: VisitorAnalyticsStatus;
  configured: boolean;
  generatedAt: string;
  timezone: 'Asia/Shanghai';
  retentionDays: number;
  cacheSeconds: number;
  archiveEnabled: boolean;
  archiveInitialSyncComplete: boolean;
  archiveStartAt: string | null;
  archiveSyncedThrough: string | null;
  archiveLastCompletedAt: string | null;
  archiveLastError: string | null;
  filters: VisitorAnalyticsFilters;
  totals: VisitorAnalyticsTotals;
  trendGranularity: 'hour' | 'day';
  trend: VisitorTrendPoint[];
  pages: VisitorDimensionPoint[];
  countries: VisitorDimensionPoint[];
  provinces: VisitorDimensionPoint[];
  cities: VisitorDimensionPoint[];
  providers: VisitorDimensionPoint[];
  sources: VisitorDimensionPoint[];
  devices: VisitorDimensionPoint[];
  browsers: VisitorDimensionPoint[];
  recentVisits: RecentVisit[];
  warnings: string[];
}
