import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import { DASHBOARD_MODE_STORAGE } from './dashboard-mode.service';
import { HeroRankingsPageComponent } from './hero-rankings-page.component';
import { LeaderboardFiltersComponent } from './leaderboard-filters.component';
import { LeaderboardSeasonSelectComponent } from './leaderboard-season-select.component';
import { PlayerAvatarComponent } from './player-avatar.component';
import { DashboardDataService } from './public-dashboard-data.service';
import { TEST_DASHBOARD_SNAPSHOT } from './public-dashboard.test-data';

describe('HeroRankingsPageComponent', () => {
  let fixture: ComponentFixture<HeroRankingsPageComponent>;
  let component: HeroRankingsPageComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [
        HeroRankingsPageComponent,
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
          provide: DASHBOARD_MODE_STORAGE,
          useValue: { getItem: () => null, setItem: () => undefined },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(HeroRankingsPageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('shows the first ten eligible heroes and preserves the full ranking', () => {
    const page = fixture.nativeElement as HTMLElement;

    expect(component.filteredRows.length).toBe(14);
    expect(component.totalPages).toBe(2);
    expect(page.querySelectorAll('tbody tr').length).toBe(10);
  });

  it('searches hero aliases by pinyin and returns to the first page', () => {
    component.goToPage(2);
    const input = fixture.nativeElement.querySelector(
      'input[type="search"]',
    ) as HTMLInputElement;
    input.value = 'xingma';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    expect(component.currentPage).toBe(1);
    expect(component.filteredRows.length).toBe(1);
    expect(component.filteredRows[0].hero.name).toBe('Celeste');
    expect(
      fixture.nativeElement.querySelector('.hero-identity strong')?.textContent,
    ).toContain('星妈');
  });

  it('does not render the removed hero position column', () => {
    const page = fixture.nativeElement as HTMLElement;
    const headers = Array.from(page.querySelectorAll('th')).map((header) =>
      header.textContent?.trim(),
    );

    expect(headers).not.toContain('定位');
  });

  it('links every visible hero to its detail page', () => {
    const links = fixture.nativeElement.querySelectorAll(
      '.hero-identity[href]',
    ) as NodeListOf<HTMLAnchorElement>;

    expect(links.length).toBe(component.visibleRows.length);
    expect(links[0].href).toContain('/heroes/');
  });

  it('shows the highest proficiency player for every visible hero', () => {
    const page = fixture.nativeElement as HTMLElement;
    const leaders = page.querySelectorAll('.proficiency-leader');
    const headers = Array.from(page.querySelectorAll('th')).map((header) =>
      header.textContent?.trim(),
    );

    expect(headers).toContain('最熟练玩家');
    expect(leaders.length).toBe(component.visibleRows.length);
    expect((leaders[0] as HTMLAnchorElement).href).toContain('/players/');
    expect(leaders[0].textContent).toMatch(/大师|精通|熟练|常用|初试/u);
  });

  it('switches to usage ranking and returns to the first page', () => {
    component.goToPage(2);
    const usageButton = fixture.nativeElement.querySelectorAll(
      '.ranking-sort-control button',
    )[1] as HTMLButtonElement;
    usageButton.click();
    fixture.detectChanges();
    const matches = component.rankingRows.map(
      (row) => row.hero.modes['3v3'].matches,
    );

    expect(component.currentPage).toBe(1);
    expect(matches).toEqual([...matches].sort((left, right) => right - left));
    expect(fixture.nativeElement.textContent).toContain(
      '按对局次数展示当前模式最常被使用的英雄',
    );
  });
});
