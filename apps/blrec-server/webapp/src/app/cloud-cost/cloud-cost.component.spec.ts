import { ChangeDetectorRef } from '@angular/core';
import { of } from 'rxjs';
import { NzMessageService } from 'ng-zorro-antd/message';

import { CloudCostComponent } from './cloud-cost.component';
import { CloudCostSummary } from './cloud-cost.model';
import { CloudCostService } from './cloud-cost.service';

const summary: CloudCostSummary = {
  provider: 'aliyun',
  status: 'ready',
  configured: true,
  generatedAt: '2026-08-12T08:00:00Z',
  billingCycle: '2026-08',
  currency: 'CNY',
  cacheSeconds: 600,
  totals: { pretaxAmount: 3.2, paymentAmount: 3, outstandingAmount: 0.2 },
  products: [],
  trend: [
    { billingCycle: '2026-07', pretaxAmount: 2, paymentAmount: 2 },
    { billingCycle: '2026-08', pretaxAmount: 3.2, paymentAmount: 3 },
  ],
  daily: [
    {
      date: '2026-08-11',
      pretaxAmount: 0.2,
      paymentAmount: 0.18,
      outstandingAmount: 0.02,
    },
  ],
  oss: null,
  cdn: null,
  warnings: [],
};

describe('CloudCostComponent', () => {
  it('loads a fresh summary and builds the cost trend', () => {
    const service = jasmine.createSpyObj<CloudCostService>('CloudCostService', [
      'summary',
    ]);
    service.summary.and.returnValue(of(summary));
    const message = jasmine.createSpyObj<NzMessageService>('NzMessageService', [
      'error',
    ]);
    const changeDetector = jasmine.createSpyObj<ChangeDetectorRef>(
      'ChangeDetectorRef',
      ['markForCheck'],
    );
    const component = new CloudCostComponent(
      service,
      message,
      changeDetector,
    );

    component.selectedMonth = new Date(2026, 7, 1);
    component.load(true);

    expect(service.summary).toHaveBeenCalledOnceWith(true, '2026-08');
    expect(component.summary).toBe(summary);
    expect(component.chartOptions.series).toBeDefined();
    expect(component.dailyChartOptions.series).toBeDefined();
    expect(component.loading).toBeFalse();
  });

  it('formats traffic without rounding everything to bytes', () => {
    const component = new CloudCostComponent(
      jasmine.createSpyObj<CloudCostService>('CloudCostService', ['summary']),
      jasmine.createSpyObj<NzMessageService>('NzMessageService', ['error']),
      jasmine.createSpyObj<ChangeDetectorRef>('ChangeDetectorRef', [
        'markForCheck',
      ]),
    );

    expect(component.bytes(1536)).toBe('1.50 KB');
  });

  it('explains storage products and hourly storage measurements', () => {
    const component = new CloudCostComponent(
      jasmine.createSpyObj<CloudCostService>('CloudCostService', ['summary']),
      jasmine.createSpyObj<NzMessageService>('NzMessageService', ['error']),
      jasmine.createSpyObj<ChangeDetectorRef>('ChangeDetectorRef', [
        'markForCheck',
      ]),
    );

    expect(
      component.productExplanation({
        productCode: 'ebs',
        productName: '块存储',
        pretaxAmount: 0,
        paymentAmount: 0,
        outstandingAmount: 0,
      }),
    ).toContain('不是 OSS');
    expect(
      component.usageExplanation({
        code: 'Storage',
        name: '标准存储（本地冗余）容量',
        usage: 18.5,
        unit: 'GB*Hour',
        listPrice: 0.12,
        listPriceUnit: '元/GB/月',
        pretaxAmount: 0,
        paymentAmount: 0,
      }),
    ).toContain('1 GB 24 小时');
    component.selectedMonth = new Date(2026, 6, 1);
    expect(
      component.storageAverage({
        code: 'Storage',
        name: '标准存储（本地冗余）容量',
        usage: 744,
        unit: 'GB*Hour',
        listPrice: 0.12,
        listPriceUnit: '元/GB/月',
        pretaxAmount: 0,
        paymentAmount: 0,
      }),
    ).toBe('折合该计量周期平均占用约 1 GB');
  });
});
