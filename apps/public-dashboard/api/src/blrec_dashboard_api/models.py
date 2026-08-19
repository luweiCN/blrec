from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal

from pydantic import AnyHttpUrl, BaseModel, Field, root_validator, validator


class StrictModel(BaseModel):
    class Config:
        allow_population_by_field_name = True
        extra = 'forbid'


class MatchImageAsset(StrictModel):
    match_id: int = Field(alias='matchId', gt=0)
    url: AnyHttpUrl
    width: int = Field(gt=0, le=10000)
    height: int = Field(gt=0, le=10000)
    sha256: str = Field(regex=r'^[0-9a-f]{64}$')


class AssetBatch(StrictModel):
    schema_version: Literal[1] = Field(alias='schemaVersion')
    generated_at: datetime = Field(alias='generatedAt')
    images: List[MatchImageAsset]
    removed_match_ids: List[int] = Field(alias='removedMatchIds')

    @validator('generated_at')
    def generated_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('asset batch generatedAt must include a timezone')
        return value

    @root_validator
    def identifiers_are_unique(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        images = values.get('images') or []
        removed = values.get('removed_match_ids') or []
        image_ids = [image.match_id for image in images]
        if len(image_ids) != len(set(image_ids)):
            raise ValueError('asset match IDs must be unique')
        if any(match_id <= 0 for match_id in removed) or len(removed) != len(
            set(removed)
        ):
            raise ValueError('removed asset IDs must be unique and positive')
        if set(image_ids).intersection(removed):
            raise ValueError('a batch cannot update and remove the same asset')
        return values


class ReplayVisibilityCompletion(StrictModel):
    public_visible: bool = Field(alias='publicVisible')


class ReplayVisibilityFailure(StrictModel):
    error: str = Field(min_length=1, max_length=500)
