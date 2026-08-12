from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, status

from blrec.visitor_analytics import (
    VisitorAnalyticsQuery,
    VisitorAnalyticsService,
    VisitorAnalyticsSummary,
)

router = APIRouter(prefix='/visitor-analytics', tags=['visitor-analytics'])
service: Optional[VisitorAnalyticsService] = None


def _service() -> VisitorAnalyticsService:
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Visitor analytics service is unavailable',
        )
    return service


@router.get('/summary', response_model=VisitorAnalyticsSummary)
async def get_summary(
    start_at: Optional[datetime] = Query(None, alias='startAt'),
    end_at: Optional[datetime] = Query(None, alias='endAt'),
    event: Literal['all', 'pageview', 'heartbeat'] = Query('pageview'),
    page: Optional[str] = Query(None, max_length=128),
    country: Optional[str] = Query(None, max_length=128),
    province: Optional[str] = Query(None, max_length=128),
    city: Optional[str] = Query(None, max_length=128),
    provider: Optional[str] = Query(None, max_length=128),
    source: Optional[str] = Query(None, max_length=128),
    device: Optional[str] = Query(None, max_length=128),
    browser: Optional[str] = Query(None, max_length=128),
    refresh: bool = Query(False),
) -> VisitorAnalyticsSummary:
    current = datetime.now(timezone.utc)
    normalized_end = end_at or current
    normalized_start = start_at or normalized_end - timedelta(days=7)
    query = VisitorAnalyticsQuery(
        start_at=normalized_start,
        end_at=normalized_end,
        event=event,
        page=page,
        country=country,
        province=province,
        city=city,
        provider=provider,
        source=source,
        device=device,
        browser=browser,
    )
    try:
        return await _service().summary(query, force_refresh=refresh)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
