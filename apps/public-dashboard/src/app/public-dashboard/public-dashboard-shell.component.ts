import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { Router } from '@angular/router';
import { Subscription } from 'rxjs';

import {
  DashboardRealtimeService,
  DashboardRealtimeUpdate,
} from './dashboard-realtime.service';
import { DashboardModeService } from './dashboard-mode.service';
import { PlayerLiveStatusService } from './player-live-status.service';
import { DashboardDataService } from './public-dashboard-data.service';
import { seasonOption } from './public-dashboard.data';
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
  realtimeRefreshState: 'idle' | 'refreshing' | 'updated' = 'idle';

  private destroyed = false;
  private activeRealtimeRefreshes = 0;
  private realtimeFeedbackTimer?: ReturnType<typeof setTimeout>;
  private realtimeSubscription?: Subscription;
  private readonly visibilityHandler = (): void => {
    if (document.visibilityState === 'visible') {
      void this.refreshDashboard();
      void this.playerLiveStatus.refresh();
    }
  };

  constructor(
    readonly data: DashboardDataService,
    readonly dashboardMode: DashboardModeService,
    private readonly router: Router,
    private readonly siteAnalytics: SiteAnalyticsService,
    private readonly siteStats: SiteStatsService,
    private readonly playerLiveStatus: PlayerLiveStatusService,
    private readonly realtime: DashboardRealtimeService,
    private readonly changeDetector: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    void this.loadDashboard();
    this.siteAnalytics.start();
    document.addEventListener('visibilitychange', this.visibilityHandler);
    this.realtimeSubscription = this.realtime.updates$.subscribe((update) => {
      void this.handleRealtimeUpdate(update);
    });
    this.realtime.start();
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
    if (this.realtimeFeedbackTimer !== undefined) {
      clearTimeout(this.realtimeFeedbackTimer);
    }
    this.realtimeSubscription?.unsubscribe();
    this.realtime.stop();
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

  get dashboardStatusLabel(): string {
    if (this.realtimeRefreshState === 'refreshing') {
      return '正在同步新数据';
    }
    if (this.realtimeRefreshState === 'updated') {
      return '数据已更新';
    }
    return this.currentSeasonLabel;
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

  private async handleRealtimeUpdate(
    update: DashboardRealtimeUpdate,
  ): Promise<void> {
    this.beginRealtimeRefresh();
    try {
      if (update === 'resync' || update === 'dashboard') {
        await this.refreshDashboard();
      }
      if (update === 'resync' || update === 'live_rooms') {
        await this.playerLiveStatus.refresh();
        if (!this.destroyed) {
          this.changeDetector.markForCheck();
        }
      }
    } finally {
      this.finishRealtimeRefresh();
    }
  }

  private beginRealtimeRefresh(): void {
    this.activeRealtimeRefreshes += 1;
    if (this.realtimeFeedbackTimer !== undefined) {
      clearTimeout(this.realtimeFeedbackTimer);
      this.realtimeFeedbackTimer = undefined;
    }
    this.realtimeRefreshState = 'refreshing';
    this.changeDetector.markForCheck();
  }

  private finishRealtimeRefresh(): void {
    this.activeRealtimeRefreshes = Math.max(0, this.activeRealtimeRefreshes - 1);
    if (this.destroyed || this.activeRealtimeRefreshes > 0) {
      return;
    }
    this.realtimeRefreshState = 'updated';
    this.changeDetector.markForCheck();
    this.realtimeFeedbackTimer = setTimeout(() => {
      if (this.destroyed) {
        return;
      }
      this.realtimeRefreshState = 'idle';
      this.realtimeFeedbackTimer = undefined;
      this.changeDetector.markForCheck();
    }, 1800);
  }
}
