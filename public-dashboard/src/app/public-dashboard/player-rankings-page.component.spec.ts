import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import {
  DASHBOARD_MODE_STORAGE,
  DashboardModeService,
} from './dashboard-mode.service';
import { LeaderboardFiltersComponent } from './leaderboard-filters.component';
import { LeaderboardSeasonSelectComponent } from './leaderboard-season-select.component';
import { PlayerRankingsPageComponent } from './player-rankings-page.component';
import { DashboardDataService } from './public-dashboard-data.service';
import { TEST_DASHBOARD_SNAPSHOT } from './public-dashboard.test-data';

describe('PlayerRankingsPageComponent', () => {
  let fixture: ComponentFixture<PlayerRankingsPageComponent>;
  let component: PlayerRankingsPageComponent;
  let dashboardMode: DashboardModeService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [
        PlayerRankingsPageComponent,
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
          provide: DASHBOARD_MODE_STORAGE,
          useValue: { getItem: () => null, setItem: () => undefined },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(PlayerRankingsPageComponent);
    component = fixture.componentInstance;
    dashboardMode = TestBed.inject(DashboardModeService);
    fixture.detectChanges();
  });

  it('shows the first ten players and paginates the full ranking', () => {
    const page = fixture.nativeElement as HTMLElement;

    expect(component.filteredRows.length).toBe(16);
    expect(component.totalPages).toBe(2);
    expect(page.querySelectorAll('tbody tr').length).toBe(10);

    const secondPage = Array.from(
      page.querySelectorAll<HTMLButtonElement>('.pagination button'),
    ).find((button) => button.getAttribute('aria-label') === '第 2 页');
    secondPage?.click();
    fixture.detectChanges();

    expect(component.visibleRows.length).toBe(6);
    expect(page.querySelector('.result-status')?.textContent).toContain(
      '11–16',
    );
  });

  it('searches stable players by game alias without changing their rank', () => {
    const input = fixture.nativeElement.querySelector(
      'input[type="search"]',
    ) as HTMLInputElement;
    input.value = '河老板';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    expect(component.filteredRows.length).toBe(1);
    expect(component.filteredRows[0].rank).toBe(1);
    expect(component.filteredRows[0].player.name).toBe('星河');
  });

  it('searches each player name segment by pinyin', () => {
    const input = fixture.nativeElement.querySelector(
      'input[type="search"]',
    ) as HTMLInputElement;
    input.value = 'helao';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    expect(component.filteredRows.length).toBe(1);
    expect(component.filteredRows[0].player.name).toBe('星河');
  });

  it('resets pagination when the season or mode changes', () => {
    component.goToPage(2);
    component.selectSeason('2026-spring');
    expect(component.currentPage).toBe(1);

    component.goToPage(2);
    dashboardMode.selectMode('brawl');
    expect(component.currentPage).toBe(1);
  });

  it('links every visible player to a stable detail route', () => {
    const links = fixture.nativeElement.querySelectorAll(
      '.directory-player[href]',
    ) as NodeListOf<HTMLAnchorElement>;

    expect(links.length).toBe(component.visibleRows.length);
    expect(links[0].href).toContain('/players/');
  });
});
