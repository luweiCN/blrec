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
import { PlayerLiveStatusService } from './player-live-status.service';
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
  private refreshTimer?: ReturnType<typeof setInterval>;
  private readonly visibilityHandler = (): void => {
    if (document.visibilityState === 'visible') {
      void this.refreshDashboard();
    }
  };

  constructor(
    readonly data: DashboardDataService,
    readonly dashboardMode: DashboardModeService,
    private readonly router: Router,
    private readonly siteAnalytics: SiteAnalyticsService,
    private readonly siteStats: SiteStatsService,
    private readonly playerLiveStatus: PlayerLiveStatusService,
    private readonly changeDetector: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    void this.loadDashboard();
    this.siteAnalytics.start();
    document.addEventListener('visibilitychange', this.visibilityHandler);
    this.refreshTimer = setInterval(() => {
      if (document.visibilityState === 'visible') {
        void this.refreshDashboard();
      }
    }, 60_000);
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
    if (this.refreshTimer !== undefined) {
      clearInterval(this.refreshTimer);
    }
    document.removeEventListener('visibilitychange', this.visibilityHandler);
    this.playerLiveStatus.stop();
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

  reloadDashboard(): void {
    void this.loadDashboard();
  }

  private async loadDashboard(): Promise<void> {
    const loading = this.data.load();
    this.changeDetector.markForCheck();
    await loading;
    if (!this.destroyed) {
      if (this.data.state.kind === 'ready') {
        this.playerLiveStatus.start();
      }
      this.changeDetector.markForCheck();
    }
  }

  private async refreshDashboard(): Promise<void> {
    const changed = await this.data.refresh();
    if (changed && !this.destroyed) {
      this.changeDetector.markForCheck();
    }
  }
}
