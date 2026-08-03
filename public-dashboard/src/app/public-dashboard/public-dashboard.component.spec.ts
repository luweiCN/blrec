import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import { DashboardDataService } from './public-dashboard-data.service';
import { PublicDashboardComponent } from './public-dashboard.component';
import { TEST_DASHBOARD_SNAPSHOT } from './public-dashboard.test-data';

describe('PublicDashboardComponent', () => {
  let fixture: ComponentFixture<PublicDashboardComponent>;
  let component: PublicDashboardComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [PublicDashboardComponent],
      imports: [CommonModule, RouterTestingModule],
      providers: [
        {
          provide: DashboardDataService,
          useValue: { snapshot: TEST_DASHBOARD_SNAPSHOT },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(PublicDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('renders the loaded snapshot leaderboard', () => {
    const page = fixture.nativeElement as HTMLElement;

    expect(page.querySelector('h1')?.textContent).toContain('每一局');
    expect(page.querySelector('.snapshot-note')?.textContent).toContain(
      '真实数据',
    );
    expect(page.querySelectorAll('tbody tr').length).toBe(10);
    expect(
      page.querySelector('.podium-slot.first .podium-name')?.textContent,
    ).toContain('星河');
    expect(
      page
        .querySelector<HTMLImageElement>('.hero-chip img')
        ?.getAttribute('src'),
    ).toBe('assets/vainglory/heroes/caine.jpg');
    expect(page.querySelector('.hero-chip')?.textContent).toContain('凯恩');
    expect(page.textContent).not.toContain('综合积分');
  });

  it('updates the ranking when a game mode is selected', () => {
    const buttons = Array.from(
      fixture.nativeElement.querySelectorAll('.mode-picker button'),
    ) as HTMLButtonElement[];
    const brawl = buttons.find((button) =>
      button.textContent?.includes('乱斗'),
    );

    brawl?.click();
    fixture.detectChanges();

    expect(component.activeMode).toBe('brawl');
    expect(component.topPlayer?.name).toBe('洛川');
    expect(brawl?.getAttribute('aria-pressed')).toBe('true');
  });

  it('shows the selected player profile', () => {
    const playerButtons = Array.from(
      fixture.nativeElement.querySelectorAll('.player-select'),
    ) as HTMLButtonElement[];

    playerButtons[1].click();
    fixture.detectChanges();

    expect(component.selectedPlayer?.name).toBe('洛川');
    expect(
      fixture.nativeElement.querySelector('#player-detail-title')?.textContent,
    ).toContain('洛川');
  });
});
