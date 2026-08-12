import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnInit,
} from '@angular/core';
import { EChartsOption } from 'echarts';
import { finalize } from 'rxjs/operators';
import { NzMessageService } from 'ng-zorro-antd/message';

import {
  BillableUsageItem,
  CloudCostSummary,
} from './cloud-cost.model';
import { CloudCostService } from './cloud-cost.service';

@Component({
  selector: 'app-cloud-cost',
  templateUrl: './cloud-cost.component.html',
  styleUrls: ['./cloud-cost.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CloudCostComponent implements OnInit {
  summary: CloudCostSummary | null = null;
  loading = false;
  chartOptions: EChartsOption = {};

  constructor(
    private readonly service: CloudCostService,
    private readonly message: NzMessageService,
    private readonly changeDetector: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    this.load();
  }

  load(refresh = false): void {
    this.loading = true;
    this.service
      .summary(refresh)
      .pipe(
        finalize(() => {
          this.loading = false;
          this.changeDetector.markForCheck();
        }),
      )
      .subscribe({
        next: (summary) => {
          this.summary = summary;
          this.chartOptions = this.buildChart(summary);
          this.changeDetector.markForCheck();
        },
        error: () => {
          this.message.error('云成本数据加载失败');
          this.changeDetector.markForCheck();
        },
      });
  }

  money(value: number, currency = 'CNY'): string {
    return new Intl.NumberFormat('zh-CN', {
      style: 'currency',
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 4,
    }).format(value);
  }

  bytes(value: number): string {
    if (value < 1024) {
      return `${value} B`;
    }
    const units = ['KB', 'MB', 'GB', 'TB'];
    let amount = value / 1024;
    let unit = units[0];
    for (let index = 1; amount >= 1024 && index < units.length; index += 1) {
      amount /= 1024;
      unit = units[index];
    }
    return `${amount.toFixed(amount >= 100 ? 0 : amount >= 10 ? 1 : 2)} ${unit}`;
  }

  usage(item: BillableUsageItem): string {
    const value = new Intl.NumberFormat('zh-CN', {
      maximumFractionDigits: 3,
    }).format(item.usage);
    return `${value}${item.unit ? ` ${item.unit}` : ''}`;
  }

  private buildChart(summary: CloudCostSummary): EChartsOption {
    return {
      animationDuration: 350,
      color: ['#1677ff', '#36cfc9'],
      aria: {
        enabled: true,
        description: '最近六个账期的应付金额和实付金额趋势',
      },
      tooltip: { trigger: 'axis', valueFormatter: (value) => `¥${value}` },
      legend: { data: ['应付金额', '实付金额'], bottom: 0 },
      grid: { top: 20, right: 20, bottom: 50, left: 54 },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: summary.trend.map((item) => item.billingCycle),
      },
      yAxis: {
        type: 'value',
        axisLabel: { formatter: (value: number) => `¥${value}` },
      },
      series: [
        {
          name: '应付金额',
          type: 'line',
          smooth: true,
          areaStyle: { opacity: 0.08 },
          data: summary.trend.map((item) => item.pretaxAmount),
        },
        {
          name: '实付金额',
          type: 'line',
          smooth: true,
          data: summary.trend.map((item) => item.paymentAmount),
        },
      ],
    };
  }
}
