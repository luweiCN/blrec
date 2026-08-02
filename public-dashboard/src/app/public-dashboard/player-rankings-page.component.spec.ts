import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import { LeaderboardFiltersComponent } from './leaderboard-filters.component';
import { LeaderboardSeasonSelectComponent } from './leaderboard-season-select.component';
import { PlayerRankingsPageComponent } from './player-rankings-page.component';

describe('PlayerRankingsPageComponent', () => {
  let fixture: ComponentFixture<PlayerRankingsPageComponent>;
  let component: PlayerRankingsPageComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [
        PlayerRankingsPageComponent,
        LeaderboardFiltersComponent,
        LeaderboardSeasonSelectComponent,
      ],
      imports: [CommonModule, RouterTestingModule],
    }).compileComponents();

    fixture = TestBed.createComponent(PlayerRankingsPageComponent);
    component = fixture.componentInstance;
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
    component.selectMode('brawl');
    expect(component.currentPage).toBe(1);
  });
});
