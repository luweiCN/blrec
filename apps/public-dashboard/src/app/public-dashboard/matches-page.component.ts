import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
} from '@angular/core';
import { Subscription } from 'rxjs';

import { DashboardModeService } from './dashboard-mode.service';
import { modeLabel, seasonOption } from './public-dashboard.data';
import { DashboardDataService } from './public-dashboard-data.service';
import { filterDashboardMatches } from './public-dashboard.matches';
import {
  DashboardMatch,
  ModeFilter,
  SeasonKey,
  SeasonOption,
} from './public-dashboard.models';

@Component({
  selector: 'app-matches-page',
  templateUrl: './matches-page.component.html',
  styleUrls: [
    './leaderboard-detail-page.scss',
    './leaderboard-detail-responsive.scss',
    './matches-page.component.scss',
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MatchesPageComponent implements OnDestroy {
  activeSeason: SeasonKey;
  activeMode: ModeFilter;
  private readonly modeSubscription: Subscription;

  constructor(
    readonly data: DashboardDataService,
    dashboardMode: DashboardModeService,
    changeDetector: ChangeDetectorRef,
  ) {
    this.activeSeason = data.snapshot.currentSeasonKey;
    this.activeMode = dashboardMode.mode;
    this.modeSubscription = dashboardMode.mode$.subscribe((mode) => {
      if (mode === this.activeMode) {
        return;
      }
      this.activeMode = mode;
      changeDetector.markForCheck();
    });
  }

  ngOnDestroy(): void {
    this.modeSubscription.unsubscribe();
  }

  get seasonOptions(): readonly SeasonOption[] {
    return this.data.snapshot.seasons;
  }

  get selectedSeason(): SeasonOption {
    return seasonOption(this.data.snapshot, this.activeSeason);
  }

  get matches(): readonly DashboardMatch[] {
    return filterDashboardMatches(
      this.data.snapshot.matches,
      this.data.snapshot.standings['all-time'].players,
      {
        seasonKey: this.activeSeason,
        mode: this.activeMode,
        playerQuery: '',
        selectedHeroes: [],
      },
    );
  }

  get wins(): number {
    return this.matches.filter((match) => match.result === 'W').length;
  }

  get playerCount(): number {
    return new Set(this.matches.map((match) => match.playerId)).size;
  }

  get replayCount(): number {
    return this.matches.filter((match) => match.replay !== undefined).length;
  }

  get averageDuration(): number {
    return this.matches.length === 0
      ? 0
      : Math.round(
          this.matches.reduce(
            (total, match) => total + match.durationSeconds,
            0,
          ) / this.matches.length,
        );
  }

  selectSeason(season: SeasonKey): void {
    this.activeSeason = season;
  }

  modeName(): string {
    return modeLabel(this.activeMode);
  }

  formatDuration(seconds: number): string {
    if (seconds === 0) {
      return '—';
    }
    return `${Math.floor(seconds / 60)}分${String(seconds % 60).padStart(2, '0')}秒`;
  }
}
