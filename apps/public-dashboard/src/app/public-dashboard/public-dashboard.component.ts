import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
} from '@angular/core';
import { Subscription } from 'rxjs';

import { DashboardMatchApiService } from './dashboard-match-api.service';
import { DashboardModeService } from './dashboard-mode.service';
import { filterDashboardMatches } from './public-dashboard.matches';
import {
  getDashboardSummary,
  getHeroRankings,
  getModeBreakdown,
  getPlayerRankings,
  getPlayerTrend,
  getRankMovement,
  heroImage,
  modeLabel,
  OVERVIEW_LIMIT,
  playerKdaForMode,
  RatingTerms,
  ratingTermsForSeason,
  RankMovement,
  seasonOption,
  selectedHeroWinRate,
  winRate,
} from './public-dashboard.data';
import { heroDisplayName } from './public-dashboard.hero-names';
import { DashboardDataService } from './public-dashboard-data.service';
import {
  DashboardSummary,
  DashboardMatch,
  DashboardMatchPlayer,
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
import { SkillTier, skillTierForRatingScore } from './skill-tier';

const EMPTY_PERFORMANCE: Performance = {
  matches: 0,
  wins: 0,
  topHero: '',
  ratingScore: null,
  provisional: false,
};

const OVERVIEW_HERO_POOL_LIMIT = 6;

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
  private readonly revisionSubscription: Subscription;
  private apiRecentMatches: readonly DashboardMatch[] | null = null;
  private recentRequestSequence = 0;

  constructor(
    private readonly data: DashboardDataService,
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
      void this.loadRecentMatches();
      void this.loadTrends();
      this.changeDetector.markForCheck();
    });
    this.revisionSubscription = data.revision$.subscribe(() => {
      void this.loadRecentMatches();
      void this.loadTrends();
      this.changeDetector.markForCheck();
    });
    void this.loadRecentMatches();
    void this.loadTrends();
  }

  ngOnDestroy(): void {
    this.recentRequestSequence += 1;
    this.modeSubscription.unsubscribe();
    this.revisionSubscription.unsubscribe();
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

  get selectedHeroPool(): readonly HeroUsage[] {
    return (
      this.selectedPlayer?.heroPool.slice(0, OVERVIEW_HERO_POOL_LIMIT) ?? []
    );
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

  get selectedRecentMatches(): readonly DashboardMatch[] {
    if (this.apiRecentMatches !== null) {
      return this.apiRecentMatches;
    }
    const player = this.selectedPlayer;
    if (player === undefined) {
      return [];
    }
    return filterDashboardMatches(
      this.data.snapshot.matches,
      this.data.snapshot.standings['all-time'].players,
      {
        seasonKey: this.activeSeason,
        mode: this.activeMode,
        fixedPlayerId: player.id,
        playerQuery: '',
        selectedHeroes: [],
      },
    ).slice(0, 3);
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

  get ratingTerms(): RatingTerms {
    return ratingTermsForSeason(
      seasonOption(this.data.snapshot, this.activeSeason),
    );
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

  playerKda(player: PlayerStanding): number | null {
    return playerKdaForMode(player, this.activeMode)?.value ?? null;
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

  recordedMatchPlayer(match: DashboardMatch): DashboardMatchPlayer | undefined {
    return match.ally.players.find((player) => player.isRecordedPlayer);
  }

  formatMatchDate(value: string): string {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(new Date(value));
  }

  winRate(value: { readonly matches: number; readonly wins: number }): number {
    return winRate(value);
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

  podiumRank(player: PlayerStanding): number {
    return this.rankings.findIndex((standing) => standing.id === player.id) + 1;
  }

  playerSkillTier(player: PlayerStanding): SkillTier | null {
    return skillTierForRatingScore(this.playerPerformance(player).ratingScore);
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

  trackMatch(_index: number, match: DashboardMatch): number {
    return match.id;
  }

  trackResult(index: number, _result: MatchResult): number {
    return index;
  }

  private async loadRecentMatches(): Promise<void> {
    if (!this.matchApi.enabled) {
      return;
    }
    const player = this.selectedPlayer;
    if (player === undefined) {
      this.apiRecentMatches = [];
      return;
    }
    const sequence = ++this.recentRequestSequence;
    const page = await this.matchApi.list({
      page: 1,
      pageSize: 3,
      seasonKey: this.activeSeason,
      mode: this.activeMode,
      playerId: player.id,
      query: '',
      heroes: [],
    });
    if (sequence !== this.recentRequestSequence) {
      return;
    }
    this.apiRecentMatches = page?.items ?? null;
    this.changeDetector.markForCheck();
  }

  private async loadTrends(): Promise<void> {
    const playerIds = this.overviewRankings.map((player) => player.id);
    if (
      await this.data.ensureTrends(
        this.activeSeason,
        this.activeMode,
        playerIds,
      )
    ) {
      this.changeDetector.markForCheck();
    }
  }
}
