import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import { DASHBOARD_MODE_STORAGE } from './dashboard-mode.service';
import { HeroRankingsPageComponent } from './hero-rankings-page.component';
import { LeaderboardFiltersComponent } from './leaderboard-filters.component';
import { LeaderboardSeasonSelectComponent } from './leaderboard-season-select.component';
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
});
