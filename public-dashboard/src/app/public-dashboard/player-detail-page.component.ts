import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
} from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { Subscription } from 'rxjs';

import { DashboardModeService } from './dashboard-mode.service';
import {
  findPlayer,
  getModeBreakdown,
  getPlayerRankings,
  heroImage,
  modeLabel,
  playerForSeason,
  seasonOption,
  winRate,
} from './public-dashboard.data';
import { heroDisplayName } from './public-dashboard.hero-names';
import { DashboardDataService } from './public-dashboard-data.service';
import {
  HeroUsage,
  MatchResult,
  ModeBreakdown,
  ModeFilter,
  Performance,
  PlayerStanding,
  SeasonKey,
  SeasonOption,
} from './public-dashboard.models';

interface PlayerSeasonRecord {
  readonly season: SeasonOption;
  readonly performance: Performance;
  readonly rank: number | null;
}

const EMPTY_PERFORMANCE: Performance = {
  matches: 0,
  wins: 0,
  topHero: '',
  ratingScore: null,
  provisional: false,
};

@Component({
  selector: 'app-player-detail-page',
  templateUrl: './player-detail-page.component.html',
  styleUrls: [
    './leaderboard-detail-page.scss',
    './leaderboard-profile-page.scss',
    './leaderboard-profile-responsive.scss',
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PlayerDetailPageComponent implements OnDestroy {
  activeSeason: SeasonKey;
  activeMode: ModeFilter;
  private readonly modeSubscription: Subscription;

  constructor(
    private readonly data: DashboardDataService,
    private readonly route: ActivatedRoute,
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

  get playerId(): number | null {
    const value = Number(this.route.snapshot.paramMap.get('playerId'));
    return Number.isSafeInteger(value) && value > 0 ? value : null;
  }

  get player(): PlayerStanding | undefined {
    return this.playerId === null
      ? undefined
      : findPlayer(this.data.snapshot, this.playerId);
  }

  get seasonPlayer(): PlayerStanding | undefined {
    return this.playerId === null
      ? undefined
      : playerForSeason(this.data.snapshot, this.activeSeason, this.playerId);
  }

  get seasonOptions(): readonly SeasonOption[] {
    return this.data.snapshot.seasons;
  }

  get selectedSeason(): SeasonOption {
    return seasonOption(this.data.snapshot, this.activeSeason);
  }

  get performance(): Performance {
    return this.seasonPlayer?.modes[this.activeMode] ?? EMPTY_PERFORMANCE;
  }

  get rank(): number | null {
    if (this.playerId === null) {
      return null;
    }
    const index = getPlayerRankings(
      this.data.snapshot,
      this.activeSeason,
      this.activeMode,
    ).findIndex((player) => player.id === this.playerId);
    return index < 0 ? null : index + 1;
  }

  get modeBreakdown(): readonly ModeBreakdown[] {
    return this.seasonPlayer === undefined
      ? []
      : getModeBreakdown(this.seasonPlayer);
  }

  get heroPool(): readonly HeroUsage[] {
    return this.seasonPlayer?.heroPool ?? [];
  }

  get seasonHistory(): readonly PlayerSeasonRecord[] {
    const playerId = this.playerId;
    if (playerId === null) {
      return [];
    }
    const records: PlayerSeasonRecord[] = [];
    for (const season of this.data.snapshot.seasons) {
      const player = playerForSeason(
        this.data.snapshot,
        season.key,
        playerId,
      );
      if (player === undefined || player.modes[this.activeMode].matches === 0) {
        continue;
      }
      const rankIndex = getPlayerRankings(
        this.data.snapshot,
        season.key,
        this.activeMode,
      ).findIndex((standing) => standing.id === playerId);
      records.push({
        season,
        performance: player.modes[this.activeMode],
        rank: rankIndex < 0 ? null : rankIndex + 1,
      });
    }
    return records;
  }

  selectSeason(season: SeasonKey): void {
    this.activeSeason = season;
  }

  heroImage(heroName: string): string {
    return heroImage(heroName);
  }

  heroDisplayName(heroName: string): string {
    return heroDisplayName(heroName);
  }

  winRate(value: { readonly matches: number; readonly wins: number }): number {
    return winRate(value);
  }

  modeLabel(): string {
    return modeLabel(this.activeMode);
  }

  trackMode(_index: number, mode: ModeBreakdown): ModeFilter {
    return mode.key;
  }

  trackHero(_index: number, hero: HeroUsage): string {
    return hero.name;
  }

  trackSeason(_index: number, record: PlayerSeasonRecord): SeasonKey {
    return record.season.key;
  }

  trackResult(index: number, _result: MatchResult): number {
    return index;
  }
}
