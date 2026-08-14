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

  it('notifies mounted match views after an image asset update', fakeAsync(() => {
    realtimeUpdates.next('matches');
    flushMicrotasks();

    expect(data.notifyMatchDataChanged).toHaveBeenCalledTimes(1);
    tick(1800);
  }));
});
