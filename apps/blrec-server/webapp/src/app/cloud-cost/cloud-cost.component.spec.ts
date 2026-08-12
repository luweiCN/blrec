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

    component.load(true);

    expect(service.summary).toHaveBeenCalledOnceWith(true);
    expect(component.summary).toBe(summary);
    expect(component.chartOptions.series).toBeDefined();
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
});
