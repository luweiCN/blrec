import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import {
  DashboardAdminHero,
  DashboardAdminMatch,
} from './dashboard-admin-api.service';
import { MatchAdminEditorModalComponent } from './match-admin-editor-modal.component';
import { MatchDetailModalComponent } from './match-detail-modal.component';
import { TEST_DASHBOARD_MATCHES } from './public-dashboard.test-data';

const ADMIN_HEROES: readonly DashboardAdminHero[] = [
  {
    id: 7,
    label: '斯凯伊 · Skye',
    thumbnailUrl: '/api/v1/vainglory/heroes/7/thumbnail',
  },
  {
    id: 11,
    label: '鹰眼 · Kestrel',
    thumbnailUrl: '/api/v1/vainglory/heroes/11/thumbnail',
  },
];

const ADMIN_MATCH: DashboardAdminMatch = {
  id: 1001,
  title: '测试对局',
  gameMode: '3v3',
  durationSeconds: 989,
  resultText: '战败',
  endReason: 'normal',
  matchKind: 'pvp',
  viewContext: 'played',
  statsEligible: true,
  winnerColor: 'orange',
  leftColor: 'teal',
  rightColor: 'orange',
  leftKills: 10,
  rightKills: 3,
  leftEconomy: 42_800,
  rightEconomy: 31_600,
  confidence: 0.953,
  recordedPlayerConfidence: 1,
  recordedPlayerSource: 'automatic',
  players: [
    {
      side: 'left',
      slot: 1,
      name: '主播',
      heroId: 7,
      heroLabel: '斯凯伊',
      heroSource: 'automatic',
      heroProbability: 0.926,
      kills: 4,
      deaths: 2,
      assists: 6,
      economy: 14_000,
      lastHits: 100,
      confidence: 0.9,
      isRecordedPlayer: true,
      afkPredictionStatus: 'active',
      afkProbability: 0,
      afkModelVersion: 'afk-v1',
      afkGateReason: '',
      afkManualOverride: null,
    },
    {
      side: 'right',
      slot: 1,
      name: '对手',
      heroId: 11,
      heroLabel: '鹰眼',
      heroSource: 'automatic',
      heroProbability: 0.934,
      kills: 2,
      deaths: 4,
      assists: 3,
      economy: 10_000,
      lastHits: 80,
      confidence: 0.91,
      isRecordedPlayer: false,
      afkPredictionStatus: 'active',
      afkProbability: 0,
      afkModelVersion: 'afk-v1',
      afkGateReason: '',
      afkManualOverride: null,
    },
  ],
};

describe('MatchDetailModalComponent', () => {
  let fixture: ComponentFixture<MatchDetailModalComponent>;
  let component: MatchDetailModalComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [MatchAdminEditorModalComponent, MatchDetailModalComponent],
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

  it('does not expose editing controls in the public build', () => {
    component.match = TEST_DASHBOARD_MATCHES[0];
    fixture.detectChanges();

    expect(
      (fixture.nativeElement as HTMLElement).querySelector('.match-admin-open'),
    ).toBeNull();
  });

  it('opens a separate admin editor with the result image and hero portraits', async () => {
    spyOnProperty(component.adminApi, 'enabled', 'get').and.returnValue(true);
    spyOn(component.adminApi, 'getMatch').and.resolveTo(ADMIN_MATCH);
    spyOn(component.adminApi, 'listHeroes').and.resolveTo(ADMIN_HEROES);
    component.match = {
      ...TEST_DASHBOARD_MATCHES[0],
      id: ADMIN_MATCH.id,
      resultImage: {
        url: '/data/result.webp',
        width: 1600,
        height: 900,
      },
    };
    fixture.detectChanges();

    component.openAdminEditor();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    const page = fixture.nativeElement as HTMLElement;
    const editor = page.querySelector('.match-admin-dialog') as HTMLElement;

    expect(editor).not.toBeNull();
    expect(page.querySelectorAll('[role="dialog"]').length).toBe(2);
    expect(page.querySelector('.match-admin-editor')).toBeNull();
    expect(
      editor.querySelector('.match-admin-proof-image img')?.getAttribute('src'),
    ).toBe('/data/result.webp');
    expect(editor.querySelectorAll('.match-admin-hero-avatar img').length).toBe(
      2,
    );
    expect(editor.textContent).toContain('K / D / A');
    expect(editor.textContent).toContain('英雄模型 92.6%');
  });
});
