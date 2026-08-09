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
  getPlayerTrend,
  getRankMovement,
  heroAverageEconomy,
  heroImage,
  heroKda,
  heroPeerComparisonKind,
  heroPeerComparisonText,
  heroPeerMetricKind,
  heroPeerMetricText,
  modeLabel,
  PlayerTrend,
  PlayerTrendPoint,
  PlayerKdaSummary,
  RankMovement,
  playerForSeason,
  playerKdaForMode,
  seasonOption,
  winRate,
} from './public-dashboard.data';
import { heroDisplayName } from './public-dashboard.hero-names';
import {
  getPlayerHeroProficiencies,
  HeroProficiency,
} from './public-dashboard.proficiency';
import { DashboardDataService } from './public-dashboard-data.service';
import {
  MatchResult,
  ModeBreakdown,
  ModeFilter,
  Performance,
  PlayerStanding,
  SeasonKey,
  SeasonOption,
} from './public-dashboard.models';
import {
  displayScoreForRatingDelta,
  displayScoreForRatingScore,
  SkillTierProgress,
  skillTierProgressForRatingScore,
} from './skill-tier';

interface PlayerSeasonRecord {
  readonly season: SeasonOption;
  readonly performance: Performance;
  readonly rank: number | null;
}

interface TrendChartPoint extends PlayerTrendPoint {
  readonly x: number;
  readonly y: number;
}

const EMPTY_PERFORMANCE: Performance = {
  matches: 0,
  wins: 0,
  topHero: '',
  ratingScore: null,
  provisional: false,
};

const TREND_CHART_WIDTH = 640;
const TREND_CHART_HEIGHT = 180;
const TREND_CHART_PADDING_X = 18;
const TREND_CHART_PADDING_Y = 18;

@Component({
  selector: 'app-player-detail-page',
  templateUrl: './player-detail-page.component.html',
  styleUrls: [
    './leaderboard-detail-page.scss',
    './leaderboard-profile-page.scss',
    './player-rank-showcase.scss',
    './leaderboard-profile-responsive.scss',
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PlayerDetailPageComponent implements OnDestroy {
  activeSeason: SeasonKey;
  activeMode: ModeFilter;
  readonly averageHeroEconomy = heroAverageEconomy;
  readonly displayScore = displayScoreForRatingScore;
  readonly heroKda = heroKda;
  readonly peerComparisonKind = heroPeerComparisonKind;
  readonly peerComparisonText = heroPeerComparisonText;
  readonly peerMetricKind = heroPeerMetricKind;
  readonly peerMetricText = heroPeerMetricText;
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

  get profileRank(): SkillTierProgress | null {
    return skillTierProgressForRatingScore(this.performance.ratingScore);
  }

  get kdaSummary(): PlayerKdaSummary | null {
    const player = this.seasonPlayer;
    return player === undefined
      ? null
      : playerKdaForMode(player, this.activeMode);
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

  get playerTrend(): PlayerTrend {
    return getPlayerTrend(
      this.data.trends,
      this.data.snapshot.snapshotId,
      this.activeSeason,
      this.activeMode,
      this.playerId ?? 0,
    );
  }

  get rankMovement(): RankMovement {
    return getRankMovement(this.playerTrend);
  }

  get rankMovementText(): string {
    switch (this.rankMovement.kind) {
      case 'new':
        return '新上榜';
      case 'same':
        return '排名持平';
      case 'up':
      case 'down':
        return this.rankMovement.text;
      case 'pending':
        return '待累计';
    }
  }

  get ratingDeltaText(): string {
    const delta = this.playerTrend.ratingDelta;
    if (delta === null) {
      return '—';
    }
    const displayDelta = displayScoreForRatingDelta(delta);
    const formattedDelta = displayDelta.toLocaleString('zh-CN');
    return displayDelta > 0 ? `+${formattedDelta}` : formattedDelta;
  }

  get ratingDeltaKind(): 'up' | 'down' | 'same' | 'pending' {
    const delta = this.playerTrend.ratingDelta;
    if (delta === null) {
      return 'pending';
    }
    if (delta > 0) {
      return 'up';
    }
    return delta < 0 ? 'down' : 'same';
  }

  get trendChartPoints(): readonly TrendChartPoint[] {
    const points = this.playerTrend.points;
    if (points.length === 0) {
      return [];
    }
    const scores = points.map((point) => point.ratingScore);
    const minimum = Math.min(...scores);
    const maximum = Math.max(...scores);
    const padding = maximum === minimum ? 5 : Math.max(2, (maximum - minimum) * 0.12);
    const domainMinimum = minimum - padding;
    const domainMaximum = maximum + padding;
    const domainRange = domainMaximum - domainMinimum;
    return points.map((point, index) => ({
      ...point,
      x:
        points.length === 1
          ? TREND_CHART_WIDTH / 2
          : TREND_CHART_PADDING_X +
            (index / (points.length - 1)) *
              (TREND_CHART_WIDTH - TREND_CHART_PADDING_X * 2),
      y:
        TREND_CHART_PADDING_Y +
        ((domainMaximum - point.ratingScore) / domainRange) *
          (TREND_CHART_HEIGHT - TREND_CHART_PADDING_Y * 2),
    }));
  }

  get trendPolyline(): string {
    return this.trendChartPoints
      .map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`)
      .join(' ');
  }

  get trendMinimumScore(): number | null {
    const scores = this.playerTrend.points.map(
      (point) => displayScoreForRatingScore(point.ratingScore) ?? 0,
    );
    return scores.length === 0 ? null : Math.min(...scores);
  }

  get trendMaximumScore(): number | null {
    const scores = this.playerTrend.points.map(
      (point) => displayScoreForRatingScore(point.ratingScore) ?? 0,
    );
    return scores.length === 0 ? null : Math.max(...scores);
  }

  get trendFirstDate(): string {
    return this.formatTrendDate(this.playerTrend.points[0]?.publicationDate);
  }

  get trendLastDate(): string {
    return this.formatTrendDate(
      this.playerTrend.points[this.playerTrend.points.length - 1]?.publicationDate,
    );
  }

  get trendChartLabel(): string {
    return `${this.player?.name ?? '玩家'}在${this.selectedSeason.label}${this.modeLabel()}的排位分趋势`;
  }

  get modeBreakdown(): readonly ModeBreakdown[] {
    return this.seasonPlayer === undefined
      ? []
      : getModeBreakdown(this.seasonPlayer);
  }

  get heroPool(): readonly HeroProficiency[] {
    return getPlayerHeroProficiencies(
      this.data.snapshot,
      this.activeSeason,
      this.activeMode,
      this.playerId ?? 0,
    );
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

  formatTrendDate(value: string | undefined): string {
    if (value === undefined) {
      return '';
    }
    const [, month, day] = value.split('-');
    return `${Number(month)}月${Number(day)}日`;
  }

  trackMode(_index: number, mode: ModeBreakdown): ModeFilter {
    return mode.key;
  }

  trackHero(_index: number, proficiency: HeroProficiency): string {
    return proficiency.usage.name;
  }

  trackSeason(_index: number, record: PlayerSeasonRecord): SeasonKey {
    return record.season.key;
  }

  trackResult(index: number, _result: MatchResult): number {
    return index;
  }

  trackTrendPoint(_index: number, point: TrendChartPoint): string {
    return point.publicationDate;
  }
}
