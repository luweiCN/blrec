import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
} from '@angular/core';
import { Subscription } from 'rxjs';

import { DashboardModeService } from './dashboard-mode.service';
import {
  getDashboardSummary,
  getHeroRankings,
  getModeBreakdown,
  getPlayerRankings,
  heroImage,
  modeLabel,
  OVERVIEW_LIMIT,
  seasonOption,
  selectedHeroWinRate,
  winRate,
} from './public-dashboard.data';
import { heroDisplayName } from './public-dashboard.hero-names';
import { DashboardDataService } from './public-dashboard-data.service';
import {
  DashboardSummary,
  HeroPerformance,
  HeroStanding,
  HeroUsage,
  MatchResult,
  ModeBreakdown,
  ModeFilter,
  Performance,
  PlayerStanding,
  SeasonKey,
} from './public-dashboard.models';

const EMPTY_PERFORMANCE: Performance = {
  matches: 0,
  wins: 0,
  topHero: '',
  ratingScore: null,
  provisional: false,
};

@Component({
  selector: 'app-public-dashboard',
  templateUrl: './public-dashboard.component.html',
  styleUrls: [
    './public-dashboard.component.scss',
    './public-dashboard.rankings.scss',
    './public-dashboard-detail-links.scss',
    './public-dashboard.responsive.scss',
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PublicDashboardComponent implements OnDestroy {
  readonly activeSeason: SeasonKey;

  activeMode: ModeFilter;
  private readonly modeSubscription: Subscription;

  constructor(
    private readonly data: DashboardDataService,
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

  get rankings(): readonly PlayerStanding[] {
    return getPlayerRankings(
      this.data.snapshot,
      this.activeSeason,
      this.activeMode,
    );
  }

  get overviewRankings(): readonly PlayerStanding[] {
    return this.rankings.slice(0, OVERVIEW_LIMIT);
  }

  get selectedPlayer(): PlayerStanding | undefined {
    return this.rankings[0];
  }

  get selectedPlayerRank(): number {
    return this.selectedPlayer === undefined ? 0 : 1;
  }

  get topPlayer(): PlayerStanding | undefined {
    return this.rankings[0];
  }

  get podiumPlayers(): readonly PlayerStanding[] {
    return [this.rankings[1], this.rankings[0], this.rankings[2]].filter(
      (player): player is PlayerStanding => player !== undefined,
    );
  }

  get selectedModeBreakdown(): readonly ModeBreakdown[] {
    return this.selectedPlayer === undefined
      ? []
      : getModeBreakdown(this.selectedPlayer);
  }

  get heroRankings(): readonly HeroStanding[] {
    return getHeroRankings(
      this.data.snapshot,
      this.activeSeason,
      this.activeMode,
    );
  }

  get overviewHeroRankings(): readonly HeroStanding[] {
    return this.heroRankings.slice(0, OVERVIEW_LIMIT);
  }

  get summary(): DashboardSummary {
    return getDashboardSummary(
      this.data.snapshot,
      this.activeSeason,
      this.activeMode,
    );
  }

  get seasonLabel(): string {
    return seasonOption(this.data.snapshot, this.activeSeason).label;
  }

  get seasonPeriod(): string {
    return seasonOption(this.data.snapshot, this.activeSeason).period;
  }

  get seasonCode(): string {
    return this.activeSeason.replace('-', ' · ').toLocaleUpperCase();
  }

  playerPerformance(player: PlayerStanding): Performance {
    return player.modes[this.activeMode];
  }

  selectedPerformance(): Performance {
    return this.selectedPlayer === undefined
      ? EMPTY_PERFORMANCE
      : this.playerPerformance(this.selectedPlayer);
  }

  selectedHeroWinRate(hero: HeroUsage): number {
    return selectedHeroWinRate(hero);
  }

  heroPerformance(hero: HeroStanding): HeroPerformance {
    return hero.modes[this.activeMode];
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

  ratingScore(value: Performance): number {
    return value.ratingScore ?? 0;
  }

  modeLabel(): string {
    return modeLabel(this.activeMode);
  }

  podiumRank(player: PlayerStanding): number {
    return this.rankings.findIndex((standing) => standing.id === player.id) + 1;
  }

  trackMode(_index: number, mode: { readonly key: ModeFilter }): ModeFilter {
    return mode.key;
  }

  trackPlayer(_index: number, player: PlayerStanding): number {
    return player.id;
  }

  trackHero(_index: number, hero: HeroStanding): string {
    return hero.id;
  }

  trackHeroUsage(_index: number, hero: HeroUsage): string {
    return hero.name;
  }

  trackResult(index: number, _result: MatchResult): number {
    return index;
  }
}
