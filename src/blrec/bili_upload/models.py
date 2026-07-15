from enum import Enum
from typing import Optional

from blrec.setting.models import BiliUploadSettings

__all__ = ('FeatureUnavailable', 'JobState', 'WriteState', 'validate_feature_gate')


class FeatureUnavailable(RuntimeError):
    pass


class WriteState(str, Enum):
    PREPARED = 'prepared'
    IN_FLIGHT = 'in_flight'
    CONFIRMED = 'confirmed'
    UNKNOWN_OUTCOME = 'unknown_outcome'
    FAILED_PERMANENT = 'failed_permanent'


class JobState(str, Enum):
    WAITING_ARTIFACTS = 'waiting_artifacts'
    READY = 'ready'
    UPLOADING = 'uploading'
    SUBMITTING = 'submitting'
    WAITING_REVIEW = 'waiting_review'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    PAUSED = 'paused'
    COMPLETED = 'completed'


def validate_feature_gate(
    settings: BiliUploadSettings,
    *,
    api_key: Optional[str],
    credential_key: Optional[bytes],
) -> None:
    if credential_key is None:
        raise FeatureUnavailable('credential key is required')
    if len(credential_key) != 32:
        raise FeatureUnavailable('credential key must decode to 32 bytes')
