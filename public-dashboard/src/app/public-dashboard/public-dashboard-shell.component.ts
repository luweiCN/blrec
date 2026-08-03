import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { Router } from '@angular/router';

import { seasonOption } from './public-dashboard.data';
import { DashboardDataService } from './public-dashboard-data.service';
import { DashboardModeService } from './dashboard-mode.service';
import { SiteAnalyticsService } from './site-analytics.service';

@Component({
  selector: 'app-public-dashboard-shell',
  templateUrl: './public-dashboard-shell.component.html',
  styleUrls: ['./public-dashboard-shell.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PublicDashboardShellComponent implements OnInit, OnDestroy {
  constructor(
    readonly data: DashboardDataService,
    readonly dashboardMode: DashboardModeService,
    private readonly router: Router,
    private readonly siteAnalytics: SiteAnalyticsService,
  ) {}

  ngOnInit(): void {
    this.siteAnalytics.start();
  }

  ngOnDestroy(): void {
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
}
