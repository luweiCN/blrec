import type { Plugin } from 'chart.js';

interface MarkerColors {
  readonly cyan: string;
  readonly gold: string;
  readonly surface: string;
}

interface ScoreMarkerOptions {
  readonly points: readonly { readonly displayScore: number }[];
  readonly latestLabel: string;
  readonly seasonPeakDisplayScore: number | null;
  readonly colors: MarkerColors;
}

const SCORE_FORMATTER = new Intl.NumberFormat('zh-CN');

export function playerRatingScoreMarkerPlugin(
  options: ScoreMarkerOptions,
): Plugin<'line'> {
  return {
    id: 'playerRatingScoreMarkers',
    afterDatasetsDraw: (chart) => {
      const latestIndex = options.points.length - 1;
      const latest = options.points[latestIndex];
      const element = chart.getDatasetMeta(0).data[latestIndex];
      if (
        latest === undefined ||
        element === undefined ||
        !('getCenterPoint' in element) ||
        typeof element.getCenterPoint !== 'function'
      ) {
        return;
      }

      const { x, y } = element.getCenterPoint();
      const { ctx, chartArea } = chart;
      const seasonPeak = options.seasonPeakDisplayScore;
      const yScale = chart.scales['y'];
      const peakY =
        seasonPeak !== null &&
        seasonPeak !== latest.displayScore &&
        yScale !== undefined
          ? yScale.getPixelForValue(seasonPeak)
          : null;

      ctx.save();
      ctx.font = '700 12px system-ui, -apple-system, sans-serif';

      if (seasonPeak !== null && peakY !== null) {
        const peakLabel = `赛季最高 ${SCORE_FORMATTER.format(seasonPeak)}`;
        const peakHeight = 25;
        const peakWidth = Math.min(
          ctx.measureText(peakLabel).width + 18,
          chartArea.width,
        );
        const peakX = chartArea.left + 2;
        const peakLabelY = Math.min(
          Math.max(2, peakY - peakHeight - 5),
          chartArea.bottom - peakHeight,
        );

        ctx.beginPath();
        ctx.setLineDash([5, 4]);
        ctx.moveTo(chartArea.left, peakY);
        ctx.lineTo(chartArea.right, peakY);
        ctx.strokeStyle = options.colors.gold;
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.setLineDash([]);
        drawLabel(
          ctx,
          peakLabel,
          peakX,
          peakLabelY,
          peakWidth,
          peakHeight,
          options.colors.surface,
          options.colors.gold,
        );
      }

      const label = `${options.latestLabel} ${SCORE_FORMATTER.format(latest.displayScore)}`;
      const labelHeight = 27;
      const markerGap = 11;
      const labelWidth = Math.min(
        ctx.measureText(label).width + 20,
        chartArea.width,
      );
      const labelX = Math.min(
        Math.max(x - labelWidth / 2, chartArea.left),
        chartArea.right - labelWidth,
      );
      const placeBelowPeak =
        peakY !== null && Math.abs(peakY - y) < labelHeight + markerGap;
      const labelY = placeBelowPeak
        ? Math.min(y + markerGap, chartArea.bottom - labelHeight)
        : Math.max(2, y - labelHeight - markerGap);

      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x, placeBelowPeak ? labelY : labelY + labelHeight);
      ctx.strokeStyle = options.colors.cyan;
      ctx.lineWidth = 1;
      ctx.stroke();
      drawLabel(
        ctx,
        label,
        labelX,
        labelY,
        labelWidth,
        labelHeight,
        options.colors.surface,
        options.colors.cyan,
      );
      ctx.restore();
    },
  };
}

function drawLabel(
  context: CanvasRenderingContext2D,
  label: string,
  x: number,
  y: number,
  width: number,
  height: number,
  background: string,
  color: string,
): void {
  context.beginPath();
  context.roundRect(x, y, width, height, 7);
  context.fillStyle = background;
  context.fill();
  context.strokeStyle = color;
  context.stroke();
  context.fillStyle = color;
  context.textAlign = 'center';
  context.textBaseline = 'middle';
  context.fillText(label, x + width / 2, y + height / 2);
}
