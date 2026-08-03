import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { SiteStatsComponent } from './site-stats.component';

describe('SiteStatsComponent', () => {
  let fixture: ComponentFixture<SiteStatsComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [SiteStatsComponent],
      imports: [CommonModule],
    }).compileComponents();
    fixture = TestBed.createComponent(SiteStatsComponent);
  });

  it('renders the four public counters with honest labels', () => {
    fixture.componentInstance.state = {
      kind: 'ready',
      stats: {
        schemaVersion: 1,
        generatedAt: '2026-08-04T10:05:00+08:00',
        timezone: 'Asia/Shanghai',
        trackingStartedAt: '2026-08-04T00:00:00+08:00',
        activeWindowMinutes: 5,
        today: { date: '2026-08-04', visitors: 18, pageViews: 63 },
        activeVisitors: 4,
        totalPageViews: 126,
      },
    };

    fixture.detectChanges();

    const page = fixture.nativeElement as HTMLElement;
    expect(page.querySelectorAll('dl > div').length).toBe(4);
    expect(page.textContent).toContain('今日访客');
    expect(page.textContent).toContain('近 5 分钟活跃');
    expect(page.textContent).toContain('近似统计');
    expect(page.textContent).toContain('126');
  });

  it('shows an initialization state instead of invented counters', () => {
    fixture.componentInstance.state = { kind: 'unavailable' };

    fixture.detectChanges();

    const page = fixture.nativeElement as HTMLElement;
    expect(page.querySelector('dl')).toBeNull();
    expect(page.textContent).toContain('正在初始化');
  });
});
