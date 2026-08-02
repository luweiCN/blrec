import { ChangeDetectionStrategy, Component } from '@angular/core';

import {
  DETAIL_PAGE_SIZE,
  getHeroRankingRows,
  heroImage,
  heroMatchesQuery,
  modeLabel,
  seasonOption,
  winRate,
} from './public-dashboard.data';
import { heroDisplayName } from './public-dashboard.hero-names';
import {
  CURRENT_SEASON_KEY,
  HeroPerformance,
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
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class HeroRankingsPageComponent {
  activeSeason: SeasonKey = CURRENT_SEASON_KEY;
  activeMode: ModeFilter = 'all';
  searchQuery = '';
  currentPage = 1;

  get rankingRows(): readonly HeroRankingRow[] {
    return getHeroRankingRows(this.activeSeason, this.activeMode);
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
    return seasonOption(this.activeSeason);
  }

  selectSeason(season: SeasonKey): void {
    this.activeSeason = season;
    this.currentPage = 1;
  }

  selectMode(mode: ModeFilter): void {
    this.activeMode = mode;
    this.currentPage = 1;
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
}
