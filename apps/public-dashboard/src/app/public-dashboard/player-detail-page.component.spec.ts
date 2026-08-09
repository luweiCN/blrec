import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';

import {
  DASHBOARD_MODE_STORAGE,
  DashboardModeService,
} from './dashboard-mode.service';
import { LeaderboardFiltersComponent } from './leaderboard-filters.component';
import { LeaderboardSeasonSelectComponent } from './leaderboard-season-select.component';
import { PlayerAvatarComponent } from './player-avatar.component';
import { PlayerDetailPageComponent } from './player-detail-page.component';
import { DashboardDataService } from './public-dashboard-data.service';
import {
  TEST_DASHBOARD_SNAPSHOT,
  TEST_DASHBOARD_TRENDS,
} from './public-dashboard.test-data';

describe('PlayerDetailPageComponent', () => {
  let fixture: ComponentFixture<PlayerDetailPageComponent>;
  let component: PlayerDetailPageComponent;
  let dashboardMode: DashboardModeService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [
        PlayerDetailPageComponent,
        LeaderboardFiltersComponent,
        LeaderboardSeasonSelectComponent,
        PlayerAvatarComponent,
      ],
      imports: [CommonModule, RouterTestingModule],
      providers: [
        {
          provide: DashboardDataService,
          useValue: {
            snapshot: TEST_DASHBOARD_SNAPSHOT,
            trends: TEST_DASHBOARD_TRENDS,
          },
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
    const heroLinks = Array.from(
      page.querySelectorAll('.profile-hero-link'),
    ).map((element) => element.textContent ?? '');
    expect(heroLinks.some((value) => value.includes('凯恩'))).toBeTrue();
    expect(page.querySelectorAll('.trend-point').length).toBe(3);
    expect(page.querySelector('.rating-trend-summary')?.textContent).toContain(
      '+6',
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
});
