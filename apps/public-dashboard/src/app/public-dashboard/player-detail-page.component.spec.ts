import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';
import { Subject } from 'rxjs';

import {
  DASHBOARD_MODE_STORAGE,
  DashboardModeService,
} from './dashboard-mode.service';
import { LeaderboardFiltersComponent } from './leaderboard-filters.component';
import { LeaderboardSeasonSelectComponent } from './leaderboard-season-select.component';
import { MatchDetailModalComponent } from './match-detail-modal.component';
import { MatchExplorerComponent } from './match-explorer.component';
import { PlayerAvatarComponent } from './player-avatar.component';
import { PlayerDetailPageComponent } from './player-detail-page.component';
import { PlayerRatingTrendChartComponent } from './player-rating-trend-chart.component';
import { PlayerRoomLinksComponent } from './player-room-links.component';
import { DashboardDataService } from './public-dashboard-data.service';
import { DashboardTrends } from './public-dashboard.models';
import { SkillTierBadgeComponent } from './skill-tier-badge.component';
import {
  TEST_DASHBOARD_SNAPSHOT,
  TEST_DASHBOARD_TRENDS,
} from './public-dashboard.test-data';

function selectSeasonFromPage(
  fixture: ComponentFixture<PlayerDetailPageComponent>,
  label: string,
): void {
  const page = fixture.nativeElement as HTMLElement;
  const trigger = page.querySelector(
    '.season-trigger',
  ) as HTMLButtonElement;
  trigger.click();
  fixture.detectChanges();
  const option = Array.from(
    page.querySelectorAll<HTMLButtonElement>('.season-options button'),
  ).find((button) => button.textContent?.includes(label));
  option?.click();
  fixture.detectChanges();
}

describe('PlayerDetailPageComponent', () => {
  let fixture: ComponentFixture<PlayerDetailPageComponent>;
  let component: PlayerDetailPageComponent;
  let dashboardMode: DashboardModeService;
  let dashboardData: {
    snapshot: typeof TEST_DASHBOARD_SNAPSHOT;
    trends: DashboardTrends;
    revision$: Subject<string>;
    matchRevision$: Subject<void>;
  };

  beforeEach(async () => {
    dashboardData = {
      snapshot: TEST_DASHBOARD_SNAPSHOT,
      trends: TEST_DASHBOARD_TRENDS,
      revision$: new Subject<string>(),
      matchRevision$: new Subject<void>(),
    };
    await TestBed.configureTestingModule({
      declarations: [
        PlayerDetailPageComponent,
        LeaderboardFiltersComponent,
        LeaderboardSeasonSelectComponent,
        MatchDetailModalComponent,
        MatchExplorerComponent,
        PlayerAvatarComponent,
        PlayerRoomLinksComponent,
        PlayerRatingTrendChartComponent,
        SkillTierBadgeComponent,
      ],
      imports: [CommonModule, RouterTestingModule],
      providers: [
        {
          provide: DashboardDataService,
          useValue: dashboardData,
        },
        {
          provide: ActivatedRoute,
          useValue: {
            snapshot: { paramMap: convertToParamMap({ playerId: '1' }) },
          },
        },
        {
          provide: DASHBOARD_MODE_STORAGE,
          useValue: { getItem: () => null, setItem: () => undefined },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(PlayerDetailPageComponent);
    component = fixture.componentInstance;
    dashboardMode = TestBed.inject(DashboardModeService);
    fixture.detectChanges();
  });

  it('shows the player identity, 3V3 record, hero pool and season history', () => {
    const page = fixture.nativeElement as HTMLElement;

    expect(component.player?.name).toBe('星河');
    expect(component.performance.matches).toBe(102);
    expect(component.kdaSummary?.matches).toBe(145);
    expect(component.kdaSummary?.value).toBeCloseTo(3.68, 2);
    expect(component.heroPool.length).toBe(7);
    expect(component.seasonHistory.length).toBeGreaterThan(1);
    expect(page.querySelector('h1')?.textContent).toContain('星河');
    expect(page.querySelector('.profile-kda-value')?.textContent).toContain(
      '145 局有效',
    );
    expect(page.querySelector('.profile-rank-showcase')?.textContent).toContain(
      '登峰造极 · 铜',
    );
    expect(page.querySelector('.profile-rank-showcase')?.textContent).toContain(
      '2,058',
    );
    expect(page.querySelector('.player-profile-summary')?.textContent).toContain(
      '当前排位分',
    );
    expect(page.querySelector('.profile-rank-showcase')?.textContent).toContain(
      '当前段位',
    );
    expect(page.querySelector('.profile-rank-showcase')?.textContent).toContain(
      '赛季最高排位分',
    );
    expect(page.querySelector('.next-match-context')?.textContent).toContain(
      '68.6%',
    );
    expect(page.querySelector('.next-match-outcomes')?.textContent).toContain(
      '+6',
    );
    expect(page.querySelector('.next-match-outcomes')?.textContent).toContain(
      '−18',
    );
    expect(page.querySelector('.next-match-outcomes')?.textContent).toContain(
      '2,064',
    );
    const promotionCards = page.querySelectorAll('.promotion-goal-card');
    expect(promotionCards.length).toBe(3);
    expect(Array.from(promotionCards).map((card) => card.textContent)).toEqual([
      jasmine.stringMatching(/下一小段.*9\s*段.*银.*还差.*76.*最快.*13\s*局/u),
      jasmine.stringMatching(/下一大段.*10\s*段.*铜.*还差.*342.*最快.*57\s*局/u),
      jasmine.stringMatching(/最终目标.*10\s*段.*金.*还差.*742.*最快.*124\s*局/u),
    ]);
    expect(
      Array.from(promotionCards).every((card) =>
        card.textContent?.includes('保持当前胜率'),
      ),
    ).toBeTrue();
    expect(page.querySelector('.rating-forecast-note')?.textContent).toContain(
      '实际局数会随后续胜负变化',
    );
    const rankProgress = page.querySelector('.profile-rank-progress');
    expect(rankProgress?.getAttribute('role')).toBe('progressbar');
    expect(rankProgress?.getAttribute('aria-valuemin')).toBe('2000');
    expect(rankProgress?.getAttribute('aria-valuemax')).toBe('2400');
    expect(rankProgress?.getAttribute('aria-valuenow')).toBe('2058');
    expect(page.querySelectorAll('.profile-rank-labels > span').length).toBe(3);
    const heroLinks = Array.from(
      page.querySelectorAll('.profile-hero-link'),
    ).map((element) => element.textContent ?? '');
    expect(heroLinks.some((value) => value.includes('凯恩'))).toBeTrue();
    expect(page.querySelectorAll('.trend-chart-data tbody tr').length).toBe(3);
    expect(page.querySelector('.trend-chart-shell canvas')).not.toBeNull();
    expect(
      page.querySelector('.trend-chart-data')?.textContent,
    ).toContain('2,016');
    expect(page.querySelectorAll('.trend-range-filter button').length).toBe(3);
    expect(page.querySelector('.rating-trend-summary')?.textContent).toContain(
      '+18',
    );
    expect(page.querySelector('.rating-trend-summary')?.textContent).toContain(
      '当前段位与排位分',
    );
    expect(
      page.querySelector('.trend-chart-shell canvas')?.getAttribute('aria-label'),
    ).toContain('当前且为赛季最高：8月3日，2,058 排位分');
    expect(page.querySelector('.season-history-table')?.textContent).toContain(
      '赛季最高',
    );
    expect(page.querySelector('.season-history-table')?.textContent).toContain(
      '综合',
    );
    expect(page.querySelector('.usage-rank')?.textContent).toContain('/');
    expect(page.querySelector('.peer-comparison')?.textContent).toMatch(
      /其他玩家|暂无其他玩家/u,
    );
    const comparisons = Array.from(
      page.querySelectorAll('.peer-comparison'),
    ).map((element) => element.textContent ?? '');
    expect(comparisons.some((value) => value.includes('KDA'))).toBeTrue();
    const scores = component.heroPool.map((record) => record.score);
    expect(scores).toEqual([...scores].sort((left, right) => right - left));
    expect(page.querySelector('.proficiency-score')?.textContent).toMatch(
      /大师|精通|熟练|常用|初试/u,
    );
  });

  it('makes the current score primary while retaining the season record', () => {
    spyOnProperty(component, 'performance', 'get').and.returnValue({
      ...component.performance,
      ratingScore: 940,
      currentRatingScore: 939.3333333333334,
    });
    dashboardData.revision$.next('peak-current-test');
    fixture.detectChanges();

    const page = fixture.nativeElement as HTMLElement;
    const showcase = page.querySelector('.profile-rank-showcase');
    const current = showcase?.querySelector('.profile-rank-score');
    const record = showcase?.querySelector('.profile-season-record');
    const chart = fixture.debugElement.query(
      By.directive(PlayerRatingTrendChartComponent),
    ).componentInstance as PlayerRatingTrendChartComponent;

    expect(component.profileRank?.skillTier.displayScore).toBe(2_818);
    expect(current?.textContent).toContain('当前排位分');
    expect(current?.textContent).toContain('2,818');
    expect(record?.textContent).toContain('赛季最高排位分');
    expect(record?.textContent).toContain('2,820');
    expect(
      page.querySelector('.profile-rank-progress')?.getAttribute('aria-valuenow'),
    ).toBe('2818');
    expect(chart.latestPointLabel).toBe('当前');
    expect(chart.seasonPeakDisplayScore).toBe(2_820);
  });

  it('shows only distinct unfinished goals beyond the season peak', () => {
    const goal = (targetDisplayScore: number) => ({
      targetDisplayScore,
      allWinMatches: 10,
      currentWinRateMatches: 20,
    });
    const bronzePerformance = {
      matches: 100,
      wins: 70,
      topHero: 'Caine',
      ratingScore: 833,
      currentRatingScore: 800,
      provisional: false,
      ratingForecast: {
        nextWinScore: 802,
        nextLossScore: 796,
        nextDivision: goal(2600),
        nextTier: null,
        ultimate: goal(2800),
      },
    } as unknown as typeof component.performance;

    const cases: readonly {
      readonly performance: typeof component.performance;
      readonly currentDisplayScore: number;
      readonly targets: readonly number[];
    }[] = [
      {
        performance: bronzePerformance,
        currentDisplayScore: 2400,
        targets: [2600, 2800],
      },
      {
        performance: {
          ...bronzePerformance,
          ratingScore: 2755 / 3,
          currentRatingScore: 900,
          ratingForecast: {
            nextWinScore: 902,
            nextLossScore: 896,
            nextDivision: goal(2800),
            nextTier: null,
            ultimate: goal(2800),
          },
        } as unknown as typeof component.performance,
        currentDisplayScore: 2700,
        targets: [2800],
      },
      {
        performance: {
          ...bronzePerformance,
          ratingScore: 940,
          currentRatingScore: 900,
          ratingForecast: {
            nextWinScore: 902,
            nextLossScore: 896,
            nextDivision: null,
            nextTier: null,
            ultimate: goal(2800),
          },
        } as unknown as typeof component.performance,
        currentDisplayScore: 2700,
        targets: [],
      },
    ];

    for (const value of cases) {
      const caseFixture = TestBed.createComponent(PlayerDetailPageComponent);
      const caseComponent = caseFixture.componentInstance;
      spyOnProperty(caseComponent, 'performance', 'get').and.returnValue(
        value.performance,
      );
      caseFixture.detectChanges();

      expect(caseComponent.currentDisplayScore).toBe(value.currentDisplayScore);
      expect(
        caseComponent.promotionGoals.map(
          (item) => item.forecast.targetDisplayScore,
        ),
      ).toEqual(value.targets);
      expect(
        caseFixture.nativeElement.querySelectorAll('.promotion-goal-card')
          .length,
      ).toBe(value.targets.length);
      expect(
        caseFixture.nativeElement.querySelector('.promotion-goal-grid') ===
          null,
      ).toBe(value.targets.length === 0);
      caseFixture.destroy();
    }
  });

  it('follows the persisted global mode', () => {
    dashboardMode.selectMode('brawl');
    fixture.detectChanges();

    expect(component.activeMode).toBe('brawl');
    expect(component.performance.matches).toBe(46);
  });

  it('shows the whole season by default and still filters long histories', () => {
    const endDate = new Date(Date.UTC(2026, 7, 3));
    dashboardData.trends = {
      schemaVersion: 1,
      updatedAt: TEST_DASHBOARD_SNAPSHOT.generatedAt,
      publications: Array.from({ length: 40 }, (_, index) => {
        const publicationDate = new Date(
          endDate.getTime() - (39 - index) * 24 * 60 * 60 * 1_000,
        )
          .toISOString()
          .slice(0, 10);
        const standing = {
          playerId: 1,
          rank: 40 - index,
          ratingScore: 640 + index,
        };
        return {
          snapshotId:
            index === 39
              ? TEST_DASHBOARD_SNAPSHOT.snapshotId
              : `trend-${index}`,
          publicationDate,
          sourceLastMatchId: 12_000 + index,
          standings: {
            '2026-summer': {
              all: [standing],
              '3v3': [standing],
              brawl: [],
              '5v5': [],
            },
          },
        };
      }),
    };
    fixture.destroy();
    fixture = TestBed.createComponent(PlayerDetailPageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();

    expect(component.activeTrendRange).toBe('all');
    expect(component.visibleTrendPoints.length).toBe(40);
    expect(
      fixture.nativeElement.querySelectorAll('.trend-chart-data tbody tr').length,
    ).toBe(40);
    expect(
      fixture.nativeElement.querySelector('.rating-trend-heading-actions')
        ?.textContent,
    ).toContain('共 40 个每日节点');

    const rangeButtons = fixture.nativeElement.querySelectorAll(
      '.trend-range-filter button',
    ) as NodeListOf<HTMLButtonElement>;
    expect(rangeButtons[2].textContent).toContain('整个赛季');
    expect(rangeButtons[2].getAttribute('aria-pressed')).toBe('true');
    rangeButtons[0].click();
    fixture.detectChanges();
    expect(component.visibleTrendPoints.length).toBe(7);
    expect(
      fixture.nativeElement.querySelectorAll('.trend-chart-data tbody tr').length,
    ).toBe(7);

    rangeButtons[1].click();
    fixture.detectChanges();
    expect(component.visibleTrendPoints.length).toBe(30);
    expect(
      fixture.nativeElement.querySelector('.rating-trend-heading-actions')
        ?.textContent,
    ).toContain('显示 30 / 40 天');

    component.selectSeason('2026-spring');
    expect(component.activeTrendRange).toBe('all');
  });

  it('uses scope-specific rating terms and only forecasts the current season', () => {
    expect(component.ratingMetricLabel).toBe('赛季最高排位分');
    expect(component.tierMetricLabel).toBe('赛季最高段位');
    expect(component.latestMetricLabel).toBe('当前段位与排位分');
    expect(component.latestContextLabel).toBe('当前');
    expect(
      fixture.nativeElement.querySelector('.rating-forecast-section'),
    ).not.toBeNull();

    selectSeasonFromPage(fixture, '2026 春季赛');
    expect(component.ratingMetricLabel).toBe('赛季最高排位分');
    expect(component.tierMetricLabel).toBe('赛季最高段位');
    expect(component.latestMetricLabel).toBe('赛季末段位与排位分');
    expect(component.latestContextLabel).toBe('赛季末');
    expect(
      fixture.nativeElement.querySelector('.profile-season-record'),
    ).not.toBeNull();
    expect(
      fixture.nativeElement.querySelector('.rating-forecast-section'),
    ).toBeNull();

    selectSeasonFromPage(fixture, '跨赛季总榜');
    expect(component.ratingMetricLabel).toBe('综合排位分');
    expect(component.tierMetricLabel).toBe('综合段位');
    expect(component.latestMetricLabel).toBe('综合段位与排位分');
    expect(component.latestContextLabel).toBe('综合');
    expect(
      fixture.nativeElement.querySelector('.profile-season-record'),
    ).toBeNull();
    expect(
      fixture.nativeElement.querySelector('.rating-forecast-section'),
    ).toBeNull();
    expect(
      fixture.nativeElement.querySelector('.trend-range-filter button:last-child')
        ?.textContent,
    ).toContain('全部记录');
  });

  it('sorts the hero pool by usage, win rate or KDA', () => {
    component.selectHeroSort('usage');
    const matches = component.heroPool.map((record) => record.usage.matches);
    expect(matches).toEqual([...matches].sort((left, right) => right - left));

    component.selectHeroSort('win-rate');
    const winRates = component.heroPool.map((record) =>
      component.winRate(record.usage),
    );
    expect(winRates).toEqual(
      [...winRates].sort((left, right) => right - left),
    );

    component.selectHeroSort('kda');
    const kdas = component.heroPool.map(
      (record) => component.heroKda(record.usage) ?? Number.NEGATIVE_INFINITY,
    );
    expect(kdas).toEqual([...kdas].sort((left, right) => right - left));
  });

  it('renders four hero-pool sort options', () => {
    const buttons = fixture.nativeElement.querySelectorAll(
      '.profile-table-heading-actions .profile-hero-sort button',
    ) as NodeListOf<HTMLButtonElement>;

    expect(buttons.length).toBe(4);
    expect(Array.from(buttons).map((button) => button.textContent?.trim())).toEqual([
      '熟练度',
      '使用次数',
      '胜率',
      'KDA',
    ]);
  });
});
