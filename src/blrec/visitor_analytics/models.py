from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from blrec.utils.string import camel_case


class VisitorAnalyticsModel(BaseModel):
    class Config:
        alias_generator = camel_case
        allow_population_by_field_name = True


class VisitorAnalyticsFilters(VisitorAnalyticsModel):
    start_at: datetime
    end_at: datetime
    event: Literal['all', 'pageview', 'heartbeat'] = 'pageview'
    page: Optional[str] = None
    country: Optional[str] = None
    province: Optional[str] = None
    city: Optional[str] = None
    provider: Optional[str] = None
    source: Optional[str] = None
    device: Optional[str] = None
    browser: Optional[str] = None


class VisitorAnalyticsTotals(VisitorAnalyticsModel):
    visitors: int = 0
    events: int = 0
    page_views: int = 0
    heartbeats: int = 0


class VisitorTrendPoint(VisitorAnalyticsModel):
    bucket: str
    visitors: int = 0
    events: int = 0


class VisitorDimensionPoint(VisitorAnalyticsModel):
    value: str
    visitors: int = 0
    events: int = 0


class RecentVisit(VisitorAnalyticsModel):
    occurred_at: str
    visitor: str
    page: str
    source: str
    device: str
    browser: str
    country: str
    province: str
    city: str


class VisitorAnalyticsSummary(VisitorAnalyticsModel):
    provider: Literal['aliyun-sls']
    status: Literal['not_configured', 'ready', 'partial', 'error']
    configured: bool
    generated_at: datetime
    timezone: Literal['Asia/Shanghai'] = 'Asia/Shanghai'
    retention_days: int
    cache_seconds: int
    filters: VisitorAnalyticsFilters
    totals: VisitorAnalyticsTotals
    trend_granularity: Literal['hour', 'day'] = 'day'
    trend: List[VisitorTrendPoint] = Field(default_factory=list)
    pages: List[VisitorDimensionPoint] = Field(default_factory=list)
    countries: List[VisitorDimensionPoint] = Field(default_factory=list)
    provinces: List[VisitorDimensionPoint] = Field(default_factory=list)
    cities: List[VisitorDimensionPoint] = Field(default_factory=list)
    providers: List[VisitorDimensionPoint] = Field(default_factory=list)
    sources: List[VisitorDimensionPoint] = Field(default_factory=list)
    devices: List[VisitorDimensionPoint] = Field(default_factory=list)
    browsers: List[VisitorDimensionPoint] = Field(default_factory=list)
    recent_visits: List[RecentVisit] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
