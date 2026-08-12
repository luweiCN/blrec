export type CloudCostStatus =
  | 'not_configured'
  | 'ready'
  | 'partial'
  | 'error';

export interface CostTotals {
  pretaxAmount: number;
  paymentAmount: number;
  outstandingAmount: number;
}

export interface ProductCost extends CostTotals {
  productCode: string;
  productName: string;
}

export interface CostTrendPoint {
  billingCycle: string;
  pretaxAmount: number;
  paymentAmount: number;
}

export interface BillableUsageItem {
  code: string;
  name: string;
  usage: number;
  unit: string;
  pretaxAmount: number;
  paymentAmount: number;
}

export interface OssUsage extends CostTotals {
  bucket: string;
  items: BillableUsageItem[];
}

export interface CdnDailyUsage {
  date: string;
  trafficBytes: number;
  requests: number;
}

export interface CdnUsage extends CostTotals {
  domain: string;
  trafficBytes: number;
  requests: number;
  daily: CdnDailyUsage[];
}

export interface CloudCostSummary {
  provider: 'aliyun';
  status: CloudCostStatus;
  configured: boolean;
  generatedAt: string;
  billingCycle: string;
  currency: string;
  cacheSeconds: number;
  totals: CostTotals;
  products: ProductCost[];
  trend: CostTrendPoint[];
  oss: OssUsage | null;
  cdn: CdnUsage | null;
  warnings: string[];
}
