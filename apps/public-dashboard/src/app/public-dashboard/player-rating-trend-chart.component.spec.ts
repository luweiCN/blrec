import { CommonModule } from '@angular/common';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import {
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

  it('marks the current point and announces the undated season record separately', () => {
    component.ariaLabel = '星河在 2026 夏季赛的当日排位分趋势';
    component.latestPointLabel = '当前';
    component.seasonPeakDisplayScore = 2_820;
    component.points = [
      trendPoint('2026-08-01', 2_400),
      trendPoint('2026-08-02', 2_818),
    ];
    fixture.detectChanges();

    const page = fixture.nativeElement as HTMLElement;
    const canvas = page.querySelector('canvas');
    const fallbackCaption = page.querySelector('.trend-chart-data caption');

    expect(canvas?.getAttribute('aria-label')).toContain(
      '当前：8月2日，2,818 排位分',
    );
    expect(canvas?.getAttribute('aria-label')).toContain(
      '赛季最高：2,820 排位分',
    );
    expect(canvas?.getAttribute('aria-label')).not.toContain('最高点');
    expect(fallbackCaption?.textContent).toContain('当前：8月2日，2,818');
    expect(fallbackCaption?.textContent).toContain('赛季最高：2,820');
  });

  it('combines the current and record labels when both scores are equal', () => {
    component.latestPointLabel = '当前';
    component.seasonPeakDisplayScore = 2_820;
    component.points = [trendPoint('2026-08-03', 2_820)];
    fixture.detectChanges();

    const page = fixture.nativeElement as HTMLElement;
    const canvas = page.querySelector('canvas');

    expect(canvas?.getAttribute('aria-label')).toContain(
      '当前且为赛季最高：8月3日，2,820 排位分',
    );
    expect(page.querySelector('.trend-chart-data caption')?.textContent).toContain(
      '当前且为赛季最高',
    );
  });
});
