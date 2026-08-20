import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import {
  highestTrendPointIndex,
  PlayerRatingTrendChartComponent,
  PlayerRatingTrendChartPoint,
} from './player-rating-trend-chart.component';

function trendPoint(
  publicationDate: string,
  displayScore: number,
  recorded = true,
): PlayerRatingTrendChartPoint {
  return {
    publicationDate,
    displayScore,
    rank: 1,
    displayDelta: null,
    recorded,
  };
}

describe('PlayerRatingTrendChartComponent', () => {
  let fixture: ComponentFixture<PlayerRatingTrendChartComponent>;
  let component: PlayerRatingTrendChartComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      declarations: [PlayerRatingTrendChartComponent],
      imports: [CommonModule],
    }).compileComponents();

    fixture = TestBed.createComponent(PlayerRatingTrendChartComponent);
    component = fixture.componentInstance;
  });

  afterEach(() => fixture.destroy());

  it('selects the highest recorded day and falls back to the first maximum', () => {
    expect(
      highestTrendPointIndex([
        trendPoint('2026-08-01', 2_400),
        trendPoint('2026-08-02', 2_620),
        trendPoint('2026-08-03', 2_580),
      ]),
    ).toBe(1);
    expect(
      highestTrendPointIndex([
        trendPoint('2026-08-01', 2_620, false),
        trendPoint('2026-08-02', 2_620, true),
        trendPoint('2026-08-03', 2_620, true),
      ]),
    ).toBe(1);
    expect(
      highestTrendPointIndex([
        trendPoint('2026-08-01', 2_620, false),
        trendPoint('2026-08-02', 2_620, false),
      ]),
    ).toBe(0);
    expect(highestTrendPointIndex([])).toBe(-1);
  });

  it('announces and marks the highest point without requiring a tooltip', () => {
    component.ariaLabel = '星河在 2026 夏季赛的当日排位分趋势';
    component.points = [
      trendPoint('2026-08-01', 2_400),
      trendPoint('2026-08-02', 2_620),
      trendPoint('2026-08-03', 2_580),
    ];
    fixture.detectChanges();

    const page = fixture.nativeElement as HTMLElement;
    const canvas = page.querySelector('canvas');
    const peakRow = page.querySelector('.trend-chart-peak');

    expect(canvas?.getAttribute('aria-label')).toContain(
      '最高点：8月2日，2,620 排位分',
    );
    expect(peakRow?.textContent).toContain('最高点');
    expect(peakRow?.textContent).toContain('2,620');
    expect(page.querySelectorAll('.trend-chart-peak').length).toBe(1);
  });
});
