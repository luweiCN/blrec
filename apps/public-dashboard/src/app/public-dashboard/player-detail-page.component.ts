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
  heroGoldPerMinute,
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
  PlayerHeroSort,
} from './public-dashboard.proficiency';
import { DashboardDataService } from './public-dashboard-data.service';
import {
  MatchResult,
  ModeBreakdown,
  ModeFilter,
  Performance,
  PlayerStanding,
  RatingForecast,
  RatingGoalForecast,
  SeasonKey,
  SeasonOption,
} from './public-dashboard.models';
import {
  displayScoreForRatingDelta,
  displayScoreForRatingScore,
  SkillTier,
  SkillTierProgress,
  skillTierForRatingScore,
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
  readonly xPercent: number;
  readonly yPercent: number;
}

type PlayerPromotionGoal =
  | {
      readonly kind: 'goal';
      readonly key: 'next-division' | 'next-tier' | 'ultimate';
      readonly title: string;
      readonly forecast: RatingGoalForecast;
      readonly remainingScore: number;
      readonly skillTier: SkillTier;
    }
  | {
      readonly kind: 'maximum';
      readonly key: 'next-division' | 'next-tier';
      readonly title: string;
      readonly message: string;
    };

const EMPTY_PERFORMANCE: Performance = {
  matches: 0,
  wins: 0,
  topHero: '',
  ratingScore: null,
  provisional: false,
  ratingForecast: null,
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
    './player-rating-forecast.scss',
    './leaderboard-profile-responsive.scss',
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PlayerDetailPageComponent implements OnDestroy {
  activeSeason: SeasonKey;
  activeMode: ModeFilter;
  activeHeroSort: PlayerHeroSort = 'proficiency';
  readonly heroGoldPerMinute = heroGoldPerMinute;
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

  get ratingForecast(): RatingForecast | null {
    return this.performance.ratingForecast ?? null;
  }

  get currentDisplayScore(): number | null {
    return displayScoreForRatingScore(this.performance.ratingScore);
  }

  get nextWinDisplayScore(): number | null {
    return displayScoreForRatingScore(
      this.ratingForecast?.nextWinScore ?? null,
    );
  }

  get nextLossDisplayScore(): number | null {
    return displayScoreForRatingScore(
      this.ratingForecast?.nextLossScore ?? null,
    );
  }

  get nextWinDisplayDelta(): number | null {
    const current = this.currentDisplayScore;
    const next = this.nextWinDisplayScore;
    return current === null || next === null ? null : next - current;
  }

  get nextLossDisplayDelta(): number | null {
    const current = this.currentDisplayScore;
    const next = this.nextLossDisplayScore;
    return current === null || next === null ? null : current - next;
  }

  get promotionGoals(): readonly PlayerPromotionGoal[] {
    const forecast = this.ratingForecast;
    const currentDisplayScore = this.currentDisplayScore;
    if (forecast === null || currentDisplayScore === null) {
      return [];
    }
    return [
      this.promotionGoal(
        'next-division',
        '下一小段',
        forecast.nextDivision,
        currentDisplayScore,
        '已达最高小段位',
      ),
      this.promotionGoal(
        'next-tier',
        '下一大段',
        forecast.nextTier,
        currentDisplayScore,
        '已达最高大段位',
      ),
      this.promotionGoal(
        'ultimate',
        '最终目标',
        forecast.ultimate,
        currentDisplayScore,
        '',
      ),
    ];
  }

  get forecastRecordLabel(): string {
    return this.selectedSeason.current || this.activeSeason === 'all-time'
      ? '当前已收录战绩'
      : '所选榜单末尾战绩';
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
    return points.map((point, index) => {
      const x =
        points.length === 1
          ? TREND_CHART_WIDTH / 2
          : TREND_CHART_PADDING_X +
            (index / (points.length - 1)) *
              (TREND_CHART_WIDTH - TREND_CHART_PADDING_X * 2);
      const y =
        TREND_CHART_PADDING_Y +
        ((domainMaximum - point.ratingScore) / domainRange) *
          (TREND_CHART_HEIGHT - TREND_CHART_PADDING_Y * 2);
      return {
        ...point,
        x,
        y,
        xPercent: (x / TREND_CHART_WIDTH) * 100,
        yPercent: (y / TREND_CHART_HEIGHT) * 100,
      };
    });
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
      this.activeHeroSort,
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

  selectHeroSort(sort: PlayerHeroSort): void {
    this.activeHeroSort = sort;
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

  trendPointLabel(point: PlayerTrendPoint): string {
    const score = displayScoreForRatingScore(point.ratingScore);
    const scoreLabel = score?.toLocaleString('zh-CN') ?? '暂无排位分';
    return `${this.formatTrendDate(point.publicationDate)}：${scoreLabel}，第 ${point.rank} 名`;
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

  trackPromotionGoal(
    _index: number,
    goal: PlayerPromotionGoal,
  ): PlayerPromotionGoal['key'] {
    return goal.key;
  }

  maximumGoalMessage(goal: PlayerPromotionGoal): string {
    return goal.kind === 'maximum' ? goal.message : '';
  }

  private promotionGoal(
    key: PlayerPromotionGoal['key'],
    title: string,
    forecast: RatingGoalForecast | null,
    currentDisplayScore: number,
    maximumMessage: string,
  ): PlayerPromotionGoal {
    if (forecast === null) {
      if (key === 'ultimate') {
        throw new Error('the ultimate promotion goal is required');
      }
      return { kind: 'maximum', key, title, message: maximumMessage };
    }
    const skillTier = skillTierForRatingScore(
      Math.ceil(forecast.targetDisplayScore / 3),
    );
    if (skillTier === null) {
      throw new Error('promotion goal contains an invalid target score');
    }
    return {
      kind: 'goal',
      key,
      title,
      forecast,
      remainingScore: Math.max(
        0,
        forecast.targetDisplayScore - currentDisplayScore,
      ),
      skillTier,
    };
  }
}
