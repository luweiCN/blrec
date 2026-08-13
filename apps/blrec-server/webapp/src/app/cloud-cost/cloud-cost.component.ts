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
  ProductCost,
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
  dailyChartOptions: EChartsOption = {};
  selectedMonth: Date | null = startOfMonth(new Date());

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
      .summary(refresh, this.billingCycle)
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
          this.dailyChartOptions = this.buildDailyChart(summary);
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

  get billingCycle(): string {
    return formatBillingCycle(this.selectedMonth || new Date());
  }

  changeMonth(value: Date | null): void {
    if (value === null) {
      return;
    }
    this.selectedMonth = startOfMonth(value);
    this.load();
  }

  disableFutureMonth = (value: Date): boolean =>
    startOfMonth(value).getTime() > startOfMonth(new Date()).getTime() ||
    startOfMonth(value).getTime() < oldestAvailableMonth().getTime();

  unitPrice(item: BillableUsageItem): string {
    if (item.listPrice === null) {
      return '账单未返回单价';
    }
    const value = new Intl.NumberFormat('zh-CN', {
      maximumFractionDigits: 8,
    }).format(item.listPrice);
    return `目录单价 ${value}${item.listPriceUnit ? ` ${item.listPriceUnit}` : ''}`;
  }

  usageExplanation(item: BillableUsageItem): string {
    const text = `${item.code} ${item.name} ${item.unit}`.toLowerCase();
    if (text.includes('流量') || text.includes('traffic')) {
      if (text.includes('回源')) {
        return 'CDN 节点未命中缓存时，从 OSS 取回文件产生的流量。';
      }
      if (text.includes('外网')) {
        return '客户端绕过 CDN、直接从 OSS 下载文件产生的公网流量。';
      }
      return '账期内对应方向的累计传输流量。';
    }
    if (text.includes('get')) {
      return '读取或查询 OSS 文件与元数据产生的 API 请求次数。';
    }
    if (text.includes('put')) {
      return '上传、覆盖或写入 OSS 文件产生的 API 请求次数。';
    }
    if (text.includes('请求')) {
      return '账期内对应 API 请求次数的累计计量。';
    }
    if (this.isAccumulatedStorage(item)) {
      return '按小时累计的容量计费量，不等于当前占用；例如持续存 1 GB 24 小时会记为 24 GB·小时。';
    }
    return '阿里云账单返回的本账期累计计量。';
  }

  storageAverage(item: BillableUsageItem): string | null {
    if (!this.isAccumulatedStorage(item)) {
      return null;
    }
    const hours = billedHours(this.billingCycle, new Date());
    if (hours === 0) {
      return null;
    }
    return `折合该计量周期平均占用约 ${new Intl.NumberFormat('zh-CN', {
      maximumFractionDigits: 3,
    }).format(item.usage / hours)} GB`;
  }

  productExplanation(product: ProductCost): string {
    const text = `${product.productCode} ${product.productName}`.toLowerCase();
    if (
      text.includes('块存储') ||
      text.includes('快存储') ||
      text.includes('ebs') ||
      text.includes('disk')
    ) {
      return '云服务器使用的云盘/块存储，不是 OSS';
    }
    if (text.includes('oss') || text.includes('对象存储')) {
      return '本站 JSON、图片等静态文件使用的对象存储';
    }
    if (text.includes('cdn') || text.includes('内容分发')) {
      return '将站点文件缓存到各地节点的内容分发服务';
    }
    if (text.includes('ecs') || text.includes('云服务器')) {
      return '承载后端服务的云服务器';
    }
    return `阿里云账单中的独立云产品；产品代码 ${product.productCode}`;
  }

  private isAccumulatedStorage(item: BillableUsageItem): boolean {
    const text = `${item.code} ${item.name} ${item.unit}`.toLowerCase();
    return (
      text.includes('storage') ||
      text.includes('存储') ||
      text.includes('容量') ||
      text.includes('gb*hour') ||
      text.includes('gb·hour') ||
      text.includes('gb-hour')
    );
  }

  private buildChart(summary: CloudCostSummary): EChartsOption {
    return {
      animationDuration: 350,
      color: ['#1677ff', '#36cfc9'],
      aria: {
        enabled: true,
        description: '最近六个账期的应付金额和实付金额趋势',
      },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value) => `¥${Number(value).toFixed(4)}`,
      },
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

  private buildDailyChart(summary: CloudCostSummary): EChartsOption {
    return {
      animationDuration: 350,
      color: ['#1677ff', '#36cfc9'],
      aria: {
        enabled: true,
        description: `${summary.billingCycle} 每日应付金额和实付金额趋势`,
      },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value) => `¥${Number(value).toFixed(4)}`,
      },
      legend: { data: ['每日应付', '每日实付'], bottom: 0 },
      grid: { top: 20, right: 20, bottom: 50, left: 54 },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: summary.daily.map((item) => item.date.slice(5)),
      },
      yAxis: {
        type: 'value',
        axisLabel: { formatter: (value: number) => `¥${value}` },
      },
      series: [
        {
          name: '每日应付',
          type: 'bar',
          data: summary.daily.map((item) => item.pretaxAmount),
        },
        {
          name: '每日实付',
          type: 'line',
          smooth: true,
          data: summary.daily.map((item) => item.paymentAmount),
        },
      ],
    };
  }
}

function startOfMonth(value: Date): Date {
  return new Date(value.getFullYear(), value.getMonth(), 1);
}

function formatBillingCycle(value: Date): string {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}`;
}

function oldestAvailableMonth(): Date {
  const current = startOfMonth(new Date());
  return new Date(current.getFullYear(), current.getMonth() - 17, 1);
}

function billedHours(billingCycle: string, now: Date): number {
  const [year, month] = billingCycle.split('-').map(Number);
  const current = startOfMonth(now);
  if (year === current.getFullYear() && month === current.getMonth() + 1) {
    return now.getDate() * 24;
  }
  return new Date(year, month, 0).getDate() * 24;
}
