import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import {
  DashboardAdminApiService,
  DashboardAdminHero,
  DashboardAdminMatch,
} from './dashboard-admin-api.service';
import { MatchAdminEditorModalComponent } from './match-admin-editor-modal.component';
import { TEST_DASHBOARD_MATCHES } from './public-dashboard.test-data';

const HEROES: readonly DashboardAdminHero[] = [
  {
    id: 7,
    label: '斯凯伊 · Skye',
    thumbnailUrl: '/heroes/7.jpg',
  },
  {
    id: 11,
    label: '鹰眼 · Kestrel',
    thumbnailUrl: '/heroes/11.jpg',
  },
];

const EDITABLE_MATCH: DashboardAdminMatch = {
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

describe('MatchAdminEditorModalComponent', () => {
  let fixture: ComponentFixture<MatchAdminEditorModalComponent>;
  let component: MatchAdminEditorModalComponent;
  let adminApi: {
    getMatch: jasmine.Spy<(matchId: number) => Promise<DashboardAdminMatch>>;
    listHeroes: jasmine.Spy<() => Promise<readonly DashboardAdminHero[]>>;
    updateMatch: jasmine.Spy;
    heroThumbnail: (hero: DashboardAdminHero) => string;
  };

  beforeEach(async () => {
    adminApi = {
      getMatch: jasmine.createSpy('getMatch').and.callFake(async () => ({
        ...EDITABLE_MATCH,
        players: EDITABLE_MATCH.players.map((player) => ({ ...player })),
      })),
      listHeroes: jasmine.createSpy('listHeroes').and.resolveTo(HEROES),
      updateMatch: jasmine.createSpy('updateMatch'),
      heroThumbnail: (hero) => hero.thumbnailUrl,
    };
    await TestBed.configureTestingModule({
      declarations: [MatchAdminEditorModalComponent],
      imports: [CommonModule],
      providers: [
        { provide: DashboardAdminApiService, useValue: adminApi },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(MatchAdminEditorModalComponent);
    component = fixture.componentInstance;
    component.match = {
      ...TEST_DASHBOARD_MATCHES[0],
      id: EDITABLE_MATCH.id,
      resultImage: {
        url: '/data/result.webp',
        width: 1600,
        height: 900,
      },
    };
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  });

  it('keeps the result image visible and defaults to compact summaries', () => {
    const editor = fixture.nativeElement as HTMLElement;

    expect(editor.querySelector('.match-admin-workspace')).not.toBeNull();
    expect(
      editor.querySelector('.match-admin-proof-image img')?.getAttribute('src'),
    ).toBe('/data/result.webp');
    expect(editor.querySelector('.match-admin-fields')).toBeNull();
    expect(editor.querySelector('.match-admin-player-editor')).toBeNull();
    expect(editor.querySelectorAll('.match-admin-player-summary').length).toBe(2);
    expect(editor.querySelector('.match-admin-player-summary')?.textContent).toContain(
      '斯凯伊 · Skye',
    );
  });

  it('opens only one editable section at a time', () => {
    component.toggleBasicEditor();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.match-admin-fields')).not.toBeNull();

    const player = component.editableMatch?.players[0];
    expect(player).toBeDefined();
    if (player === undefined) {
      return;
    }
    component.togglePlayerEditor(player);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.match-admin-fields')).toBeNull();
    expect(
      fixture.nativeElement.querySelectorAll('.match-admin-player-editor').length,
    ).toBe(1);
  });

  it('allows any player to replace the recorded player', () => {
    const opponent = component.editableMatch?.players[1];
    expect(opponent).toBeDefined();
    if (opponent === undefined) {
      return;
    }
    component.togglePlayerEditor(opponent);
    component.setRecordedPlayer(opponent);
    fixture.detectChanges();

    expect(component.recordedPlayer).toBe('right:1');
    expect(component.recordedPlayerLabel()).toContain('右1');
    const selected = fixture.nativeElement.querySelector(
      '.match-admin-recorded-toggle.selected',
    ) as HTMLButtonElement;
    expect(selected.textContent).toContain('主播本人');
  });

  it('uses the portrait picker to change a hero without native value matching', () => {
    const player = component.editableMatch?.players[0];
    expect(player).toBeDefined();
    if (player === undefined) {
      return;
    }
    component.togglePlayerEditor(player);
    component.openHeroPicker(player);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.match-admin-hero-picker')).not.toBeNull();

    component.selectHero(11);
    fixture.detectChanges();

    expect(player.heroId).toBe(11);
    expect(
      fixture.nativeElement.querySelector('.match-admin-hero-trigger')
        ?.textContent,
    ).toContain('鹰眼 · Kestrel');
  });
});
