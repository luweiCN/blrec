import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
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
import { SeasonCorrectionNoticeComponent } from './season-correction-notice.component';
import { SkillTierBadgeComponent } from './skill-tier-badge.component';
import {
  TEST_DASHBOARD_SNAPSHOT,
  TEST_DASHBOARD_TRENDS,
} from './public-dashboard.test-data';

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
        SeasonCorrectionNoticeComponent,
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
    expect(page.querySelector('.season-correction-notice')?.textContent).toContain(
      '近期分数、段位和排名可能会调整',
    );
    expect(page.querySelector('.profile-kda-value')?.textContent).toContain(
      '145 局有效',
    );
    expect(page.querySelector('.profile-rank-showcase')?.textContent).toContain(
      '登峰造极 · 铜',
    );
    expect(page.querySelector('.profile-rank-showcase')?.textContent).toContain(
      '2,058',
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

  it('follows the persisted global mode', () => {
    dashboardMode.selectMode('brawl');
    fixture.detectChanges();

    expect(component.activeMode).toBe('brawl');
    expect(component.performance.matches).toBe(46);
  });

  it('shows thirty trend points by default and filters long histories', () => {
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

    expect(component.visibleTrendPoints.length).toBe(30);
    expect(
      fixture.nativeElement.querySelectorAll('.trend-chart-data tbody tr').length,
    ).toBe(30);
    expect(
      fixture.nativeElement.querySelector('.rating-trend-heading-actions')
        ?.textContent,
    ).toContain('显示 30 / 40 天');

    const rangeButtons = fixture.nativeElement.querySelectorAll(
      '.trend-range-filter button',
    ) as NodeListOf<HTMLButtonElement>;
    rangeButtons[0].click();
    fixture.detectChanges();
    expect(component.visibleTrendPoints.length).toBe(7);
    expect(
      fixture.nativeElement.querySelectorAll('.trend-chart-data tbody tr').length,
    ).toBe(7);

    rangeButtons[2].click();
    fixture.detectChanges();
    expect(component.visibleTrendPoints.length).toBe(40);
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
