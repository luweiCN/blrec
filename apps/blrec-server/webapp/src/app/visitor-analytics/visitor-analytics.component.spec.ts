import { ChangeDetectorRef } from '@angular/core';
import { of } from 'rxjs';
import { NzMessageService } from 'ng-zorro-antd/message';

import { VisitorAnalyticsComponent } from './visitor-analytics.component';
import { VisitorAnalyticsSummary } from './visitor-analytics.model';
import { VisitorAnalyticsService } from './visitor-analytics.service';

const summary: VisitorAnalyticsSummary = {
  provider: 'aliyun-sls',
  status: 'ready',
  configured: true,
  generatedAt: '2026-08-12T08:00:00Z',
  timezone: 'Asia/Shanghai',
  retentionDays: 7,
  cacheSeconds: 300,
  archiveEnabled: true,
  archiveInitialSyncComplete: true,
  archiveStartAt: '2026-08-05T08:00:00Z',
  archiveSyncedThrough: '2026-08-12T07:58:00Z',
  archiveLastCompletedAt: '2026-08-12T08:00:00Z',
  archiveLastError: null,
  filters: {
    startAt: '2026-08-11T08:00:00Z',
    endAt: '2026-08-12T08:00:00Z',
    event: 'pageview',
    page: null,
    country: null,
    province: null,
    city: null,
    provider: null,
    source: null,
    device: null,
    browser: null,
  },
  totals: { visitors: 4, events: 8, pageViews: 8, heartbeats: 0 },
  trendGranularity: 'hour',
  trend: [{ bucket: '2026-08-12 08:00', visitors: 4, events: 8 }],
  pages: [{ value: 'players', visitors: 3, events: 5 }],
  countries: [],
  provinces: [],
  cities: [],
  providers: [],
  sources: [],
  devices: [],
  browsers: [],
  recentVisits: [],
  warnings: [],
};

describe('VisitorAnalyticsComponent', () => {
  it('forwards all selected filters and builds the trend', () => {
    const service = jasmine.createSpyObj<VisitorAnalyticsService>(
      'VisitorAnalyticsService',
      ['summary'],
    );
    service.summary.and.returnValue(of(summary));
    const message = jasmine.createSpyObj<NzMessageService>('NzMessageService', [
      'error',
      'warning',
    ]);
    const component = new VisitorAnalyticsComponent(
      service,
      message,
      jasmine.createSpyObj<ChangeDetectorRef>('ChangeDetectorRef', [
        'markForCheck',
      ]),
    );
    component.dateRange = [
      new Date('2026-08-11T08:00:00Z'),
      new Date('2026-08-12T08:00:00Z'),
    ];
    component.page = 'players';
    component.province = '北京';

    component.load(true);

    const [query, refresh] = service.summary.calls.mostRecent().args;
    expect(query.page).toBe('players');
    expect(query.province).toBe('北京');
    expect(refresh).toBeTrue();
    expect(component.summary).toBe(summary);
    expect(component.chartOptions.series).toBeDefined();
  });

  it('does not query beyond the SLS retention window', () => {
    const service = jasmine.createSpyObj<VisitorAnalyticsService>(
      'VisitorAnalyticsService',
      ['summary'],
    );
    const message = jasmine.createSpyObj<NzMessageService>('NzMessageService', [
      'error',
      'warning',
    ]);
    const component = new VisitorAnalyticsComponent(
      service,
      message,
      jasmine.createSpyObj<ChangeDetectorRef>('ChangeDetectorRef', [
        'markForCheck',
      ]),
    );
    component.dateRange = [
      new Date('2026-08-01T00:00:00Z'),
      new Date('2026-08-09T00:00:00Z'),
    ];

    component.load();

    expect(service.summary).not.toHaveBeenCalled();
    expect(message.warning).toHaveBeenCalledWith(
      '原始访问日志只保留最近 7 天',
    );
  });

  it('allows long ranges after the local archive is enabled', () => {
    const service = jasmine.createSpyObj<VisitorAnalyticsService>(
      'VisitorAnalyticsService',
      ['summary'],
    );
    service.summary.and.returnValue(of(summary));
    const component = new VisitorAnalyticsComponent(
      service,
      jasmine.createSpyObj<NzMessageService>('NzMessageService', [
        'error',
        'warning',
      ]),
      jasmine.createSpyObj<ChangeDetectorRef>('ChangeDetectorRef', [
        'markForCheck',
      ]),
    );
    component.summary = summary;
    component.dateRange = [
      new Date('2026-05-01T00:00:00Z'),
      new Date('2026-08-01T00:00:00Z'),
    ];

    component.load();

    expect(service.summary).toHaveBeenCalled();
  });

  it('offers rolling and calendar time shortcuts', () => {
    jasmine.clock().install();
    jasmine.clock().mockDate(new Date(2026, 7, 12, 15, 30));
    try {
      const service = jasmine.createSpyObj<VisitorAnalyticsService>(
        'VisitorAnalyticsService',
        ['summary'],
      );
      const component = new VisitorAnalyticsComponent(
        service,
        jasmine.createSpyObj<NzMessageService>('NzMessageService', [
          'error',
          'warning',
        ]),
        jasmine.createSpyObj<ChangeDetectorRef>('ChangeDetectorRef', [
          'markForCheck',
        ]),
      );

      expect(Object.keys(component.datePresetRanges)).toEqual([
        '今天',
        '昨天',
        '最近24小时',
        '最近7天',
        '最近30天',
        '最近3个月',
        '最近6个月',
        '最近1年',
        '本周',
        '上周',
        '本月',
        '上月',
        '本季度',
        '上季度',
        '今年',
        '去年',
        '全部',
      ]);
      const today = component.datePresetRanges['今天']();
      expect(today[0]).toEqual(new Date(2026, 7, 12));
      expect(today[1]).toEqual(new Date(2026, 7, 12, 15, 30));
      const previousMonth = component.datePresetRanges['上月']();
      expect(previousMonth).toEqual([
        new Date(2026, 6, 1),
        new Date(2026, 7, 1),
      ]);
      expect(component.datePresetRanges['本季度']()[0]).toEqual(
        new Date(2026, 6, 1),
      );
      expect(component.datePresetRanges['上季度']()).toEqual([
        new Date(2026, 3, 1),
        new Date(2026, 6, 1),
      ]);
      expect(component.datePresetRanges['去年']()).toEqual([
        new Date(2025, 0, 1),
        new Date(2026, 0, 1),
      ]);
      expect(component.datePresetRanges['全部']()[0]).toEqual(new Date(0));
    } finally {
      jasmine.clock().uninstall();
    }
  });
});
