import { NO_ERRORS_SCHEMA } from '@angular/core';
import {
  ComponentFixture,
  fakeAsync,
  flushMicrotasks,
  TestBed,
  tick,
} from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';
import { Subject } from 'rxjs';

import { DashboardModeService } from './dashboard-mode.service';
import { DashboardOwnerAccessService } from './dashboard-owner-access.service';
import { DashboardRealtimeService } from './dashboard-realtime.service';
import { DashboardDataService } from './public-dashboard-data.service';
import { PlayerLiveStatusService } from './player-live-status.service';
import { PublicDashboardShellComponent } from './public-dashboard-shell.component';
import { SiteAnalyticsService } from './site-analytics.service';
import { SiteStatsService } from './site-stats.service';

describe('PublicDashboardShellComponent', () => {
  let fixture: ComponentFixture<PublicDashboardShellComponent>;
  let realtimeUpdates: Subject<
    'resync' | 'dashboard' | 'live_rooms' | 'matches'
  >;
  let data: {
    state: { kind: 'loading' | 'ready' };
    snapshotOrNull: null;
    load: jasmine.Spy<() => Promise<void>>;
    refresh: jasmine.Spy<() => Promise<boolean>>;
    notifyMatchDataChanged: jasmine.Spy<() => void>;
  };

  beforeEach(async () => {
    realtimeUpdates = new Subject();
    data = {
      state: { kind: 'loading' },
      snapshotOrNull: null,
      load: jasmine.createSpy('load').and.resolveTo(),
      refresh: jasmine.createSpy('refresh').and.resolveTo(false),
      notifyMatchDataChanged: jasmine.createSpy('notifyMatchDataChanged'),
    };
    await TestBed.configureTestingModule({
      declarations: [PublicDashboardShellComponent],
      imports: [RouterTestingModule],
      providers: [
        { provide: DashboardDataService, useValue: data },
        {
          provide: DashboardModeService,
          useValue: { mode: '3v3', selectMode: () => undefined },
        },
        {
          provide: DashboardOwnerAccessService,
          useValue: {
            active: false,
            unlock: () => Promise.resolve(false),
            validateStored: () => Promise.resolve(false),
            lock: () => undefined,
          },
        },
        {
          provide: SiteAnalyticsService,
          useValue: { start: () => undefined, stop: () => undefined },
        },
        {
          provide: SiteStatsService,
          useValue: { load: () => Promise.resolve({ kind: 'unavailable' }) },
        },
        {
          provide: DashboardRealtimeService,
          useValue: {
            updates$: realtimeUpdates,
            start: () => undefined,
            stop: () => undefined,
          },
        },
        {
          provide: PlayerLiveStatusService,
          useValue: {
            start: () => undefined,
            stop: () => undefined,
            refresh: () => Promise.resolve(),
          },
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();

    fixture = TestBed.createComponent(PublicDashboardShellComponent);
    fixture.detectChanges();
  });

  it('renders the application shell and a local skeleton before data is ready', () => {
    expect(data.load).toHaveBeenCalledTimes(1);
    expect(
      fixture.nativeElement.querySelector('.dashboard-loading'),
    ).not.toBeNull();
    expect(fixture.nativeElement.querySelector('.site-header')).not.toBeNull();
  });

  it('opens and closes the update notes dialog from the stable header action', () => {
    const page = fixture.nativeElement as HTMLElement;
    const trigger = page.querySelector<HTMLButtonElement>(
      '.update-notes-trigger',
    );

    trigger?.click();
    fixture.detectChanges();
    expect(fixture.componentInstance.updateNotesOpen).toBeTrue();
    const dialog = page.querySelector<HTMLDialogElement>(
      '.update-notes-dialog',
    );
    expect(dialog?.open).toBeTrue();
    expect(dialog?.textContent).toContain(
      '十段单局涨跌改为随当前分数变化',
    );
    expect(dialog?.textContent).toContain('十段铜胜一局');
    expect(dialog?.textContent).toContain('失败通常扣 6～12 分');
    expect(dialog?.textContent).toContain('隐藏实力不再参与单局加减');
    expect(dialog?.textContent).toContain(
      '新赛季会从较低段位重新开始',
    );
    expect(dialog?.textContent).toContain('10 段铜约从 8 段铜开始');
    expect(dialog?.textContent).not.toContain('软重置');
    expect(dialog?.textContent).not.toContain('具体参数不公开');

    page
      .querySelector<HTMLButtonElement>('[aria-label="关闭更新说明"]')
      ?.click();
    fixture.detectChanges();
    expect(fixture.componentInstance.updateNotesOpen).toBeFalse();
    expect(dialog?.open).toBeFalse();
  });

  it('shows feedback while realtime data is refreshing', fakeAsync(() => {
    let finishRefresh!: (changed: boolean) => void;
    data.state = { kind: 'ready' };
    data.refresh.and.returnValue(
      new Promise<boolean>((resolve) => {
        finishRefresh = resolve;
      }),
    );
    fixture.detectChanges();

    realtimeUpdates.next('dashboard');
    fixture.detectChanges();
    const status = fixture.nativeElement.querySelector(
      '.data-status',
    ) as HTMLElement;
    expect(status.textContent).toContain('正在同步新数据');
    expect(status.getAttribute('aria-busy')).toBe('true');

    finishRefresh(true);
    flushMicrotasks();
    fixture.detectChanges();
    expect(status.textContent).toContain('数据已更新');

    tick(1800);
  }));

  it('documents the legacy v1 contract: a dashboard event reloads the aggregate document', fakeAsync(() => {
    data.state = { kind: 'ready' };
    data.refresh.calls.reset();

    realtimeUpdates.next('dashboard');
    flushMicrotasks();

    expect(data.refresh).toHaveBeenCalledTimes(1);
    tick(1800);
  }));

  it('notifies mounted match views after an image asset update', fakeAsync(() => {
    realtimeUpdates.next('matches');
    flushMicrotasks();

    expect(data.notifyMatchDataChanged).toHaveBeenCalledTimes(1);
    tick(1800);
  }));
});
