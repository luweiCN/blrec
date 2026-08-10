import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { MatchDetailModalComponent } from './match-detail-modal.component';
import { MatchExplorerComponent } from './match-explorer.component';
import {
  TEST_DASHBOARD_MATCHES,
  TEST_DASHBOARD_SNAPSHOT,
} from './public-dashboard.test-data';

describe('MatchExplorerComponent', () => {
  let fixture: ComponentFixture<MatchExplorerComponent>;
  let component: MatchExplorerComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [MatchDetailModalComponent, MatchExplorerComponent],
      imports: [CommonModule],
    }).compileComponents();

    fixture = TestBed.createComponent(MatchExplorerComponent);
    component = fixture.componentInstance;
    component.matches = TEST_DASHBOARD_MATCHES;
    component.players =
      TEST_DASHBOARD_SNAPSHOT.standings['all-time'].players;
    component.seasonKey = '2026-summer';
    component.mode = 'all';
    fixture.detectChanges();
  });

  it('shows ten matches per page in live-time order', () => {
    const page = fixture.nativeElement as HTMLElement;

    expect(component.filteredMatches.length).toBe(12);
    expect(component.pageMatches.length).toBe(10);
    expect(page.querySelectorAll('.match-row').length).toBe(10);
    expect(component.pageMatches[0].playedAt).toBe(
      TEST_DASHBOARD_MATCHES[0].playedAt,
    );

    const nextButton = page.querySelector(
      '.match-pagination button:last-child',
    ) as HTMLButtonElement;
    nextButton.click();
    fixture.detectChanges();

    expect(component.page).toBe(2);
    expect(component.pageMatches.length).toBe(2);
    expect(page.querySelectorAll('.match-row').length).toBe(2);
  });

  it('combines player and lineup filters and resets pagination', () => {
    component.nextPage();
    component.playerQuery = '茉莉';
    component.toggleHero('Caine');
    fixture.detectChanges();

    expect(component.page).toBe(1);
    expect(component.filteredMatches.length).toBeGreaterThan(0);
    expect(
      component.filteredMatches.every((match) =>
        [...match.ally.players, ...match.enemy.players].some(
          (player) => player.heroName === 'Caine',
        ),
      ),
    ).toBeTrue();
  });

  it('opens one accessible match detail dialog from a row', () => {
    const row = fixture.nativeElement.querySelector(
      '.match-row',
    ) as HTMLButtonElement;
    row.click();
    fixture.detectChanges();

    const dialog = fixture.nativeElement.querySelector(
      '[role="dialog"]',
    ) as HTMLElement;
    expect(dialog).not.toBeNull();
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.textContent).toContain('对局详情');
  });
});
