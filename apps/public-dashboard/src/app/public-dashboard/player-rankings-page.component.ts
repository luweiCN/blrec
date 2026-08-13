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
  getPlayerRankingRows,
  getPlayerTrend,
  getRankMovement,
  heroImage,
  modeLabel,
  PlayerRankingSort,
  playerMatchesQuery,
  playerKdaForMode,
  RankMovement,
  seasonOption,
  winRate,
} from './public-dashboard.data';
import { heroDisplayName } from './public-dashboard.hero-names';
import { DashboardDataService } from './public-dashboard-data.service';
import {
  MatchResult,
  ModeFilter,
  Performance,
  PlayerRankingRow,
  PlayerStanding,
  SeasonKey,
  SeasonOption,
} from './public-dashboard.models';

@Component({
  selector: 'app-player-rankings-page',
  templateUrl: './player-rankings-page.component.html',
  styleUrls: [
    './leaderboard-detail-page.scss',
    './leaderboard-detail-responsive.scss',
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PlayerRankingsPageComponent implements OnDestroy {
  activeSeason: SeasonKey;
  activeMode: ModeFilter;
  searchQuery = '';
  activeSort: PlayerRankingSort = 'rating';
  currentPage = 1;
  private readonly modeSubscription: Subscription;
  private readonly revisionSubscription: Subscription;

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
      this.currentPage = 1;
      changeDetector.markForCheck();
    });
    this.revisionSubscription = data.revision$.subscribe(() => {
      this.clampPage();
      changeDetector.markForCheck();
    });
  }

  ngOnDestroy(): void {
    this.modeSubscription.unsubscribe();
    this.revisionSubscription.unsubscribe();
  }

  get seasonOptions(): readonly SeasonOption[] {
    return this.data.snapshot.seasons;
  }

  get rankingRows(): readonly PlayerRankingRow[] {
    return getPlayerRankingRows(
      this.data.snapshot,
      this.activeSeason,
      this.activeMode,
      this.activeSort,
    );
  }

  get rankingHint(): string {
    switch (this.activeSort) {
      case 'rating':
        return '按排位分从高到低排列。';
      case 'matches':
        return '按当前模式累计对局数排列。';
      case 'wins':
        return '按当前模式累计胜场数排列。';
      case 'win-rate':
        return '当前模式至少 20 局后进入胜率排序。';
    }
  }

  get rankingCaption(): string {
    switch (this.activeSort) {
      case 'rating':
        return '排位分排名';
      case 'matches':
        return '对局数排名';
      case 'wins':
        return '胜场数排名';
      case 'win-rate':
        return '胜率排名';
    }
  }

  get filteredRows(): readonly PlayerRankingRow[] {
    return this.rankingRows.filter((row) =>
      playerMatchesQuery(row.player, this.searchQuery),
    );
  }

  get visibleRows(): readonly PlayerRankingRow[] {
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
  }

  selectSort(sort: PlayerRankingSort): void {
    this.activeSort = sort;
    this.currentPage = 1;
  }

  updateSearch(event: Event): void {
    this.searchQuery = (event.target as HTMLInputElement).value;
    this.currentPage = 1;
  }

  goToPage(page: number): void {
    this.currentPage = Math.min(Math.max(page, 1), this.totalPages);
  }

  playerPerformance(player: PlayerStanding): Performance {
    return player.modes[this.activeMode];
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

  playerKda(player: PlayerStanding): number | null {
    return playerKdaForMode(player, this.activeMode)?.value ?? null;
  }

  rankMovement(player: PlayerStanding): RankMovement {
    return getRankMovement(
      getPlayerTrend(
        this.data.trends,
        this.data.snapshot.snapshotId,
        this.activeSeason,
        this.activeMode,
        player.id,
      ),
    );
  }

  modeLabel(): string {
    return modeLabel(this.activeMode);
  }

  trackRow(_index: number, row: PlayerRankingRow): number {
    return row.player.id;
  }

  trackResult(index: number, _result: MatchResult): number {
    return index;
  }

  trackPage(_index: number, page: number): number {
    return page;
  }

  private clampPage(): void {
    this.currentPage = Math.min(this.currentPage, this.totalPages);
  }
}
