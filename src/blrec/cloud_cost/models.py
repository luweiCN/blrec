from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from blrec.utils.string import camel_case


class CloudCostModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class CostTotals(CloudCostModel):
    pretax_amount: float = 0
    payment_amount: float = 0
    outstanding_amount: float = 0


class ProductCost(CostTotals):
    product_code: str
    product_name: str


class CostTrendPoint(CloudCostModel):
    billing_cycle: str
    pretax_amount: float
    payment_amount: float


class BillableUsageItem(CloudCostModel):
    code: str
    name: str
    usage: float
    unit: str
    list_price: Optional[float] = None
    list_price_unit: str = ''
    pretax_amount: float
    payment_amount: float


class OssUsage(CostTotals):
    bucket: str
    storage_bytes: Optional[int] = None
    object_count: Optional[int] = None
    storage_measured_at: Optional[datetime] = None
    items: List[BillableUsageItem] = Field(default_factory=list)


class DailyCost(CostTotals):
    date: str


class CdnDailyUsage(CloudCostModel):
    date: str
    traffic_bytes: int = 0
    requests: int = 0


class CdnUsage(CostTotals):
    domain: str
    traffic_bytes: int = 0
    requests: int = 0
    daily: List[CdnDailyUsage] = Field(default_factory=list)


class CloudCostSummary(CloudCostModel):
    provider: Literal['aliyun']
    status: Literal['not_configured', 'ready', 'partial', 'error']
    configured: bool
    generated_at: datetime
    billing_cycle: str
    currency: str = 'CNY'
    cache_seconds: int
    totals: CostTotals
    products: List[ProductCost] = Field(default_factory=list)
    trend: List[CostTrendPoint] = Field(default_factory=list)
    daily: List[DailyCost] = Field(default_factory=list)
    oss: Optional[OssUsage] = None
    cdn: Optional[CdnUsage] = None
    warnings: List[str] = Field(default_factory=list)
