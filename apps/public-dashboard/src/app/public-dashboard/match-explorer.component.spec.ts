import { CommonModule } from '@angular/common';
import {
  ComponentFixture,
  fakeAsync,
  flushMicrotasks,
  TestBed,
  tick,
} from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import {
  DashboardMatchApiService,
  DashboardMatchPage,
} from './dashboard-match-api.service';
import { MatchAdminEditorModalComponent } from './match-admin-editor-modal.component';
import { MatchDetailModalComponent } from './match-detail-modal.component';
import { MatchExplorerComponent } from './match-explorer.component';
import { DashboardDataService } from './public-dashboard-data.service';
import { DashboardMatch } from './public-dashboard.models';
import {
  TEST_DASHBOARD_MATCHES,
  TEST_DASHBOARD_SNAPSHOT,
} from './public-dashboard.test-data';

describe('MatchExplorerComponent', () => {
  let fixture: ComponentFixture<MatchExplorerComponent>;
  let component: MatchExplorerComponent;
  let matchApi: {
    enabled: boolean;
    list: jasmine.Spy<(query: unknown) => Promise<DashboardMatchPage>>;
  };

  beforeEach(async () => {
    matchApi = {
      enabled: false,
      list: jasmine.createSpy('list'),
    };
    await TestBed.configureTestingModule({
      declarations: [
        MatchAdminEditorModalComponent,
        MatchDetailModalComponent,
        MatchExplorerComponent,
      ],
      imports: [CommonModule, RouterTestingModule],
      providers: [
        { provide: DashboardMatchApiService, useValue: matchApi },
      ],
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

  it('shows twenty matches per page in live-time order', () => {
    const matches: readonly DashboardMatch[] = Array.from(
      { length: 25 },
      (_, index) => ({
        ...TEST_DASHBOARD_MATCHES[index % TEST_DASHBOARD_MATCHES.length],
        id: 2_000 - index,
        playedAt: new Date(Date.UTC(2026, 7, 10, 20, -index)).toISOString(),
      }),
    );
    fixture.componentRef.setInput('matches', matches);
    fixture.detectChanges();
    const page = fixture.nativeElement as HTMLElement;

    expect(component.filteredMatches.length).toBe(25);
    expect(component.pageMatches.length).toBe(20);
    expect(page.querySelectorAll('.match-row').length).toBe(20);
    expect(component.pageMatches[0].playedAt).toBe(
      matches[0].playedAt,
    );

    const nextButton = page.querySelector(
      '.match-pagination button:last-child',
    ) as HTMLButtonElement;
    nextButton.click();
    fixture.detectChanges();

    expect(component.page).toBe(2);
    expect(component.pageMatches.length).toBe(5);
    expect(page.querySelectorAll('.match-row').length).toBe(5);
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
      '.match-row-detail-hitbox',
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

  it('links the associated player without opening the match dialog', () => {
    const playerLink = fixture.nativeElement.querySelector(
      '.match-player-link',
    ) as HTMLAnchorElement;

    expect(playerLink.getAttribute('href')).toBe(
      `/players/${TEST_DASHBOARD_MATCHES[0].playerId}`,
    );
  });

  it('shows replay and result-image actions outside the detail hit area', () => {
    fixture.componentRef.setInput('matches', [
      {
        ...TEST_DASHBOARD_MATCHES[0],
        resultImage: {
          url: 'https://vg.luwei.host/data/match-images/001/1200-0123456789abcdef.webp',
          width: 1600,
          height: 900,
        },
      },
      TEST_DASHBOARD_MATCHES[1],
    ]);
    fixture.detectChanges();
    const page = fixture.nativeElement as HTMLElement;

    expect(page.querySelectorAll('.match-replay-link').length).toBe(2);
    expect(page.querySelectorAll('.match-image-link').length).toBe(2);
    expect(page.querySelectorAll('.match-image-link.available').length).toBe(1);
    expect(page.querySelectorAll('.match-image-link.disabled').length).toBe(1);
    expect(page.querySelector('.match-row')?.textContent).not.toContain(
      '含战绩图',
    );
  });

  it('shows the fourth edit action only in internal admin mode', () => {
    expect(fixture.nativeElement.querySelector('.match-edit-link')).toBeNull();
    spyOnProperty(component.adminApi, 'enabled', 'get').and.returnValue(true);
    fixture.detectChanges();

    const editButton = fixture.nativeElement.querySelector(
      '.match-edit-link',
    ) as HTMLButtonElement;
    expect(editButton).not.toBeNull();
    editButton.click();
    expect(component.selectedAdminMatch?.id).toBe(
      TEST_DASHBOARD_MATCHES[0].id,
    );
    expect(fixture.nativeElement.querySelector('app-match-detail-modal')).toBeNull();
  });

  it('keeps live preanalysis outside the compact result badge', () => {
    fixture.componentRef.setInput('matches', [
      { ...TEST_DASHBOARD_MATCHES[0], analysisProvisional: true },
    ]);
    fixture.detectChanges();
    const page = fixture.nativeElement as HTMLElement;

    expect(page.querySelector('.match-outcome')?.textContent).not.toContain(
      '直播预识别',
    );
    expect(page.querySelector('.match-provisional')?.textContent).toContain(
      '直播预识别',
    );
  });

  it('marks a duplicate match as visible but not scored', () => {
    fixture.componentRef.setInput('matches', [
      {
        ...TEST_DASHBOARD_MATCHES[0],
        duplicateOfMatchId: 1199,
        duplicateReviewState: 'pending',
      },
    ]);
    fixture.detectChanges();
    const page = fixture.nativeElement as HTMLElement;

    expect(page.querySelector('.match-duplicate')?.textContent).toContain(
      '疑似与对局 #1199 重复 · 待复核 · 不计分',
    );
  });

  it('closes the whole modal after a result image opened from the list', () => {
    fixture.componentRef.setInput('matches', [
      {
        ...TEST_DASHBOARD_MATCHES[0],
        resultImage: {
          url: 'https://vg.luwei.host/data/match-images/001/1200-0123456789abcdef.webp',
          width: 1600,
          height: 900,
        },
      },
    ]);
    fixture.detectChanges();
    const page = fixture.nativeElement as HTMLElement;

    (page.querySelector('.match-image-link') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(page.querySelector('.match-image-lightbox')).not.toBeNull();

    (
      page.querySelector(
        '.match-image-lightbox button',
      ) as HTMLButtonElement
    ).click();
    fixture.detectChanges();

    expect(page.querySelector('[role="dialog"]')).toBeNull();
  });

  it('uses compact economy and expands the result image from its thumbnail', () => {
    fixture.componentRef.setInput('matches', [
      {
        ...TEST_DASHBOARD_MATCHES[0],
        resultImage: {
          url: 'https://vg.luwei.host/data/match-images/001/1200-0123456789abcdef.webp',
          width: 1600,
          height: 900,
        },
        rating: {
          scope: '3v3',
          seasonKey: '2026-summer',
          matchNumber: 12,
          scoreBefore: 2058,
          scoreDelta: 6,
          scoreAfter: 2064,
          provisional: false,
          modelVersion: 3,
        },
      },
    ]);
    fixture.detectChanges();
    const row = fixture.nativeElement as HTMLElement;
    expect(row.textContent).toContain('蓝 40.9K');
    expect(row.textContent).toContain('2,058');
    expect(row.textContent).toContain('→');
    expect(row.textContent).toContain('2,064');
    expect(row.textContent).toContain('本局 +6');
    expect(
      row.querySelector('.match-rating-change')?.getAttribute('aria-label'),
    ).toBe('排位分从 2058 变为 2064，本局增加 6');

    (row.querySelector('.match-row-detail-hitbox') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(row.textContent).not.toContain('赛前');
    expect(row.textContent).not.toContain('赛后');
    expect(row.textContent).toContain('16.5K');

    (row.querySelector('.match-result-thumbnail') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(row.querySelector('.match-image-lightbox')).not.toBeNull();
  });

  it('keeps an AFK-protected loss compact and explains it from the score', () => {
    const match = TEST_DASHBOARD_MATCHES[0];
    fixture.componentRef.setInput('matches', [
      {
        ...match,
        result: 'L',
        rating: {
          scope: '3v3',
          seasonKey: '2026-summer',
          matchNumber: 13,
          scoreBefore: 2058,
          scoreDelta: 0,
          scoreAfter: 2058,
          provisional: false,
          modelVersion: 8,
          afkAdjustment: 'protected_loss',
          afkPlayerDeficit: 1,
        },
      },
    ]);
    fixture.detectChanges();

    const page = fixture.nativeElement as HTMLElement;
    expect(page.querySelector('.match-rating-reason')).toBeNull();
    expect(page.querySelector('.match-rating-delta.protected')?.textContent).toContain(
      '本局 0',
    );
    expect(page.querySelector('.match-rating-help')?.getAttribute('aria-label')).toBe(
      '己方队友挂机，触发失败保护，本局不扣排位分',
    );
    expect(page.querySelector('.match-rating-tooltip')?.textContent).toContain(
      '己方队友挂机，触发失败保护，本局不扣排位分',
    );
  });

  it('shows a local loading skeleton while the API page is pending', async () => {
    let resolvePage!: (page: DashboardMatchPage) => void;
    const pending = new Promise<DashboardMatchPage>((resolve) => {
      resolvePage = resolve;
    });
    matchApi.enabled = true;
    matchApi.list.and.returnValue(pending);
    component.ngOnChanges();
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector('.match-list-loading'),
    ).not.toBeNull();

    resolvePage({ items: [], page: 1, pageSize: 20, total: 0 });
    await pending;
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.match-list-loading')).toBeNull();
  });

  it('reloads the API page when result-image metadata changes', async () => {
    matchApi.enabled = true;
    matchApi.list.and.resolveTo({ items: [], page: 1, pageSize: 20, total: 0 });

    TestBed.inject(DashboardDataService).notifyMatchDataChanged();
    await fixture.whenStable();

    expect(matchApi.list).toHaveBeenCalledTimes(1);
  });

  it('renders replay checking independently and refreshes only the current page', fakeAsync(() => {
    matchApi.enabled = true;
    matchApi.list.and.returnValues(
      Promise.resolve({
        items: [
          {
            ...TEST_DASHBOARD_MATCHES[0],
            replay: undefined,
            replayStatus: 'checking',
          },
        ],
        page: 1,
        pageSize: 20,
        total: 1,
      }),
      Promise.resolve({
        items: [
          {
            ...TEST_DASHBOARD_MATCHES[0],
            replayStatus: 'available',
          },
        ],
        page: 1,
        pageSize: 20,
        total: 1,
      }),
    );

    component.ngOnChanges();
    flushMicrotasks();
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector('.match-replay-link.checking')
        ?.textContent,
    ).toContain('加载中');
    expect(matchApi.list).toHaveBeenCalledTimes(1);

    tick(1_500);
    flushMicrotasks();
    fixture.detectChanges();

    expect(matchApi.list).toHaveBeenCalledTimes(2);
    expect(
      fixture.nativeElement.querySelector('.match-replay-link.available')
        ?.textContent,
    ).toContain('回放');
  }));
});
