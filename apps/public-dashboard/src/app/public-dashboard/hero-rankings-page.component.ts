import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
} from '@angular/core';
import { Subscription } from 'rxjs';

import { DashboardModeService } from './dashboard-mode.service';
import {
  DETAIL_PAGE_SIZE,
  getHeroRankingRows,
  HeroRankingSort,
  heroImage,
  heroMatchesQuery,
  modeLabel,
  seasonOption,
  winRate,
} from './public-dashboard.data';
import { heroDisplayName } from './public-dashboard.hero-names';
import {
  getHeroProficiencyLeader,
  HeroProficiency,
} from './public-dashboard.proficiency';
import { DashboardDataService } from './public-dashboard-data.service';
import {
  HeroPerformance,
  HeroDataScope,
  HeroRankingRow,
  HeroStanding,
  ModeFilter,
  SeasonKey,
  SeasonOption,
} from './public-dashboard.models';

@Component({
  selector: 'app-hero-rankings-page',
  templateUrl: './hero-rankings-page.component.html',
  styleUrls: [
    './leaderboard-detail-page.scss',
    './leaderboard-detail-responsive.scss',
    './hero-rankings-toolbar.scss',
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class HeroRankingsPageComponent implements OnDestroy {
  activeSeason: SeasonKey;
  activeMode: ModeFilter;
  searchQuery = '';
  activeSort: HeroRankingSort = 'win-rate';
  activeScope: HeroDataScope = 'streamer';
  currentPage = 1;
  private readonly modeSubscription: Subscription;
  private readonly revisionSubscription: Subscription;

  constructor(
    private readonly data: DashboardDataService,
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
      this.currentPage = 1;
      this.changeDetector.markForCheck();
    });
    this.revisionSubscription = data.revision$.subscribe(() => {
      this.clampPage();
      this.changeDetector.markForCheck();
    });
  }

  ngOnDestroy(): void {
    this.modeSubscription.unsubscribe();
    this.revisionSubscription.unsubscribe();
  }

  get seasonOptions(): readonly SeasonOption[] {
    return this.data.snapshot.seasons;
  }

  get rankingRows(): readonly HeroRankingRow[] {
    return getHeroRankingRows(
      this.data.snapshot,
      this.activeSeason,
      this.activeMode,
      this.activeSort,
      this.activeScope,
    );
  }

  get rankingHint(): string {
    const sample =
      this.activeScope === 'streamer'
        ? '只统计主播本人使用的英雄。'
        : '统计完整结算阵容，同一局仅在高置信重复时合并。';
    return this.activeSort === 'win-rate'
      ? `${sample} 当前模式至少 20 局后进入胜率排名。`
      : `${sample} 按对局次数展示当前模式最常被使用的英雄。`;
  }

  get rankingCaption(): string {
    return this.activeSort === 'win-rate' ? '英雄胜率排名' : '英雄使用次数排名';
  }

  get filteredRows(): readonly HeroRankingRow[] {
    return this.rankingRows.filter((row) =>
      heroMatchesQuery(row.hero, this.searchQuery),
    );
  }

  get visibleRows(): readonly HeroRankingRow[] {
    const start = (this.currentPage - 1) * DETAIL_PAGE_SIZE;
    return this.filteredRows.slice(start, start + DETAIL_PAGE_SIZE);
  }

  get totalPages(): number {
    return Math.max(1, Math.ceil(this.filteredRows.length / DETAIL_PAGE_SIZE));
  }

  get pageNumbers(): readonly number[] {
    return Array.from(
      { length: this.totalPages },
      (_value, index) => index + 1,
    );
  }

  get resultStart(): number {
    return this.filteredRows.length === 0
      ? 0
      : (this.currentPage - 1) * DETAIL_PAGE_SIZE + 1;
  }

  get resultEnd(): number {
    return Math.min(
      this.currentPage * DETAIL_PAGE_SIZE,
      this.filteredRows.length,
    );
  }

  get selectedSeason(): SeasonOption {
    return seasonOption(this.data.snapshot, this.activeSeason);
  }

  selectSeason(season: SeasonKey): void {
    this.activeSeason = season;
    this.currentPage = 1;
    void this.loadSeason();
  }

  selectSort(sort: HeroRankingSort): void {
    this.activeSort = sort;
    this.currentPage = 1;
  }

  selectScope(scope: HeroDataScope): void {
    this.activeScope = scope;
    this.currentPage = 1;
    if (scope === 'environment') {
      void this.loadEnvironment();
    }
  }

  updateSearch(event: Event): void {
    this.searchQuery = (event.target as HTMLInputElement).value;
    this.currentPage = 1;
  }

  goToPage(page: number): void {
    this.currentPage = Math.min(Math.max(page, 1), this.totalPages);
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

  proficiencyLeader(hero: HeroStanding): HeroProficiency | null {
    if (this.activeScope === 'environment') {
      return null;
    }
    return getHeroProficiencyLeader(
      this.data.snapshot,
      this.activeSeason,
      this.activeMode,
      hero.name,
    );
  }

  winRate(value: { readonly matches: number; readonly wins: number }): number {
    return winRate(value);
  }

  modeLabel(): string {
    return modeLabel(this.activeMode);
  }

  trackRow(_index: number, row: HeroRankingRow): string {
    return row.hero.id;
  }

  trackPage(_index: number, page: number): number {
    return page;
  }

  private clampPage(): void {
    this.currentPage = Math.min(this.currentPage, this.totalPages);
  }

  private async loadSeason(): Promise<void> {
    await this.data.ensureStandings(this.activeSeason);
    if (this.activeScope === 'environment') {
      await this.data.ensureEnvironment(this.activeSeason);
    }
    this.changeDetector.markForCheck();
  }

  private async loadEnvironment(): Promise<void> {
    if (await this.data.ensureEnvironment(this.activeSeason)) {
      this.changeDetector.markForCheck();
    }
  }
}
