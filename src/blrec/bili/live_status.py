from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Dict, FrozenSet, Optional


class ObservedStatus(str, Enum):
    UNKNOWN = 'unknown'
    STALE = 'stale'
    PREPARING = 'preparing'
    LIVE = 'live'
    ROUND = 'round'


class StatusSource(str, Enum):
    BATCH = 'batch'
    CONFIRMATION = 'confirmation'
    WSS = 'wss'
    LOCAL = 'local'


class BreakerState(str, Enum):
    CLOSED = 'closed'
    OPEN = 'open'
    HALF_OPEN = 'half_open'
    PAUSED = 'paused'


@dataclass(frozen=True)
class StatusSnapshot:
    uid: int
    room_id: int
    status: ObservedStatus
    observed_at: float
    source: StatusSource
    live_time: int
    observation_key: Optional[str]


@dataclass(frozen=True)
class BatchStatusResult:
    snapshots: Dict[int, StatusSnapshot]
    missing_uids: FrozenSet[int]


@dataclass(frozen=True)
class CoordinatorMetrics:
    mode: str
    interval_seconds: int
    batch_size: int
    registered_rooms: int
    active_websockets: int
    last_success_at: Optional[float]
    snapshot_max_age_seconds: Optional[float]
    missing_results: int
    fallback_requests: int
    breaker_state: BreakerState
    breaker_reason: Optional[str]


LiveStatusListener = Callable[[StatusSnapshot], Awaitable[None]]
StatusConfirmer = Callable[[], Awaitable[StatusSnapshot]]
