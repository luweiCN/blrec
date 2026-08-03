import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import {
  DASHBOARD_MODE_STORAGE,
  DashboardModeService,
} from './dashboard-mode.service';
import { PlayerAvatarComponent } from './player-avatar.component';
import { DashboardDataService } from './public-dashboard-data.service';
import { PublicDashboardComponent } from './public-dashboard.component';
import { TEST_DASHBOARD_SNAPSHOT } from './public-dashboard.test-data';

describe('PublicDashboardComponent', () => {
  let fixture: ComponentFixture<PublicDashboardComponent>;
  let component: PublicDashboardComponent;
  let dashboardMode: DashboardModeService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [PublicDashboardComponent, PlayerAvatarComponent],
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

    fixture = TestBed.createComponent(PublicDashboardComponent);
    component = fixture.componentInstance;
    dashboardMode = TestBed.inject(DashboardModeService);
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
    expect(page.querySelector('.hero-intro')?.textContent).toContain(
      '历史数据正在持续同步中',
    );
    expect(page.querySelector('.hero-intro')?.textContent).not.toContain(
      '重复建档',
    );
  });

  it('updates the ranking when the global game mode changes', () => {
    dashboardMode.selectMode('brawl');
    fixture.detectChanges();

    expect(component.activeMode).toBe('brawl');
    expect(component.topPlayer?.name).toBe('洛川');
  });

  it('links player names and hero names without separate arrow controls', () => {
    const page = fixture.nativeElement as HTMLElement;

    expect(page.querySelector<HTMLAnchorElement>('.player-select')?.href).toContain(
      '/players/',
    );
    expect(page.querySelector<HTMLAnchorElement>('.hero-name a')?.href).toContain(
      '/heroes/',
    );
    expect(page.querySelector('.row-detail-link')).toBeNull();
    expect(page.textContent).not.toContain('→');
  });

  it('limits the featured player hero pool to six heroes', () => {
    const page = fixture.nativeElement as HTMLElement;
    expect(component.selectedPlayer?.heroPool.length).toBe(7);
    expect(component.selectedHeroPool.length).toBe(6);
    expect(page.querySelectorAll('.hero-pool li').length).toBe(6);
  });
});
