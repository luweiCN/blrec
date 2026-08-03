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
import { PlayerDetailPageComponent } from './player-detail-page.component';
import { DashboardDataService } from './public-dashboard-data.service';
import { TEST_DASHBOARD_SNAPSHOT } from './public-dashboard.test-data';

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
      ],
      imports: [CommonModule, RouterTestingModule],
      providers: [
        {
          provide: DashboardDataService,
          useValue: { snapshot: TEST_DASHBOARD_SNAPSHOT },
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
    expect(component.heroPool.length).toBe(3);
    expect(component.seasonHistory.length).toBeGreaterThan(1);
    expect(page.querySelector('h1')?.textContent).toContain('星河');
    expect(page.querySelector('.profile-hero-link')?.textContent).toContain(
      '凯恩',
    );
  });

  it('follows the persisted global mode', () => {
    dashboardMode.selectMode('brawl');
    fixture.detectChanges();

    expect(component.activeMode).toBe('brawl');
    expect(component.performance.matches).toBe(46);
  });
});
