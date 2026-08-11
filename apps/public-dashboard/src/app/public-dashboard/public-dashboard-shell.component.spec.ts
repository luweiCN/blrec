import { NO_ERRORS_SCHEMA } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';

import { DashboardModeService } from './dashboard-mode.service';
import { DashboardDataService } from './public-dashboard-data.service';
import { PublicDashboardShellComponent } from './public-dashboard-shell.component';
import { SiteAnalyticsService } from './site-analytics.service';
import { SiteStatsService } from './site-stats.service';

describe('PublicDashboardShellComponent', () => {
  let fixture: ComponentFixture<PublicDashboardShellComponent>;
  let data: {
    state: { kind: 'loading' };
    snapshotOrNull: null;
    load: jasmine.Spy<() => Promise<void>>;
  };

  beforeEach(async () => {
    data = {
      state: { kind: 'loading' },
      snapshotOrNull: null,
      load: jasmine.createSpy('load').and.resolveTo(),
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
});
