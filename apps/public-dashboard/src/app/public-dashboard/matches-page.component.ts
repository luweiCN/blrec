import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
} from '@angular/core';
import { Subscription } from 'rxjs';

import {
  DashboardMatchApiService,
  DashboardMatchSummary,
} from './dashboard-match-api.service';
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
  private readonly revisionSubscription: Subscription;
  private apiSummary: DashboardMatchSummary | null = null;
  private summaryRequestSequence = 0;

  constructor(
    readonly data: DashboardDataService,
    private readonly matchApi: DashboardMatchApiService,
    dashboardMode: DashboardModeService,
    private readonly changeDetector: ChangeDetectorRef,
  ) {
    this.activeSeason = data.snapshot.currentSeasonKey;
    this.activeMode = dashboardMode.mode;
    this.modeSubscription = dashboardMode.mode$.subscribe((mode) => {
      if (mode === this.activeMode) {
        return;
      }
      this.activeMode = mode;
      void this.loadSummary();
      this.changeDetector.markForCheck();
    });
    this.revisionSubscription = data.revision$.subscribe(() => {
      void this.loadSummary();
      this.changeDetector.markForCheck();
    });
    void this.loadSummary();
  }

  ngOnDestroy(): void {
    this.summaryRequestSequence += 1;
    this.modeSubscription.unsubscribe();
    this.revisionSubscription.unsubscribe();
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
    return (
      this.apiSummary?.wins ??
      this.matches.filter((match) => match.result === 'W').length
    );
  }

  get matchCount(): number {
    return this.apiSummary?.matches ?? this.matches.length;
  }

  get playerCount(): number {
    return (
      this.apiSummary?.players ??
      new Set(this.matches.map((match) => match.playerId)).size
    );
  }

  get replayCount(): number {
    return (
      this.apiSummary?.replays ??
      this.matches.filter((match) => match.replay !== undefined).length
    );
  }

  get averageDuration(): number {
    if (this.apiSummary !== null) {
      return this.apiSummary.averageDurationSeconds;
    }
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
    void this.loadSummary();
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

  private async loadSummary(): Promise<void> {
    if (!this.matchApi.enabled) {
      return;
    }
    const sequence = ++this.summaryRequestSequence;
    const summary = await this.matchApi.summary({
      seasonKey: this.activeSeason,
      mode: this.activeMode,
    });
    if (sequence !== this.summaryRequestSequence) {
      return;
    }
    this.apiSummary = summary;
    this.changeDetector.markForCheck();
  }
}
