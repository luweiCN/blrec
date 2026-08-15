import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';
import { Subject } from 'rxjs';

import {
  DASHBOARD_MODE_STORAGE,
  DashboardModeService,
} from './dashboard-mode.service';
import { LeaderboardFiltersComponent } from './leaderboard-filters.component';
import { LeaderboardSeasonSelectComponent } from './leaderboard-season-select.component';
import { PlayerAvatarComponent } from './player-avatar.component';
import { PlayerRankingsPageComponent } from './player-rankings-page.component';
import { PlayerRoomLinksComponent } from './player-room-links.component';
import { DashboardDataService } from './public-dashboard-data.service';
import { SkillTierBadgeComponent } from './skill-tier-badge.component';
import {
  TEST_DASHBOARD_SNAPSHOT,
  TEST_DASHBOARD_TRENDS,
} from './public-dashboard.test-data';

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
        PlayerAvatarComponent,
        PlayerRoomLinksComponent,
        SkillTierBadgeComponent,
      ],
      imports: [CommonModule, RouterTestingModule],
      providers: [
        {
          provide: DashboardDataService,
          useValue: {
            snapshot: TEST_DASHBOARD_SNAPSHOT,
            trends: TEST_DASHBOARD_TRENDS,
            revision$: new Subject<string>(),
          },
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
    expect(page.querySelectorAll('.directory-score img').length).toBe(10);
    expect(
      page.querySelectorAll('app-player-room-links.compact-live').length,
    ).toBe(10);

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

  it('sorts players by matches, wins or established win rate', () => {
    component.goToPage(2);
    component.selectSort('matches');
    fixture.detectChanges();
    const matches = component.rankingRows.map(
      (row) => row.player.modes['3v3'].matches,
    );
    expect(component.currentPage).toBe(1);
    expect(matches).toEqual([...matches].sort((left, right) => right - left));

    component.selectSort('wins');
    const wins = component.rankingRows.map(
      (row) => row.player.modes['3v3'].wins,
    );
    expect(wins).toEqual([...wins].sort((left, right) => right - left));

    const winRateButton = fixture.nativeElement.querySelectorAll(
      '.ranking-sort-control button',
    )[3] as HTMLButtonElement;
    winRateButton.click();
    fixture.detectChanges();
    const winRates = component.rankingRows.map((row) =>
      component.winRate(row.player.modes['3v3']),
    );
    expect(
      component.rankingRows.every(
        (row) => row.player.modes['3v3'].matches >= 20,
      ),
    ).toBeTrue();
    expect(winRates).toEqual(
      [...winRates].sort((left, right) => right - left),
    );
    expect(fixture.nativeElement.textContent).toContain('至少 20 局');
    expect(fixture.nativeElement.querySelector('.rank-movement')).toBeNull();
  });

  it('uses the same combined sort and search toolbar as the hero ranking', () => {
    const toolbar = fixture.nativeElement.querySelector(
      '.player-directory-toolbar .directory-actions',
    ) as HTMLElement;

    expect(toolbar.querySelectorAll('.ranking-sort-control button').length).toBe(4);
    expect(toolbar.querySelector('input[type="search"]')).not.toBeNull();
  });

  it('links every visible player to a stable detail route', () => {
    const links = fixture.nativeElement.querySelectorAll(
      '.directory-player-main[href]',
    ) as NodeListOf<HTMLAnchorElement>;

    expect(links.length).toBe(component.visibleRows.length);
    expect(links[0].href).toContain('/players/');
  });

  it('keeps rank movement in the rank column', () => {
    const movement = fixture.nativeElement.querySelector(
      '.directory-rank .rank-movement',
    ) as HTMLElement;

    expect(movement.textContent).toContain('↑1');
    expect(movement.closest('td')?.getAttribute('data-label')).toBe('排名');
  });
});
