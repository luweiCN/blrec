import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnInit,
} from '@angular/core';
import { EChartsOption } from 'echarts';
import { finalize } from 'rxjs/operators';
import { NzMessageService } from 'ng-zorro-antd/message';

import {
  VisitorAnalyticsEvent,
  VisitorAnalyticsQuery,
  VisitorAnalyticsSummary,
  VisitorDimensionPoint,
} from './visitor-analytics.model';
import { VisitorAnalyticsService } from './visitor-analytics.service';

type DimensionKind = 'page' | 'source' | 'device' | 'browser';

@Component({
  selector: 'app-visitor-analytics',
  templateUrl: './visitor-analytics.component.html',
  styleUrls: ['./visitor-analytics.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class VisitorAnalyticsComponent implements OnInit {
  summary: VisitorAnalyticsSummary | null = null;
  loading = false;
  chartOptions: EChartsOption = {};
  dateRange: Date[];
  event: VisitorAnalyticsEvent = 'pageview';
  page = '';
  country = '';
  province = '';
  city = '';
  provider = '';
  source = '';
  device = '';
  browser = '';

  constructor(
    private readonly service: VisitorAnalyticsService,
    private readonly message: NzMessageService,
    private readonly changeDetector: ChangeDetectorRef,
  ) {
    const end = new Date();
    const start = new Date(end.getTime() - 6 * 24 * 60 * 60 * 1000);
    this.dateRange = [start, end];
  }

  ngOnInit(): void {
    this.load();
  }

  load(refresh = false): void {
    if (this.dateRange.length !== 2 || !this.dateRange[0] || !this.dateRange[1]) {
      this.message.warning('请选择查询时间范围');
      return;
    }
    const query = this.query();
    if (query.endAt.getTime() <= query.startAt.getTime()) {
      this.message.warning('结束时间必须晚于开始时间');
      return;
    }
    const retentionDays = this.summary?.retentionDays ?? 7;
    if (
      query.endAt.getTime() - query.startAt.getTime() >
      retentionDays * 86400000
    ) {
      this.message.warning(`原始访问日志只保留最近 ${retentionDays} 天`);
      return;
    }
    this.loading = true;
    this.service
      .summary(query, refresh)
      .pipe(
        finalize(() => {
          this.loading = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (summary) => {
          this.summary = summary;
          this.chartOptions = this.buildChart(summary);
          this.changeDetector.markForCheck();
        },
        error: () => {
          this.message.error('访问分析加载失败');
          this.changeDetector.markForCheck();
        },
      });
  }

  reset(): void {
    const end = new Date();
    this.dateRange = [
      new Date(end.getTime() - 6 * 24 * 60 * 60 * 1000),
      end,
    ];
    this.event = 'pageview';
    this.page = '';
    this.country = '';
    this.province = '';
    this.city = '';
    this.provider = '';
    this.source = '';
    this.device = '';
    this.browser = '';
    this.load();
  }

  dimensionLabel(kind: DimensionKind, value: string): string {
    if (kind === 'page') {
      return (
        {
          overview: '总览',
          matches: '对局',
          players: '玩家榜',
          'player-detail': '玩家详情',
          heroes: '英雄榜',
          'hero-detail': '英雄详情',
          'guide-rankings': '榜单说明',
          'guide-play': '游玩指南',
          'guide-download': '下载指南',
          other: '其他页面',
          unknown: '旧版记录（页面未知）',
        }[value] || value
      );
    }
    if (kind === 'source') {
      return (
        { direct: '直接访问', internal: '站内跳转', unknown: '未知来源' }[
          value
        ] || value
      );
    }
    if (kind === 'device') {
      return (
        { mobile: '手机', tablet: '平板', desktop: '电脑' }[value] || value
      );
    }
    return value;
  }

  location(country: string, province: string, city: string): string {
    return [country, province, city]
      .filter((value, index, values) => value && values.indexOf(value) === index)
      .join(' · ');
  }

  maxVisitors(items: VisitorDimensionPoint[]): number {
    return Math.max(1, ...items.map((item) => item.visitors));
  }

  trackDimension(_index: number, item: VisitorDimensionPoint): string {
    return item.value;
  }

  private query(): VisitorAnalyticsQuery {
    return {
      startAt: this.dateRange[0],
      endAt: this.dateRange[1],
      event: this.event,
      page: this.page,
      country: this.country,
      province: this.province,
      city: this.city,
      provider: this.provider,
      source: this.source,
      device: this.device,
      browser: this.browser,
    };
  }

  private buildChart(summary: VisitorAnalyticsSummary): EChartsOption {
    return {
      animationDuration: 350,
      color: ['#1677ff', '#36cfc9'],
      aria: {
        enabled: true,
        description: `按${summary.trendGranularity === 'hour' ? '小时' : '天'}聚合的访客和访问事件趋势`,
      },
      tooltip: { trigger: 'axis' },
      legend: { data: ['访客', '访问事件'], bottom: 0 },
      grid: { top: 18, right: 20, bottom: 50, left: 46 },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: summary.trend.map((item) => item.bucket),
      },
      yAxis: { type: 'value', minInterval: 1 },
      series: [
        {
          name: '访客',
          type: 'line',
          smooth: true,
          symbolSize: 7,
          areaStyle: { opacity: 0.08 },
          data: summary.trend.map((item) => item.visitors),
        },
        {
          name: '访问事件',
          type: 'line',
          smooth: true,
          symbolSize: 7,
          data: summary.trend.map((item) => item.events),
        },
      ],
    };
  }
}
