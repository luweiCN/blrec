from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from blrec.cloud_cost.aliyun import AliyunCloudCostService
from blrec.cloud_cost.models import CloudCostSummary

router = APIRouter(prefix='/cloud-cost', tags=['cloud-cost'])
service: Optional[AliyunCloudCostService] = None


def _service() -> AliyunCloudCostService:
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Cloud cost service is unavailable',
        )
    return service


@router.get('/summary', response_model=CloudCostSummary)
async def get_summary(refresh: bool = Query(False)) -> CloudCostSummary:
    return await _service().summary(force_refresh=refresh)
