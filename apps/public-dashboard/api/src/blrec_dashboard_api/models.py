from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import AnyHttpUrl, BaseModel, Field, root_validator, validator


class StrictModel(BaseModel):
    class Config:
        allow_population_by_field_name = True
        extra = 'forbid'


class IngestLiveRoom(StrictModel):
    room_id: int = Field(alias='roomId', gt=0)
    title: str = Field(max_length=240)
    started_at: datetime = Field(alias='startedAt')

    @validator('started_at')
    def started_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('live room startedAt must include a timezone')
        return value


class IngestPlayer(StrictModel):
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=80)
    initial: str = Field(min_length=1, max_length=4)
    room_label: str = Field(alias='roomLabel', max_length=120)
    room_ids: List[int] = Field(alias='roomIds')
    live_rooms: List[IngestLiveRoom] = Field(default_factory=list, alias='liveRooms')
    aliases: List[str]
    avatar_url: Optional[AnyHttpUrl] = Field(default=None, alias='avatarUrl')

    @validator('name', 'initial', 'room_label')
    def trim_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError('player text must be trimmed')
        return value

    @validator('room_ids')
    def room_ids_are_unique_and_positive(cls, value: List[int]) -> List[int]:
        if any(room_id <= 0 for room_id in value) or len(set(value)) != len(value):
            raise ValueError('player room IDs must be unique and positive')
        return value

    @validator('aliases')
    def aliases_are_trimmed_and_unique(cls, value: List[str]) -> List[str]:
        if any(not alias.strip() or alias != alias.strip() for alias in value):
            raise ValueError('player aliases must be non-empty and trimmed')
        if len({alias.casefold() for alias in value}) != len(value):
            raise ValueError('player aliases must be unique')
        return value

    @root_validator
    def live_rooms_belong_to_player(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        room_ids = set(values.get('room_ids') or [])
        live_rooms = values.get('live_rooms') or []
        live_room_ids = [room.room_id for room in live_rooms]
        if len(live_room_ids) != len(set(live_room_ids)):
            raise ValueError('player live room IDs must be unique')
        if not set(live_room_ids).issubset(room_ids):
            raise ValueError('player live rooms must belong to player room IDs')
        return values


class IngestMatchPlayer(StrictModel):
    slot: int = Field(ge=1, le=5)
    name: str = Field(min_length=1, max_length=80)
    hero_name: str = Field(alias='heroName', max_length=80)
    kills: Optional[int] = Field(default=None, ge=0)
    deaths: Optional[int] = Field(default=None, ge=0)
    assists: Optional[int] = Field(default=None, ge=0)
    economy: Optional[int] = Field(default=None, ge=0)
    last_hits: Optional[int] = Field(default=None, alias='lastHits', ge=0)
    is_recorded_player: bool = Field(alias='isRecordedPlayer')


class IngestMatchTeam(StrictModel):
    role: Literal['ally', 'enemy']
    side: Literal['left', 'right']
    color: Literal['teal', 'orange']
    kills: Optional[int] = Field(default=None, ge=0)
    economy: Optional[int] = Field(default=None, ge=0)
    players: List[IngestMatchPlayer] = Field(min_items=1, max_items=5)

    @root_validator
    def player_slots_are_unique(cls, values: dict) -> dict:
        players = values.get('players') or []
        slots = [player.slot for player in players]
        if len(slots) != len(set(slots)):
            raise ValueError('match team player slots must be unique')
        return values


class IngestReplay(StrictModel):
    kind: Literal['match', 'full']
    url: AnyHttpUrl


class IngestResultImage(StrictModel):
    url: AnyHttpUrl
    width: int = Field(gt=0, le=10000)
    height: int = Field(gt=0, le=10000)


class IngestMatch(StrictModel):
    id: int = Field(gt=0)
    player_id: int = Field(alias='playerId', gt=0)
    season_key: str = Field(alias='seasonKey', regex=r'^\d{4}-(spring|summer|autumn)$')
    mode: Literal['3v3', 'brawl', '5v5']
    played_at: datetime = Field(alias='playedAt')
    duration_seconds: int = Field(alias='durationSeconds', gt=0, le=86400)
    result: Literal['W', 'L']
    stream_title: str = Field(alias='streamTitle', max_length=240)
    analysis_provisional: bool = Field(default=False, alias='analysisProvisional')
    ally: IngestMatchTeam
    enemy: IngestMatchTeam
    replay: Optional[IngestReplay] = None
    result_image: Optional[IngestResultImage] = Field(default=None, alias='resultImage')

    @validator('played_at')
    def played_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('match playedAt must include a timezone')
        return value

    @root_validator
    def validate_teams(cls, values: dict) -> dict:
        ally = values.get('ally')
        enemy = values.get('enemy')
        mode = values.get('mode')
        if ally is None or enemy is None:
            return values
        if ally.role != 'ally' or enemy.role != 'enemy':
            raise ValueError('match ally and enemy roles must match their fields')
        if ally.side == enemy.side or ally.color == enemy.color:
            raise ValueError('match teams must use distinct sides and colors')
        maximum_slot = 3 if mode == '3v3' else 5
        if any(
            player.slot > maximum_slot
            for team in (ally, enemy)
            for player in team.players
        ):
            raise ValueError('match player slot does not match its mode')
        recorded = [
            player
            for team in (ally, enemy)
            for player in team.players
            if player.is_recorded_player
        ]
        if len(recorded) > 1 or (recorded and recorded[0] not in ally.players):
            raise ValueError('recorded player must be unique and on the ally team')
        return values


class IngestBatch(StrictModel):
    schema_version: Literal[1] = Field(alias='schemaVersion')
    generated_at: datetime = Field(alias='generatedAt')
    source_last_match_id: int = Field(alias='sourceLastMatchId', ge=0)
    players: List[IngestPlayer]
    matches: List[IngestMatch]
    removed_match_ids: List[int] = Field(alias='removedMatchIds')

    @validator('generated_at')
    def generated_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('batch generatedAt must include a timezone')
        return value

    @root_validator
    def identifiers_are_unique(cls, values: dict) -> dict:
        players = values.get('players') or []
        matches = values.get('matches') or []
        removed = values.get('removed_match_ids') or []
        player_ids = [player.id for player in players]
        match_ids = [match.id for match in matches]
        if len(player_ids) != len(set(player_ids)):
            raise ValueError('batch player IDs must be unique')
        if len(match_ids) != len(set(match_ids)):
            raise ValueError('batch match IDs must be unique')
        if any(match_id <= 0 for match_id in removed) or len(removed) != len(
            set(removed)
        ):
            raise ValueError('removed match IDs must be unique and positive')
        if set(match_ids).intersection(removed):
            raise ValueError('a batch cannot update and remove the same match')
        return values
