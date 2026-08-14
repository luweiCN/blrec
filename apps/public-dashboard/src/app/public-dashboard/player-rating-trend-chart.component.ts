import {
  AfterViewInit,
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  ElementRef,
  Input,
  OnChanges,
  OnDestroy,
  SimpleChanges,
  ViewChild,
} from '@angular/core';
import type { Chart, ChartConfiguration, TooltipItem } from 'chart.js';

export interface PlayerRatingTrendChartPoint {
  readonly publicationDate: string;
  readonly rank: number;
  readonly displayScore: number;
  readonly displayDelta: number | null;
  readonly recorded: boolean;
}

interface ChartColors {
  readonly cyan: string;
  readonly gold: string;
  readonly surface: string;
  readonly ink: string;
  readonly inkSoft: string;
  readonly inkMuted: string;
  readonly line: string;
}

@Component({
  selector: 'app-player-rating-trend-chart',
  templateUrl: './player-rating-trend-chart.component.html',
  styleUrls: ['./player-rating-trend-chart.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PlayerRatingTrendChartComponent
  implements AfterViewInit, OnChanges, OnDestroy
{
  @Input() points: readonly PlayerRatingTrendChartPoint[] = [];
  @Input() ariaLabel = '玩家排位分趋势';

  @ViewChild('chartCanvas')
  private chartCanvas?: ElementRef<HTMLCanvasElement>;

  chartLoadFailed = false;
  private chart: Chart<'line', number[], string> | null = null;
  private viewReady = false;
  private destroyed = false;
  private renderRevision = 0;
  private renderedSignature = '';

  constructor(
    private readonly host: ElementRef<HTMLElement>,
    private readonly changeDetector: ChangeDetectorRef,
  ) {}

  ngAfterViewInit(): void {
    this.viewReady = true;
    this.scheduleRender();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['points'] !== undefined) {
      this.scheduleRender();
    }
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    this.renderRevision += 1;
    this.chart?.destroy();
  }

  formatDate(value: string): string {
    const [, month, day] = value.split('-');
    return `${Number(month)}月${Number(day)}日`;
  }

  deltaText(point: PlayerRatingTrendChartPoint): string {
    const delta = point.displayDelta;
    if (delta === null) {
      return '首次记录';
    }
    if (delta === 0) {
      return point.recorded ? '较前一日持平' : '当日无新对局';
    }
    return `较前一日 ${delta > 0 ? '+' : '−'}${Math.abs(delta).toLocaleString('zh-CN')}`;
  }

  trackPoint(
    _index: number,
    point: PlayerRatingTrendChartPoint,
  ): string {
    return point.publicationDate;
  }

  private scheduleRender(): void {
    if (!this.viewReady || this.destroyed) {
      return;
    }
    const signature = this.points
      .map(
        (point) =>
          `${point.publicationDate}:${point.displayScore}:${point.rank}:${point.displayDelta}:${point.recorded}`,
      )
      .join('|');
    if (signature === this.renderedSignature && this.chart !== null) {
      return;
    }
    this.renderedSignature = signature;
    const revision = ++this.renderRevision;
    void this.renderChart(revision);
  }

  private async renderChart(revision: number): Promise<void> {
    try {
      const chartJs = await import('chart.js');
      if (this.destroyed || revision !== this.renderRevision) {
        return;
      }
      chartJs.Chart.register(
        chartJs.CategoryScale,
        chartJs.LinearScale,
        chartJs.LineController,
        chartJs.LineElement,
        chartJs.PointElement,
        chartJs.Tooltip,
      );
      const canvas = this.chartCanvas?.nativeElement;
      if (canvas === undefined) {
        return;
      }
      this.chart?.destroy();
      this.chart = new chartJs.Chart(canvas, this.chartConfiguration());
      this.chartLoadFailed = false;
    } catch (_error: unknown) {
      if (!this.destroyed && revision === this.renderRevision) {
        this.chartLoadFailed = true;
        this.changeDetector.markForCheck();
      }
    }
  }

  private chartConfiguration(): ChartConfiguration<'line', number[], string> {
    const colors = this.chartColors();
    const points = this.points;
    const lastIndex = points.length - 1;
    return {
      type: 'line',
      data: {
        labels: points.map((point) => this.formatDate(point.publicationDate)),
        datasets: [
          {
            data: points.map((point) => point.displayScore),
            borderColor: colors.cyan,
            borderWidth: 2,
            borderCapStyle: 'round',
            borderJoinStyle: 'round',
            cubicInterpolationMode: 'monotone',
            tension: 0.22,
            fill: false,
            spanGaps: true,
            pointRadius: points.map((_point, index) =>
              index === lastIndex ? 6 : 4,
            ),
            pointHoverRadius: points.map((_point, index) =>
              index === lastIndex ? 8 : 7,
            ),
            pointHitRadius: 14,
            pointBorderWidth: 2,
            pointBorderColor: points.map((_point, index) =>
              index === lastIndex ? colors.gold : colors.cyan,
            ),
            pointBackgroundColor: points.map((_point, index) =>
              index === lastIndex ? colors.gold : colors.surface,
            ),
            pointHoverBorderColor: points.map((_point, index) =>
              index === lastIndex ? colors.gold : colors.cyan,
            ),
            pointHoverBackgroundColor: points.map((_point, index) =>
              index === lastIndex ? colors.gold : colors.cyan,
            ),
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        normalized: true,
        animation: this.prefersReducedMotion()
          ? false
          : { duration: 360, easing: 'easeOutQuart' },
        interaction: {
          mode: 'nearest',
          axis: 'x',
          intersect: false,
        },
        layout: {
          padding: { top: 12, right: 8, bottom: 0, left: 0 },
        },
        plugins: {
          tooltip: {
            enabled: true,
            displayColors: false,
            backgroundColor: colors.surface,
            borderColor: colors.line,
            borderWidth: 1,
            cornerRadius: 8,
            padding: 11,
            titleColor: colors.ink,
            bodyColor: colors.inkSoft,
            footerColor: colors.inkMuted,
            callbacks: {
              title: (items: TooltipItem<'line'>[]) => {
                const point = points[items[0]?.dataIndex ?? 0];
                return point === undefined
                  ? ''
                  : this.formatDate(point.publicationDate);
              },
              label: (item: TooltipItem<'line'>) => {
                const point = points[item.dataIndex];
                return point === undefined
                  ? ''
                  : `排位分 ${point.displayScore.toLocaleString('zh-CN')}`;
              },
              afterLabel: (item: TooltipItem<'line'>) => {
                const point = points[item.dataIndex];
                return point === undefined
                  ? ''
                  : `第 ${point.rank} 名 · ${this.deltaText(point)}`;
              },
            },
          },
        },
        scales: {
          x: {
            border: { display: false },
            grid: { display: false },
            ticks: {
              autoSkip: true,
              maxRotation: 0,
              maxTicksLimit: 6,
              color: colors.inkMuted,
              padding: 8,
              font: { size: 12, weight: 650 },
            },
          },
          y: {
            border: { display: false },
            grace: '12%',
            grid: {
              color: colors.line,
              drawTicks: false,
            },
            ticks: {
              maxTicksLimit: 3,
              color: colors.inkMuted,
              padding: 10,
              font: { size: 12, weight: 650 },
              callback: (value: string | number) =>
                Number(value).toLocaleString('zh-CN'),
            },
          },
        },
      },
    };
  }

  private chartColors(): ChartColors {
    const styles = getComputedStyle(this.host.nativeElement);
    const color = (name: string, fallback: string): string =>
      styles.getPropertyValue(name).trim() || fallback;
    return {
      cyan: color('--cyan', '#3ad4e7'),
      gold: color('--gold', '#f4bd43'),
      surface: color('--surface-2', '#1d1c30'),
      ink: color('--ink', '#f4f3fb'),
      inkSoft: color('--ink-soft', '#d2d0df'),
      inkMuted: color('--ink-muted', '#9a98ad'),
      line: color('--line-soft', 'rgba(95, 91, 124, 0.42)'),
    };
  }

  private prefersReducedMotion(): boolean {
    return (
      typeof window !== 'undefined' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
  }
}
