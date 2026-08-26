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

  it('marks the AFK hero and explains the applied rating adjustment', () => {
    const match = TEST_DASHBOARD_MATCHES[0];
    component.match = {
      ...match,
      ally: {
        ...match.ally,
        players: match.ally.players.map((player, index) => ({
          ...player,
          afkStatus: index === 1 ? 'afk' : 'active',
        })),
      },
      rating: {
        scope: 'all',
        seasonKey: '2026-summer',
        matchNumber: 1,
        scoreBefore: 1000,
        scoreDelta: 0,
        scoreAfter: 1000,
        provisional: false,
        modelVersion: 8,
        afkAdjustment: 'protected_loss',
        afkPlayerDeficit: 0,
      },
    };
    fixture.detectChanges();
    const dialog = fixture.nativeElement as HTMLElement;

    const afkRows = dialog.querySelectorAll('.match-player--afk');
    expect(afkRows.length).toBe(1);
    expect(afkRows[0].textContent).toContain('挂机');
    expect(dialog.querySelector('.match-afk-adjustment')?.textContent).toContain(
      '己方队友挂机，触发失败保护，本局不扣排位分',
    );
    expect(dialog.querySelector('.match-afk-adjustment')?.textContent).toContain(
      '计分说明',
    );
  });

  it('never places the internal editor inside the detail dialog', () => {
    component.match = TEST_DASHBOARD_MATCHES[0];
    fixture.detectChanges();
    const dialog = fixture.nativeElement as HTMLElement;

    expect(dialog.querySelector('.match-admin-open')).toBeNull();
    expect(dialog.querySelector('app-match-admin-editor-modal')).toBeNull();
  });
});
