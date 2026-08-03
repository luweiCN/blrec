import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';

import { DASHBOARD_MODE_STORAGE } from './dashboard-mode.service';
import { HeroDetailPageComponent } from './hero-detail-page.component';
import { LeaderboardFiltersComponent } from './leaderboard-filters.component';
import { LeaderboardSeasonSelectComponent } from './leaderboard-season-select.component';
import { PlayerAvatarComponent } from './player-avatar.component';
import { DashboardDataService } from './public-dashboard-data.service';
import { TEST_DASHBOARD_SNAPSHOT } from './public-dashboard.test-data';

describe('HeroDetailPageComponent', () => {
  let fixture: ComponentFixture<HeroDetailPageComponent>;
  let component: HeroDetailPageComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [
        HeroDetailPageComponent,
        LeaderboardFiltersComponent,
        LeaderboardSeasonSelectComponent,
        PlayerAvatarComponent,
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
            snapshot: { paramMap: convertToParamMap({ heroId: 'Caine' }) },
          },
        },
        {
          provide: DASHBOARD_MODE_STORAGE,
          useValue: { getItem: () => null, setItem: () => undefined },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(HeroDetailPageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('shows the hero identity, mode records, players and season history', () => {
    const page = fixture.nativeElement as HTMLElement;

    expect(component.hero?.name).toBe('Caine');
    expect(component.identity?.officialName).toBe('凯恩');
    expect(component.modeRecords.length).toBe(3);
    expect(component.playerRecords.length).toBeGreaterThan(0);
    expect(component.seasonHistory.length).toBeGreaterThan(0);
    expect(page.querySelector('h1')?.textContent).toContain('凯恩');
    expect(page.querySelector('.profile-player-link')?.textContent).toContain(
      '星河',
    );
  });
});
