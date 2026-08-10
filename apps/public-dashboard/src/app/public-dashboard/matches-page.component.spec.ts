import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import {
  DASHBOARD_MODE_STORAGE,
  DashboardModeService,
} from './dashboard-mode.service';
import { LeaderboardFiltersComponent } from './leaderboard-filters.component';
import { LeaderboardSeasonSelectComponent } from './leaderboard-season-select.component';
import { MatchDetailModalComponent } from './match-detail-modal.component';
import { MatchExplorerComponent } from './match-explorer.component';
import { MatchesPageComponent } from './matches-page.component';
import { DashboardDataService } from './public-dashboard-data.service';
import {
  TEST_DASHBOARD_SNAPSHOT,
  TEST_DASHBOARD_TRENDS,
} from './public-dashboard.test-data';

describe('MatchesPageComponent', () => {
  let fixture: ComponentFixture<MatchesPageComponent>;
  let component: MatchesPageComponent;
  let dashboardMode: DashboardModeService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [
        LeaderboardFiltersComponent,
        LeaderboardSeasonSelectComponent,
        MatchDetailModalComponent,
        MatchExplorerComponent,
        MatchesPageComponent,
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
          provide: DASHBOARD_MODE_STORAGE,
          useValue: { getItem: () => null, setItem: () => undefined },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(MatchesPageComponent);
    component = fixture.componentInstance;
    dashboardMode = TestBed.inject(DashboardModeService);
    fixture.detectChanges();
  });

  it('shows summary banners and a paginated match archive', () => {
    const page = fixture.nativeElement as HTMLElement;

    expect(page.querySelectorAll('.match-summary-grid article').length).toBe(4);
    expect(component.matches.length).toBe(12);
    expect(page.querySelectorAll('.match-row').length).toBe(10);
    expect(page.textContent).toContain('按直播中的实际发生时间');
  });

  it('follows the single global mode filter', () => {
    dashboardMode.selectMode('5v5');
    fixture.detectChanges();

    expect(component.activeMode).toBe('5v5');
    expect(component.matches).toEqual([]);
    expect(fixture.nativeElement.textContent).toContain('5V5');
  });
});
