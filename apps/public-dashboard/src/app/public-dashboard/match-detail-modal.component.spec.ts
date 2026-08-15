import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import { MatchDetailModalComponent } from './match-detail-modal.component';
import { TEST_DASHBOARD_MATCHES } from './public-dashboard.test-data';

describe('MatchDetailModalComponent', () => {
  let fixture: ComponentFixture<MatchDetailModalComponent>;
  let component: MatchDetailModalComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [MatchDetailModalComponent],
      imports: [CommonModule, RouterTestingModule],
    }).compileComponents();

    fixture = TestBed.createComponent(MatchDetailModalComponent);
    component = fixture.componentInstance;
  });

  it('shows only lineup and KDA for a brawl result', () => {
    component.match = { ...TEST_DASHBOARD_MATCHES[0], mode: 'brawl' };
    fixture.detectChanges();
    const dialog = fixture.nativeElement as HTMLElement;

    expect(dialog.querySelector('.match-player-kda')).not.toBeNull();
    expect(dialog.querySelector('.match-player-economy')).toBeNull();
    expect(dialog.textContent).not.toContain('经济');
    expect(dialog.textContent).not.toContain('补刀');
  });

  it('shows economy but never the ambiguous third statistic for 3v3', () => {
    component.match = TEST_DASHBOARD_MATCHES[0];
    fixture.detectChanges();
    const dialog = fixture.nativeElement as HTMLElement;

    expect(dialog.querySelector('.match-player-economy')).not.toBeNull();
    expect(dialog.textContent).toContain('经济');
    expect(dialog.textContent).not.toContain('补刀');
  });
});
