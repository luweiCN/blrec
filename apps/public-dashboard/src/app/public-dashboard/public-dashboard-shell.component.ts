import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { Router } from '@angular/router';

import { seasonOption } from './public-dashboard.data';
import { DashboardDataService } from './public-dashboard-data.service';
import { DashboardModeService } from './dashboard-mode.service';
import { SiteAnalyticsService } from './site-analytics.service';
import { SiteStatsState } from './site-stats.models';
import { SiteStatsService } from './site-stats.service';

@Component({
  selector: 'app-public-dashboard-shell',
  templateUrl: './public-dashboard-shell.component.html',
  styleUrls: ['./public-dashboard-shell.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PublicDashboardShellComponent implements OnInit, OnDestroy {
  siteStatsState: SiteStatsState = { kind: 'loading' };

  private destroyed = false;

  constructor(
    readonly data: DashboardDataService,
    readonly dashboardMode: DashboardModeService,
    private readonly router: Router,
    private readonly siteAnalytics: SiteAnalyticsService,
    private readonly siteStats: SiteStatsService,
    private readonly changeDetector: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    void this.loadDashboard();
    this.siteAnalytics.start();
    void this.siteStats.load().then((state) => {
      if (this.destroyed) {
        return;
      }
      this.siteStatsState = state;
      this.changeDetector.markForCheck();
    });
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    this.siteAnalytics.stop();
  }

  get isGameGuideActive(): boolean {
    return (
      this.router.url.startsWith('/guide/download') ||
      this.router.url.startsWith('/guide/play')
    );
  }

  get currentSeasonLabel(): string {
    const snapshot = this.data.snapshotOrNull;
    return snapshot === null
      ? '数据载入中'
      : seasonOption(snapshot, snapshot.currentSeasonKey).label;
  }

  get lastUpdatedLabel(): string {
    const snapshot = this.data.snapshotOrNull;
    if (snapshot === null) {
      return '';
    }
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(new Date(snapshot.generatedAt));
  }

  reloadDashboard(): void {
    void this.loadDashboard();
  }

  private async loadDashboard(): Promise<void> {
    const loading = this.data.load();
    this.changeDetector.markForCheck();
    await loading;
    if (!this.destroyed) {
      this.changeDetector.markForCheck();
    }
  }
}
