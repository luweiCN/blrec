import { ChangeDetectionStrategy, Component } from '@angular/core';

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
  ModeOption,
  MODE_OPTIONS,
  Performance,
  PlayerStanding,
  SeasonKey,
} from './public-dashboard.models';

const EMPTY_PERFORMANCE: Performance = {
  matches: 0,
  wins: 0,
  topHero: '',
};

@Component({
  selector: 'app-public-dashboard',
  templateUrl: './public-dashboard.component.html',
  styleUrls: [
    './public-dashboard.component.scss',
    './public-dashboard.rankings.scss',
    './public-dashboard.responsive.scss',
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PublicDashboardComponent {
  readonly modeOptions: readonly ModeOption[] = MODE_OPTIONS;
  readonly activeSeason: SeasonKey;

  activeMode: ModeFilter = 'all';
  selectedPlayerId: number | null;

  constructor(private readonly data: DashboardDataService) {
    this.activeSeason = data.snapshot.currentSeasonKey;
    this.selectedPlayerId =
      getPlayerRankings(data.snapshot, this.activeSeason, this.activeMode)[0]
        ?.id ?? null;
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
    return (
      this.rankings.find((player) => player.id === this.selectedPlayerId) ??
      this.rankings[0]
    );
  }

  get selectedPlayerRank(): number {
    const index = this.rankings.findIndex(
      (player) => player.id === this.selectedPlayerId,
    );
    return index < 0 ? 0 : index + 1;
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

  selectMode(mode: ModeFilter): void {
    this.activeMode = mode;
    this.selectedPlayerId = this.rankings[0]?.id ?? null;
  }

  selectPlayer(playerId: number): void {
    this.selectedPlayerId = playerId;
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

  modeLabel(): string {
    return modeLabel(this.activeMode);
  }

  trendLabel(trend: number): string {
    if (trend > 0) {
      return '上升 ' + trend + ' 名';
    }
    if (trend < 0) {
      return '下降 ' + Math.abs(trend) + ' 名';
    }
    return '暂无历史趋势';
  }

  podiumRank(player: PlayerStanding): number {
    return this.rankings.findIndex((standing) => standing.id === player.id) + 1;
  }

  trackMode(_index: number, mode: ModeOption): ModeFilter {
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
